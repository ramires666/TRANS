"""Общие утилиты: конфиг, логирование, пути, шрифты, JSON I/O."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("root", str(ROOT))
    return cfg


def ensure_dirs(cfg: dict[str, Any]) -> None:
    for key in ("tmp_dir", "log_dir"):
        p = ROOT / cfg[key]
        p.mkdir(parents=True, exist_ok=True)
    Path(cfg["cache_db"]).parent.mkdir(parents=True, exist_ok=True)


def setup_logger(cfg: dict[str, Any], name: str = "trans") -> logging.Logger:
    log_path = ROOT / cfg["log_path"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def resolve_path(rel: str) -> Path:
    """Resolve path relative to project root."""
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    return p


def save_json(obj: Any, path: str | os.PathLike) -> None:
    p = resolve_path(path) if not Path(path).is_absolute() else Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def load_json(path: str | os.PathLike) -> Any:
    p = resolve_path(path) if not Path(path).is_absolute() else Path(path)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_cyrillic_font(configured: str | None = None) -> str:
    """Возвращает путь к TTF-шрифту с поддержкой кириллицы."""
    if configured and Path(configured).exists():
        return str(Path(configured).resolve())

    candidates = []
    # matplotlib bundled DejaVu
    try:
        import matplotlib
        mpl_dir = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
        candidates.append(mpl_dir / "DejaVuSans.ttf")
    except Exception:
        pass

    # Windows fonts
    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates.extend([
        win_fonts / "arial.ttf",
        win_fonts / "DejaVuSans.ttf",
        win_fonts / "NotoSans-Regular.ttf",
    ])

    # repo local
    candidates.append(ROOT / "assets" / "DejaVuSans.ttf")

    for c in candidates:
        if c.exists():
            return str(c.resolve())
    raise FileNotFoundError("Не найден TTF-шрифт с поддержкой кириллицы. Укажите cyrillic_font в config.yaml")


# --- Якоря и перекрёстные ссылки ---

ANCHOR_PATTERNS = {
    # тип: (regex, формат перевода)
}


def load_glossary(path: str | os.PathLike) -> dict[str, str]:
    p = resolve_path(path)
    g = {}
    if not p.exists():
        return g
    with open(p, "r", encoding="utf-8") as fh:
        header = True
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if header:
                header = False
                if line.lower().startswith("zh,"):
                    continue
            parts = line.split(",", 1)
            if len(parts) == 2:
                g[parts[0].strip()] = parts[1].strip()
    return g
