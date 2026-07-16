"""Этап 5 — структурная и типографическая валидация результата."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import fitz

from pipeline.anchors import compiled_anchors
from pipeline.config.loader import (ensure_dirs, load_config, resolve_path,
                                    setup_logger)


_PAGE_NUMBER_RE = re.compile(r"^(?:\d+|[ivxlcdm]+)$", re.IGNORECASE)
_BAD_LAYOUT_KEYS = {
    "lost", "lost_count", "lost_blocks", "lost_segments", "dropped",
    "dropped_blocks", "notfit", "not_fit", "not_fitted",
    "notfit_count", "not_fit_count", "notfit_blocks", "unplaced",
    "unplaced_blocks", "unexpected_insert_failure", "source_retained_blocks",
}
_ANCHOR_DASH_TRANSLATION = str.maketrans({
    "\u00ad": "-",  # soft hyphen из некоторых встроенных PDF-шрифтов
    "\u2010": "-",
    "\u2011": "-",
    "\u2212": "-",
})


def _is_han(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x323AF
    )


def _is_target_char(ch: str, lang: str) -> bool:
    """Возвращает True для букв целевой письменности, без цифр/пунктуации."""
    lang = (lang or "").lower().split("-", 1)[0]
    cp = ord(ch)
    if lang == "ru":
        return 0x0400 <= cp <= 0x052F
    if lang == "zh":
        return _is_han(ch)
    if lang == "ja":
        return (_is_han(ch) or 0x3040 <= cp <= 0x30FF
                or 0x31F0 <= cp <= 0x31FF)
    if lang in {"en", "de", "fr", "es"}:
        return ch.isalpha() and "LATIN" in unicodedata.name(ch, "")
    return ch.isalpha()


def _meaningful_chars(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def _stats(pdf_path: str, cfg: dict) -> dict:
    del cfg  # зарезервировано для совместимости и будущих правил извлечения
    d = fitz.open(str(pdf_path))
    try:
        n = d.page_count
        toc = d.get_toc()
        levels = [t[0] for t in toc] if toc else []
        xrefs: set[int] = set()
        page_texts: list[str] = []
        spans: list[dict[str, Any]] = []

        for pno in range(n):
            page = d.load_page(pno)
            for img in page.get_images(full=True):
                xrefs.add(img[0])

            page_text = page.get_text("text") or ""
            page_texts.append(page_text)
            raw = page.get_text("dict")
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = str(span.get("text") or "")
                        if not text.strip():
                            continue
                        spans.append({
                            "page": pno,
                            "text": text,
                            "size": float(span.get("size") or 0.0),
                            "bbox": tuple(float(v) for v in span.get(
                                "bbox", (0.0, 0.0, 0.0, 0.0))),
                            "page_height": float(page.rect.height),
                        })

        full_text = "\n".join(page_texts)
        return {
            "pages": n,
            "images": len(xrefs),
            "toc_len": len(toc),
            "max_level": max(levels) if levels else 0,
            "min_level": min(levels) if levels else 0,
            "metadata": dict(d.metadata or {}),
            "text": full_text,
            "page_texts": page_texts,
            "spans": spans,
            "meaningful_pages": [_meaningful_chars(t) for t in page_texts],
            "han_chars": sum(_is_han(ch) for ch in full_text),
        }
    finally:
        d.close()


def _rect_area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rects_match(a: tuple[float, float, float, float],
                 b: tuple[float, float, float, float]) -> bool:
    """Нечёткое пространственное сопоставление исходного и выходного span."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    smaller = min(_rect_area(a), _rect_area(b))
    if smaller > 0 and inter / smaller >= 0.25:
        return True
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    return b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1


def _obvious_small_source_service(span: dict[str, Any]) -> bool:
    text = span["text"].strip()
    chars = _meaningful_chars(text)
    edge = (span["bbox"][1] <= span["page_height"] * 0.07
            or span["bbox"][3] >= span["page_height"] * 0.93)
    return bool(_PAGE_NUMBER_RE.fullmatch(text)) or chars <= 3 or (edge and chars <= 40)


