"""Этап 4 — Сборка _RU.pdf.

Метод «копия+правка»: открываем копию исходника, для каждого текстового блока
накладываем redact-аннотацию с переводом и применяем. Картинки/вектор сохраняем
параметром images=fitz.PDF_REDACT_IMAGE_NONE. Кириллицу — через зарегистрированный
TTF-шрифт (DejaVuSans/Arial).

Дополнительно:
- перевод TOC-закладок
- заполнение метаданных
- автосжатие fontsize при переполнении
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (ROOT, load_config, setup_logger, load_json,
                   ensure_dirs, resolve_path, find_cyrillic_font)


def _load_translations(segments_ru_path: str) -> dict[int, dict]:
    segs = load_json(segments_ru_path)
    return {s["id"]: s for s in segs}


def _translate_toc_title(title: str, segs_by_id: list[dict]) -> str:
    """Подбирает перевод заголовка по совпадению нормализованного текста с сегментом heading."""
    return None  # используется внешняя функция


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_heading_lookup(segments: list[dict]) -> dict[str, str]:
    """norm(zh heading text) -> ru text (без лидирующих точек TOC)."""
    out = {}
    for s in segments:
        if s["type"] != "heading":
            continue
        zh = s["text"]
        # убираем точки-лидеры и хвостовой номер страницы
        cleaned = re.sub(r"[\.\s]*\d+\s*$", "", zh)
        cleaned = re.sub(r"\.{2,}", "", cleaned).strip()
        key = _norm(cleaned)
        if key and key not in out:
            out[key] = s["ru"]
    return out


def _translate_toc(toc: list, heading_lookup: dict[str, str]) -> list:
    new_toc = []
    for lvl, title, page in toc:
        cleaned = re.sub(r"\.{2,}", "", title).strip()
        key = _norm(cleaned)
        ru = heading_lookup.get(key)
        if not ru:
            # попробуем без хвоста
            cleaned2 = re.sub(r"[\.\s]*\d+\s*$", "", cleaned)
            ru = heading_lookup.get(_norm(cleaned2))
        if not ru:
            ru = title  # оставляем как есть
        new_toc.append([lvl, ru, page])
    return new_toc


def _insert_text_fit(page: fitz.Page, rect: fitz.Rect, text: str,
                     fontname: str, fontfile: str, fontsize: float,
                     min_size: float, align: int, color) -> tuple[float, int]:
    """Вписывает текст в rect через insert_textbox с автосжатием fontsize.

    Возвращает (итоговый size, флаг_overflow). 0 = влезло, 1 = сжато, 2 = не влезло.
    """
    size = fontsize
    last_rc = -1
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, color=color, align=align, render_mode=0,
        )
        last_rc = rc
        if rc >= 0:
            # влезло. Если уменьшали — overflow=1, иначе 0
            return size, (1 if size < fontsize - 0.01 else 0)
        # не влезло (rc == -1) — уменьшаем
        size -= 0.5
    # даже на минимальном не влезло — вставим как есть на минимальном
    page.insert_textbox(
        rect, text, fontname=fontname, fontfile=fontfile,
        fontsize=max(min_size, size), color=color, align=align, render_mode=0,
    )
    return max(min_size, size), 2


def build(cfg: dict, logger, segments_ru_path: str | None = None,
          out_path: str | None = None) -> str:
    src_pdf = resolve_path(cfg["pdf_path"])
    out_path = resolve_path(out_path or cfg["out_path"])
    segments_ru_path = segments_ru_path or "intermediate/segments_ru.json"

    segs = load_json(segments_ru_path)
    segs_by_page: dict[int, list[dict]] = {}
    for s in segs:
        segs_by_page.setdefault(s["page"], []).append(s)

    font_path = cfg.get("cyrillic_font") or ""
    if not font_path:
        font_path = find_cyrillic_font("")
    logger.info("Шрифт кириллицы: %s", font_path)

    logger.info("Открываю копию исходника: %s", src_pdf)
    doc = fitz.open(str(src_pdf))

    # регистрируем шрифт на каждой странице (один раз)
    fontname = "cyr"
    # Текст размещается через page.insert_textbox с fontfile (кастомный TTF),
    # т.к. redact-аннотация с text= поддерживает только встроенные шрифты без кириллицы.

    total_blocks = 0
    skipped_empty = 0
    overflows = 0
    notfit = 0

    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        page_segs = segs_by_page.get(pno, [])
        if not page_segs:
            continue

        min_size = float(cfg.get("builder_min_fontsize", 6.0))
        default_size = float(cfg.get("builder_default_fontsize", 10.0))

        for seg in page_segs:
            ru = (seg.get("ru") or "").strip()
            if not ru:
                skipped_empty += 1
                continue
            bb = seg["bbox"]
            rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                skipped_empty += 1
                continue

            # исходный размер шрифта сегмента — ориентир
            orig_size = float(seg.get("size") or default_size)
            if orig_size <= 0:
                orig_size = default_size

            align = fitz.TEXT_ALIGN_LEFT
            if seg["type"] in ("caption_fig", "caption_tab"):
                align = fitz.TEXT_ALIGN_CENTER

            fill = (0, 0, 0)
            try:
                color = int(seg.get("color") or 0)
                if color != 0:
                    r = ((color >> 16) & 0xFF) / 255.0
                    g = ((color >> 8) & 0xFF) / 255.0
                    b = (color & 0xFF) / 255.0
                    fill = (r, g, b)
            except Exception:
                pass

            # Шаг 1: redact без текста — заливаем белым, чтобы убрать оригинал
            try:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            except Exception as e:
                logger.warning("add_redact p.%d seg#%d: %s", pno + 1, seg["id"], e)
                skipped_empty += 1
                continue

        # применяем redactions, не трогая изображения — один раз на страницу
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception as e:
            logger.warning("apply_redactions p.%d: %s", pno + 1, e)

        # Шаг 2: вписываем переведённый текст кастомным шрифтом
        for seg in page_segs:
            ru = (seg.get("ru") or "").strip()
            if not ru:
                continue
            bb = seg["bbox"]
            rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue

            orig_size = float(seg.get("size") or default_size)
            if orig_size <= 0:
                orig_size = default_size

            align = fitz.TEXT_ALIGN_LEFT
            if seg["type"] in ("caption_fig", "caption_tab"):
                align = fitz.TEXT_ALIGN_CENTER

            fill = (0, 0, 0)
            try:
                color = int(seg.get("color") or 0)
                if color != 0:
                    r = ((color >> 16) & 0xFF) / 255.0
                    g = ((color >> 8) & 0xFF) / 255.0
                    b = (color & 0xFF) / 255.0
                    fill = (r, g, b)
            except Exception:
                pass

            size, status = _insert_text_fit(
                page, rect, ru, fontname, font_path, orig_size, min_size, align, fill)
            if status == 2:
                notfit += 1
            elif status == 1:
                overflows += 1
            total_blocks += 1

        if (pno + 1) % 20 == 0:
            logger.info("  build page %d/%d", pno + 1, doc.page_count)

    # TOC
    try:
        orig_toc = doc.get_toc()
        lookup = _build_heading_lookup(segs)
        new_toc = _translate_toc(orig_toc, lookup)
        doc.set_toc(new_toc)
        logger.info("TOC переведён: %d закладок", len(new_toc))
    except Exception as e:
        logger.warning("TOC: %s", e)

    # Метаданные
    md = dict(doc.metadata or {})
    md_cfg = cfg.get("metadata", {})
    for k in ("title", "author", "subject", "keywords"):
        if md_cfg.get(k):
            md[k] = md_cfg[k]
    try:
        doc.set_metadata(md)
    except Exception as e:
        logger.warning("set_metadata: %s", e)

    # Сохранение
    logger.info("Сохраняю %s (блоков: %d, сжато: %d, не_влезло: %d, пропущено: %d)",
                out_path, total_blocks, overflows, notfit, skipped_empty)
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
    logger.info("Готово: %s", out_path)
    return str(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка _RU.pdf из segments_ru.json")
    ap.add_argument("--in", dest="inp", help="segments_ru.json")
    ap.add_argument("--out", dest="out", help="выходной PDF")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    build(cfg, logger, segments_ru_path=args.inp, out_path=args.out)


if __name__ == "__main__":
    main()
