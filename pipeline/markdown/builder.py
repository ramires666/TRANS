"""Сборка PDF из Markdown-перевода (overlay поверх исходного PDF)."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz

from pipeline.config.loader import ROOT, ensure_dirs, load_config, resolve_path, setup_logger
from pipeline.fonts.fonts import find_target_font
from pipeline.io.artifacts import artifact_paths, load_json, source_hash


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------

def _md_to_html(md: str, css: str = "") -> str:
    """Превращает Markdown в HTML, пригодный для PyMuPDF insert_htmlbox."""
    try:
        import markdown as md_lib
        html_body = md_lib.markdown(md, extensions=["tables", "fenced_code"])
    except Exception:
        # Fallback: просто оборачиваем в <pre>
        escaped = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html_body = f"<pre>{escaped}</pre>"

    style = f"<style>{css}</style>" if css else ""
    return f"<!DOCTYPE html><html><head>{style}</head><body>{html_body}</body></html>"


def _default_css(font_family: str, font_size_pt: float, line_height: float = 1.25) -> str:
    return (
        "body {{ margin: 0; padding: 0; font-family: '{family}', sans-serif; "
        "font-size: {size:.1f}pt; line-height: {lh:.2f}; color: #000; "
        "word-wrap: break-word; overflow-wrap: break-word; }}\n"
        "h1 {{ font-size: {h1:.1f}pt; margin: 0.3em 0 0.15em 0; font-weight: bold; }}\n"
        "h2 {{ font-size: {h2:.1f}pt; margin: 0.25em 0 0.12em 0; font-weight: bold; }}\n"
        "h3 {{ font-size: {h3:.1f}pt; margin: 0.2em 0 0.1em 0; font-weight: bold; }}\n"
        "h4 {{ font-size: {h4:.1f}pt; margin: 0.15em 0 0.08em 0; font-weight: bold; }}\n"
        "p {{ margin: 0 0 0.2em 0; }}\n"
        "table {{ border-collapse: collapse; width: 100%; margin: 0.3em 0; font-size: {tbl:.1f}pt; }}\n"
        "th, td {{ border: 1px solid #333; padding: 0.1em 0.2em; text-align: left; }}\n"
        "ul, ol {{ margin: 0.2em 0; padding-left: 1.2em; }}\n"
        "li {{ margin: 0.05em 0; }}\n"
        "pre {{ background: #f5f5f5; padding: 0.2em; overflow: auto; font-size: {pre:.1f}pt; }}\n"
        "code {{ font-size: {code:.1f}pt; }}\n"
    ).format(
        family=font_family, size=font_size_pt, lh=line_height,
        h1=font_size_pt * 1.5, h2=font_size_pt * 1.25, h3=font_size_pt * 1.1, h4=font_size_pt,
        tbl=font_size_pt * 0.9, pre=font_size_pt * 0.85, code=font_size_pt * 0.85,
    )


def _font_family_name(font_path: str) -> str:
    """Возвращает имя семейства по имени файла (упрощённо)."""
    name = Path(font_path).stem
    # PyMuPDF Story использует family-name из fontconfig/системных шрифтов.
    # Для встроенных TTF в insert_htmlbox лучше указать имя, совпадающее с stem.
    return name


def _text_blocks_bbox(page: fitz.Page) -> fitz.Rect:
    """Объединяет bbox всех текстовых блоков страницы."""
    d = page.get_text("dict")
    rects = []
    for b in d.get("blocks", []):
        if b.get("type") == 0:
            rects.append(fitz.Rect(b["bbox"]))
    if not rects:
        return page.rect
    r = rects[0]
    for rr in rects[1:]:
        r |= rr
    # Добавляем небольшие поля, чтобы HTML не прилипал к краям
    r.x0 = max(page.rect.x0, r.x0 - 2)
    r.y0 = max(page.rect.y0, r.y0 - 2)
    r.x1 = min(page.rect.x1, r.x1 + 2)
    r.y1 = min(page.rect.y1, r.y1 + 2)
    return r


def _avg_font_size(page: fitz.Page) -> float:
    """Средний размер шрифта на странице."""
    d = page.get_text("dict")
    sizes = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                sz = float(sp.get("size", 0))
                if sz > 0:
                    sizes.append(sz)
    if not sizes:
        return 10.0
    return sum(sizes) / len(sizes)


def _clean_text(page: fitz.Page) -> None:
    """Удаляет весь текст со страницы, сохраняя изображения и вектор."""
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") == 0 and b.get("lines"):
            try:
                page.add_redact_annot(fitz.Rect(b["bbox"]), fill=(1, 1, 1))
            except Exception:
                pass
    if page.annots:
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception:
            pass


def build_pdf(pdf_path: str, pages_md: dict[int, str], cfg: dict, logger,
              out_path: str | None = None) -> str:
    """Собирает PDF: исходный PDF как подложка + Markdown поверх.

    Args:
        pdf_path: путь к исходному PDF.
        pages_md: словарь {номер_страницы (0-based): markdown}.
        cfg: конфигурация.
        logger: логгер.
        out_path: путь для сохранения (опционально).

    Returns:
        Путь к сохранённому PDF.
    """
    src_pdf = resolve_path(pdf_path)
    out_path = resolve_path(out_path or cfg.get("out_path", "_RU.pdf"))

    font_path = cfg.get("target_font") or find_target_font("")
    font_family = _font_family_name(font_path)
    logger.info("[markdown builder] Шрифт: %s (family=%s)", font_path, font_family)

    logger.info("[markdown builder] Открываю исходник: %s", src_pdf)
    doc = fitz.open(str(src_pdf))

    default_fontsize = float(cfg.get("builder_default_fontsize", 10.0))
    min_fontsize = float(cfg.get("builder_min_fontsize", 6.0))

    for pno in range(doc.page_count):
        if pno not in pages_md:
            logger.warning("[markdown builder] Нет Markdown для страницы %d", pno + 1)
            continue

        page = doc.load_page(pno)
        md = pages_md[pno]
        if not md.strip():
            logger.info("[markdown builder] Страница %d пустая — skip", pno + 1)
            continue

        # 1. Удаляем старый текст
        _clean_text(page)

        # 2. Определяем область для нового текста
        text_rect = _text_blocks_bbox(page)
        # Если страница почти целиком текст — берём page.rect с полями
        if text_rect.width < 20 or text_rect.height < 20:
            margin = 36  # 0.5 inch
            text_rect = fitz.Rect(
                page.rect.x0 + margin, page.rect.y0 + margin,
                page.rect.x1 - margin, page.rect.y1 - margin)

        # 3. Размер шрифта (немного уменьшаем относительно среднего,
        #                    чтобы русский перевод влезал лучше)
        avg_size = _avg_font_size(page)
        font_size = avg_size * 0.85 if avg_size > 0 else default_fontsize
        font_size = max(min_fontsize, min(font_size, default_fontsize * 1.1))

        # 4-6. Вставляем HTML с автоподбором размера шрифта
        inserted = False
        current_size = font_size
        max_rect = fitz.Rect(
            page.rect.x0 + 18, page.rect.y0 + 18,
            page.rect.x1 - 18, page.rect.y1 - 18)
        while current_size >= min_fontsize and not inserted:
            css = _default_css(font_family, current_size)
            html = _md_to_html(md, css)
            try:
                spare, scale = page.insert_htmlbox(text_rect, html, css=css)
                if spare >= 0:
                    logger.debug(
                        "[markdown builder] Страница %d вставлена "
                        "(size=%.1f, spare=%.1f, scale=%.3f)",
                        pno + 1, current_size, spare, scale)
                    inserted = True
                    break
                # Не влезло — пробуем во всей странице или уменьшаем шрифт
                if text_rect != max_rect:
                    spare2, scale2 = page.insert_htmlbox(max_rect, html, css=css)
                    if spare2 >= 0:
                        logger.debug(
                            "[markdown builder] Страница %d вставлена в max_rect "
                            "(size=%.1f, spare=%.1f, scale=%.3f)",
                            pno + 1, current_size, spare2, scale2)
                        inserted = True
                        break
                current_size *= 0.9
            except Exception as e:
                logger.exception(
                    "[markdown builder] Ошибка вставки HTML на страницу %d: %s",
                    pno + 1, e)
                break
        if not inserted:
            logger.warning(
                "[markdown builder] Не удалось вставить Markdown на страницу %d "
                "(достигнут min_fontsize=%.1f)", pno + 1, min_fontsize)

        if (pno + 1) % 20 == 0:
            logger.info("[markdown builder] build page %d/%d", pno + 1, doc.page_count)

    # Метаданные
    try:
        md = dict(doc.metadata or {})
        md_cfg = cfg.get("metadata", {})
        for k in ("title", "author", "subject", "keywords"):
            if md_cfg.get(k):
                md[k] = md_cfg[k]
        if md:
            doc.set_metadata(md)
    except Exception as e:
        logger.warning("[markdown builder] set_metadata: %s", e)

    logger.info("[markdown builder] Сохраняю %s", out_path)
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
    logger.info("[markdown builder] Готово: %s", out_path)
    return str(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Сборка PDF из Markdown (pages_md.json) + исходного PDF")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--md", dest="md_json")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    src_pdf = args.inp or cfg["pdf_path"]
    sh = source_hash(src_pdf)
    apaths = artifact_paths(cfg, sh)
    md_json = args.md_json or str(apaths.get("pages_md") or (ROOT / cfg.get("tmp_dir", "intermediate") / sh / "pages_md.json"))
    out = args.out or cfg.get("out_path", "_RU.pdf")

    pages_md_raw = load_json(md_json)
    pages_md = {int(k): v for k, v in pages_md_raw.items()}

    build_pdf(src_pdf, pages_md, cfg, logger, out_path=out)


if __name__ == "__main__":
    main()