def _source_span_was_small(out_span: dict[str, Any],
                           src_page_spans: list[dict[str, Any]],
                           threshold: float) -> bool:
    matches = [
        sp for sp in src_page_spans
        if _rects_match(out_span["bbox"], sp["bbox"])
    ]
    return (bool(matches)
            and max(sp["size"] for sp in matches) <= threshold + 0.25
            and any(_obvious_small_source_service(sp) for sp in matches))


def _readability_stats(src_stats: dict, out_stats: dict,
                       cfg: dict) -> dict[str, Any]:
    target_lang = str(cfg.get("target_lang", "ru"))
    min_size = float(cfg.get(
        "validator_min_readable_fontsize",
        cfg.get("min_readable_fontsize", 8.0),
    ))
    min_bad_chars = int(cfg.get("validator_min_small_text_chars", 4))
    target_chars = 0
    bad_chars = 0
    observed_min: float | None = None
    examples: list[str] = []
    src_by_page: dict[int, list[dict[str, Any]]] = {}
    for span in src_stats["spans"]:
        src_by_page.setdefault(span["page"], []).append(span)

    for span in out_stats["spans"]:
        count = sum(_is_target_char(ch, target_lang) for ch in span["text"])
        if not count:
            continue
        target_chars += count
        if observed_min is None or span["size"] < observed_min:
            observed_min = span["size"]
        if min_size <= 0 or span["size"] + 0.05 >= min_size:
            continue

        stripped = span["text"].strip()
        edge = (span["bbox"][1] <= span["page_height"] * 0.05
                or span["bbox"][3] >= span["page_height"] * 0.95)
        if edge and (count <= 3 or _PAGE_NUMBER_RE.fullmatch(stripped)):
            continue
        if _source_span_was_small(
                span, src_by_page.get(span["page"], []), min_size):
            continue

        bad_chars += count
        if len(examples) < 5:
            sample = re.sub(r"\s+", " ", stripped)[:60]
            examples.append(f"p.{span['page'] + 1} {span['size']:.1f}pt: {sample!r}")

    return {
        "min_size": min_size,
        "target_chars": target_chars,
        "bad_chars": bad_chars,
        "bad_ratio": bad_chars / target_chars if target_chars else 0.0,
        "observed_min": observed_min,
        "examples": examples,
        "failed": bad_chars >= max(1, min_bad_chars),
    }


def _counter_details(counter: Counter[str]) -> str:
    parts = [f"{key}x{count}" if count > 1 else key
             for key, count in sorted(counter.items())]
    return ", ".join(parts[:10])


def _invalid_text_markers(text: str) -> list[str]:
    invalid: list[str] = []
    if "\ufffd" in text:
        invalid.append("U+FFFD")
    if "\x00" in text:
        invalid.append("NUL")
    return invalid


def _normalize_anchor_text(text: str) -> str:
    return (text or "").translate(_ANCHOR_DASH_TRANSLATION)


def _layout_failure_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        s = value.strip().lower()
        if not s or s in {"0", "false", "none", "ok"}:
            return False
        try:
            return float(s) > 0
        except ValueError:
            return True
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, dict):
        for key in ("count", "total", "value"):
            if key in value:
                return _layout_failure_value(value[key])
        for key in ("items", "blocks", "segments"):
            if key in value:
                return _layout_failure_value(value[key])
        return bool(value)
    return bool(value)


def _layout_failures(data: Any, path: str = "report") -> list[str]:
    failures: list[str] = []
    if isinstance(data, dict):
        for raw_key, value in data.items():
            key = str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")
            child_path = f"{path}.{raw_key}"
            if key in _BAD_LAYOUT_KEYS and _layout_failure_value(value):
                failures.append(child_path)
            if key in {"status", "state"} and isinstance(value, str):
                status = value.strip().lower().replace("-", "_").replace(" ", "_")
                if status in {"lost", "notfit", "not_fit", "dropped", "unplaced"}:
                    failures.append(f"{child_path}={value}")
            failures.extend(_layout_failures(value, child_path))
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            failures.extend(_layout_failures(value, f"{path}[{idx}]"))
    return failures


