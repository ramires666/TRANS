"""Post-process a completed PDF by translating text inside raster images."""
from __future__ import annotations

import io
import json
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any, Callable

import fitz
from PIL import Image, ImageDraw, ImageFont

from pipeline.fonts.fonts import find_target_font
from pipeline.vision.ocr import (VisionCache, VisionTranslator, clean_region,
                                 _pixmap_to_image)


ProgressCallback = Callable[[dict[str, Any]], None]


def _text_width(font: ImageFont.FreeTypeFont, text: str) -> float:
    try:
        return float(font.getlength(text))
    except AttributeError:
        bbox = font.getbbox(text)
        return float(bbox[2] - bbox[0])


def _split_long_word(word: str, font: ImageFont.FreeTypeFont,
                     max_width: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and _text_width(font, candidate) > max_width:
            chunks.append(current)
            current = char
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [word]


def _wrap_text(text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            pieces = (_split_long_word(word, font, max_width)
                      if _text_width(font, word) > max_width else [word])
            for piece in pieces:
                candidate = piece if not current else f"{current} {piece}"
                if current and _text_width(font, candidate) > max_width:
                    lines.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            lines.append(current)
    return lines


def _layout_at_size(text: str, font_path: str, size: int,
                    width: int, height: int) -> dict | None:
    try:
        font = ImageFont.truetype(font_path, size=size)
    except OSError:
        return None
    lines = _wrap_text(text, font, width)
    if not lines:
        return None
    ascent, descent = font.getmetrics()
    line_height = max(1, ascent + descent)
    spacing = max(1, int(round(size * 0.12)))
    total_height = len(lines) * line_height + (len(lines) - 1) * spacing
    if total_height > height:
        return None
    if any(_text_width(font, line) > width + 0.01 for line in lines):
        return None
    return {
        "font": font,
        "size": size,
        "lines": lines,
        "line_height": line_height,
        "spacing": spacing,
        "height": total_height,
    }


def _fit_text(text: str, font_path: str, width: int, height: int,
              min_font_size: int) -> dict | None:
    if width < 1 or height < 1:
        return None
    low = max(1, int(min_font_size))
    high = max(low, min(256, int(height)))
    best = _layout_at_size(text, font_path, low, width, height)
    if best is None:
        return None
    while low <= high:
        middle = (low + high) // 2
        layout = _layout_at_size(text, font_path, middle, width, height)
        if layout is None:
            high = middle - 1
        else:
            best = layout
            low = middle + 1
    return best


def _pixel_rect(bbox: list[float], width: int, height: int
                ) -> tuple[int, int, int, int] | None:
    x0 = max(0, min(width, int(math.floor(bbox[0] * width / 1000.0))))
    y0 = max(0, min(height, int(math.floor(bbox[1] * height / 1000.0))))
    x1 = max(0, min(width, int(math.ceil(bbox[2] * width / 1000.0))))
    y1 = max(0, min(height, int(math.ceil(bbox[3] * height / 1000.0))))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _expanded_rect(rect: tuple[int, int, int, int], width: int, height: int,
                   padding: int, padding_ratio: float = 0.04
                   ) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    # Vision bbox часто проходит прямо по glyph outline. Процентный внешний
    # запас закрывает крайние штрихи CJK; фиксированный padding ниже остаётся
    # внутренним отступом уже переведённого текста.
    ratio = max(0.0, min(float(padding_ratio), 0.25))
    pad_x = max(int(padding), int(round((x1 - x0) * ratio)))
    pad_y = max(int(padding), int(round((y1 - y0) * ratio)))
    return (max(0, x0 - pad_x), max(0, y0 - pad_y),
            min(width, x1 + pad_x), min(height, y1 + pad_y))


def _border_median(image: Image.Image, rect: tuple[int, int, int, int],
                   thickness: int = 2) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    pixels = rgb.load()
    x0, y0, x1, y1 = rect
    samples: list[tuple[int, int, int]] = []
    for distance in range(1, max(1, thickness) + 1):
        top, bottom = y0 - distance, y1 - 1 + distance
        left, right = x0 - distance, x1 - 1 + distance
        if 0 <= top < rgb.height:
            samples.extend(pixels[x, top] for x in range(x0, x1))
        if 0 <= bottom < rgb.height:
            samples.extend(pixels[x, bottom] for x in range(x0, x1))
        if 0 <= left < rgb.width:
            samples.extend(pixels[left, y] for y in range(y0, y1))
        if 0 <= right < rgb.width:
            samples.extend(pixels[right, y] for y in range(y0, y1))
    if not samples:
        # Full-image regions have no outside ring: use their own perimeter.
        if y0 < y1:
            samples.extend(pixels[x, y0] for x in range(x0, x1))
            samples.extend(pixels[x, y1 - 1] for x in range(x0, x1))
        if x0 < x1:
            samples.extend(pixels[x0, y] for y in range(y0, y1))
            samples.extend(pixels[x1 - 1, y] for y in range(y0, y1))
    if not samples:
        return 255, 255, 255
    return tuple(int(round(statistics.median(channel)))
                 for channel in zip(*samples))


def _foreground(background: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = (0.2126 * background[0] + 0.7152 * background[1]
                 + 0.0722 * background[2])
    return (0, 0, 0) if luminance >= 145 else (255, 255, 255)


def overlay_regions(image: Image.Image, regions: list[dict], font_path: str,
                    *, source_lang: str = "auto", target_lang: str = "ru",
                    min_confidence: float = 0.65, min_font_size: int = 12,
                    padding: int = 2,
                    text_align: str = "center",
                    bbox_padding_ratio: float = 0.04
                    ) -> tuple[Image.Image, dict]:
    """Paint validated translated regions onto a copy of a PIL image."""
    original = image.copy()
    has_alpha = "A" in image.getbands()
    working = image.convert("RGBA" if has_alpha else "RGB")
    sample_image = image.convert("RGB")
    plans: list[dict] = []
    skipped = 0

    for raw_region in regions or []:
        region = clean_region(raw_region, source_lang, target_lang)
        if region is None or region["confidence"] < min_confidence:
            skipped += 1
            continue
        pixel_rect = _pixel_rect(region["bbox"], image.width, image.height)
        if pixel_rect is None:
            skipped += 1
            continue
        block_rect = _expanded_rect(
            pixel_rect, image.width, image.height, max(0, int(padding)),
            bbox_padding_ratio)
        x0, y0, x1, y1 = block_rect
        inner_padding = max(1, int(padding))
        text_width = x1 - x0 - 2 * inner_padding
        text_height = y1 - y0 - 2 * inner_padding
        layout = _fit_text(region["translation"], font_path, text_width,
                           text_height, min_font_size)
        if layout is None:
            skipped += 1
            continue
        plans.append({
            "rect": block_rect,
            "background": _border_median(sample_image, block_rect),
            "layout": layout,
            "padding": inner_padding,
        })

    if not plans:
        return original, {
            "modified": False,
            "processed": 0,
            "skipped": skipped,
            "min_font": None,
        }

    draw = ImageDraw.Draw(working)
    for plan in plans:
        x0, y0, x1, y1 = plan["rect"]
        background = plan["background"]
        fill = background + ((255,) if working.mode == "RGBA" else ())
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill)
        layout = plan["layout"]
        text_color = _foreground(background)
        text_fill = text_color + ((255,) if working.mode == "RGBA" else ())
        cursor_y = y0 + plan["padding"] + max(
            0, ((y1 - y0 - 2 * plan["padding"] - layout["height"]) // 2))
        for line in layout["lines"]:
            bbox = draw.textbbox((0, 0), line or " ", font=layout["font"])
            if text_align == "left":
                cursor_x = x0 + plan["padding"] - bbox[0]
            else:
                inner_width = x1 - x0 - 2 * plan["padding"]
                line_width = bbox[2] - bbox[0]
                cursor_x = (x0 + plan["padding"]
                            + max(0, (inner_width - line_width) // 2)
                            - bbox[0])
            draw.text(
                (cursor_x, cursor_y - bbox[1]),
                line,
                font=layout["font"],
                fill=text_fill,
            )
            cursor_y += layout["line_height"] + layout["spacing"]

    return working, {
        "modified": True,
        "processed": len(plans),
        "skipped": skipped,
        "min_font": min(plan["layout"]["size"] for plan in plans),
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _temporary_pdf_path(output_path: Path) -> Path:
    handle, temporary = tempfile.mkstemp(
        prefix=f".{output_path.stem}.", suffix=".tmp.pdf",
        dir=str(output_path.parent))
    os.close(handle)
    os.unlink(temporary)
    return Path(temporary)


def _notify(progress: ProgressCallback | None, logger, payload: dict) -> None:
    if progress is None:
        return
    try:
        progress(payload)
    except TypeError:
        try:
            progress(payload["current"], payload["total"])
        except Exception as exc:
            logger.debug("Vision progress callback failed: %s", exc)
    except Exception as exc:
        logger.debug("Vision progress callback failed: %s", exc)


def _record_skip(report: dict, reason: str) -> None:
    report["skipped"] += 1
    report["skip_reasons"][reason] = report["skip_reasons"].get(reason, 0) + 1


def postprocess_images(input_pdf, out_pdf, cfg: dict, logger,
                       progress: ProgressCallback | None = None) -> dict:
    """Translate raster-image text in a completed PDF and save atomically."""
    input_path = Path(input_pdf).resolve()
    output_path = Path(out_pdf).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = Path(cfg.get("vision_report_path") or
                       f"{output_path}.vision.json").resolve()
    source_lang = cfg.get("source_lang", "zh")
    target_lang = cfg.get("target_lang", "ru")
    min_confidence = float(cfg.get("vision_confidence_threshold", 0.65))
    min_image_size = int(cfg.get("vision_min_image_size", 64))
    min_font_size = int(cfg.get("vision_min_fontsize", 12))
    padding = int(cfg.get("vision_overlay_padding", 2))
    text_align = str(cfg.get("vision_text_align", "center")).lower()
    if text_align not in {"left", "center"}:
        text_align = "center"
    bbox_padding_ratio = float(cfg.get("vision_bbox_padding_ratio", 0.04))
    font_path = find_target_font(
        cfg.get("vision_font") or cfg.get("target_font"), target_lang)

    report = {
        "input_pdf": str(input_path),
        "output_pdf": str(output_path),
        "report_path": str(report_path),
        "processed": 0,
        "skipped": 0,
        "errors": [],
        "min_font": None,
        "regions_processed": 0,
        "regions_detected": 0,
        "regions_rejected": 0,
        "regions_skipped_overlay": 0,
        "images_total": 0,
        "unique_images": 0,
        "duplicate_xrefs": 0,
        "images_failed": 0,
        "cache_hits": 0,
        "attempts": 0,
        "retries": 0,
        "outcome": "running",
        "skip_reasons": {},
    }

    translator = VisionTranslator(cfg)
    if not translator.enabled:
        raise ValueError(
            "Vision postprocess is not configured: set vision_llm_model "
            "and vision_llm_base_url (or llm_base_url)")
    cache_path = cfg.get("vision_cache_path") or cfg.get("vision_cache_db")
    cache = VisionCache(str(cache_path)) if cache_path else None
    document = None
    temporary_output: Path | None = None
    try:
        document = fitz.open(str(input_path))
        xref_pages: dict[int, tuple[int, int]] = {}
        occurrences = 0
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            for image_info in page.get_images(full=True):
                xref = int(image_info[0])
                if xref <= 0:
                    continue
                smask = int(image_info[1] or 0)
                occurrences += 1
                xref_pages.setdefault(xref, (page_number, smask))
        report["images_total"] = occurrences
        report["unique_images"] = len(xref_pages)
        report["duplicate_xrefs"] = occurrences - len(xref_pages)

        total = len(xref_pages)
        for current, (xref, placement) in enumerate(xref_pages.items(), start=1):
            page_number, smask = placement
            state = "skipped"
            try:
                pixmap = fitz.Pixmap(document, xref)
                if smask > 0:
                    mask = fitz.Pixmap(document, smask)
                    try:
                        pixmap = fitz.Pixmap(pixmap, mask)
                    finally:
                        mask = None
                image = _pixmap_to_image(pixmap)
                if image.width < min_image_size or image.height < min_image_size:
                    _record_skip(report, "too_small")
                    continue
                regions = translator.translate_image(
                    image, source_lang, target_lang, cache)
                call_stats = dict(getattr(translator, "last_call", {}) or {})
                report["cache_hits"] += int(bool(call_stats.get("cached")))
                report["attempts"] += int(call_stats.get("attempts") or 0)
                report["retries"] += int(call_stats.get("retries") or 0)
                report["regions_detected"] += int(
                    call_stats.get("raw_regions") or 0
                )
                report["regions_rejected"] += int(
                    call_stats.get("rejected_regions") or 0
                )
                overlaid, overlay_report = overlay_regions(
                    image,
                    regions,
                    font_path,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    min_confidence=min_confidence,
                    min_font_size=min_font_size,
                    padding=padding,
                    text_align=text_align,
                    bbox_padding_ratio=bbox_padding_ratio,
                )
                report["regions_skipped_overlay"] += int(
                    overlay_report.get("skipped") or 0
                )
                if not overlay_report["modified"]:
                    reason = "no_valid_regions" if regions else "no_regions"
                    _record_skip(report, reason)
                    continue
                buffer = io.BytesIO()
                overlaid.save(buffer, format="PNG")
                document.load_page(page_number).replace_image(
                    xref, stream=buffer.getvalue())
                report["processed"] += 1
                report["regions_processed"] += overlay_report["processed"]
                used_size = overlay_report["min_font"]
                if used_size is not None:
                    report["min_font"] = (used_size if report["min_font"] is None
                                          else min(report["min_font"], used_size))
                state = "processed"
            except Exception as exc:
                report["errors"].append({
                    "page": page_number + 1,
                    "xref": xref,
                    "error": str(exc),
                })
                logger.warning("Vision image xref=%d page=%d: %s",
                               xref, page_number + 1, exc)
                report["images_failed"] += 1
                report["skip_reasons"]["error"] = (
                    report["skip_reasons"].get("error", 0) + 1
                )
                state = "error"
            finally:
                _notify(progress, logger, {
                    "stage": "vision_images",
                    "current": current,
                    "total": total,
                    "xref": xref,
                    "page": page_number + 1,
                    "state": state,
                })

        report["outcome"] = (
            "partial"
            if report["errors"] and report["processed"]
            else "failed"
            if report["errors"]
            else "success"
        )
        expected_page_count = document.page_count
        temporary_output = _temporary_pdf_path(output_path)
        document.save(str(temporary_output), garbage=4, deflate=True)
        document.close()
        document = None
        verification = fitz.open(str(temporary_output))
        try:
            if verification.page_count != expected_page_count:
                raise RuntimeError(
                    "Vision output validation failed: "
                    f"pages={verification.page_count}, expected={expected_page_count}")
            # Force page-tree access so a superficially openable but broken
            # document is rejected before replacing the destination.
            for page_number in range(verification.page_count):
                verification.load_page(page_number)
        finally:
            verification.close()
        os.replace(temporary_output, output_path)
        temporary_output = None
        _atomic_json(report_path, report)
        logger.info(
            "Vision postprocess: outcome=%s processed=%d skipped=%d failed=%d "
            "errors=%d cache_hits=%d retries=%d regions=%d/%d min_font=%s",
            report["outcome"], report["processed"], report["skipped"],
            report["images_failed"], len(report["errors"]),
            report["cache_hits"], report["retries"],
            report["regions_processed"], report["regions_detected"],
            report["min_font"],
        )
        return report
    finally:
        if document is not None:
            document.close()
        if cache is not None:
            cache.close()
        if temporary_output is not None:
            try:
                temporary_output.unlink()
            except OSError:
                pass
