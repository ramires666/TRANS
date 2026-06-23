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
import re
import sys
from pathlib import Path

from pipeline.anchors import (LIST_BULLET, LIST_NUM, compiled_anchors,
                              find_anchors_in_text, section_patterns)
from pipeline.config.loader import load_config, resolve_path, setup_logger
from pipeline.io.artifacts import save_json, load_json

PAT_LIST_NUM = re.compile(LIST_NUM)
PAT_LIST_BULLET = re.compile(LIST_BULLET)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_toc_index(toc: list) -> dict:
    idx = {}
    for lvl, title, page in toc:
        idx[_norm(title)] = {"lvl": lvl, "page": page, "title": title}
    return idx


def _block_text(block: dict) -> str:
    # Склеиваем спаны внутри строки без разделителя (они соседствуют),
    # а СТРОКИ блока — через "\n". Это сохраняет переносы строк и ведущие
    # пробелы/табуляции каждой строки ровно как в оригинале, чтобы LLM получил
    # структурированный фрагмент, а не сплошную кашу.
    lines = []
    for ln in block["lines"]:
        lines.append("".join(sp["text"] for sp in ln["spans"]))
    text = "\n".join(lines)
    # Нормализуем маркер списка □ (U+25A1, часто извлекается PyMuPDF как
    # literal) в обычный буллет • — и классификатор, и LLM, и валидатор
    # работают с • корректно. Заменяем только в начале строк.
    text = re.sub(r"(?m)(^\s*)□", r"\1•", text)
    return text


def _block_first_style(block: dict) -> tuple[str, float, int]:
    for ln in block["lines"]:
        for sp in ln["spans"]:
            return sp.get("font", ""), float(sp.get("size", 0)), int(sp.get("color", 0))
    return "", 0.0, 0


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


def _merge_close_blocks(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b["type"] != 0 or not b["lines"]:
            out.append(b)
            continue
        if out and out[-1]["type"] == 0 and out[-1]["lines"]:
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
        blocks = [b for b in pinfo["blocks"] if b["type"] == 0]
        blocks = _merge_close_blocks(blocks)
        tables = pinfo.get("tables", [])

        for b in blocks:
            raw = _block_text(b)
            text = raw.strip()
            if not text:
                continue
            font, size, color = _block_first_style(b)

            stype, secid = _classify(text, toc_idx, chapter_re, num_re, num_only_re, compiled_anch)
            if stype == "heading":
                if secid:
                    current_section = secid
                section_id = secid or current_section
            else:
                section_id = current_section

            cell_table_idx = None
            cx, cy = _block_center(b)
            for ti, t in enumerate(tables):
                if _point_in_rect(cx, cy, t["bbox"]):
                    cell_table_idx = ti
                    break
            if cell_table_idx is not None and stype == "paragraph":
                stype = "cell"

            anchors = find_anchors_in_text(text, compiled_anch)
            sid += 1
            segs.append({
                "id": sid, "type": stype, "page": pno, "bbox": b["bbox"],
                "section_id": section_id, "anchors": anchors,
                "font": font, "size": size, "color": color,
                "table_idx": cell_table_idx,
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