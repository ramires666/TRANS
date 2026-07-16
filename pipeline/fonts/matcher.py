"""Подбор ближайшего похожего шрифта для перевода.

Для каждого сегмента известен исходный шрифт (поле ``font`` в segments.json).
Этот модуль отображает имя оригинального шрифта в TTF-файл, который:
  * поддерживает глифы целевого языка (по умолчанию кириллица);
  * по возможности совпадает по семейству (serif/sans/mono), весу и стилю.

Реестр кандидатов строится один раз (лениво) из:
  * assets/  проекта;
  * TTF, поставляемых с matplotlib;
  * системной папки шрифтов Windows (%WINDIR%\\Fonts).

Каждый TTF открывается через ``fitz.Font``: определяется постскрипт-имя,
наличие глифов целевого языка и (по имени файла) — bold/italic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import fitz

from pipeline.config.loader import ROOT


# ----------------------------------------------------------------------------
# Классификация
# ----------------------------------------------------------------------------

FAMILY_SERIF = "serif"
FAMILY_SANS = "sans"
FAMILY_MONO = "mono"
FAMILY_UNKNOWN = "unknown"

# Ключевые слова в именах шрифтов.
_SERIF_HINTS = (
    "serif", "times", "roman", "georgia", "garamond", "Cambria",
    "palatino", "book antiqua", "antqua", "bkant", "bookos", "century",
    "simsun", "songti", "song", "ming", "mincho",  # CJK с засечками
    "noto serif", "sourcehanserif", "droidsansfallback",
)
_SANS_HINTS = (
    "sans", "arial", "helvetica", "calibri", "verdana", "tahoma",
    "segoe", "roboto", "open sans", "dejavu", "liberation sans",
    "noto sans", "sourcehansans", "msyh", "yahei", "yu gothic",
    "simhei", "candara", "bahnschrift", "frutiger",
)
_MONO_HINTS = (
    "mono", "courier", "consol", "cascadia", "menlo", "lucida console",
    "noto sans mono", "liberation mono", "dejavu sans mono",
)
_BOLD_HINTS = ("bold", "bd", "black", "heavy", "demibold", "semibold",
               "med", "medium", "ariblk", "-calibri-bold")
_ITALIC_HINTS = ("italic", "ital", "oblique", "-it", "_it", "italicmt")

# Глифы для проверки покрытия целевого языка.
_LANG_GLYPHS = {
    "ru": [0x0410, 0x044F, 0x0451, 0x0401, 0x044F],  # А-я, ё, Ё
    "en": [ord("A"), ord("z"), ord("@"), ord("##"[0])],
    "de": [ord("Ä"), ord("ä"), ord("ß"), ord("Ö"), ord("Ü")],
}


def _lower(s: str) -> str:
    return (s or "").lower()


@dataclass(frozen=True)
class FontTraits:
    family: str          # serif / sans / mono / unknown
    bold: bool
    italic: bool


def classify_name(name: str) -> FontTraits:
    """Классифицирует шрифт по имени (PostScript/filename)."""
    s = _lower(name)
    bold = any(h in s for h in _BOLD_HINTS) or "bold" in s
    italic = any(h in s for h in _ITALIC_HINTS)

    if any(h in s for h in _MONO_HINTS):
        family = FAMILY_MONO
    elif any(h in s for h in _SERIF_HINTS):
        family = FAMILY_SERIF
    elif any(h in s for h in _SANS_HINTS):
        family = FAMILY_SANS
    else:
        family = FAMILY_UNKNOWN
    return FontTraits(family=family, bold=bold, italic=italic)


# ----------------------------------------------------------------------------
# Реестр кандидатов
# ----------------------------------------------------------------------------

@dataclass
class Candidate:
    path: Path
    ps_name: str          # PostScript-имя (из fitz.Font)
    filename: str
    traits: FontTraits
    covers_lang: bool


def _check_glyphs(font: fitz.Font, codepoints: list[int]) -> bool:
    try:
        return all(font.has_glyph(cp) for cp in codepoints)
    except Exception:
        return False


def _candidate_from(path: Path, lang_codepoints: list[int]) -> Candidate | None:
    try:
        font = fitz.Font(fontfile=str(path))
    except Exception:
        return None
    if not font:
        return None
    ps_name = font.name or path.stem
    covers = _check_glyphs(font, lang_codepoints)
    # имя для классификации = PostScript + имя файла (улучшает поиск bold/italic)
    traits = classify_name(ps_name + " " + path.name)
    return Candidate(
        path=path,
        ps_name=ps_name,
        filename=path.name,
        traits=traits,
        covers_lang=covers,
    )


@lru_cache(maxsize=2)
def _registry(target_lang: str = "ru") -> tuple[Candidate, ...]:
    codepoints = _LANG_GLYPHS.get(target_lang, _LANG_GLYPHS["ru"])

    dirs: list[Path] = [
        ROOT / "assets",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    ]
    try:
        import matplotlib
        dirs.append(Path(matplotlib.__file__).parent
                    / "mpl-data" / "fonts" / "ttf")
    except Exception:
        pass

    seen: set[Path] = set()
    out: list[Candidate] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.glob("*.ttf"):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            c = _candidate_from(rp, codepoints)
            if c is not None:
                out.append(c)
    return tuple(out)


# ----------------------------------------------------------------------------
# Скоринг и подбор
# ----------------------------------------------------------------------------

def _score(orig: FontTraits, cand: FontTraits) -> int:
    """Чем выше — тем лучше. Покрытие языка проверяется отдельно."""
    s = 0
    # совпадение семейства — самый важный критерий
    if orig.family != FAMILY_UNKNOWN:
        if orig.family == cand.family:
            s += 100
        elif cand.family == FAMILY_UNKNOWN:
            s += 20
        else:
            s -= 50
    else:
        # оригинал не распознан — предпочитаем sans (нейтральный)
        if cand.family == FAMILY_SANS:
            s += 40
        elif cand.family == FAMILY_SERIF:
            s += 20
        elif cand.family == FAMILY_MONO:
            s += 5

    # вес
    if orig.bold == cand.bold:
        s += 30
    elif orig.bold and not cand.bold:
        s -= 10
    # стиль
    if orig.italic == cand.italic:
        s += 20
    elif orig.italic and not cand.italic:
        s -= 5
    return s


def match_font(orig_name: str, target_lang: str = "ru",
               fallback: str | None = None) -> str:
    """Возвращает путь к ближайшему TTF для ``orig_name``.

    Если ничего не подходит (нет покрытия языка) — возвращается ``fallback``
    или первый кандидат с покрытием, или поднимается ``FileNotFoundError``.
    """
    orig = classify_name(orig_name or "")
    best: Candidate | None = None
    best_score = -10**9
    any_cover: Candidate | None = None

    for c in _registry(target_lang):
        if c.covers_lang:
            if any_cover is None:
                any_cover = c
            sc = _score(orig, c.traits)
            if sc > best_score:
                best_score = sc
                best = c

    if best is not None:
        return str(best.path)
    if fallback and Path(fallback).exists():
        return fallback
    if any_cover is not None:
        return str(any_cover.path)
    raise FileNotFoundError(
        f"Не найден TTF с покрытием '{target_lang}' для замены '{orig_name}'. "
        "Положите шрифт в assets/ или укажите target_font в config.yaml."
    )


def match_fontname(orig_name: str, target_lang: str = "ru") -> str:
    """То же, что ``match_font``, но возвращает PostScript-имя для fitz."""
    return _ps_name_for(match_font(orig_name, target_lang), target_lang)


@lru_cache(maxsize=256)
def _ps_name_for(path: str, target_lang: str) -> str:
    try:
        return fitz.Font(fontfile=path).name or Path(path).stem
    except Exception:
        return Path(path).stem


# ----------------------------------------------------------------------------
# Диагностика
# ----------------------------------------------------------------------------

def describe_match(orig_name: str, target_lang: str = "ru") -> dict:
    """Для инспекции/лога: что подобрано и почему."""
    orig = classify_name(orig_name)
    path = match_font(orig_name, target_lang)
    cand = next((c for c in _registry(target_lang)
                 if str(c.path) == path), None)
    return {
        "orig_name": orig_name,
        "orig_traits": {"family": orig.family,
                        "bold": orig.bold, "italic": orig.italic},
        "matched_path": path,
        "matched_ps_name": cand.ps_name if cand else None,
        "matched_traits": ({"family": cand.traits.family,
                            "bold": cand.traits.bold,
                            "italic": cand.traits.italic}
                           if cand else None),
        "covers_lang": cand.covers_lang if cand else None,
    }


def main() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(description="Подбор похожего шрифта")
    ap.add_argument("names", nargs="*", help="имена исходных шрифтов")
    ap.add_argument("--lang", default="ru")
    ap.add_argument("--dump", action="store_true",
                    help="дамп реестра кандидатов")
    args = ap.parse_args()

    if args.dump:
        for c in _registry(args.lang):
            print(f"  {'+' if c.covers_lang else '-'} "
                  f"{c.ps_name:32s} {c.traits.family:8s} "
                  f"b={int(c.traits.bold)} i={int(c.traits.italic)} "
                  f"<- {c.path}")
        return
    for n in args.names:
        print(json.dumps(describe_match(n, args.lang), ensure_ascii=False))


if __name__ == "__main__":
    main()