def _load_layout_report(out: Path, cfg: dict) -> tuple[Path | None, Any, str | None]:
    inline = cfg.get("builder_report")
    artifacts = cfg.get("artifacts")
    if inline is None and isinstance(artifacts, dict):
        inline = artifacts.get("builder_report")
    if inline is not None:
        return None, inline, None

    configured = cfg.get("builder_report_path")
    if configured:
        report_path = resolve_path(configured)
        required = True
    else:
        report_path = Path(str(out) + ".layout.json")
        required = False
    if not report_path.exists():
        error = f"не найден обязательный отчёт компоновки: {report_path}" if required else None
        return report_path, None, error
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            return report_path, json.load(fh), None
    except Exception as exc:
        return report_path, None, f"не удалось прочитать отчёт компоновки {report_path}: {exc}"


def _reported_overflow_pages(report: Any) -> set[int]:
    """Страницы, чей текст штатно перенесён в appendix согласно sidecar."""
    if not isinstance(report, dict):
        return set()
    try:
        appendix_pages = int(report.get("appendix_pages") or 0)
    except (TypeError, ValueError):
        return set()
    if appendix_pages <= 0 or _layout_failures(report):
        return set()
    records = report.get("overflow")
    if not isinstance(records, list):
        return set()
    pages: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            pages.add(int(record["page"]))
        except (KeyError, TypeError, ValueError):
            continue
    return pages


