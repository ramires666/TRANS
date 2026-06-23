"""Якоря перекрёстных ссылок (рисунки, таблицы, главы).

Паттерны зависят от языковой пары (source_lang, target_lang). Из конфига
можно переопределить конкретный якорь через раздел `anchors`.

Пример конфига::

    anchors:
      fig:
        src_regex: '图\\s*(\\d+-\\d+)'
        dst_label: 'Рис.'
        dst_regex: 'Рис\\.?\\s*(\\d+-\\d+)'
"""
from __future__ import annotations

import re
from typing import Any

# Пресеты для типичных языковых пар. Ключ — source_lang (без target_lang):
# перевод идёт в условный «Рис. N-M»/«Табл. N-N» для русского, либо
# «Fig. N-M»/«Table N-N» для английского и т.д.
_PRESETS: dict[str, dict[str, dict]] = {
    "zh": {
        "fig": {"src": r"图\s*(\d+-\d+)", "dst_label": "Рис.", "dst": r"Рис\.?\s*(\d+-\d+)"},
        "tab": {"src": r"表\s*(\d+-\d+)", "dst_label": "Табл.", "dst": r"Табл\.?\s*(\d+-\d+)"},
    },
    "en": {
        "fig": {"src": r"Fig\.?\s*(\d+-\d+)", "dst_label": "Рис.", "dst": r"Рис\.?\s*(\d+-\d+)"},
        "tab": {"src": r"Table\s*(\d+-\d+)", "dst_label": "Табл.", "dst": r"Табл\.?\s*(\d+-\d+)"},
    },
}

# Паттерны заголовков глав — пока универсальны (ASCII-номера +本章/section).
SECTION_PATTERNS = {
    "zh": {
        "chapter": r"^第(\d+)章\s*(.+)$",
        "num": r"^(\d+(?:\.\d+){0,2})\s+(.+)$",
        "num_only": r"^(\d+(?:\.\d+){0,2})\s*$",
    },
    "default": {
        "chapter": r"^Chapter\s+(\d+)\.?\s*(.*)$",
        "num": r"^(\d+(?:\.\d+){0,2})\s+(.+)$",
        "num_only": r"^(\d+(?:\.\d+){0,2})\s*$",
    },
}

LIST_BULLET = r"^\s*[•●○\-–▪◆]\s+(.*)$"
LIST_NUM = r"^\s*(\d+)\.\s+(.*)$"


def _anchor(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    source_lang = cfg.get("source_lang", "zh")
    presets = _PRESETS.get(source_lang, {})
    custom = cfg.get("anchors") or {}
    out: dict[str, dict[str, str]] = {}
    for kind in ("fig", "tab"):
        c = custom.get(kind, {})
        p = presets.get(kind, {})
        out[kind] = {
            "src_regex": c.get("src_regex") or p.get("src", ""),
            "dst_label": c.get("dst_label") or p.get("dst_label", ""),
            "dst_regex": c.get("dst_regex") or p.get("dst", ""),
        }
    return out


def compiled_anchors(cfg: dict[str, Any]) -> dict[str, dict[str, re.Pattern | str]]:
    a = _anchor(cfg)
    out: dict[str, dict[str, re.Pattern | str]] = {}
    for kind, spec in a.items():
        out[kind] = {
            "src": re.compile(spec["src_regex"]) if spec["src_regex"] else None,
            "dst": re.compile(spec["dst_regex"], re.IGNORECASE) if spec["dst_regex"] else None,
            "dst_label": spec["dst_label"],
        }
    return out


def section_patterns(cfg: dict[str, Any]) -> dict[str, str]:
    src_lang = cfg.get("source_lang", "zh")
    custom = cfg.get("section_patterns") or {}
    preset = SECTION_PATTERNS.get(src_lang, SECTION_PATTERNS["default"])
    return {k: custom.get(k) or v for k, v in preset.items()}


def find_anchors_in_text(text: str, compiled: dict[str, dict[str, re.Pattern | str]]) -> list[dict]:
    out = []
    for kind, spec in compiled.items():
        src_re = spec["src"]
        if not src_re:
            continue
        for m in src_re.finditer(text):
            out.append({"kind": kind, "raw": m.group(0), "num": m.group(1)})
    return out