from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import fitz

from pipeline.config.loader import load_config
from pipeline.fonts.fonts import find_target_font
from pipeline.io.artifacts import save_json
from pipeline.pdf.builder import (_insert_text_fit, _overflow_preview,
                                  _prepare_text_for_layout, _plan_page_layout,
                                  _resolve_font_for_seg, build)


LOGGER = logging.getLogger("test-builder")
LOGGER.addHandler(logging.NullHandler())


class BuilderUnitTests(unittest.TestCase):
    def test_soft_wraps_collapse_but_list_items_remain_logical(self):
        text = "Первая физическая\nстрока абзаца\n• пункт один\nпродолжение пункта"
        self.assertEqual(
            _prepare_text_for_layout(text, "paragraph", {}),
            "Первая физическая строка абзаца\n• пункт один продолжение пункта",
        )

    def test_failed_insert_does_not_draw_partial_microtext(self):
        doc = fitz.open()
        page = doc.new_page(width=100, height=100)
        size, status = _insert_text_fit(
            page, fitz.Rect(10, 10, 20, 15), "Очень длинный текст",
            "helv", None, 9, 9, fitz.TEXT_ALIGN_LEFT, (0, 0, 0),
        )
        self.assertEqual(status, 2)
        self.assertEqual(size, 9)
        self.assertEqual(page.get_text("text").strip(), "")
        doc.close()

    def test_small_source_style_never_forces_small_target_text(self):
        doc = fitz.open()
        page = doc.new_page(width=240, height=120)
        page.insert_text((20, 45), "Source label", fontsize=6)
        bbox = list(page.search_for("Source label")[0])
        seg = {
            "id": 1, "type": "paragraph", "page": 0, "bbox": bbox,
            "font": "Helvetica", "size": 6.0, "color": 0,
            "text": "Source label", "ru": "Читаемая подпись",
        }
        cfg = load_config()
        cfg.update({
            "source_lang": "en", "target_lang": "ru",
            "match_fonts": False, "builder_min_fontsize": 8.5,
        })
        plans, overflow = _plan_page_layout(
            page, [seg], cfg, "ru", find_target_font("", "ru"),
            LOGGER, 10.0,
        )
        if plans:
            self.assertGreaterEqual(plans[0]["size"], 8.5)
        else:
            self.assertEqual(len(overflow), 1)
            self.assertIsNotNone(overflow[0]["marker_size"])
        doc.close()

    def test_caption_anchors_use_safe_default_font(self):
        default = "C:/fonts/DejaVuSans.ttf"
        fontname, fontfile = _resolve_font_for_seg(
            {
                "type": "caption_fig",
                "font": "BookAntiqua",
                "anchors": [{"kind": "fig", "num": "3-1"}],
            },
            {"match_fonts": True},
            "ru",
            default,
            LOGGER,
        )
        self.assertEqual(default, fontfile)
        self.assertTrue(fontname.startswith("tgt_"))

    def test_overflow_preview_keeps_context_when_space_allows(self):
        doc = fitz.open()
        page = doc.new_page(width=260, height=100)
        preview, size = _overflow_preview(
            page,
            fitz.Rect(10, 10, 240, 45),
            "Readable translated context that continues in appendix",
            "[T7]",
            "helv",
            None,
            9.0,
            fitz.TEXT_ALIGN_LEFT,
        )
        self.assertIsNotNone(size)
        self.assertIn("Readable", preview)
        self.assertIn("[T7]", preview)
        doc.close()


class BuilderIntegrationTests(unittest.TestCase):
    def test_unfitted_text_moves_to_readable_appendix_without_graphics_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source.pdf"
            segments_path = root / "segments.json"
            out = root / "translated.pdf"

            doc = fitz.open()
            page = doc.new_page(width=300, height=220)
            shape = page.new_shape()
            shape.draw_rect(fitz.Rect(35, 35, 265, 90))
            shape.finish(fill=(0.85, 0.85, 0.85), color=(0.2, 0.2, 0.2))
            shape.commit()
            page.insert_text((45, 62), "Source", fontsize=12)
            bbox = list(page.search_for("Source")[0])
            doc.save(src)
            doc.close()

            save_json([{
                "id": 1,
                "type": "paragraph",
                "page": 0,
                "bbox": bbox,
                "section_id": None,
                "anchors": [],
                "font": "Helvetica",
                "size": 12.0,
                "color": 0,
                "table_idx": None,
                "text": "Source",
                "ru": "Полный читаемый перевод " * 40,
            }], segments_path)

            cfg = load_config()
            cfg.update({
                "pdf_path": str(src),
                "out_path": str(out),
                "source_lang": "en",
                "target_lang": "ru",
                "match_fonts": False,
                "builder_min_fontsize": 9.0,
                "builder_overflow_policy": "appendix",
                "enable_vision_ocr": False,
            })
            build(cfg, LOGGER, str(segments_path), str(out))

            report = json.loads(
                Path(str(out) + ".layout.json").read_text(encoding="utf-8"))
            self.assertEqual(report["notfit"], 0)
            self.assertEqual(report["lost"], 0)
            self.assertEqual(report["overflow_blocks"], 1)
            self.assertGreaterEqual(report["appendix_pages"], 1)

            result = fitz.open(out)
            self.assertGreater(result.page_count, 1)
            self.assertIn("[T1]", result[0].get_text("text"))
            self.assertTrue(result[0].get_drawings())
            target_sizes = []
            for page in result:
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if any("\u0400" <= ch <= "\u052f"
                                   for ch in span.get("text", "")):
                                target_sizes.append(float(span["size"]))
            self.assertTrue(target_sizes)
            self.assertGreaterEqual(min(target_sizes), 8.95)
            result.close()


if __name__ == "__main__":
    unittest.main()
