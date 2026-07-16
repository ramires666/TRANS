"""Этап 4 — Сборка _RU.pdf: копия+редакт+перевод TOC+метаданные."""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import tempfile
from pathlib import Path

import fitz

from pipeline.config.loader import (ROOT, ensure_dirs, load_config,
                                     resolve_path, setup_logger)
from pipeline.fonts.fonts import find_target_font
from pipeline.fonts.matcher import match_font
from pipeline.io.artifacts import load_json, save_json


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_heading_lookup(segments: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in segments:
        if s["type"] != "heading":
            continue
        zh = s["text"]
        cleaned = re.sub(r"[\.\s]*\d+\s*$", "", zh)
        cleaned = re.sub(r"\.{2,}", "", cleaned).strip()
        key = _norm(cleaned)
        if key and key not in out:
            out[key] = s.get("ru") or s["text"]
    return out


def _normalize_markers(text: str, cfg: dict | None = None) -> str:
    """Нормализует маркеры списков в стандартный bullet для любого языка.

    Конфигурируется через ``bullet_chars``, ``bullet_replace`` и ``remove_pua``
    в config.yaml. По умолчанию обрабатывает широкий набор bullet-символов.
    """
    cfg = cfg or {}
    chars = cfg.get("bullet_chars", "•●○◌◦◆◇■□▪▫▶▷◀◁▲△▼▽◄►◅▻")
    replace = cfg.get("bullet_replace", "•") or "•"
    remove_pua = cfg.get("remove_pua", True)

    if chars:
        # экранируем для регулярного выражения
        escaped = "".join(c if c not in r"\^$.*+?{}[]|()" else "\\" + c for c in chars)
        text = re.sub(rf"(?m)(^\s*)[{escaped}]", r"\1" + replace, text)
    if remove_pua:
        text = re.sub(r"[\uf000-\uf8ff]", "", text)
    # NUL иногда появляется вместо отсутствующего глифа в исходном PDF.
    # Не сворачиваем пробелы и не вызываем strip(): это разрушало отступы
    # списков и противоречило логической структуре перевода.
    return text.replace("\x00", "")


_LOGICAL_ITEM_RE = re.compile(r"^(?:[•\-–—▪]|\(?\d+[.)])\s*")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u052f]")


def _prepare_text_for_layout(text: str, seg_type: str,
                             cfg: dict | None = None) -> str:
    """Удаляет визуальные soft-wrap переносы перед автоматической вёрсткой.

    Физические строки китайского PDF значительно короче русских. Сохранение
    этих переносов 1-в-1 вынуждало ``insert_textbox`` создавать лишние строки
    и уменьшать кегль. Логические границы списков и пустые строки сохраняются,
    остальные строки одного блока склеиваются и затем переносятся MuPDF по
    фактической ширине целевого шрифта.
    """
    normalized = _normalize_markers(text or "", cfg)
    raw_lines = normalized.splitlines()
    if len(raw_lines) <= 1:
        return normalized.strip()

    out: list[str] = []
    paragraph = ""

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            out.append(paragraph.strip())
            paragraph = ""

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if _LOGICAL_ITEM_RE.match(line):
            flush()
            out.append(line)
            continue
        if paragraph:
            paragraph += " " + line
        elif out and out[-1] and _LOGICAL_ITEM_RE.match(out[-1]):
            # Продолжение физически перенесённого пункта списка.
            out[-1] += " " + line
        else:
            paragraph = line
    flush()

    # Заголовок / подпись / ячейка тоже могут содержать логические пункты,
    # поэтому единый алгоритм безопаснее специальных ``replace('\\n', ' ')``.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def _translate_toc(toc: list, heading_lookup: dict[str, str]) -> list:
    new_toc = []
    for lvl, title, page in toc:
        cleaned = re.sub(r"\.{2,}", "", title).strip()
        key = _norm(cleaned)
        ru = heading_lookup.get(key)
        if not ru:
            cleaned2 = re.sub(r"[\.\s]*\d+\s*$", "", cleaned)
            ru = heading_lookup.get(_norm(cleaned2)) or title
        new_toc.append([lvl, ru, page])
    return new_toc


