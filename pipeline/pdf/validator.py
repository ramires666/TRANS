"""Этап 5 — Валидация результата."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz

from pipeline.anchors import compiled_anchors
from pipeline.config.loader import (ensure_dirs, load_config, resolve_path,
                                    setup_logger)


def _stats(pdf_path: str, cfg: dict) -> dict:
    d = fitz.open(str(pdf_path))
    n = d.page_count
    toc = d.get_toc()
    levels = [t[0] for t in toc] if toc else []
    # Уникальные image-xref'ы по всему документу — apply_redactions может
    # дублировать ссылки на одной странице, но xref-объект тот же.
    xrefs: set[int] = set()
    for i in range(n):
        for img in d.load_page(i).get_images(full=True):
            xrefs.add(img[0])
    n_imgs = len(xrefs)
    md = dict(d.metadata or {})
    full_text = "".join(d.load_page(i).get_text("text") for i in range(n))
    d.close()
    return {
        "pages": n, "images": n_imgs,
        "toc_len": len(toc), "max_level": max(levels) if levels else 0,
        "min_level": min(levels) if levels else 0,
        "metadata": md, "text": full_text,
    }


def validate(src: str, out: str, logger, cfg: dict | None = None) -> int:
    cfg = cfg or {}
    src = resolve_path(src)
    out = resolve_path(out)
    if not out.exists():
        logger.error("Не найден результат: %s", out)
        return 2

    s = _stats(str(src), cfg)
    o = _stats(str(out), cfg)
    problems: list[str] = []

    if s["pages"] != o["pages"]:
        problems.append(f"Страниц: src={s['pages']} out={o['pages']}")
    # Толеранс по изображениям: apply_redactions консолидирует image+smask
    # в один xref, поэтому допускаем до 30% расхождения.
    img_diff = abs(s["images"] - o["images"])
    img_tol = max(10, int(s["images"] * 0.30))
    if img_diff > img_tol:
        problems.append(f"Изображений: src={s['images']} out={o['images']} (допустимо ±{img_tol})")
    if s["toc_len"] != o["toc_len"]:
        problems.append(f"TOC длина: src={s['toc_len']} out={o['toc_len']}")
    if s["max_level"] != o["max_level"]:
        problems.append(f"TOC max уровень: src={s['max_level']} out={o['max_level']}")

    anch = compiled_anchors(cfg) if cfg else {}
    src_figs: set[str] = set()
    src_tabs: set[str] = set()
    ru_figs: set[str] = set()
    ru_tabs: set[str] = set()
    if anch.get("fig", {}).get("src"):
        src_figs = {m.group(1) for m in anch["fig"]["src"].finditer(s["text"])}
    if anch.get("tab", {}).get("src"):
        src_tabs = {m.group(1) for m in anch["tab"]["src"].finditer(s["text"])}
    if anch.get("fig", {}).get("dst"):
        ru_figs = {m.group(1) for m in anch["fig"]["dst"].finditer(o["text"])}
    if anch.get("tab", {}).get("dst"):
        ru_tabs = {m.group(1) for m in anch["tab"]["dst"].finditer(o["text"])}

    missing_fig = [n for n in src_figs if n not in ru_figs and n not in o["text"]]
    missing_tab = [n for n in src_tabs if n not in ru_tabs and n not in o["text"]]
    if missing_fig:
        problems.append(f"Потеряны якоря рисунков: {missing_fig[:10]} (всего {len(missing_fig)})")
    if missing_tab:
        problems.append(f"Потеряны якоря таблиц: {missing_tab[:10]} (всего {len(missing_tab)})")

    if "\ufffd" in o["text"]:
        problems.append("Обнаружены символы-заглушки (U+FFFD) — проблема со шрифтом")

    md = o["metadata"]
    cfg_md = (cfg or {}).get("metadata") or {}
    for k in ("title", "author", "subject"):
        expected = (cfg_md.get(k) or "").strip()
        if expected:
            continue
        if s["metadata"].get(k) and not md.get(k):
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
    ap.add_argument("--src")
    ap.add_argument("--out")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    src = args.src or cfg["pdf_path"]
    out = args.out or cfg["out_path"]
    sys.exit(validate(src, out, logger, cfg))


if __name__ == "__main__":
    main()