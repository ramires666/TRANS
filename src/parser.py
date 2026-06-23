"""Этап 1 — Парсинг PDF в intermediate/parse.json.

Для каждой страницы сохраняем:
- rect
- текстовые блоки (type==0) со спанами, bbox, шрифтом, размером, цветом
- изображения (type==1) с bbox
- drawings (линии/прямоугольники) — для детектора таблиц
- таблицы (find_tables + собственный детектор по линиям)
- TOC и метаданные — один раз в корне

Формат parse.json:
{
  "meta": {...}, "toc": [[lvl, title, page], ...],
  "pages": [ {page, rect, blocks, images, drawings, tables}, ... ]
}
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import ROOT, load_config, setup_logger, save_json, ensure_dirs, resolve_path


def _rect_to_list(r: fitz.Rect) -> list[float]:
    return [float(r.x0), float(r.y0), float(r.x1), float(r.y1)]


def _extract_spans(line: dict) -> list[dict]:
    spans = []
    for sp in line.get("spans", []):
        spans.append({
            "text": sp.get("text", ""),
            "bbox": _rect_to_list(fitz.Rect(sp["bbox"])),
            "size": float(sp.get("size", 0)),
            "font": str(sp.get("font", "")),
            "color": int(sp.get("color", 0)),
            "flags": int(sp.get("flags", 0)),
            "ascender": float(sp.get("ascender", 0)),
            "descender": float(sp.get("descender", 0)),
        })
    return spans


def _extract_block(block: dict) -> dict:
    lines = []
    for ln in block.get("lines", []):
        lines.append({
            "bbox": _rect_to_list(fitz.Rect(ln["bbox"])),
            "dir": list(ln.get("dir", [1.0, 0.0])),
            "wmode": int(ln.get("wmode", 0)),
            "spans": _extract_spans(ln),
        })
    return {
        "type": int(block.get("type", 0)),
        "bbox": _rect_to_list(fitz.Rect(block["bbox"])),
        "number": int(block.get("number", 0)),
        "lines": lines,
    }


def _detect_tables_lines(page: fitz.Page, drawings: list[dict]) -> list[dict]:
    """Простой детектор «нарисованных» таблиц по группам горизонтальных/вертикальных линий."""
    h_lines = []
    v_lines = []
    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                dx = p2.x - p1.x
                dy = p2.y - p1.y
                if abs(dy) < 0.5 and abs(dx) > 2:
                    h_lines.append((p1.y, p1.x, p2.x))
                elif abs(dx) < 0.5 and abs(dy) > 2:
                    v_lines.append((p1.x, p1.y, p2.y))
            elif item[0] == "re":
                r = item[1]
                if (r.x1 - r.x0) > 2 and (r.y1 - r.y0) < 1.5:
                    h_lines.append((r.y0, r.x0, r.x1))
                elif (r.y1 - r.y0) > 2 and (r.x1 - r.x0) < 1.5:
                    v_lines.append((r.x0, r.y0, r.y1))

    if len(h_lines) < 2 or len(v_lines) < 2:
        return []

    h_ys = sorted({round(y, 1) for y, _, _ in h_lines})
    v_xs = sorted({round(x, 1) for x, _, _ in v_lines})
    if len(h_ys) < 2 or len(v_xs) < 2:
        return []

    tables = []
    used: set[tuple[int, int]] = set()
    for i in range(len(h_ys) - 1):
        y0 = h_ys[i]
        y1 = h_ys[i + 1]
        if y1 - y0 < 4:
            continue
        for j in range(len(v_xs) - 1):
            x0 = v_xs[j]
            x1 = v_xs[j + 1]
            if x1 - x0 < 4:
                continue
            key = (i, j)
            if key in used:
                continue
            used.add(key)
            tables.append({
                "bbox": [x0, y0, x1, y1],
                "method": "lines",
            })
    # Группируем соседние ячейки в таблицы (грубая эвристика): возвращаем как есть,
    # сегментер разнесёт текст по ячейкам.
    return tables


def parse_pdf(pdf_path: str, cfg: dict, logger) -> dict:
    pdf_path = resolve_path(pdf_path)
    logger.info("Открываю PDF: %s", pdf_path)
    doc = fitz.open(str(pdf_path))

    meta = {
        "page_count": doc.page_count,
        "metadata": dict(doc.metadata or {}),
        "src_path": str(pdf_path.name),
    }
    toc = doc.get_toc()
    logger.info("Страниц: %d, TOC: %d", doc.page_count, len(toc))

    pages = []
    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        rect = page.rect
        d = page.get_text("dict")

        blocks = []
        for b in d.get("blocks", []):
            blocks.append(_extract_block(b))

        images = []
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                bbox = _rect_to_list(rects[0]) if rects else None
            except Exception:
                bbox = None
            images.append({"xref": xref, "bbox": bbox, "width": img[2], "height": img[3]})

        drawings = []
        try:
            for dr in page.get_drawings():
                drawings.append({
                    "rect": _rect_to_list(dr.get("rect", fitz.Rect())),
                    "fill": bool(dr.get("fill")),
                    "color": dr.get("color"),
                    "items_count": len(dr.get("items", [])),
                })
        except Exception:
            pass

        # Таблицы: find_tables + собственный детектор
        tables_ft = []
        try:
            tabs = page.find_tables()
            for t in tabs.tables:
                tables_ft.append({"bbox": _rect_to_list(t.bbox), "method": "find_tables"})
        except Exception:
            pass
        tables_lines = _detect_tables_lines(page, page.get_drawings() if drawings else [])
        tables = tables_ft + tables_lines

        pages.append({
            "page": pno,
            "rect": _rect_to_list(rect),
            "blocks": blocks,
            "images": images,
            "drawings_count": len(drawings),
            "tables": tables,
        })

        if pno % 20 == 0:
            logger.info("  parse page %d/%d", pno + 1, doc.page_count)

    doc.close()
    return {"meta": meta, "toc": toc, "pages": pages}


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсинг PDF -> parse.json")
    ap.add_argument("--in", dest="inp", help="PDF (по умолчанию из config.yaml)")
    ap.add_argument("--out", dest="out", help="путь вывода parse.json")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    pdf = args.inp or cfg["pdf_path"]
    out = args.out or cfg["parse_path"]
    data = parse_pdf(pdf, cfg, logger)
    save_json(data, out)
    logger.info("Готово: %s (страниц: %d)", resolve_path(out), len(data["pages"]))


if __name__ == "__main__":
    main()
