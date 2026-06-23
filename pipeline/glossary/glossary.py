"""Загрузка глоссария из CSV. Поддерживает любой формат `source,target`.

Первая строка — заголовок (опционально). Если первая строка выглядит как
header (`zh,ru`, `source,target`, `en,ru`) — пропускаем, иначе парсим сразу.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.config.loader import resolve_path


def load_glossary(path: str | Path) -> dict[str, str]:
    p = resolve_path(path)
    g: dict[str, str] = {}
    if not p.exists():
        return g
    with open(p, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    if not lines:
        return g

    first = lines[0].strip()
    if "," in first:
        head_a, head_b = (s.strip().lower() for s in first.split(",", 1))
        if head_a in ("zh", "source", "src", "en", "ja", "de", "fr") and head_b in (
            "ru", "target", "dst", "en", "de", "fr"
        ):
            lines = lines[1:]

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        if len(parts) == 2:
            k, v = parts[0].strip(), parts[1].strip()
            if k and v:
                g[k] = v
    return g