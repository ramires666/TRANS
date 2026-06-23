"""Этап 2 — Сегментация parse.json -> segments.json.

Склеивает спаны в логические сегменты:
- heading     — заголовок (по совпадению с TOC или шаблону 第N章/N.N.N)
- listItem    — пункт нумерованного/маркированного списка
- caption     — подпись рисунка/таблицы (图N-M / 表N-N ...)
- paragraph   — обычный абзац
- cell        — ячейка таблицы (текст внутри bbox таблицы)

Каждый сегмент: {id, type, page, bbox, section_id, text, anchors:[...], font, size, color, align}

Перекрёстные ссылки (якоря) выделяются регулярками и помечаются как untranslatable_tokens.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import ROOT, load_config, setup_logger, save_json, load_json, ensure_dirs, resolve_path

# Регулярки якорей
RE_FIG = re.compile(r"图\s*(\d+)-(\d+)")
RE_TAB = re.compile(r"表\s*(\d+)-(\d+)")
RE_SEC = re.compile(r"第?(\d+)\.(\d+)(?:\.(\d+))?\s*章节?")
RE_APPENDIX = re.compile(r"附录\s*([A-Z])")

# Шаблоны заголовков
RE_HEAD_CHAPTER = re.compile(r"^第(\d+)章\s*(.+)$")
RE_HEAD_NUM = re.compile(r"^(\d+(?:\.\d+){0,2})\s+(.+)$")
RE_HEAD_NUM_ONLY = re.compile(r"^(\d+(?:\.\d+){0,2})\s*$")

# Маркер списка: цифра-точка, Wingdings-маркер, «•», «-», буква-скобка
RE_LIST_NUM = re.compile(r"^\s*(\d+)\.\s+(.*)$")
RE_LIST_BULLET = re.compile(r"^\s*[•●○\-–▪◆]\s+(.*)$")

# Подпись
RE_CAPTION_FIG = re.compile(r"^\s*图\s*\d+-\d+")
RE_CAPTION_TAB = re.compile(r"^\s*表\s*\d+-\d+")


def _find_anchors(text: str) -> list[dict]:
    out = []
    for m in RE_FIG.finditer(text):
        out.append({"kind": "fig", "raw": m.group(0), "num": f"{m.group(1)}-{m.group(2)}"})
    for m in RE_TAB.finditer(text):
        out.append({"kind": "tab", "raw": m.group(0), "num": f"{m.group(1)}-{m.group(2)}"})
    for m in RE_SEC.finditer(text):
        out.append({"kind": "sec", "raw": m.group(0), "num": m.group(0).strip()})
    for m in RE_APPENDIX.finditer(text):
        out.append({"kind": "appx", "raw": m.group(0), "letter": m.group(1)})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_toc_index(toc: list) -> dict:
    """title_norm -> (lvl, page, title). Совпадение по нормализованному тексту."""
    idx = {}
    for lvl, title, page in toc:
        idx[_norm(title)] = {"lvl": lvl, "page": page, "title": title}
    return idx


def _block_text(block: dict) -> str:
    parts = []
    for ln in block["lines"]:
        for sp in ln["spans"]:
            parts.append(sp["text"])
    return "".join(parts)


def _block_first_style(block: dict) -> tuple[str, float, int]:
    for ln in block["lines"]:
        for sp in ln["spans"]:
            return sp.get("font", ""), float(sp.get("size", 0)), int(sp.get("color", 0))
    return "", 0.0, 0


def _classify(text: str, toc_idx: dict, style: tuple[str, float, int]) -> tuple[str, str | None]:
    """Возвращает (type, section_id_or_None). section_id — канонический номер главы."""
    s = text.strip()
    if not s:
        return "empty", None

    n = _norm(s)
    if n in toc_idx:
        info = toc_idx[n]
        return "heading", _section_id_from_title(info["title"])

    m = RE_HEAD_CHAPTER.match(s)
    if m:
        return "heading", f"ch{m.group(1)}"
    m = RE_HEAD_NUM.match(s)
    if m:
        return "heading", m.group(1)
    if RE_HEAD_NUM_ONLY.match(s):
        return "heading", None

    if RE_CAPTION_FIG.match(s):
        return "caption_fig", None
    if RE_CAPTION_TAB.match(s):
        return "caption_tab", None

    if RE_LIST_NUM.match(s) or RE_LIST_BULLET.match(s):
        return "listItem", None

    return "paragraph", None


def _section_id_from_title(title: str) -> str | None:
    s = title.strip()
    m = RE_HEAD_CHAPTER.match(s)
    if m:
        return f"ch{m.group(1)}"
    m = RE_HEAD_NUM.match(s)
    if m:
        return m.group(1)
    return None


def _point_in_rect(x: float, y: float, rect: list[float]) -> bool:
    return rect[0] - 1 <= x <= rect[2] + 1 and rect[1] - 1 <= y <= rect[3] + 1


def _block_center(block: dict) -> tuple[float, float]:
    bb = block["bbox"]
    return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2


def _merge_close_blocks(blocks: list[dict]) -> list[dict]:
    """Склеить соседние текстовые блоки одной строки (gap < 1pt) в один — для абзацев."""
    out = []
    for b in blocks:
        if b["type"] != 0 or not b["lines"]:
            out.append(b)
            continue
        if out and out[-1]["type"] == 0 and out[-1]["lines"]:
            prev = out[-1]
            pb = prev["bbox"]
            cb = b["bbox"]
            same_row = abs((pb[1] + pb[3]) / 2 - (cb[1] + cb[3]) / 2) < 2.5
            close = cb[0] - pb[2] < 2.0 and cb[0] - pb[2] > -50
            if same_row and close:
                prev["lines"].extend(b["lines"])
                prev["bbox"] = [min(pb[0], cb[0]), min(pb[1], cb[1]),
                                max(pb[2], cb[2]), max(pb[3], cb[3])]
                continue
        out.append(b)
    return out


def segment(parse_data: dict, cfg: dict, logger) -> list[dict]:
    toc = parse_data.get("toc", [])
    toc_idx = _build_toc_index(toc)
    pages = parse_data["pages"]
    segments: list[dict] = []
    sid = 0

    current_section: str | None = None

    for pinfo in pages:
        pno = pinfo["page"]
        blocks = [b for b in pinfo["blocks"] if b["type"] == 0]
        blocks = _merge_close_blocks(blocks)
        tables = pinfo.get("tables", [])

        for b in blocks:
            text = _block_text(b).strip()
            if not text:
                continue
            font, size, color = _block_first_style(b)

            stype, secid = _classify(text, toc_idx, (font, size, color))
            if stype == "heading":
                if secid:
                    current_section = secid
                section_id = secid or current_section
            else:
                section_id = current_section

            # Принадлежность ячейке таблицы?
            cell_table_idx = None
            cx, cy = _block_center(b)
            for ti, t in enumerate(tables):
                if _point_in_rect(cx, cy, t["bbox"]):
                    cell_table_idx = ti
                    break
            if cell_table_idx is not None and stype == "paragraph":
                stype = "cell"

            anchors = _find_anchors(text)
            sid += 1
            segments.append({
                "id": sid,
                "type": stype,
                "page": pno,
                "bbox": b["bbox"],
                "section_id": section_id,
                "text": text,
                "anchors": anchors,
                "font": font,
                "size": size,
                "color": color,
                "table_idx": cell_table_idx,
            })

    logger.info("Сегментов: %d", len(segments))
    by_type: dict[str, int] = {}
    for s in segments:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    logger.info("По типам: %s", by_type)
    return segments


def main() -> None:
    ap = argparse.ArgumentParser(description="Сегментация parse.json -> segments.json")
    ap.add_argument("--in", dest="inp", help="parse.json (по умолчанию из config)")
    ap.add_argument("--out", dest="out", help="segments.json")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    inp = args.inp or cfg["parse_path"]
    out = args.out or cfg["segments_path"]
    data = load_json(inp)
    segs = segment(data, cfg, logger)
    save_json(segs, out)
    logger.info("Готово: %s", resolve_path(out))


if __name__ == "__main__":
    main()
