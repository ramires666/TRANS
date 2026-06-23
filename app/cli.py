"""Оркестратор конвейера перевода PDF.

Этапы: parse -> segment -> translate -> build -> validate.
RESUME привязан к sha256 исходного PDF: артефакты лежат в
`intermediate/<source_hash>/`, смена PDF автоматически инвалидирует кэш.

Usage:
    python -m app.cli --in file.pdf --out _RU.pdf --resume
    python -m app.cli --from-stage translate --resume
    python -m app.cli --validate _RU.pdf
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from pipeline.config.loader import (ROOT, ensure_dirs, load_config, resolve_path,
                                    setup_logger)
from pipeline.io.artifacts import artifact_paths, source_hash, stage_done

STAGES = ["parse", "segment", "translate", "build", "validate"]


def _mod(stage: str):
    table = {
        "parse": "pipeline.pdf.parser",
        "segment": "pipeline.text.segmenter",
        "translate": "pipeline.translate.translator",
        "build": "pipeline.pdf.builder",
        "validate": "pipeline.pdf.validator",
    }
    return importlib.import_module(table[stage])


def run_stage(stage: str, cfg: dict, logger, args, ap: dict, src_hash: str) -> bool:
    logger.info("=== ЭТАП: %s ===", stage.upper())
    try:
        if stage == "parse":
            mod = _mod(stage)
            from pipeline.io.artifacts import save_json
            data = mod.parse_pdf(args.inp or cfg["pdf_path"], cfg, logger)
            save_json(data, str(ap["parse"]))
        elif stage == "segment":
            mod = _mod(stage)
            from pipeline.io.artifacts import load_json, save_json
            data = load_json(str(ap["parse"]))
            segs = mod.segment(data, cfg, logger)
            save_json(segs, str(ap["segments"]))
        elif stage == "translate":
            mod = _mod(stage)
            from pipeline.io.artifacts import load_json, save_json
            segs = load_json(str(ap["segments"]))
            if args.limit:
                segs = segs[:args.limit]
            tr = mod.Translator(cfg, logger, str(ap["cache_db"]), str(ap["errors"]))
            try:
                translations = tr.translate_all(segs)
            finally:
                tr.close()
            for s in segs:
                s["ru"] = translations.get(s["id"], s["text"])
            save_json(segs, str(ap["segments_ru"]))
        elif stage == "build":
            mod = _mod(stage)
            cfg_local = dict(cfg)
            cfg_local["pdf_path"] = args.inp or cfg["pdf_path"]
            mod.build(cfg_local, logger,
                      segments_ru_path=str(ap["segments_ru"]),
                      out_path=args.out or cfg["out_path"])
        elif stage == "validate":
            mod = _mod(stage)
            src = args.inp or cfg["pdf_path"]
            out = args.out or cfg["out_path"]
            rc = mod.validate(src, out, logger, cfg)
            return rc == 0
        return True
    except Exception as e:
        logger.exception("Этап %s завершился ошибкой: %s", stage, e)
        return False


def main() -> None:
    ap_cli = argparse.ArgumentParser(description="Конвейер перевода PDF")
    ap_cli.add_argument("--in", dest="inp")
    ap_cli.add_argument("--out", dest="out")
    ap_cli.add_argument("--config")
    ap_cli.add_argument("--from-stage", dest="from_stage", default="parse",
                        choices=STAGES)
    ap_cli.add_argument("--stop-stage", dest="stop_stage", default="validate",
                        choices=STAGES)
    ap_cli.add_argument("--resume", action="store_true",
                        help="пропустить стадии, чьи артефакты готовы (по sha256 исходника)")
    ap_cli.add_argument("--limit", type=int, default=0)
    ap_cli.add_argument("--validate", dest="validate_only")
    ap_cli.add_argument("--inspect", dest="inspect_pdf")
    args = ap_cli.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    if args.inspect_pdf:
        _mod("parse")  # noop check
        from pipeline.pdf import inspect as insp
        insp.inspect(args.inspect_pdf)
        return

    if args.validate_only:
        from pipeline.pdf.validator import validate
        src = args.inp or cfg["pdf_path"]
        rc = validate(src, args.validate_only, logger, cfg)
        sys.exit(0 if rc == 0 else 1)

    src_pdf = args.inp or cfg["pdf_path"]
    sh = source_hash(src_pdf)
    ap = artifact_paths(cfg, sh)
    logger.info("Исходник: %s  hash=%s", resolve_path(src_pdf), sh)

    start_idx = STAGES.index(args.from_stage)
    stop_idx = STAGES.index(args.stop_stage)

    artifact_stages = {"parse", "segment", "translate"}

    for i in range(start_idx):
        prev = STAGES[i]
        if prev in artifact_stages and not stage_done(cfg, sh, prev):
            start_idx = i
            logger.warning("Артефакт стадии '%s' отсутствует — откатываюсь к ней",
                           prev)
            break

    if args.resume:
        for i in range(start_idx, stop_idx + 1):
            stage = STAGES[i]
            if stage == "validate":
                break
            if stage_done(cfg, sh, stage):
                logger.info("RESUME: стадия %s готова (%s) — пропускаю",
                            stage, ap[{"parse": "parse",
                                      "segment": "segments",
                                      "translate": "segments_ru"}[stage]])
                start_idx = i + 1
            else:
                break

    for i in range(start_idx, stop_idx + 1):
        stage = STAGES[i]
        ok = run_stage(stage, cfg, logger, args, ap, sh)
        if not ok:
            logger.error("Конвейер остановлен на стадии %s", stage)
            sys.exit(2)
    logger.info("Конвейер завершён успешно.")


if __name__ == "__main__":
    main()