def _insert_text_fit(page, rect, text, fontname, fontfile, fontsize,
                     min_size, align, color) -> tuple[float, int]:
    """Вставляет текст не ниже ``min_size``.

    status: 0 - исходный размер, 1 - уменьшен, 2 - не помещается. В отличие
    от прежней реализации функция не делает повторную заведомо неуспешную
    вставку после достижения минимума и не маскирует потерю текста.
    """
    size = fontsize
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, color=color, align=align, render_mode=0)
        if rc >= 0:
            return size, (1 if size < fontsize - 0.01 else 0)
        size -= 0.5
    return min_size, 2


def _compute_fit_size(page, rect, text, fontname, fontfile, fontsize,
                      min_size, align) -> float | None:
    """Вычисляет размер шрифта, при котором text влезает в rect, БЕЗ вставки.

    Использует render_mode=3 (невидимый) чтобы не рисовать.
    """
    size = fontsize
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, align=align, render_mode=3)
        if rc >= 0:
            return size
        size -= 0.5
    return None


# ---------------------------------------------------------------------------
# Кластеризация пунктов списка для визуально согласованного размера шрифта.
# ---------------------------------------------------------------------------


def _cluster_listitems(items: list[dict], gap_y: float = 60.0
                       ) -> list[list[int]]:
    """Кластеризует пункты списка в группы (один список).

    Сортируем по y0, группы разрываются при большом вертикальном gap.
    """
    n = len(items)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: items[i]["bbox"][1])
    clusters: list[list[int]] = []
    current: list[int] = []
    prev_y1 = None
    for idx in order:
        bb = items[idx]["bbox"]
        y0, y1 = bb[1], bb[3]
        if prev_y1 is not None and y0 - prev_y1 > gap_y:
            if current:
                clusters.append(current)
            current = []
        current.append(idx)
        prev_y1 = max(prev_y1 or y1, y1)
    if current:
        clusters.append(current)
    return clusters


def _is_listlike(seg: dict) -> bool:
    """True если сегмент похож на пункт списка (нумерованный/маркированный),
    даже если сегментер ошибочно классифицировал его как heading/paragraph.
    """
    if seg["type"] == "listItem":
        return True
    ru = (seg.get("ru") or seg.get("text") or "").strip()
    # нумерованный пункт: «1.», «2.», «1)», «(1)» в начале
    if re.match(r"^\d+[\.\)]\s+\S", ru):
        return True
    # маркированный: •, –, —, ▪ в начале
    if re.match(r"^[•\-–—▪]\s+\S", ru):
        return True
    return False


def _role_min_size(seg: dict, cfg: dict) -> float:
    """Читаемый минимум по роли, а не один глобальный порог."""
    if seg.get("type") == "cell":
        return float(cfg.get("builder_table_min_fontsize", 7.5))
    if seg.get("type") in ("caption_fig", "caption_tab"):
        return float(cfg.get("builder_caption_min_fontsize", 8.0))
    if seg.get("type") == "heading":
        return float(cfg.get("builder_heading_min_fontsize", 9.0))
    return float(cfg.get("builder_min_fontsize", 8.5))


def _has_target_letters(text: str, target_lang: str) -> bool:
    """Отличает переводимый текст от чисел / служебных кодов."""
    lang = (target_lang or "").lower().replace("_", "-").split("-", 1)[0]
    if lang in {"ru", "uk", "bg", "sr"}:
        return bool(_CYRILLIC_RE.search(text))
    if lang == "zh":
        return bool(_HAN_RE.search(text))
    if lang == "ja":
        return bool(_HAN_RE.search(text) or re.search(r"[\u3040-\u30ff]", text))
    return any(ch.isalpha() for ch in text)


