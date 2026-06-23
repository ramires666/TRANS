"""Этап 1 — Парсинг PDF в parse.json."""
from __future__ import annotations

import argparse
from pathlib import Path

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


def _detect_tables_lines(drawings: list[dict]) -> list[dict]:
    h_lines, v_lines = [], []
    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                dx, dy = p2.x - p1.x, p2.y - p1.y
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
    out = []
    used: set[tuple[int, int]] = set()
    for i in range(len(h_ys) - 1):
        y0, y1 = h_ys[i], h_ys[i + 1]
        if y1 - y0 < 4:
            continue
        for j in range(len(v_xs) - 1):
            x0, x1 = v_xs[j], v_xs[j + 1]
            if x1 - x0 < 4:
                continue
            if (i, j) in used:
                continue
            used.add((i, j))
            out.append({"bbox": [x0, y0, x1, y1], "method": "lines"})
    return out


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

        drawings = []
        try:
            for dr in page.get_drawings():
                drawings.append({
                    "rect": _rect(dr.get("rect", fitz.Rect())),
                    "fill": bool(dr.get("fill")),
                    "color": dr.get("color"),
                    "items_count": len(dr.get("items", [])),
                })
        except Exception:
            pass

        tables_ft = []
        try:
            for t in page.find_tables().tables:
                tables_ft.append({"bbox": _rect(t.bbox), "method": "find_tables"})
        except Exception:
            pass
        tables_lines = _detect_tables_lines(
            page.get_drawings() if drawings else [])
        tables = tables_ft + tables_lines

        pages.append({
            "page": pno, "rect": _rect(rect), "blocks": blocks,
            "images": images, "drawings_count": len(drawings), "tables": tables,
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