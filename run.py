"""Оркестратор конвейера перевода PDF (ZH -> RU).

Этапы: parse -> segment -> translate -> build -> validate
Поддерживает resume (--resume) и запуск с определённой стадии (--from-stage).

Usage:
    python run.py --in "file.pdf" --out "_RU.pdf" --resume
    python run.py --from-stage translate --resume
    python run.py --validate "_RU.pdf"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from utils import ROOT, load_config, setup_logger, ensure_dirs, resolve_path

import importlib

STAGES = ["parse", "segment", "translate", "build", "validate"]


def _import(mod: str):
    return importlib.import_module(mod)


def run_stage(stage: str, cfg: dict, logger, args) -> bool:
    logger.info("=== ЭТАП: %s ===", stage.upper())
    try:
        if stage == "parse":
            mod = _import("parser")
            data = mod.parse_pdf(args.inp or cfg["pdf_path"], cfg, logger)
            from utils import save_json
            save_json(data, cfg["parse_path"])
        elif stage == "segment":
            mod = _import("segmenter")
            from utils import load_json, save_json
            data = load_json(cfg["parse_path"])
            segs = mod.segment(data, cfg, logger)
            save_json(segs, cfg["segments_path"])
        elif stage == "translate":
            mod = _import("translator")
            from utils import load_json, save_json
            segs = load_json(cfg["segments_path"])
            if args.limit:
                segs = segs[:args.limit]
            tr = mod.Translator(cfg, logger)
            try:
                translations = tr.translate_all(segs)
            finally:
                tr.close()
            for s in segs:
                s["ru"] = translations.get(s["id"], s["text"])
            save_json(segs, "intermediate/segments_ru.json")
        elif stage == "build":
            mod = _import("builder")
            mod.build(cfg, logger,
                      segments_ru_path="intermediate/segments_ru.json",
                      out_path=args.out or cfg["out_path"])
        elif stage == "validate":
            mod = _import("validator")
            src = args.inp or cfg["pdf_path"]
            out = args.out or cfg["out_path"]
            rc = mod.validate(src, out, logger)
            return rc == 0
        return True
    except Exception as e:
        logger.exception("Этап %s завершился ошибкой: %s", stage, e)
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Конвейер перевода PDF ZH->RU")
    ap.add_argument("--in", dest="inp", help="исходный PDF")
    ap.add_argument("--out", dest="out", help="выходной PDF")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--from-stage", dest="from_stage", default="parse",
                    choices=STAGES, help="начать со стадии")
    ap.add_argument("--stop-stage", dest="stop_stage", default="validate",
                    choices=STAGES, help="закончить стадией")
    ap.add_argument("--resume", action="store_true", help="пропустить стадии, чьи артефакты готовы")
    ap.add_argument("--limit", type=int, default=0, help="ограничить сегментов (перевод)")
    ap.add_argument("--validate", dest="validate_only", help="только валидация указанного PDF")
    ap.add_argument("--inspect", dest="inspect_pdf", help="только сводка по PDF")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    if args.inspect_pdf:
        mod = _import("inspect_pdf")
        mod.inspect(args.inspect_pdf)
        return

    if args.validate_only:
        mod = _import("validator")
        src = args.inp or cfg["pdf_path"]
        rc = mod.validate(src, args.validate_only, logger)
        sys.exit(0 if rc == 0 else 1)

    # Определяем стартовую стадию с учётом resume
    start_idx = STAGES.index(args.from_stage)
    stop_idx = STAGES.index(args.stop_stage)

    if args.resume:
        artifacts = {
            "parse": cfg["parse_path"],
            "segment": cfg["segments_path"],
            "translate": "intermediate/segments_ru.json",
            "build": args.out or cfg["out_path"],
        }
        for i in range(start_idx, stop_idx + 1):
            stage = STAGES[i]
            if stage == "validate":
                break
            art = artifacts.get(stage)
            if art and resolve_path(art).exists():
                logger.info("RESUME: стадия %s уже имеет артефакт %s — пропускаю", stage, art)
                start_idx = i + 1
            else:
                break
        start_idx = max(start_idx, STAGES.index(args.from_stage))

    ok = True
    for i in range(start_idx, stop_idx + 1):
        stage = STAGES[i]
        ok = run_stage(stage, cfg, logger, args)
        if not ok:
            logger.error("Конвейер остановлен на стадии %s", stage)
            sys.exit(2)

    logger.info("Конвейер завершён успешно.")


if __name__ == "__main__":
    main()
