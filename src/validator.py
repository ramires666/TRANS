"""Этап 5 — Валидация результата.

Сравнивает исходный PDF и _RU.pdf:
- число страниц
- число изображений
- длина TOC, deepest уровень
- наличие всех якорей 图N-M / 表N-N из оригинала в результате (по числам)
- отсутствие «□□□» (tofu) и ошибок шрифта
- метаданные заполнены
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import ROOT, load_config, setup_logger, ensure_dirs, resolve_path

RE_FIG_ZH = re.compile(r"图\s*(\d+-\d+)")
RE_TAB_ZH = re.compile(r"表\s*(\d+-\d+)")
RE_FIG_RU = re.compile(r"Рис\.?\s*(\d+-\d+)", re.IGNORECASE)
RE_TAB_RU = re.compile(r"Табл\.?\s*(\d+-\d+)", re.IGNORECASE)


def _stats(pdf_path: str) -> dict:
    d = fitz.open(str(pdf_path))
    toc = d.get_toc()
    levels = [t[0] for t in toc] if toc else []
    n_imgs = sum(len(d.load_page(i).get_images(full=True)) for i in range(d.page_count))
    md = dict(d.metadata or {})
    full_text = "".join(d.load_page(i).get_text("text") for i in range(d.page_count))
    d.close()
    return {
        "pages": len(d) if False else _pages(pdf_path),
        "images": n_imgs,
        "toc_len": len(toc),
        "max_level": max(levels) if levels else 0,
        "min_level": min(levels) if levels else 0,
        "metadata": md,
        "text": full_text,
    }


def _pages(pdf_path: str) -> int:
    d = fitz.open(str(pdf_path))
    n = d.page_count
    d.close()
    return n


def validate(src: str, out: str, logger) -> int:
    src = resolve_path(src)
    out = resolve_path(out)
    if not out.exists():
        logger.error("Не найден результат: %s", out)
        return 2

    s = _stats(str(src))
    o = _stats(str(out))
    problems: list[str] = []

    if s["pages"] != o["pages"]:
        problems.append(f"Страниц: src={s['pages']} out={o['pages']}")
    if s["images"] != o["images"]:
        problems.append(f"Изображений: src={s['images']} out={o['images']}")
    if s["toc_len"] != o["toc_len"]:
        problems.append(f"TOC длина: src={s['toc_len']} out={o['toc_len']}")
    if s["max_level"] != o["max_level"]:
        problems.append(f"TOC max уровень: src={s['max_level']} out={o['max_level']}")

    # Якоря: все N-M из оригинала должны встречаться в переводе (как Рис./Табл. или просто число)
    src_figs = {m.group(1) for m in RE_FIG_ZH.finditer(s["text"])}
    src_tabs = {m.group(1) for m in RE_TAB_ZH.finditer(s["text"])}
    ru_figs = {m.group(1) for m in RE_FIG_RU.finditer(o["text"])}
    ru_tabs = {m.group(1) for m in RE_TAB_RU.finditer(o["text"])}
    # допустимо также просто число в тексте
    missing_fig = [n for n in src_figs if n not in ru_figs and n not in o["text"]]
    missing_tab = [n for n in src_tabs if n not in ru_tabs and n not in o["text"]]
    if missing_fig:
        problems.append(f"Потеряны якоря рисунков: {missing_fig[:10]} (всего {len(missing_fig)})")
    if missing_tab:
        problems.append(f"Потеряны якоря таблиц: {missing_tab[:10]} (всего {len(missing_tab)})")

    # Tofu / ошибки шрифта
    if "□" in o["text"] or "\ufffd" in o["text"]:
        problems.append("Обнаружены символы-заглушки (□/) — проблема со шрифтом")

    # Метаданные
    md = o["metadata"]
    for k in ("title", "author", "subject"):
        if not md.get(k):
            problems.append(f"Метаданные пусты: {k}")

    logger.info("--- Сравнение ---")
    logger.info("  страниц:  src=%d out=%d", s["pages"], o["pages"])
    logger.info("  изображ.: src=%d out=%d", s["images"], o["images"])
    logger.info("  TOC:      src=%d (max %d)  out=%d (max %d)",
                s["toc_len"], s["max_level"], o["toc_len"], o["max_level"])
    logger.info("  якоря:    fig src=%d ru=%d  tab src=%d ru=%d",
                len(src_figs), len(ru_figs), len(src_tabs), len(ru_tabs))
    logger.info("  метаданные: title=%r author=%r subject=%r",
                md.get("title", "")[:40], md.get("author", ""), md.get("subject", "")[:40])

    if problems:
        logger.error("НАЙДЕНЫ ПРОБЛЕМЫ:")
        for p in problems:
            logger.error("  - %s", p)
        return 1

    logger.info("ВАЛИДАЦИЯ ПРОЙДЕНА")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Валидация _RU.pdf")
    ap.add_argument("--src", help="исходный PDF (по умолчанию из config)")
    ap.add_argument("--out", help="_RU.pdf (по умолчанию из config)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--validate", dest="val_out", help="путь к PDF для валидации (alias --out)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    src = args.src or cfg["pdf_path"]
    out = args.out or args.val_out or cfg["out_path"]
    sys.exit(validate(src, out, logger))


if __name__ == "__main__":
    main()
