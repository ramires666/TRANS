"""I/O артефактов и RESUME-кэш, привязанный к sha256 исходного PDF.

Артефакты каждого исходника лежат в `intermediate/<hash>/`,
поэтому смена PDF не подхватит чужой parse.json / segments_ru.json.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pipeline.config.loader import ROOT, resolve_path


def source_hash(pdf_path: str | os.PathLike) -> str:
    p = resolve_path(pdf_path)
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def workdir(cfg: dict, src_hash: str) -> Path:
    d = ROOT / cfg.get("tmp_dir", "intermediate") / src_hash
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(obj: Any, path: str | os.PathLike) -> None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def load_json(path: str | os.PathLike) -> Any:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def artifact_paths(cfg: dict, src_hash: str) -> dict[str, Path]:
    """Пути артефактов конвейера для конкретного исходника."""
    wd = workdir(cfg, src_hash)
    return {
        "parse": wd / "parse.json",
        "segments": wd / "segments.json",
        "segments_ru": wd / "segments_ru.json",
        "pages_md": wd / "pages_md.json",
        "cache_db": wd / "translations.db",
        "errors": ROOT / cfg.get("log_dir", "log") / "errors.jsonl",
    }


def stage_done(cfg: dict, src_hash: str, stage: str) -> bool:
    ap = artifact_paths(cfg, src_hash)
    key_map = {
        "parse": "parse",
        "segment": "segments",
        "translate": "segments_ru",
    }
    if stage not in key_map:
        return False
    return ap[key_map[stage]].exists()