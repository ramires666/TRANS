"""Сводка по PDF: страницы, шрифты, изображения, TOC. Этап 0.1 плана.

Файл назван inspect_pdf.py (а не inspect.py), чтобы не затенять stdlib-модуль
inspect, который импортирует PyMuPDF.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz  # PyMuPDF


def inspect(pdf_path: str, max_page_sample: int = 5) -> None:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        print(f"Файл не найден: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doc = fitz.open(str(pdf_path))
    print(f"Файл: {pdf_path.name}")
    print(f"Страниц: {doc.page_count}")

    md = doc.metadata or {}
    print("\n--- Метаданные ---")
    for k in ("title", "author", "subject", "keywords", "creator", "producer"):
        print(f"  {k}: {md.get(k, '')!r}")

    toc = doc.get_toc()
    print(f"\nTOC (закладки): {len(toc)}")
    if toc:
        levels = [t[0] for t in toc]
        print(f"  уровни: min={min(levels)} max={max(levels)}")
        print("  первые 10:")
        for lvl, title, page in toc[:10]:
            print(f"    [{lvl}] {title}  -> p.{page}")

    total_imgs = 0
    total_drawings = 0
    total_chars = 0
    all_fonts: set[str] = set()
    tables_detected = 0

    sample_pages = list(range(min(max_page_sample, doc.page_count)))
    for pno in sample_pages:
        page = doc.load_page(pno)
        imgs = page.get_images(full=True)
        total_imgs += len(imgs)
        total_drawings += len(page.get_drawings())
        text = page.get_text("text")
        total_chars += len(text)
        for f in page.get_fonts(full=True):
            all_fonts.add(f[3])
        try:
            tabs = page.find_tables()
            tables_detected += len(tabs.tables) if tabs else 0
        except Exception:
            pass

    print("\n--- Выборка по первым {} стр. ---".format(len(sample_pages)))
    print(f"  изображений: {total_imgs}")
    print(f"  drawings: {total_drawings}")
    print(f"  текстовых символов: {total_chars}")
    print(f"  таблиц (find_tables): {tables_detected}")
    print(f"  шрифты в выборке: {sorted(all_fonts)}")

    # Полные totals по изображениям (быстро)
    full_imgs = sum(len(doc.load_page(i).get_images(full=True)) for i in range(doc.page_count))
    print(f"\nВсего изображений во всём PDF: {full_imgs}")

    doc.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Сводка по PDF (PyMuPDF)")
    ap.add_argument("--in", dest="inp", required=True, help="путь к PDF")
    ap.add_argument("--sample", type=int, default=5, help="сколько страниц детализировать")
    args = ap.parse_args()
    inspect(args.inp, args.sample)


if __name__ == "__main__":
    main()
