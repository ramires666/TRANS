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

# Исходные шаблоны зависят от source_lang, а оформление результата —
# от target_lang. Это не позволяет английскому запуску наследовать русские
# подписи «Рис.»/«Табл.».
_SOURCE_PRESETS: dict[str, dict[str, str]] = {
    "zh": {
        "fig": r"图\s*(\d+-\d+)",
        "tab": r"表\s*(\d+-\d+)",
    },
    "en": {
        "fig": r"Fig\.?\s*(\d+-\d+)",
        "tab": r"Table\s*(\d+-\d+)",
    },
}
_TARGET_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "ru": {
        "fig": {"label": "Рис.", "regex": r"Рис\.?\s*(\d+-\d+)"},
        "tab": {"label": "Табл.", "regex": r"Табл\.?\s*(\d+-\d+)"},
    },
    "en": {
        "fig": {"label": "Fig.", "regex": r"Fig\.?\s*(\d+-\d+)"},
        "tab": {"label": "Table", "regex": r"Table\s*(\d+-\d+)"},
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

# В китайских PDF типографские маркеры часто идут вплотную к тексту
# (``•项目``). Для дефиса пробел всё ещё обязателен, иначе ``-option`` будет
# ошибочно распознан как элемент списка.
LIST_BULLET = r"^\s*(?:[•●○▪◆□]\s*|[-–]\s+)(.*)$"
LIST_NUM = r"^\s*(\d+)\.\s+(.*)$"


def _anchor(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    source_lang = cfg.get("source_lang", "zh")
    target_lang = cfg.get("target_lang", "ru")
    source_presets = _SOURCE_PRESETS.get(source_lang, {})
    target_presets = _TARGET_PRESETS.get(
        target_lang, _TARGET_PRESETS["en"]
    )
    custom = cfg.get("anchors") or {}
    out: dict[str, dict[str, str]] = {}
    for kind in ("fig", "tab"):
        c = custom.get(kind, {})
        target = target_presets.get(kind, {})
        out[kind] = {
            "src_regex": c.get("src_regex") or source_presets.get(kind, ""),
            "dst_label": c.get("dst_label") or target.get("label", ""),
            "dst_regex": c.get("dst_regex") or target.get("regex", ""),
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
