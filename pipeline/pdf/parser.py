"""Этап 1 — Парсинг PDF в parse.json."""
from __future__ import annotations

import argparse
from collections import defaultdict

import fitz

from pipeline.config.loader import (ensure_dirs, load_config, resolve_path,
                                     setup_logger)
from pipeline.io.artifacts import save_json


def _rect(r: fitz.Rect) -> list[float]:
    return [float(r.x0), float(r.y0), float(r.x1), float(r.y1)]


def _extract_spans(line: dict) -> list[dict]:
    out = []
    for sp in line.get("spans", []):
        out.append({
            "text": sp.get("text", ""),
            "bbox": _rect(fitz.Rect(sp["bbox"])),
            "size": float(sp.get("size", 0)),
            "font": str(sp.get("font", "")),
            "color": int(sp.get("color", 0)),
            "flags": int(sp.get("flags", 0)),
            "ascender": float(sp.get("ascender", 0)),
            "descender": float(sp.get("descender", 0)),
        })
    return out


def _extract_block(block: dict) -> dict:
    lines = []
    for ln in block.get("lines", []):
        lines.append({
            "bbox": _rect(fitz.Rect(ln["bbox"])),
            "dir": list(ln.get("dir", [1.0, 0.0])),
            "wmode": int(ln.get("wmode", 0)),
            "spans": _extract_spans(ln),
        })
    return {
        "type": int(block.get("type", 0)),
        "bbox": _rect(fitz.Rect(block["bbox"])),
        "number": int(block.get("number", 0)),
        "lines": lines,
    }


def _rect_key(value) -> tuple[float, float, float, float]:
    """Stable key for matching ``Table.cells`` with ``Table.rows`` cells."""
    rect = fitz.Rect(value)
    return tuple(round(float(v), 4) for v in (rect.x0, rect.y0, rect.x1, rect.y1))


def _table_to_dict(table) -> dict:
    """Serialize a PyMuPDF table without assuming flat-cell ordering.

    ``Table.cells`` is not guaranteed to be row-major. Row and column metadata
    therefore comes from ``Table.rows`` and is joined back to the flat cells by
    rectangle coordinates. If an older API exposes only flat cells, their
    row/column values intentionally remain ``None``.
    """
    records: dict[int, dict] = {}
    indices_by_rect: dict[tuple[float, float, float, float], list[int]] = defaultdict(list)

    flat_cells = list(getattr(table, "cells", None) or [])
    for index, cell in enumerate(flat_cells):
        if cell is None:
            continue
        bbox = _rect(fitz.Rect(cell))
        records[index] = {
            "index": index,
            "bbox": bbox,
            "row": None,
            "col": None,
        }
        indices_by_rect[_rect_key(cell)].append(index)

    rows = list(getattr(table, "rows", None) or [])
    next_index = len(flat_cells)
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(getattr(row, "cells", None) or []):
            if cell is None:
                continue
            key = _rect_key(cell)
            matching = indices_by_rect.get(key, [])
            index = next((i for i in matching
                          if records[i]["row"] is None), None)
            if index is None and matching:
                # A merged cell can be referenced by more than one row slot.
                # Keep its first physical position instead of duplicating it.
                continue
            if index is None:
                index = next_index
                next_index += 1
                records[index] = {
                    "index": index,
                    "bbox": _rect(fitz.Rect(cell)),
                    "row": None,
                    "col": None,
                }
                indices_by_rect[key].append(index)
            records[index]["row"] = row_index
            records[index]["col"] = col_index

    return {
        "bbox": _rect(fitz.Rect(table.bbox)),
        "method": "find_tables",
        "row_count": int(getattr(table, "row_count", len(rows)) or 0),
        "col_count": int(getattr(table, "col_count", 0) or 0),
        "cells": [records[i] for i in sorted(records)],
    }


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
        blocks = [_extract_block(b) for b in d.get("blocks", [])]

        images = []
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                bbox = _rect(rects[0]) if rects else None
            except Exception:
                bbox = None
            images.append({"xref": xref, "bbox": bbox, "width": img[2], "height": img[3]})

        raw_drawings = []
        try:
            # Reuse these paths in find_tables: page.get_drawings() is one of
            # the most expensive page-level operations and should run once.
            raw_drawings = list(page.get_drawings())
        except Exception:
            pass

        tables = []
        try:
            finder = page.find_tables(paths=raw_drawings)
            for table in finder.tables:
                try:
                    tables.append(_table_to_dict(table))
                except Exception:
                    continue
        except Exception:
            pass

        # Do not synthesize cells from the Cartesian product of every page
        # line: unrelated rules and frames otherwise become phantom tables.
        # If find_tables found nothing, leaving ``tables`` empty is safer.

        pages.append({
            "page": pno, "rect": _rect(rect), "blocks": blocks,
            "images": images, "drawings_count": len(raw_drawings), "tables": tables,
        })
        if pno % 20 == 0:
            logger.info("  parse page %d/%d", pno + 1, doc.page_count)

    doc.close()
    return {"meta": meta, "toc": toc, "pages": pages}


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсинг PDF -> parse.json")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    pdf = args.inp or cfg["pdf_path"]
    data = parse_pdf(pdf, cfg, logger)
    save_json(data, args.out or cfg["parse_path"])
    logger.info("Готово: %s", resolve_path(args.out or cfg["parse_path"]))


if __name__ == "__main__":
    main()