def _candidate_layout_rects(page, page_segs: list[dict], cfg: dict,
                            default_size: float) -> dict[int, fitz.Rect]:
    """Расширяет тесные bbox только в реально свободное место страницы."""
    base: dict[int, fitz.Rect] = {}
    for idx, seg in enumerate(page_segs):
        bb = seg.get("layout_bbox") or seg.get("bbox")
        if not bb:
            continue
        rect = fitz.Rect(bb)
        if rect.is_empty or rect.width < 1 or rect.height < 1:
            continue
        base[idx] = rect
    if not base:
        return {}

    content_x0 = min(r.x0 for r in base.values())
    content_x1 = max(r.x1 for r in base.values())
    content_width = max(1.0, content_x1 - content_x0)
    result: dict[int, fitz.Rect] = {}

    for idx, rect0 in base.items():
        seg = page_segs[idx]
        rect = fitz.Rect(rect0)
        size = max(
            float(seg.get("size") or default_size) or default_size,
            _role_min_size(seg, cfg),
        )
        is_cell = seg.get("type") == "cell"

        if not is_cell:
            rect.x0 = max(page.rect.x0, rect.x0 - 1.0)
            rect.x1 = min(page.rect.x1, rect.x1 + 1.0)

            # Подпись должна центрироваться относительно контентной области,
            # а короткий заголовок / абзац может занять пустое место справа.
            if seg.get("type") in ("caption_fig", "caption_tab"):
                rect.x0, rect.x1 = content_x0, content_x1
            elif rect.width < content_width * 0.80:
                right_peer = any(
                    j != idx and other.x0 >= rect.x1 - 1
                    and other.y0 < rect.y1 and other.y1 > rect.y0
                    for j, other in base.items())
                if not right_peer:
                    rect.x1 = content_x1

            # MuPDF textbox требует место не только под glyph bbox, но и под
            # ascender/descender и leading; 1.55 даёт строке читаемый кегль.
            desired_y1 = max(rect.y1 + 2.0, rect.y0 + size * 1.55)
            blockers = [
                other.y0 for j, other in base.items()
                if j != idx and other.y0 >= rect.y1 - 0.5
                and other.x0 < rect.x1 and other.x1 > rect.x0
            ]
            if blockers:
                desired_y1 = min(desired_y1, min(blockers) - 0.5)
            rect.y1 = max(rect.y1, min(page.rect.y1, desired_y1))

        result[idx] = rect
    return result


def _cluster_cell_rows(page_segs: list[dict], idxs: list[int],
                       tolerance: float = 3.0) -> list[list[int]]:
    """Группирует только одну строку таблицы, не всю таблицу целиком."""
    explicit: dict[tuple, list[int]] = {}
    loose: list[int] = []
    for idx in idxs:
        seg = page_segs[idx]
        if seg.get("table_idx") is not None and seg.get("row") is not None:
            explicit.setdefault((seg.get("table_idx"), seg.get("row")), []).append(idx)
        else:
            loose.append(idx)
    rows = list(explicit.values())
    for idx in sorted(loose, key=lambda i: page_segs[i]["bbox"][1]):
        y0 = (page_segs[idx].get("layout_bbox") or page_segs[idx]["bbox"])[1]
        for row in rows:
            ref = (page_segs[row[0]].get("layout_bbox") or
                   page_segs[row[0]]["bbox"])[1]
            if abs(y0 - ref) <= tolerance:
                row.append(idx)
                break
        else:
            rows.append([idx])
    return rows


def _harmonize_group_sizes(final: dict[int, float], groups: list[list[int]],
                           max_shrink: float,
                           minima: dict[int, float] | None = None) -> None:
    """Выравнивает близкие элементы, не протягивая один outlier на всю группу."""
    for group in groups:
        values = [final[i] for i in group if i in final]
        if len(values) < 2:
            continue
        target = max(min(values), max(values) - max_shrink)
        for i in group:
            if i in final:
                final[i] = max(
                    (minima or {}).get(i, 0.0),
                    min(final[i], target),
                )


