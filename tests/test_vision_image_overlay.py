import io
import logging
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageChops, ImageDraw

from pipeline.fonts.fonts import find_target_font
from pipeline.vision import image_overlay
from pipeline.vision.image_overlay import (_expanded_rect, overlay_regions,
                                           postprocess_images)


def _test_image() -> Image.Image:
    image = Image.new("RGB", (180, 90), (220, 225, 230))
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 150, 60), fill=(245, 245, 245))
    draw.rectangle((45, 35, 125, 48), fill=(40, 40, 40))
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_pdf(path: Path, *, duplicate: bool = False) -> int:
    document = fitz.open()
    page = document.new_page(width=240, height=140)
    xref = page.insert_image(fitz.Rect(20, 20, 200, 110),
                             stream=_png_bytes(_test_image()))
    if duplicate:
        second = document.new_page(width=240, height=140)
        second.insert_image(fitz.Rect(20, 20, 200, 110), xref=xref)
    document.save(path)
    document.close()
    return xref


def _first_image_samples(path: Path) -> bytes:
    document = fitz.open(path)
    try:
        xref = document[0].get_images(full=True)[0][0]
        return bytes(fitz.Pixmap(document, xref).samples)
    finally:
        document.close()


def _region(confidence: float = 0.99) -> dict:
    return {
        "bbox": [100, 180, 900, 800],
        "source_text": "\u6587\u672c XG-200",
        "translation": "\u0422\u0435\u043a\u0441\u0442 XG-200",
        "confidence": confidence,
    }


def test_pil_overlay_draws_opaque_translated_block_with_fitted_ttf():
    source = _test_image()
    result, report = overlay_regions(
        source,
        [_region()],
        find_target_font("", "ru"),
        source_lang="zh",
        target_lang="ru",
        min_font_size=8,
        padding=3,
    )

    assert report["modified"] is True
    assert report["processed"] == 1
    assert report["min_font"] >= 8
    assert ImageChops.difference(source.convert(result.mode), result).getbbox()


def test_tight_vision_bbox_gets_proportional_outer_padding():
    assert _expanded_rect((252, 112, 944, 203), 1200, 360, 3, 0.04) == (
        224, 108, 972, 207,
    )


def test_invalid_or_low_confidence_region_leaves_image_unchanged():
    source = _test_image()
    invalid = _region(confidence=0.1)
    invalid["bbox"] = [500, 500, 500, 700]

    result, report = overlay_regions(
        source,
        [_region(confidence=0.1), invalid],
        find_target_font("", "ru"),
        source_lang="zh",
        target_lang="ru",
        min_confidence=0.65,
    )

    assert report["modified"] is False
    assert report["processed"] == 0
    assert result.mode == source.mode
    assert result.tobytes() == source.tobytes()


def test_postprocess_deduplicates_shared_xref_and_reports_progress(tmp_path,
                                                                  monkeypatch):
    source_pdf = tmp_path / "source.pdf"
    output_pdf = tmp_path / "output.pdf"
    _make_pdf(source_pdf, duplicate=True)

    class _FakeTranslator:
        calls = 0

        def __init__(self, cfg):
            self.enabled = True

        def translate_image(self, image, src_lang, tgt_lang, cache=None):
            type(self).calls += 1
            return [_region()]

    monkeypatch.setattr(image_overlay, "VisionTranslator", _FakeTranslator)
    updates = []
    report = postprocess_images(
        source_pdf,
        output_pdf,
        {
            "source_lang": "zh",
            "target_lang": "ru",
            "vision_min_image_size": 1,
            "target_font": find_target_font("", "ru"),
        },
        logging.getLogger("vision-test"),
        progress=updates.append,
    )

    assert _FakeTranslator.calls == 1
    assert report["processed"] == 1
    assert report["duplicate_xrefs"] == 1
    assert report["errors"] == []
    assert len(updates) == 1 and updates[0]["state"] == "processed"
    assert output_pdf.exists()
    assert Path(report["report_path"]).exists()


def test_postprocess_invalid_region_does_not_replace_raster(tmp_path,
                                                            monkeypatch):
    source_pdf = tmp_path / "source.pdf"
    output_pdf = tmp_path / "output.pdf"
    _make_pdf(source_pdf)
    before = _first_image_samples(source_pdf)

    class _InvalidTranslator:
        def __init__(self, cfg):
            self.enabled = True

        def translate_image(self, image, src_lang, tgt_lang, cache=None):
            bad = _region(confidence=0.2)
            bad["bbox"] = [-10, 100, -10, 900]
            return [bad]

    monkeypatch.setattr(image_overlay, "VisionTranslator", _InvalidTranslator)
    report = postprocess_images(
        source_pdf,
        output_pdf,
        {
            "source_lang": "zh",
            "target_lang": "ru",
            "vision_min_image_size": 1,
            "target_font": find_target_font("", "ru"),
        },
        logging.getLogger("vision-test"),
    )

    assert report["processed"] == 0
    assert report["skipped"] == 1
    assert _first_image_samples(output_pdf) == before


def test_postprocess_requires_explicit_vision_model(tmp_path):
    source_pdf = tmp_path / "source.pdf"
    _make_pdf(source_pdf)

    with pytest.raises(ValueError, match="vision_llm_model"):
        postprocess_images(
            source_pdf,
            tmp_path / "output.pdf",
            {
                "llm_base_url": "http://127.0.0.1:8080/v1",
                "llm_model": "text-only",
                "target_font": find_target_font("", "ru"),
            },
            logging.getLogger("vision-test"),
        )
