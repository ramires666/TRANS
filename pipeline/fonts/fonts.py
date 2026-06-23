"""Поиск TTF-шрифта с поддержкой целевого языка."""
from __future__ import annotations

import os
from pathlib import Path

from pipeline.config.loader import ROOT


def find_target_font(configured: str | None = None) -> str:
    """Возвращает путь к TTF-шрифту с поддержкой глифов целевого языка."""
    if configured and Path(configured).exists():
        return str(Path(configured).resolve())

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
        if c.exists():
            return str(c.resolve())
    raise FileNotFoundError(
        "TTF-шрифт с поддержкой целевого языка не найден. "
        "Укажите target_font в config/config.yaml или положите assets/DejaVuSans.ttf"
    )