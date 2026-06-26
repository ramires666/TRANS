"""Этап 4 — Сборка _RU.pdf: копия+редакт+перевод TOC+метаданные."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz

from pipeline.config.loader import (ROOT, ensure_dirs, load_config,
                                     resolve_path, setup_logger)
from pipeline.fonts.fonts import find_target_font
from pipeline.fonts.matcher import match_font, describe_match
from pipeline.io.artifacts import load_json


def _overlay_image_translations(page, cfg: dict, target_font: str, logger,
                                 cache_path: str | None = None):
    """Опционально: OCR + перевод текста в изображениях на странице.

    Добавляет переведённые подписи под изображения, если в config включён
    ``enable_vision_ocr``. Безопасен при отсутствии vision-модели/PIL.
    """
    if not cfg.get("enable_vision_ocr"):
        return
    try:
        from pipeline.vision.ocr import VisionTranslator, VisionCache
        from PIL import Image
    except Exception as e:
        logger.warning("Vision OCR недоступен: %s", e)
        return

    src_lang = cfg.get("source_lang", "zh")
    tgt_lang = cfg.get("target_lang", "ru")
    translator = VisionTranslator(cfg)
    if not translator.enabled:
        logger.warning("Vision OCR не настроен: задайте vision_llm_model")
        return

    cache = VisionCache(cache_path) if cache_path else None
    try:
        doc = page.parent
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                translated = translator.translate_image(pil_img, src_lang, tgt_lang, cache)
                if not translated:
                    continue
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                r = rects[0]
                label_rect = fitz.Rect(r.x0, r.y1 + 2, r.x1, r.y1 + 20)
                page.insert_textbox(label_rect, translated, fontname="tgt",
                                    fontfile=target_font, fontsize=6,
                                    color=(0.3, 0.3, 0.3))
            except Exception as e:
                logger.debug("Vision OCR для xref %d не удался: %s", xref, e)
                continue
    finally:
        if cache:
            cache.close()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _build_heading_lookup(segments: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in segments:
        if s["type"] != "heading":
            continue
        zh = s["text"]
        cleaned = re.sub(r"[\.\s]*\d+\s*$", "", zh)
        cleaned = re.sub(r"\.{2,}", "", cleaned).strip()
        key = _norm(cleaned)
        if key and key not in out:
            out[key] = s.get("ru") or s["text"]
    return out


def _normalize_markers(text: str, cfg: dict | None = None) -> str:
    """Нормализует маркеры списков в стандартный bullet для любого языка.

    Конфигурируется через ``bullet_chars``, ``bullet_replace`` и ``remove_pua``
    в config.yaml. По умолчанию обрабатывает широкий набор bullet-символов.
    """
    cfg = cfg or {}
    chars = cfg.get("bullet_chars", "•●○◌◦◆◇■□▪▫▶▷◀◁▲△▼▽◄►◅▻")
    replace = cfg.get("bullet_replace", "•") or "•"
    remove_pua = cfg.get("remove_pua", True)

    if chars:
        # экранируем для регулярного выражения
        escaped = "".join(c if c not in r"\^$.*+?{}[]|()" else "\\" + c for c in chars)
        text = re.sub(rf"(?m)(^\s*)[{escaped}]", r"\1" + replace, text)
    if remove_pua:
        text = re.sub(r"[\uf000-\uf8ff]", "", text)
    # Убираем возможные двойные пробелы после удаления
    text = re.sub(r"  +", " ", text).strip()
    return text


def _translate_toc(toc: list, heading_lookup: dict[str, str]) -> list:
    new_toc = []
    for lvl, title, page in toc:
        cleaned = re.sub(r"\.{2,}", "", title).strip()
        key = _norm(cleaned)
        ru = heading_lookup.get(key)
        if not ru:
            cleaned2 = re.sub(r"[\.\s]*\d+\s*$", "", cleaned)
            ru = heading_lookup.get(_norm(cleaned2)) or title
        new_toc.append([lvl, ru, page])
    return new_toc


def _insert_text_fit(page, rect, text, fontname, fontfile, fontsize,
                     min_size, align, color) -> tuple[float, int]:
    size = fontsize
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, color=color, align=align, render_mode=0)
        if rc >= 0:
            return size, (1 if size < fontsize - 0.01 else 0)
        size -= 0.5
    page.insert_textbox(
        rect, text, fontname=fontname, fontfile=fontfile,
        fontsize=max(min_size, size), color=color, align=align, render_mode=0)
    return max(min_size, size), 2


def _compute_fit_size(page, rect, text, fontname, fontfile, fontsize,
                      min_size, align) -> float:
    """Вычисляет размер шрифта, при котором text влезает в rect, БЕЗ вставки.

    Использует render_mode=3 (невидимый) чтобы не рисовать.
    """
    size = fontsize
    while size >= min_size:
        rc = page.insert_textbox(
            rect, text, fontname=fontname, fontfile=fontfile,
            fontsize=size, align=align, render_mode=3)
        if rc >= 0:
            return size
        size -= 0.5
    return max(min_size, size)


# ---------------------------------------------------------------------------
# Кластеризация сегментов по пространственной близости для единого размера
# шрифта. Группируем ячейки в таблицы, пункты списков — в списки.
# ---------------------------------------------------------------------------

def _cluster_cells(cells: list[dict], gap_y: float = 30.0,
                   gap_x: float = 50.0) -> list[list[int]]:
    """Кластеризует ячейки в таблицы по пространственной близости.

    Возвращает список кластеров, каждый — список индексов в ``cells``.

    Алгоритм:
      1. Сортируем по y0.
      2. Разбиваем на «строки» — ячейки с близким y0 (±5px).
      3. Объединяем строки в таблицы, если зазор между ними ≤ gap_y
         И их x-диапазоны пересекаются (та же таблица по горизонтали).
    """
    n = len(cells)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: cells[i]["bbox"][1])

    # Группируем в строки (ячейки на одной горизонтальной линии)
    rows: list[list[int]] = []
    current_row: list[int] = [order[0]]
    row_y0 = cells[order[0]]["bbox"][1]
    for idx in order[1:]:
        y0 = cells[idx]["bbox"][1]
        if abs(y0 - row_y0) <= 5:
            current_row.append(idx)
        else:
            rows.append(current_row)
            current_row = [idx]
            row_y0 = y0
    rows.append(current_row)

    # Объединяем строки в таблицы
    def _row_xrange(row_idxs):
        xs0 = [cells[i]["bbox"][0] for i in row_idxs]
        xs1 = [cells[i]["bbox"][2] for i in row_idxs]
        return min(xs0), max(xs1)

    def _rows_overlap(r1, r2):
        x0a, x1a = _row_xrange(r1)
        x0b, x1b = _row_xrange(r2)
        # пересечение x-диапазонов
        return x0a <= x1b and x0b <= x1a

    clusters: list[list[int]] = []
    current: list[list[int]] = [rows[0]]
    prev_y1 = max(cells[i]["bbox"][3] for i in rows[0])
    for row in rows[1:]:
        y0 = min(cells[i]["bbox"][1] for i in row)
        gap = y0 - prev_y1
        # объединяем если зазор маленький И x-диапазоны пересекаются
        if gap <= gap_y and any(_rows_overlap(row, prev_row)
                                for prev_row in current[-3:]):
            current.append(row)
        else:
            # новая таблица
            flat = [i for r in current for i in r]
            clusters.append(flat)
            current = [row]
        prev_y1 = max(prev_y1, max(cells[i]["bbox"][3] for i in row))
    flat = [i for r in current for i in r]
    clusters.append(flat)
    return clusters


def _cluster_listitems(items: list[dict], gap_y: float = 60.0
                       ) -> list[list[int]]:
    """Кластеризует пункты списка в группы (один список).

    Сортируем по y0, группы разрываются при большом вертикальном gap.
    """
    n = len(items)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: items[i]["bbox"][1])
    clusters: list[list[int]] = []
    current: list[int] = []
    prev_y1 = None
    for idx in order:
        bb = items[idx]["bbox"]
        y0, y1 = bb[1], bb[3]
        if prev_y1 is not None and y0 - prev_y1 > gap_y:
            if current:
                clusters.append(current)
            current = []
        current.append(idx)
        prev_y1 = max(prev_y1 or y1, y1)
    if current:
        clusters.append(current)
    return clusters


def _cluster_by_size(segs: list[dict], idxs: list[int],
                     tolerance: float = 1.5) -> list[list[int]]:
    """Разбивает индексы на подгруппы по близости исходного размера шрифта.

    Сегменты с одинаковым оригинальным размером должны иметь одинаковый
    итоговый размер._tolerance — допустимое отклонение для объединения.
    """
    if not idxs:
        return []
    sorted_idx = sorted(idxs, key=lambda i: float(segs[i].get("size") or 0))
    groups: list[list[int]] = []
    current = [sorted_idx[0]]
    prev_sz = float(segs[sorted_idx[0]].get("size") or 0)
    for idx in sorted_idx[1:]:
        sz = float(segs[idx].get("size") or 0)
        if abs(sz - prev_sz) <= tolerance:
            current.append(idx)
        else:
            groups.append(current)
            current = [idx]
        prev_sz = sz
    groups.append(current)
    return groups


def _is_listlike(seg: dict) -> bool:
    """True если сегмент похож на пункт списка (нумерованный/маркированный),
    даже если сегментер ошибочно классифицировал его как heading/paragraph.
    """
    if seg["type"] == "listItem":
        return True
    ru = (seg.get("ru") or seg.get("text") or "").strip()
    # нумерованный пункт: «1.», «2.», «1)», «(1)» в начале
    if re.match(r"^\d+[\.\)]\s+\S", ru):
        return True
    # маркированный: •, –, —, ▪ в начале
    if re.match(r"^[•\-–—▪]\s+\S", ru):
        return True
    return False


def _plan_font_sizes(page, page_segs: list[dict], cfg: dict,
                     target_lang: str, font_path: str, logger,
                     min_size: float, default_size: float
                     ) -> dict[int, float]:
    """Смарт-планирование размеров шрифта для всех сегментов страницы.

    Возвращает dict: индекс сегмента -> финальный размер шрифта.

    Алгоритм:
      1. Вычислить fit_size для каждого сегмента (на временной странице).
      2. Кластеризовать cell-ы в таблицы, listLike-ы в списки.
      3. Внутри каждого кластера разбить по оригинальному размеру шрифта
         и назначить min(fit_size) по подгруппе.
      4. Для остальных сегментов — индивидуальный fit_size.
    """
    # Временная страница для замеров (не засоряет реальную страницу)
    page_rect = page.rect
    tmp_doc = fitz.open()
    tmp_page = tmp_doc.new_page(width=page_rect.width, height=page_rect.height)

    # Проход 1: вычислить fit_size для каждого рендеримого сегмента
    fit_sizes: dict[int, float] = {}
    renderable: list[int] = []
    for idx, seg in enumerate(page_segs):
        ru = (seg.get("ru") or "").strip()
        if not ru:
            continue
        ru = _normalize_markers(ru, cfg)
        bb = seg["bbox"]
        rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])
        if rect.is_empty or rect.width < 1 or rect.height < 1:
            continue
        orig_size = float(seg.get("size") or default_size) or default_size
        align = fitz.TEXT_ALIGN_CENTER if seg["type"] in (
            "caption_fig", "caption_tab") else fitz.TEXT_ALIGN_LEFT
        fn, ff = _resolve_font_for_seg(
            seg, cfg, target_lang, font_path, logger)
        fit_sz = _compute_fit_size(
            tmp_page, rect, ru, fn, ff, orig_size, min_size, align)
        fit_sizes[idx] = fit_sz
        renderable.append(idx)

    tmp_doc.close()

    final: dict[int, float] = dict(fit_sizes)  # по умолчанию = fit_size

    # Кластеризация ячеек в таблицы
    cell_idxs = [i for i in renderable if page_segs[i]["type"] == "cell"]
    if cell_idxs:
        cell_segs = [page_segs[i] for i in cell_idxs]
        clusters = _cluster_cells(cell_segs)
        for cluster in clusters:
            orig_idxs = [cell_idxs[j] for j in cluster]
            sub_groups = _cluster_by_size(page_segs, orig_idxs)
            for group in sub_groups:
                if not group:
                    continue
                group_fit = min(fit_sizes[i] for i in group)
                for i in group:
                    final[i] = group_fit

    # Кластеризация list-like сегментов (listItem + нумерованные heading/paragraph)
    list_idxs = [i for i in renderable if _is_listlike(page_segs[i])]
    if list_idxs:
        list_segs = [page_segs[i] for i in list_idxs]
        clusters = _cluster_listitems(list_segs)
        for cluster in clusters:
            orig_idxs = [list_idxs[j] for j in cluster]
            sub_groups = _cluster_by_size(page_segs, orig_idxs)
            for group in sub_groups:
                if not group:
                    continue
                group_fit = min(fit_sizes[i] for i in group)
                for i in group:
                    final[i] = group_fit

    return final


def _resolve_font_for_seg(seg: dict, cfg: dict, target_lang: str,
                          default_path: str, logger) -> tuple[str, str]:
    """Возвращает (fontname, fontfile) для сегмента.

    Если ``cfg["match_fonts"]`` включён — подбирает ближайший похожий шрифт
    под оригинальный ``seg['font']``. Иначе — общий ``default_path``.
    """
    if not cfg.get("match_fonts"):
        return "tgt", default_path
    orig = seg.get("font") or ""
    try:
        path = match_font(orig, target_lang, fallback=default_path)
    except Exception as e:
        logger.warning("match_font(%r): %s — fallback", orig, e)
        return "tgt", default_path
    # fontname должен быть уникален на странице для каждого TTF,
    # но fitz допускает повторную регистрацию того же файла под одним alias.
    return "mtch", path


def build(cfg: dict, logger, segments_ru_path: str | None = None,
          out_path: str | None = None) -> str:
    src_pdf = resolve_path(cfg["pdf_path"])
    out_path = resolve_path(out_path or cfg["out_path"])
    segments_ru_path = segments_ru_path or "intermediate/segments_ru.json"
    if not Path(segments_ru_path).is_absolute():
        from pipeline.config.loader import ROOT
        segments_ru_path = str(ROOT / segments_ru_path)

    segs = load_json(segments_ru_path)
    by_page: dict[int, list[dict]] = {}
    for s in segs:
        by_page.setdefault(s["page"], []).append(s)

    font_path = cfg.get("target_font") or cfg.get("cyrillic_font") or ""
    if not font_path:
        font_path = find_target_font("")
    logger.info("Базовый шрифт: %s", font_path)

    target_lang = cfg.get("target_lang", "ru")
    match_fonts = bool(cfg.get("match_fonts", False))
    if match_fonts:
        logger.info("Подбор шрифта по оригиналу включён (lang=%s)", target_lang)

    logger.info("Открываю копию исходника: %s", src_pdf)
    doc = fitz.open(str(src_pdf))

    total_blocks = skipped_empty = overflows = notfit = 0
    min_size = float(cfg.get("builder_min_fontsize", 6.0))
    default_size = float(cfg.get("builder_default_fontsize", 10.0))

    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        page_segs = by_page.get(pno, [])
        if not page_segs:
            continue

        for seg in page_segs:
            ru = (seg.get("ru") or "").strip()
            if not ru:
                continue
            ru = _normalize_markers(ru, cfg)
            bb = seg["bbox"]
            rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue
            try:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            except Exception:
                continue
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        except Exception as e:
            logger.warning("apply_redactions p.%d: %s", pno + 1, e)

        # Смарт-планирование размеров шрифта
        planned = _plan_font_sizes(
            page, page_segs, cfg, target_lang, font_path, logger,
            min_size, default_size)

        # Рендеринг с запланированными размерами
        for idx, seg in enumerate(page_segs):
            if idx not in planned:
                skipped_empty += 1
                continue
            ru = (seg.get("ru") or "").strip()
            if not ru:
                skipped_empty += 1
                continue
            ru = _normalize_markers(ru, cfg)
            bb = seg["bbox"]
            rect = fitz.Rect(bb[0], bb[1], bb[2], bb[3])

            final_size = planned[idx]
            align = fitz.TEXT_ALIGN_CENTER if seg["type"] in (
                "caption_fig", "caption_tab") else fitz.TEXT_ALIGN_LEFT

            fill = (0, 0, 0)
            try:
                color = int(seg.get("color") or 0)
                if color != 0:
                    fill = (((color >> 16) & 0xFF) / 255.0,
                            ((color >> 8) & 0xFF) / 255.0,
                            (color & 0xFF) / 255.0)
            except Exception:
                pass

            fn, ff = _resolve_font_for_seg(
                seg, cfg, target_lang, font_path, logger)
            size, status = _insert_text_fit(
                page, rect, ru, fn, ff, final_size,
                min_size, align, fill)
            if status == 2:
                notfit += 1
            elif status == 1:
                overflows += 1
            total_blocks += 1

        # Опциональный OCR текстa в изображениях
        try:
            from pipeline.io.artifacts import source_hash
            sh = source_hash(str(src_pdf))
            vision_cache = str(ROOT / cfg.get("tmp_dir", "intermediate") / sh / "vision_cache.db")
            _overlay_image_translations(page, cfg, font_path, logger, vision_cache)
        except Exception as e:
            logger.debug("Vision OCR на странице %d не удался: %s", pno + 1, e)

        if (pno + 1) % 20 == 0:
            logger.info("  build page %d/%d", pno + 1, doc.page_count)

    try:
        orig_toc = doc.get_toc()
        new_toc = _translate_toc(orig_toc, _build_heading_lookup(segs))
        doc.set_toc(new_toc)
        logger.info("TOC переведён: %d закладок", len(new_toc))
    except Exception as e:
        logger.warning("TOC: %s", e)

    md = dict(doc.metadata or {})
    md_cfg = cfg.get("metadata", {})
    src_doc = fitz.open(str(src_pdf))
    src_md = dict(src_doc.metadata or {})
    src_doc.close()

    for k in ("title", "author", "subject", "keywords"):
        if md_cfg.get(k):
            md[k] = md_cfg[k]
        elif not md.get(k) and src_md.get(k):
            md[k] = src_md[k]
    try:
        doc.set_metadata(md)
    except Exception as e:
        logger.warning("set_metadata: %s", e)

    logger.info("Сохраняю %s (блоков: %d, сжато: %d, не_влезло: %d, пропущено: %d)",
                out_path, total_blocks, overflows, notfit, skipped_empty)
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
    logger.info("Готово: %s", out_path)
    return str(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка _RU.pdf")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    build(cfg, logger, segments_ru_path=args.inp, out_path=args.out)


if __name__ == "__main__":
    main()