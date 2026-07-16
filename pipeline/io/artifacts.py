"""I/O артефактов и RESUME-кэш, привязанный к sha256 исходного PDF.

Артефакты каждого исходника лежат в `intermediate/<hash>/`,
поэтому смена PDF не подхватит чужой parse.json / segments_ru.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    # Атомарная замена не оставляет усечённый JSON при остановке процесса.
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=p.parent,
                prefix=p.name + ".", suffix=".tmp", delete=False) as fh:
            tmp_name = fh.name
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    finally:
        # При ошибке сериализации/fsync/replace исходный файл остаётся целым, а
        # недописанный временный файл не накапливается рядом с артефактами.
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


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
        "manifest": wd / "manifest.json",
    }


_STAGE_FILES = {
    "parse": ("pipeline/pdf/parser.py",),
    "segment": (
        "pipeline/text/segmenter.py",
        "pipeline/anchors.py",
        # segmenter импортирует отсюда _normalize_markers.
        "pipeline/pdf/builder.py",
    ),
    "translate": (
        "pipeline/translate/translator.py",
        "pipeline/glossary/glossary.py",
    ),
    "markdown": ("pipeline/markdown/translator.py",),
}

_STAGE_CFG = {
    "parse": ("source_lang",),
    "segment": (
        "source_lang", "target_lang", "anchors", "bullet_chars",
        "bullet_replace", "remove_pua", "section_patterns",
    ),
    "translate": (
        "source_lang", "target_lang", "llm_base_url", "llm_model",
        "temperature", "top_p", "max_tokens", "enable_thinking",
        "llm_batch_max_items", "llm_batch_max_chars",
        "translation_max_attempts", "llm_max_attempts", "glossary_path",
        "translation_layout_budget", "translation_layout_fill",
        "translation_avg_char_width", "builder_min_fontsize",
        "builder_table_min_fontsize", "builder_caption_min_fontsize",
        "builder_heading_min_fontsize",
    ),
    "markdown": (
        "source_lang", "target_lang", "llm_base_url", "llm_model",
        "enable_thinking", "max_tokens", "temperature", "top_p",
        "markdown_max_tokens", "markdown_temperature", "markdown_top_p",
        "markdown_system_prompt",
    ),
}

_STAGE_ARTIFACTS = {
    "parse": "parse",
    "segment": "segments",
    "translate": "segments_ru",
    "markdown": "pages_md",
}


def _digest_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_signature(cfg: dict, src_hash: str, stage: str) -> str:
    """Сигнатура артефакта с учётом кода и влияющей конфигурации."""
    if stage not in _STAGE_FILES:
        raise ValueError(f"Неизвестная стадия: {stage}")
    payload: dict[str, Any] = {
        "source": src_hash,
        "stage": stage,
        "code": {
            rel: _digest_file(ROOT / rel) for rel in _STAGE_FILES[stage]
        },
        "config": {key: cfg.get(key) for key in _STAGE_CFG[stage]},
    }
    if stage == "segment":
        payload["dependency"] = stage_signature(cfg, src_hash, "parse")
    elif stage == "translate":
        payload["dependency"] = stage_signature(cfg, src_hash, "segment")
        glossary = resolve_path(cfg.get("glossary_path", "config/glossary.csv"))
        payload["glossary_digest"] = _digest_file(glossary)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_manifest_for_update(path: Path, src_hash: str) -> dict[str, Any]:
    manifest: Any = {}
    if path.exists():
        try:
            manifest = load_json(path)
        except Exception:
            manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    manifest["source_hash"] = src_hash
    manifest["stages"] = stages
    return manifest


def mark_stage_done(cfg: dict, src_hash: str, stage: str) -> None:
    ap = artifact_paths(cfg, src_hash)
    if stage not in _STAGE_ARTIFACTS:
        raise ValueError(f"Неизвестная стадия: {stage}")
    artifact = ap[_STAGE_ARTIFACTS[stage]]
    if not artifact.exists():
        raise FileNotFoundError(f"Артефакт стадии {stage} отсутствует: {artifact}")
    manifest = _load_manifest_for_update(ap["manifest"], src_hash)
    manifest["stages"][stage] = {
        "signature": stage_signature(cfg, src_hash, stage),
        "complete": True,
        "artifact_digest": _digest_file(artifact),
    }
    save_json(manifest, ap["manifest"])


def mark_stage_incomplete(cfg: dict, src_hash: str, stage: str,
                          **details: Any) -> None:
    """Явно запрещает RESUME-пропуск частично сформированного артефакта."""
    ap = artifact_paths(cfg, src_hash)
    if stage not in _STAGE_ARTIFACTS:
        raise ValueError(f"Неизвестная стадия: {stage}")
    manifest = _load_manifest_for_update(ap["manifest"], src_hash)
    record: dict[str, Any] = {
        "signature": stage_signature(cfg, src_hash, stage),
        "complete": False,
    }
    if details:
        record["details"] = details
    manifest["stages"][stage] = record
    save_json(manifest, ap["manifest"])


def stage_done(cfg: dict, src_hash: str, stage: str) -> bool:
    ap = artifact_paths(cfg, src_hash)
    artifact_key = _STAGE_ARTIFACTS.get(stage)
    if not artifact_key or not ap[artifact_key].exists():
        return False
    if not ap["manifest"].exists():
        return False
    try:
        manifest = load_json(ap["manifest"])
        if not isinstance(manifest, dict) or manifest.get("source_hash") != src_hash:
            return False
        entry = manifest.get("stages", {}).get(stage, {})
        if not isinstance(entry, dict) or entry.get("complete", True) is not True:
            return False
        if entry.get("signature") != stage_signature(cfg, src_hash, stage):
            return False
        recorded_digest = entry.get("artifact_digest")
        if recorded_digest and recorded_digest != _digest_file(ap[artifact_key]):
            return False
        return True
    except Exception:
        return False