def _plan_page_layout(page, page_segs: list[dict], cfg: dict,
                      target_lang: str, font_path: str, logger,
                      default_size: float) -> tuple[dict[int, dict], list[dict]]:
    """Полный preflight страницы до удаления исходного текста."""
    rects = _candidate_layout_rects(page, page_segs, cfg, default_size)
    tmp_doc = fitz.open()
    tmp_page = tmp_doc.new_page(width=page.rect.width, height=page.rect.height)
    plans: dict[int, dict] = {}
    overflow: list[dict] = []

    for idx, seg in enumerate(page_segs):
        rect = rects.get(idx)
        text = _prepare_text_for_layout(seg.get("ru") or "", seg.get("type", ""), cfg)
        if rect is None or not text:
            continue
        original_size = float(seg.get("size") or default_size) or default_size
        readable_min = _role_min_size(seg, cfg)
        if _has_target_letters(text, target_lang):
            preferred_size = max(original_size, readable_min)
            min_size = readable_min
        else:
            # Номера страниц и code-only подписи сохраняют исходный масштаб.
            preferred_size = original_size
            min_size = min(original_size, readable_min)
        align = fitz.TEXT_ALIGN_CENTER if seg.get("type") in (
            "caption_fig", "caption_tab") else fitz.TEXT_ALIGN_LEFT
        fn, ff = _resolve_font_for_seg(seg, cfg, target_lang, font_path, logger)
        source_lang = str(cfg.get("source_lang", "zh")).lower().split("-", 1)[0]
        normalized_target = str(target_lang).lower().split("-", 1)[0]
        if source_lang == "zh" and normalized_target != "zh" and _HAN_RE.search(text):
            overflow.append({
                "idx": idx, "id": seg.get("id", idx + 1),
                "page": seg.get("page", page.number), "type": seg.get("type"),
                "text": text, "rect": list(rect), "fontname": fn,
                "fontfile": ff, "min_size": min_size, "align": align,
                "marker": None, "marker_size": None,
                "invalid_source_script": True,
            })
            continue
        fit_size = _compute_fit_size(
            tmp_page, rect, text, fn, ff, preferred_size, min_size, align)
        if fit_size is None:
            label = f"[T{seg.get('id', idx + 1)}]"
            marker, marker_size = _overflow_preview(
                tmp_page, rect, text, label, fn, ff, min_size, align
            )
            overflow.append({
                "idx": idx, "id": seg.get("id", idx + 1),
                "page": seg.get("page", page.number), "type": seg.get("type"),
                "text": text, "rect": list(rect), "fontname": fn,
                "fontfile": ff, "min_size": min_size, "align": align,
                "marker": marker, "marker_size": marker_size,
            })
            continue
        plans[idx] = {
            "rect": rect, "text": text, "size": fit_size,
            "original_size": original_size, "preferred_size": preferred_size,
            "min_size": min_size,
            "align": align, "fontname": fn, "fontfile": ff,
        }

    tmp_doc.close()

    final_sizes = {idx: plan["size"] for idx, plan in plans.items()}
    final_minima = {idx: plan["min_size"] for idx, plan in plans.items()}
    cell_idxs = [i for i in plans if page_segs[i].get("type") == "cell"]
    _harmonize_group_sizes(
        final_sizes, _cluster_cell_rows(page_segs, cell_idxs),
        float(cfg.get("builder_group_max_shrink", 1.0)), final_minima)

    list_idxs = [i for i in plans if _is_listlike(page_segs[i])]
    list_groups = [
        [list_idxs[j] for j in group]
        for group in _cluster_listitems([page_segs[i] for i in list_idxs])
    ] if list_idxs else []
    _harmonize_group_sizes(
        final_sizes, list_groups,
        float(cfg.get("builder_group_max_shrink", 1.0)), final_minima)
    for idx, size in final_sizes.items():
        plans[idx]["size"] = size
    return plans, overflow


def _overflow_preview(tmp_page, rect: fitz.Rect, text: str, label: str,
                      fontname: str, fontfile: str | None, min_size: float,
                      align: int) -> tuple[str, float | None]:
    """Подбирает читаемый локальный анонс вместо голого ``[T…]``.

    Полный текст остаётся в приложении, но исходная страница сохраняет хотя бы
    начало смысла и ссылку на продолжение.
    """
    fallback_size = _compute_fit_size(
        tmp_page, rect, label, fontname, fontfile,
        min_size, min_size, align,
    )
    words = re.sub(r"\s+", " ", text or "").strip().split(" ")
    if not words:
        return label, fallback_size

    best_text = label
    best_size = fallback_size
    low, high = 1, len(words)
    while low <= high:
        mid = (low + high) // 2
        prefix = " ".join(words[:mid]).rstrip(".,;:!?")
        candidate = f"{prefix}… {label}"
        size = _compute_fit_size(
            tmp_page, rect, candidate, fontname, fontfile,
            min_size, min_size, align,
        )
        if size is not None:
            best_text, best_size = candidate, size
            low = mid + 1
        else:
            high = mid - 1
    return best_text, best_size