def validate(src: str, out: str, logger, cfg: dict | None = None) -> int:
    cfg = cfg or {}
    src_path = resolve_path(src)
    out_path = resolve_path(out)
    if not out_path.exists():
        logger.error("Не найден результат: %s", out_path)
        return 2

    try:
        s = _stats(str(src_path), cfg)
        o = _stats(str(out_path), cfg)
    except Exception as exc:
        logger.error("Не удалось открыть/проанализировать PDF: %s", exc)
        return 2

    problems: list[str] = []

    report_path, report_data, report_error = _load_layout_report(out_path, cfg)
    if report_error:
        problems.append(report_error)
    report_failures: list[str] = []
    if report_data is not None:
        report_failures = list(dict.fromkeys(_layout_failures(report_data)))
        if report_failures:
            where = str(report_path) if report_path else "inline builder_report"
            problems.append(
                f"Отчёт компоновки содержит lost/notfit/source_retained "
                f"({where}): "
                + ", ".join(report_failures[:10]))
    overflow_source_pages = _reported_overflow_pages(report_data)

    # Overflow appendix может добавлять страницы, но терять исходные нельзя.
    if o["pages"] < s["pages"]:
        problems.append(f"Потеряны страницы: src={s['pages']} out={o['pages']}")

    img_loss = max(0, s["images"] - o["images"])
    img_tol = max(10, int(s["images"] * 0.30))
    if img_loss > img_tol:
        problems.append(
            f"Потеряны изображения: src={s['images']} out={o['images']} "
            f"(допустимо потерять не более {img_tol})")
    if o["toc_len"] < s["toc_len"]:
        problems.append(f"TOC укорочен: src={s['toc_len']} out={o['toc_len']}")
    if o["max_level"] < s["max_level"]:
        problems.append(
            f"TOC потерял уровни: src max={s['max_level']} out max={o['max_level']}")

    # Пустые и резко потерявшие текст исходные страницы.
    empty_min = int(cfg.get("validator_empty_page_min_source_chars", 4))
    ratio_min_chars = int(cfg.get("validator_ratio_min_source_chars", 20))
    min_page_ratio = float(cfg.get("validator_min_page_text_ratio", 0.20))
    empty_pages: list[int] = []
    thinned_pages: list[tuple[int, int, int]] = []
    for pno in range(min(s["pages"], o["pages"])):
        src_chars = s["meaningful_pages"][pno]
        out_chars = o["meaningful_pages"][pno]
        if src_chars >= empty_min and out_chars == 0:
            empty_pages.append(pno + 1)
        elif (pno not in overflow_source_pages
              and src_chars >= ratio_min_chars
              and out_chars < src_chars * min_page_ratio):
            thinned_pages.append((pno + 1, src_chars, out_chars))
    if empty_pages:
        problems.append(f"Пустые выходные страницы с исходным текстом: {empty_pages[:20]}")
    if thinned_pages:
        details = ", ".join(
            f"p.{p} {out_n}/{src_n}" for p, src_n, out_n in thinned_pages[:10])
        problems.append(f"Резкая потеря текста по страницам: {details}")

    anch = compiled_anchors(cfg) if cfg else {}
    anchor_counts: dict[str, tuple[Counter[str], Counter[str]]] = {}
    for kind in ("fig", "tab"):
        spec = anch.get(kind, {})
        src_re, dst_re = spec.get("src"), spec.get("dst")
        src_text = _normalize_anchor_text(s["text"])
        dst_text = _normalize_anchor_text(o["text"])
        src_counts = Counter(m.group(1) for m in src_re.finditer(src_text)) if src_re else Counter()
        dst_counts = Counter(m.group(1) for m in dst_re.finditer(dst_text)) if dst_re else Counter()
        anchor_counts[kind] = (src_counts, dst_counts)
        if src_re and dst_re and src_counts != dst_counts:
            missing = src_counts - dst_counts
            extra = dst_counts - src_counts
            details = []
            if missing:
                details.append(f"потеряны {_counter_details(missing)}")
            if extra:
                details.append(f"лишние {_counter_details(extra)}")
            problems.append(f"Якоря {kind} не совпадают по числу: " + "; ".join(details))

    invalid = _invalid_text_markers(o["text"])
    if invalid:
        problems.append("Обнаружены недопустимые символы: " + ", ".join(invalid))

    src_lang = str(cfg.get("source_lang", "zh")).lower().split("-", 1)[0]
    target_lang = str(cfg.get("target_lang", "ru")).lower().split("-", 1)[0]
    max_residual = int(cfg.get("validator_max_residual_source_chars", 0))
    if src_lang == "zh" and target_lang != "zh" and o["han_chars"] > max_residual:
        problems.append(
            f"Остался исходный Han-текст: {o['han_chars']} символов "
            f"(допустимо {max_residual})")

    readable = _readability_stats(s, o, cfg)
    if (src_lang == "zh" and target_lang == "ru" and s["han_chars"] > 0
            and readable["target_chars"] == 0):
        problems.append("Не найден текст целевой кириллической письменности")
    if readable["failed"]:
        examples = "; ".join(readable["examples"])
        problems.append(
            f"Нечитаемый мелкий целевой текст: {readable['bad_chars']} символов "
            f"ниже {readable['min_size']:.1f} pt "
            f"({readable['bad_ratio']:.1%}); {examples}")

    md = o["metadata"]
    cfg_md = cfg.get("metadata") or {}
    for key in ("title", "author", "subject"):
        expected = str(cfg_md.get(key) or "").strip()
        actual = str(md.get(key) or "").strip()
        if expected and actual != expected:
            problems.append(f"Метаданные {key}: ожидалось {expected!r}, получено {actual!r}")
        elif not expected and s["metadata"].get(key) and not actual:
            problems.append(f"Метаданные пусты: {key}")

    fig_src, fig_out = anchor_counts.get("fig", (Counter(), Counter()))
    tab_src, tab_out = anchor_counts.get("tab", (Counter(), Counter()))
    logger.info("--- Сравнение ---")
    logger.info("  страниц:  src=%d out=%d", s["pages"], o["pages"])
    logger.info("  изображ.: src=%d out=%d", s["images"], o["images"])
    logger.info("  TOC:      src=%d (max %d)  out=%d (max %d)",
                s["toc_len"], s["max_level"], o["toc_len"], o["max_level"])
    logger.info("  якоря:    fig src=%d out=%d  tab src=%d out=%d",
                sum(fig_src.values()), sum(fig_out.values()),
                sum(tab_src.values()), sum(tab_out.values()))
    logger.info("  текст:    Han out=%d, target chars=%d, below %.1fpt=%d",
                o["han_chars"], readable["target_chars"],
                readable["min_size"], readable["bad_chars"])
    logger.info("  метаданные: title=%r author=%r subject=%r",
                md.get("title", "")[:40], md.get("author", ""),
                md.get("subject", "")[:40])

    if problems:
        logger.error("НАЙДЕНЫ ПРОБЛЕМЫ:")
        for problem in problems:
            logger.error("  - %s", problem)
        return 1
    logger.info("ВАЛИДАЦИЯ ПРОЙДЕНА")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Валидация переведённого PDF")
    ap.add_argument("--src")
    ap.add_argument("--out")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    src = args.src or cfg["pdf_path"]
    out = args.out or cfg["out_path"]
    sys.exit(validate(src, out, logger, cfg))


if __name__ == "__main__":
    main()
