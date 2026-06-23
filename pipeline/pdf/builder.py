"""Этап 4 — Сборка _RU.pdf: копия+редакт+перевод TOC+метаданные."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz

from pipeline.config.loader import (ensure_dirs, load_config, resolve_path,
                                     setup_logger)
from pipeline.fonts.fonts import find_target_font
from pipeline.io.artifacts import load_json


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
    size = fontsize
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, color=color, align=align, render_mode=0)
        if rc >= 0:
            return size, (1 if size < fontsize - 0.01 else 0)
        size -= 0.5
    page.insert_textbox(
        rect, text, fontname=fontname, fontfile=fontfile,
        fontsize=max(min_size, size), color=color, align=align, render_mode=0)
    return max(min_size, size), 2


def build(cfg: dict, logger, segments_ru_path: str | None = None,
          out_path: str | None = None) -> str:
    src_pdf = resolve_path(cfg["pdf_path"])
    out_path = resolve_path(out_path or cfg["out_path"])
    segments_ru_path = segments_ru_path or "intermediate/segments_ru.json"
    if not Path(segments_ru_path).is_absolute():
        from pipeline.config.loader import ROOT
        segments_ru_path = str(ROOT / segments_ru_path)

    segs = load_json(segments_ru_path)
    by_page: dict[int, list[dict]] = {}
    for s in segs:
        by_page.setdefault(s["page"], []).append(s)

    font_path = cfg.get("target_font") or cfg.get("cyrillic_font") or ""
    if not font_path:
        font_path = find_target_font("")
    logger.info("Шрифт: %s", font_path)

    logger.info("Открываю копию исходника: %s", src_pdf)
    doc = fitz.open(str(src_pdf))
    fontname = "tgt"

    total_blocks = skipped_empty = overflows = notfit = 0
    min_size = float(cfg.get("builder_min_fontsize", 6.0))
    default_size = float(cfg.get("builder_default_fontsize", 10.0))

    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        page_segs = by_page.get(pno, [])
        if not page_segs:
            continue

        for seg in page_segs:
            ru = (seg.get("ru") or "").strip()
            if not ru:
                continue
            bb = seg["bbox"]
            rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue
            try:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            except Exception:
                continue
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception as e:
            logger.warning("apply_redactions p.%d: %s", pno + 1, e)

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

            orig_size = float(seg.get("size") or default_size) or default_size
            align = fitz.TEXT_ALIGN_CENTER if seg["type"] in (
                "caption_fig", "caption_tab") else fitz.TEXT_ALIGN_LEFT

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
                page, rect, ru, fontname, font_path, orig_size, min_size, align, fill)
            if status == 2:
                notfit += 1
            elif status == 1:
                overflows += 1
            total_blocks += 1

        if (pno + 1) % 20 == 0:
            logger.info("  build page %d/%d", pno + 1, doc.page_count)

    try:
        orig_toc = doc.get_toc()
        new_toc = _translate_toc(orig_toc, _build_heading_lookup(segs))
        doc.set_toc(new_toc)
        logger.info("TOC переведён: %d закладок", len(new_toc))
    except Exception as e:
        logger.warning("TOC: %s", e)

    md = dict(doc.metadata or {})
    md_cfg = cfg.get("metadata", {})
    for k in ("title", "author", "subject", "keywords"):
        if md_cfg.get(k):
            md[k] = md_cfg[k]
    try:
        doc.set_metadata(md)
    except Exception as e:
        logger.warning("set_metadata: %s", e)

    logger.info("Сохраняю %s (блоков: %d, сжато: %d, не_влезло: %d, пропущено: %d)",
                out_path, total_blocks, overflows, notfit, skipped_empty)
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
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