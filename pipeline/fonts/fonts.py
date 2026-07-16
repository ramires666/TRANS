"""Поиск TTF-шрифта с поддержкой целевого языка."""
from __future__ import annotations

import os
from pathlib import Path

import fitz

from pipeline.config.loader import ROOT, resolve_path


_LANG_GLYPHS = {
    "ru": "АяЁё",
    "en": "Az",
    "de": "ÄäÖöÜüß",
    "fr": "ÀàÇçÉéŒœ",
    "es": "ÁáÉéÍíÑñÓóÚúÜü",
}


def _supports_target(path: Path, target_lang: str) -> bool:
    sample = _LANG_GLYPHS.get(target_lang, "Az")
    try:
        font = fitz.Font(fontfile=str(path))
        return all(font.has_glyph(ord(ch)) for ch in sample)
    except Exception:
        return False


def find_target_font(configured: str | None = None,
                     target_lang: str = "ru") -> str:
    """Возвращает путь к TTF-шрифту с поддержкой глифов целевого языка."""
    if configured:
        configured_path = resolve_path(configured)
        if not configured_path.exists():
            raise FileNotFoundError(f"Настроенный target_font не найден: {configured_path}")
        if not _supports_target(configured_path, target_lang):
            raise ValueError(
                f"Шрифт {configured_path} не покрывает язык '{target_lang}'")
        return str(configured_path.resolve())

    candidates: list[Path] = []

    try:
        import matplotlib
        mpl_dir = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
        candidates.append(mpl_dir / "DejaVuSans.ttf")
    except Exception:
        pass

    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates.extend([
        win_fonts / "arial.ttf",
        win_fonts / "DejaVuSans.ttf",
        win_fonts / "NotoSans-Regular.ttf",
    ])

    candidates.append(ROOT / "assets" / "DejaVuSans.ttf")

    for c in candidates:
        if c.exists() and _supports_target(c, target_lang):
            return str(c.resolve())
    raise FileNotFoundError(
        f"TTF-шрифт с поддержкой языка '{target_lang}' не найден. "
        "Укажите target_font в config/config.yaml или положите assets/DejaVuSans.ttf"
    )
