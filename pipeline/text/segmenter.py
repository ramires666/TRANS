"""Этап 2 — Сегментация parse.json -> segments.json.

Склеивает спаны в логические сегменты типов:
- heading     — заголовок (по совпадению с TOC или шаблону главы/номера)
- listItem    — пункт нумерованного/маркированного списка
- caption_fig / caption_tab — подписи рис/таб (по якорям)
- paragraph   — обычный абзац
- cell        — ячейка таблицы (текст внутри bbox таблицы)

Якоря (图N-M / 表N-N / Fig. N-M / Table N-N ...) определяются конфигом
через `pipeline.anchors`.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import re

from pipeline.anchors import (LIST_BULLET, LIST_NUM, compiled_anchors,
                              find_anchors_in_text, section_patterns)
from pipeline.config.loader import load_config, resolve_path, setup_logger
from pipeline.io.artifacts import load_json, save_json

PAT_LIST_NUM = re.compile(LIST_NUM)
PAT_LIST_BULLET = re.compile(LIST_BULLET)

_DEFAULT_MARKER_CHARS = "•●○◌◦◆◇■□▪▫▶▷◀◁▲△▼▽◄►◅▻"
_CELL_PADDING_X = 2.0
_CELL_PADDING_Y = 1.0


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_toc_index(toc: list) -> dict:
    idx = {}
    for lvl, title, page in toc:
        idx[_norm(title)] = {"lvl": lvl, "page": page, "title": title}
    return idx


from pipeline.pdf.builder import _normalize_markers

def _block_text(block: dict, cfg: dict | None = None) -> str:
    # Склеиваем спаны внутри строки без разделителя (они соседствуют),
    # а СТРОКИ блока — через "\n". Это сохраняет переносы строк и ведущие
    # пробелы/табуляции каждой строки ровно как в оригинале, чтобы LLM получил
    # структурированный фрагмент, а не сплошную кашу.
    lines = []
    for ln in block["lines"]:
        lines.append("".join(sp["text"] for sp in ln["spans"]))
    text = "\n".join(lines)
    # Нормализуем все маркеры списков для любого языка
    text = _normalize_markers(text, cfg)
    return text


def _is_pua(ch: str) -> bool:
    cp = ord(ch)
    return (0xE000 <= cp <= 0xF8FF
            or 0xF0000 <= cp <= 0xFFFFD
            or 0x100000 <= cp <= 0x10FFFD)


def _span_content_weight(text: str, marker_chars: set[str]) -> int:
    chars = [ch for ch in (text or "")
             if not ch.isspace() and not _is_pua(ch)]
    # Marker-only spans carry layout decoration, not the body typography.
    return sum(1 for ch in chars if ch not in marker_chars)


def _block_dominant_style(block: dict, cfg: dict | None = None
                          ) -> tuple[str, float, int, int]:
    """Return the style covering most substantive characters in a block."""
    cfg = cfg or {}
    marker_chars = set(cfg.get("bullet_chars") or _DEFAULT_MARKER_CHARS)
    weights: dict[tuple[str, float, int, int], int] = {}
    marker_fallback: tuple[str, float, int, int] | None = None

    for ln in block["lines"]:
        for sp in ln["spans"]:
            text = sp.get("text", "") or ""
            style = (
                str(sp.get("font", "")),
                round(float(sp.get("size", 0)), 3),
                int(sp.get("color", 0)),
                int(sp.get("flags", 0)),
            )
            non_pua = "".join(ch for ch in text if not _is_pua(ch))
            if marker_fallback is None and non_pua.strip():
                marker_fallback = style
            weight = _span_content_weight(text, marker_chars)
            if weight:
                weights[style] = weights.get(style, 0) + weight

    if weights:
        return max(weights, key=weights.get)
    return marker_fallback or ("", 0.0, 0, 0)


def _classify(text: str, toc_idx: dict, chapter_re: re.Pattern,
              num_re: re.Pattern, num_only_re: re.Pattern,
              compiled_anch: dict) -> tuple[str, str | None]:
    s = text.strip()
    if not s:
        return "empty", None

    n = _norm(s)
    if n in toc_idx:
        return "heading", _section_id_from_title(toc_idx[n]["title"], chapter_re, num_re)

    m = chapter_re.match(s)
    if m:
        return "heading", f"ch{m.group(1)}"
    m = num_re.match(s)
    if m:
        return "heading", m.group(1)
    if num_only_re.match(s):
        return "heading", None

    fig_re = compiled_anch["fig"]["src"]
    tab_re = compiled_anch["tab"]["src"]
    if fig_re and fig_re.search(s):
        return "caption_fig", None
    if tab_re and tab_re.search(s):
        return "caption_tab", None

    if PAT_LIST_NUM.match(s) or PAT_LIST_BULLET.match(s):
        return "listItem", None
    return "paragraph", None


def _section_id_from_title(title: str, chapter_re: re.Pattern, num_re: re.Pattern) -> str | None:
    s = title.strip()
    m = chapter_re.match(s)
    if m:
        return f"ch{m.group(1)}"
    m = num_re.match(s)
    if m:
        return m.group(1)
    return None


def _point_in_rect(x: float, y: float, rect: list[float]) -> bool:
    return rect[0] - 1 <= x <= rect[2] + 1 and rect[1] - 1 <= y <= rect[3] + 1


def _block_center(block: dict) -> tuple[float, float]:
    bb = block["bbox"]
    return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2


def _locate_table_cell(block: dict, tables: list[dict]) -> dict:
    """Locate a block in an exact table cell, preferring the smallest match."""
    cx, cy = _block_center(block)
    best: tuple[float, dict] | None = None
    containing_table_idx = None

    for table_idx, table in enumerate(tables):
        table_bbox = table.get("bbox")
        if not table_bbox or not _point_in_rect(cx, cy, table_bbox):
            continue
        if containing_table_idx is None:
            containing_table_idx = table_idx
        for fallback_idx, cell in enumerate(table.get("cells") or []):
            cell_bbox = cell.get("bbox")
            if not cell_bbox or not _point_in_rect(cx, cy, cell_bbox):
                continue
            width = max(0.0, float(cell_bbox[2]) - float(cell_bbox[0]))
            height = max(0.0, float(cell_bbox[3]) - float(cell_bbox[1]))
            match = {
                "table_idx": table_idx,
                "cell_idx": cell.get("index", fallback_idx),
                "row": cell.get("row"),
                "col": cell.get("col"),
                "cell_bbox": list(cell_bbox),
            }
            area = width * height
            if best is None or area < best[0]:
                best = (area, match)

    if best is not None:
        return best[1]
    return {
        "table_idx": containing_table_idx,
        "cell_idx": None,
        "row": None,
        "col": None,
        "cell_bbox": None,
    }


def _inner_cell_bbox(bbox: list[float]) -> list[float]:
    x0, y0, x1, y1 = map(float, bbox)
    pad_x = min(_CELL_PADDING_X, max(0.0, (x1 - x0 - 1.0) / 2.0))
    pad_y = min(_CELL_PADDING_Y, max(0.0, (y1 - y0 - 1.0) / 2.0))
    return [x0 + pad_x, y0 + pad_y, x1 - pad_x, y1 - pad_y]


def _with_cell_metadata(block: dict, cell: dict) -> dict:
    block["_table_idx"] = cell["table_idx"]
    block["_cell_idx"] = cell["cell_idx"]
    block["_row"] = cell["row"]
    block["_col"] = cell["col"]
    block["_cell_bbox"] = cell["cell_bbox"]
    return block


def _union_bbox(items: list[list[float]]) -> list[float]:
    return [
        min(bb[0] for bb in items),
        min(bb[1] for bb in items),
        max(bb[2] for bb in items),
        max(bb[3] for bb in items),
    ]


def _split_block_by_cells(block: dict, tables: list[dict]) -> list[dict]:
    """Split a PyMuPDF text block when its spans belong to different cells.

    PyMuPDF can return one block for two same-row table cells. Keeping that
    block intact would assign both texts to whichever cell contains its center.
    """
    assignments: list[tuple[dict, dict, dict]] = []
    distinct_keys: set[tuple[object, object]] = set()
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            cell = _locate_table_cell({"bbox": span["bbox"]}, tables)
            assignments.append((line, span, cell))
            distinct_keys.add((cell["table_idx"], cell["cell_idx"]))

    if not assignments:
        copied = deepcopy(block)
        return [_with_cell_metadata(copied,
                                    _locate_table_cell(copied, tables))]

    if len(distinct_keys) == 1:
        copied = deepcopy(block)
        return [_with_cell_metadata(copied, assignments[0][2])]

    groups: dict[tuple[object, object], dict] = {}
    for line, span, cell in assignments:
        key = (cell["table_idx"], cell["cell_idx"])
        group = groups.get(key)
        if group is None:
            group = {
                "type": block.get("type", 0),
                "bbox": [],
                "number": block.get("number", 0),
                "lines": [],
                "_cell": cell,
                "_line_groups": {},
            }
            groups[key] = group

        line_key = id(line)
        target_line = group["_line_groups"].get(line_key)
        if target_line is None:
            target_line = {
                "bbox": [],
                "dir": list(line.get("dir", [1.0, 0.0])),
                "wmode": int(line.get("wmode", 0)),
                "spans": [],
            }
            group["_line_groups"][line_key] = target_line
            group["lines"].append(target_line)
        target_line["spans"].append(deepcopy(span))

    out = []
    for group in groups.values():
        for line in group["lines"]:
            line["bbox"] = _union_bbox([sp["bbox"] for sp in line["spans"]])
        group["bbox"] = _union_bbox([line["bbox"] for line in group["lines"]])
        cell = group.pop("_cell")
        group.pop("_line_groups")
        out.append(_with_cell_metadata(group, cell))
    return out


def _can_merge_blocks(left: dict, right: dict) -> bool:
    left_table = left.get("_table_idx")
    right_table = right.get("_table_idx")
    if left_table is None and right_table is None:
        return True
    # Unknown or different cells must never be merged across a table grid.
    left_cell = left.get("_cell_idx")
    right_cell = right.get("_cell_idx")
    return (left_table == right_table
            and left_cell is not None
            and left_cell == right_cell)


def _merge_same_cell_blocks(blocks: list[dict]) -> list[dict]:
    """Create one logical text block per exact physical table cell."""
    out: list[dict] = []
    by_cell: dict[tuple[object, object], dict] = {}
    for block in blocks:
        table_idx = block.get("_table_idx")
        cell_idx = block.get("_cell_idx")
        if table_idx is None or cell_idx is None:
            out.append(block)
            continue
        key = (table_idx, cell_idx)
        existing = by_cell.get(key)
        if existing is None:
            by_cell[key] = block
            out.append(block)
            continue
        existing["lines"].extend(block.get("lines", []))
        existing["bbox"] = _union_bbox([existing["bbox"], block["bbox"]])
    for block in by_cell.values():
        block["lines"].sort(key=lambda line: (line["bbox"][1],
                                               line["bbox"][0]))
    return out


def _merge_close_blocks(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b["type"] != 0 or not b["lines"]:
            out.append(b)
            continue
        if (out and out[-1]["type"] == 0 and out[-1]["lines"]
                and _can_merge_blocks(out[-1], b)):
            prev = out[-1]
            pb, cb = prev["bbox"], b["bbox"]
            same_row = abs((pb[1] + pb[3]) / 2 - (cb[1] + cb[3]) / 2) < 2.5
            close = -50 < cb[0] - pb[2] < 2.0
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
    segs: list[dict] = []
    sid = 0
    current_section: str | None = None

    sp = section_patterns(cfg)
    chapter_re = re.compile(sp["chapter"])
    num_re = re.compile(sp["num"])
    num_only_re = re.compile(sp["num_only"])
    compiled_anch = compiled_anchors(cfg)

    for pinfo in pages:
        pno = pinfo["page"]
        tables = pinfo.get("tables", [])
        blocks = []
        for block in pinfo["blocks"]:
            if block["type"] == 0:
                blocks.extend(_split_block_by_cells(block, tables))
        blocks = _merge_same_cell_blocks(blocks)
        blocks = _merge_close_blocks(blocks)

        for b in blocks:
            raw = _block_text(b, cfg)
            text = raw.strip()
            if not text:
                continue
            font, size, color, flags = _block_dominant_style(b, cfg)

            stype, secid = _classify(text, toc_idx, chapter_re, num_re, num_only_re, compiled_anch)
            if b.get("_cell_idx") is not None:
                # Physical table structure is more reliable than a textual
                # heading/list heuristic inside a cell.
                stype, secid = "cell", None
            if stype == "heading":
                if secid:
                    current_section = secid
                section_id = secid or current_section
            else:
                section_id = current_section

            cell_table_idx = b.get("_table_idx")
            cell_idx = b.get("_cell_idx")
            cell_bbox = b.get("_cell_bbox")
            # Exact cell geometry is preferred. Keep compatibility with old
            # parse artifacts that only stored an outer table bbox.
            if (cell_table_idx is not None and cell_idx is None
                    and stype == "paragraph"):
                stype = "cell"
            layout_bbox = _inner_cell_bbox(cell_bbox) if cell_bbox else None

            anchors = find_anchors_in_text(text, compiled_anch)
            sid += 1
            segs.append({
                "id": sid, "type": stype, "page": pno, "bbox": b["bbox"],
                "section_id": section_id, "anchors": anchors,
                "font": font, "size": size, "color": color, "flags": flags,
                "table_idx": cell_table_idx,
                "cell_idx": cell_idx,
                "row": b.get("_row"), "col": b.get("_col"),
                "layout_bbox": layout_bbox,
                "text": raw,  # неизменённое содержимое блока с \n и отступами
            })

    logger.info("Сегментов: %d", len(segs))
    by_type: dict[str, int] = {}
    for s in segs:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    logger.info("По типам: %s", by_type)
    return segs


def main() -> None:
    ap = argparse.ArgumentParser(description="Сегментация parse.json -> segments.json")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    from pipeline.config.loader import ensure_dirs
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    data = load_json(args.inp or cfg["parse_path"])
    segs = segment(data, cfg, logger)
    save_json(segs, args.out or cfg["segments_path"])
    logger.info("Готово: %s", resolve_path(args.out or cfg["segments_path"]))


if __name__ == "__main__":
    main()