def _resolve_font_for_seg(seg: dict, cfg: dict, target_lang: str,
                          default_path: str, logger) -> tuple[str, str]:
    """Возвращает (fontname, fontfile) для сегмента.

    Если ``cfg["match_fonts"]`` включён — подбирает ближайший похожий шрифт
    под оригинальный ``seg['font']``. Иначе — общий ``default_path``.
    """
    # Подписи и перекрёстные ссылки должны извлекаться обратно без подмены
    # обычного дефиса на U+00AD некоторыми legacy-шрифтами (Book Antiqua и др.).
    if seg.get("anchors") or seg.get("type") in {"caption_fig", "caption_tab"}:
        alias = "tgt_" + hashlib.sha1(
            str(default_path).encode("utf-8")).hexdigest()[:8]
        return alias, default_path
    if not cfg.get("match_fonts"):
        alias = "tgt_" + hashlib.sha1(
            str(default_path).encode("utf-8")).hexdigest()[:8]
        return alias, default_path
    orig = seg.get("font") or ""
    try:
        path = match_font(orig, target_lang, fallback=default_path)
    except Exception as e:
        logger.warning("match_font(%r): %s — fallback", orig, e)
        alias = "tgt_" + hashlib.sha1(
            str(default_path).encode("utf-8")).hexdigest()[:8]
        return alias, default_path
    # Один alias для разных fontfile заставлял MuPDF переиспользовать первый
    # ресурс страницы. Alias детерминирован и уникален для файла.
    alias = "mtch_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return alias, path


def _append_overflow_pages(doc: fitz.Document, records: list[dict],
                           font_path: str, cfg: dict, logger) -> int:
    """Добавляет читаемое приложение для блоков, не вошедших в layout.

    На исходной странице остаётся короткая ссылка ``[T123]``. Полный перевод
    переносится в приложение и пагинируется ``fitz.Story`` без масштабирования,
    поэтому ни один блок не исчезает и не превращается в микрошрифт.
    """
    if not records:
        return 0
    by_page: dict[int, list[dict]] = {}
    for rec in records:
        by_page.setdefault(int(rec.get("page", 0)), []).append(rec)

    parts = ["<h1>Продолжение перевода</h1>"]
    for pno in sorted(by_page):
        parts.append(f"<h2>Исходная страница {pno + 1}</h2>")
        for rec in by_page[pno]:
            label = f"T{rec['id']}"
            body = html.escape(rec.get("text") or "").replace("\n", "<br>")
            parts.append(
                f"<section><h3>[{label}]</h3><div class='translation'>{body}</div></section>")

    font = Path(font_path)
    archive = fitz.Archive(str(font.parent))
    body_size = float(cfg.get("builder_overflow_fontsize", 9.0))
    css = (
        f"@font-face {{font-family: TargetPDF; src: url('{font.name}');}}\n"
        "body {font-family: TargetPDF, sans-serif; color: #111; margin: 0; "
        f"font-size: {body_size:.1f}pt; line-height: 1.32;}}\n"
        "h1 {font-size: 17pt; margin: 0 0 12pt 0; color: #18324a;}\n"
        "h2 {font-size: 12pt; margin: 14pt 0 6pt 0; color: #31546f; "
        "border-bottom: 0.7pt solid #9aabb7;}\n"
        "h3 {font-size: 9pt; margin: 7pt 0 2pt 0; color: #31546f;}\n"
        "section {margin: 0 0 7pt 0; break-inside: avoid;}\n"
        ".translation {margin: 0 0 5pt 0;}\n"
    )
    story = fitz.Story("".join(parts), user_css=css, archive=archive)
    page_rect = fitz.Rect(doc[0].rect) if doc.page_count else fitz.paper_rect("a4")
    margin = float(cfg.get("builder_overflow_margin", 42.0))
    content = fitz.Rect(
        page_rect.x0 + margin, page_rect.y0 + margin,
        page_rect.x1 - margin, page_rect.y1 - margin)

    def rectfn(_rect_num, _filled):
        return page_rect, content, None

    appendix = story.write_with_links(rectfn)
    count = appendix.page_count
    doc.insert_pdf(appendix)
    appendix.close()
    logger.warning(
        "Добавлено приложение: %d блоков, %d страниц", len(records), count)
    return count


