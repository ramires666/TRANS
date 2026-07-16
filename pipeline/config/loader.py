"""Конфигурация и логирование."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent


def configure_target_language(
        cfg: dict[str, Any], target_lang: str | None = None) -> dict[str, Any]:
    """Apply target-specific resources without mutating the YAML on disk."""
    target = str(target_lang or cfg.get("target_lang") or "ru").lower()
    cfg["target_lang"] = target
    glossary_paths = cfg.get("glossary_paths")
    if isinstance(glossary_paths, dict):
        selected = glossary_paths.get(target)
        if selected:
            cfg["glossary_path"] = selected
    return cfg


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    if path:
        cfg_path = Path(path)
        if not cfg_path.is_absolute():
            cfg_path = ROOT / cfg_path
    else:
        cfg_path = ROOT / "config" / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("root", str(ROOT))
    cfg.setdefault("source_lang", "zh")
    cfg.setdefault("target_lang", "ru")
    return configure_target_language(cfg)


def ensure_dirs(cfg: dict[str, Any]) -> None:
    tmp = ROOT / cfg.get("tmp_dir", "intermediate")
    tmp.mkdir(parents=True, exist_ok=True)
    log = ROOT / cfg.get("log_dir", "log")
    log.mkdir(parents=True, exist_ok=True)


def setup_logger(cfg: dict[str, Any], name: str = "trans") -> logging.Logger:
    log_path = ROOT / cfg.get("log_path", "log/translate.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # На Windows консоль часто в cp1251; принудительно используем UTF-8 writer
    # с errors='replace', чтобы не падать на юникодных путях и иероглифах.
    if sys.platform == "win32":
        import io
        stdout_writer = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace",
            line_buffering=sys.stdout.line_buffering)
        ch = logging.StreamHandler(stdout_writer)
    else:
        ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def resolve_path(rel: str | os.PathLike) -> Path:
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    return p