def _write_layout_report(out_path: Path, report: dict, cfg: dict) -> Path:
    configured = cfg.get("builder_report_path")
    path = resolve_path(configured) if configured else Path(str(out_path) + ".layout.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(report, path)
    return path


def build(cfg: dict, logger, segments_ru_path: str | None = None,
          out_path: str | None = None) -> str:
    src_pdf = resolve_path(cfg["pdf_path"])
    out_path = resolve_path(out_path or cfg["out_path"])
    if os.path.normcase(str(src_pdf.resolve())) == os.path.normcase(
            str(out_path.resolve())):
        raise ValueError("Выходной PDF не должен перезаписывать исходный файл")
    segments_ru_path = segments_ru_path or "intermediate/segments_ru.json"
    if not Path(segments_ru_path).is_absolute():
        segments_ru_path = str(ROOT / segments_ru_path)

    segs = load_json(segments_ru_path)
    by_page: dict[int, list[dict]] = {}
    for s in segs:
        by_page.setdefault(s["page"], []).append(s)

    target_lang = cfg.get("target_lang", "ru")
    font_path = cfg.get("target_font") or cfg.get("cyrillic_font") or ""
    font_path = find_target_font(font_path, target_lang)
    logger.info("Базовый шрифт: %s", font_path)

    match_fonts = bool(cfg.get("match_fonts", False))
    if match_fonts:
        logger.info("Подбор шрифта по оригиналу включён (lang=%s)", target_lang)

    logger.info("Открываю копию исходника: %s", src_pdf)
    doc = fitz.open(str(src_pdf))
    source_page_count = doc.page_count

    total_blocks = skipped_empty = shrunk = notfit = 0
    overflow_records: list[dict] = []
    retained_records: list[dict] = []
    source_retained = 0
    rendered_sizes: list[float] = []
    default_size = float(cfg.get("builder_default_fontsize", 10.0))
    overflow_policy = str(cfg.get("builder_overflow_policy", "appendix")).lower()
    if overflow_policy not in {"appendix", "keep_source"}:
        logger.warning("Неизвестный builder_overflow_policy=%r; использую appendix",
                       overflow_policy)
        overflow_policy = "appendix"

    if cfg.get("enable_vision_ocr"):
        logger.warning(
            "enable_vision_ocr больше не запускается внутри основной сборки; "
            "используйте отдельный постпроцесс перевода изображений")

    for pno in range(source_page_count):
        page = doc.load_page(pno)
        page_segs = by_page.get(pno, [])
        if not page_segs:
            continue

        # Preflight выполняется до redaction: если перевод не помещается,
        # исходный текст не стирается без безопасного marker / appendix fallback.
        planned, page_overflow = _plan_page_layout(
            page, page_segs, cfg, target_lang, font_path, logger, default_size)
        overflow_by_idx = {int(rec["idx"]): rec for rec in page_overflow}

        redact_idxs = set(planned)
        if overflow_policy == "appendix":
            redact_idxs.update(
                idx for idx, rec in overflow_by_idx.items()
                if rec.get("marker_size") is not None)

        for idx in sorted(redact_idxs):
            seg = page_segs[idx]
            rect = fitz.Rect(seg["bbox"])
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue
            try:
                page.add_redact_annot(
                    rect, fill=None, cross_out=False)
            except Exception as e:
                logger.warning("redact annot p.%d id=%s: %s",
                               pno + 1, seg.get("id"), e)
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE)
        except Exception as e:
            logger.warning("apply_redactions p.%d: %s", pno + 1, e)

        for idx, seg in enumerate(page_segs):
            if idx not in planned:
                if idx not in overflow_by_idx:
                    skipped_empty += 1
                continue
            plan = planned[idx]

            fill = (0, 0, 0)
            try:
                color = int(seg.get("color") or 0)
                if color != 0:
                    fill = (((color >> 16) & 0xFF) / 255.0,
                            ((color >> 8) & 0xFF) / 255.0,
                            (color & 0xFF) / 255.0)
            except Exception:
                pass

            size, status = _insert_text_fit(
                page, plan["rect"], plan["text"],
                plan["fontname"], plan["fontfile"], plan["size"],
                plan["size"], plan["align"], fill)
            if status == 2:
                notfit += 1
                overflow_records.append({
                    "id": seg.get("id", idx + 1), "page": pno,
                    "type": seg.get("type"), "text": plan["text"],
                    "unexpected_insert_failure": True,
                })
            else:
                rendered_sizes.append(size)
                if size < plan["original_size"] - 0.01:
                    shrunk += 1
            total_blocks += 1

        for rec in page_overflow:
            rec["source_retained"] = True
            if rec.get("invalid_source_script"):
                source_retained += 1
                retained_records.append(rec)
                continue
            if overflow_policy == "appendix":
                overflow_records.append(rec)
                if rec.get("marker_size") is not None:
                    rc = page.insert_textbox(
                        fitz.Rect(rec["rect"]), rec["marker"],
                        fontname=rec["fontname"], fontfile=rec["fontfile"],
                        fontsize=rec["marker_size"], color=(0.25, 0.25, 0.25),
                        align=rec["align"])
                    rec["marker_inserted"] = rc >= 0
                    rec["source_retained"] = False
                    if rc < 0:
                        notfit += 1
                else:
                    source_retained += 1
                    retained_records.append(rec)
            else:
                source_retained += 1
                retained_records.append(rec)

        if (pno + 1) % 20 == 0:
            logger.info("  build page %d/%d", pno + 1, doc.page_count)

    appendix_pages = 0
    if overflow_policy == "appendix" and overflow_records:
        appendix_pages = _append_overflow_pages(
            doc, overflow_records, font_path, cfg, logger)

    try:
        orig_toc = doc.get_toc()
        new_toc = _translate_toc(orig_toc, _build_heading_lookup(segs))
        doc.set_toc(new_toc)
        logger.info("TOC переведён: %d закладок", len(new_toc))
    except Exception as e:
        logger.warning("TOC: %s", e)

    md = dict(doc.metadata or {})
    md_cfg = cfg.get("metadata", {})
    src_doc = fitz.open(str(src_pdf))
    src_md = dict(src_doc.metadata or {})
    src_doc.close()

    for k in ("title", "author", "subject", "keywords"):
        if md_cfg.get(k):
            md[k] = md_cfg[k]
        elif not md.get(k) and src_md.get(k):
            md[k] = src_md[k]
    try:
        doc.set_metadata(md)
    except Exception as e:
        logger.warning("set_metadata: %s", e)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Сохраняю %s (блоков: %d, уменьшено: %d, overflow: %d, "
        "не_влезло: %d, исходник_сохранён: %d, пропущено: %d)",
        out_path, total_blocks, shrunk, len(overflow_records), notfit,
        source_retained, skipped_empty)
    output_pages = doc.page_count
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{out_path.stem}.", suffix=".tmp.pdf", dir=str(out_path.parent))
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.unlink(missing_ok=True)
        doc.save(str(temporary_path), garbage=4, deflate=True)
        doc.close()
        verification = fitz.open(str(temporary_path))
        try:
            if verification.page_count != output_pages:
                raise RuntimeError(
                    "Проверка временного PDF не пройдена: "
                    f"pages={verification.page_count}, expected={output_pages}")
            for page_number in range(verification.page_count):
                verification.load_page(page_number)
        finally:
            verification.close()
        os.replace(temporary_path, out_path)
    except Exception:
        try:
            doc.close()
        except Exception:
            pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    report = {
        "version": 2,
        "source": str(src_pdf),
        "output": str(out_path),
        "source_pages": source_page_count,
        "output_pages": output_pages,
        "total_blocks": total_blocks,
        "shrunk_blocks": shrunk,
        "overflow_blocks": len(overflow_records),
        "appendix_pages": appendix_pages,
        "source_retained_blocks": source_retained,
        "notfit": notfit,
        "lost": 0 if notfit == 0 else notfit,
        "min_rendered_fontsize": min(rendered_sizes) if rendered_sizes else None,
        "readable_body_min": float(cfg.get("builder_min_fontsize", 8.5)),
        "readable_table_min": float(cfg.get("builder_table_min_fontsize", 7.5)),
        "overflow": [
            {k: rec.get(k) for k in (
                "id", "page", "type", "marker", "marker_inserted",
                "source_retained", "unexpected_insert_failure")}
            for rec in overflow_records
        ],
        "source_retained": [
            {k: rec.get(k) for k in (
                "id", "page", "type", "source_retained",
                "invalid_source_script", "unexpected_insert_failure")}
            for rec in retained_records
        ],
    }
    report_path = _write_layout_report(out_path, report, cfg)
    logger.info("Layout report: %s", report_path)
    logger.info("Готово: %s", out_path)
    return str(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка _RU.pdf")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    build(cfg, logger, segments_ru_path=args.inp, out_path=args.out)


if __name__ == "__main__":
    main()
