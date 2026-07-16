from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import fitz

from pipeline.fonts.fonts import find_target_font
from pipeline.pdf.validator import (
    _invalid_text_markers,
    _normalize_anchor_text,
    validate,
)


class _Logger:
    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.error_lines: list[str] = []

    @staticmethod
    def _format(message, args) -> str:
        return message % args if args else str(message)

    def info(self, message, *args) -> None:
        self.info_lines.append(self._format(message, args))

    def error(self, message, *args) -> None:
        self.error_lines.append(self._format(message, args))


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.target_font = find_target_font("")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logger = _Logger()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pdf(self, name: str, pages: list[tuple[str, float, str]]) -> Path:
        path = self.root / name
        doc = fitz.open()
        for text, size, script in pages:
            page = doc.new_page(width=595, height=842)
            if not text:
                continue
            if script == "han":
                page.insert_textbox(
                    fitz.Rect(72, 72, 523, 770), text,
                    fontsize=size, fontname="china-s")
            else:
                page.insert_textbox(
                    fitz.Rect(72, 72, 523, 770), text,
                    fontsize=size, fontname="TargetFont",
                    fontfile=self.target_font)
        doc.save(path)
        doc.close()
        return path

    def _pdf_with_footer(self, name: str, body: tuple[str, float, str],
                         footer: tuple[str, float, str]) -> Path:
        path = self.root / name
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for text, size, script, point in (
                (*body, (72, 100)), (*footer, (72, 810))):
            if script == "han":
                page.insert_text(point, text, fontsize=size, fontname="china-s")
            else:
                page.insert_text(point, text, fontsize=size,
                                 fontname="TargetFont", fontfile=self.target_font)
        doc.save(path)
        doc.close()
        return path

    @staticmethod
    def _cfg(**updates) -> dict:
        cfg = {
            "source_lang": "zh",
            "target_lang": "ru",
            "validator_min_readable_fontsize": 8.0,
            "metadata": {},
        }
        cfg.update(updates)
        return cfg

    def _validate(self, src: Path, out: Path, **cfg) -> int:
        return validate(str(src), str(out), self.logger, self._cfg(**cfg))

    def test_six_point_cyrillic_fails(self) -> None:
        src = self._pdf("src.pdf", [("测试 исходного текста", 12, "han")])
        out = self._pdf("out.pdf", [("Переведенный технический текст", 6, "target")])
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("мелкий" in line for line in self.logger.error_lines))

    def test_nine_point_cyrillic_passes(self) -> None:
        src = self._pdf("src.pdf", [("测试 исходного текста", 12, "han")])
        out = self._pdf("out.pdf", [("Переведенный технический текст", 9, "target")])
        self.assertEqual(0, self._validate(src, out))

    def test_preexisting_small_service_footer_is_ignored(self) -> None:
        src = self._pdf_with_footer(
            "src.pdf", ("测试正文内容", 12, "han"), ("服务标记", 6, "han"))
        out = self._pdf_with_footer(
            "out.pdf", ("Переведенный основной текст", 9, "target"),
            ("Метка", 6, "target"))
        self.assertEqual(0, self._validate(src, out))

    def test_residual_han_fails(self) -> None:
        src = self._pdf("src.pdf", [("测试源文本内容", 12, "han")])
        out = self._pdf("out.pdf", [("Перевод 测试", 9, "han")])
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("Han" in line for line in self.logger.error_lines))

    def test_invalid_text_markers_are_detected(self) -> None:
        self.assertEqual(["U+FFFD", "NUL"],
                         _invalid_text_markers("повреждено\ufffd\x00"))

    def test_sharp_page_text_loss_fails(self) -> None:
        src_text = "测试源文本文档页面内容" * 5
        src = self._pdf("src.pdf", [(src_text, 12, "han")])
        out = self._pdf("out.pdf", [("Кратко", 9, "target")])
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("Резкая потеря" in line for line in self.logger.error_lines))

    def test_layout_report_notfit_fails(self) -> None:
        src = self._pdf("src.pdf", [("测试源文本内容", 12, "han")])
        out = self._pdf("out.pdf", [("Качественный перевод", 9, "target")])
        report = Path(str(out) + ".layout.json")
        report.write_text(json.dumps({"summary": {"lost": 0, "notfit": 1}}),
                          encoding="utf-8")
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("notfit" in line for line in self.logger.error_lines))

    def test_layout_report_retained_source_fails(self) -> None:
        src = self._pdf("src.pdf", [("测试源文本内容", 12, "han")])
        out = self._pdf("out.pdf", [("Качественный перевод", 9, "target")])
        report = Path(str(out) + ".layout.json")
        report.write_text(json.dumps({
            "source_retained_blocks": 1,
            "lost": 0,
            "notfit": 0,
        }), encoding="utf-8")
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("source_retained" in line
                            for line in self.logger.error_lines))

    def test_repeated_anchor_count_must_match_exactly(self) -> None:
        src = self._pdf("src.pdf", [("图1-1 и 图1-1", 12, "han")])
        out = self._pdf("out.pdf", [("Рис. 1-1", 9, "target")])
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("Якоря fig" in line for line in self.logger.error_lines))

    def test_anchor_soft_hyphen_is_normalized(self) -> None:
        self.assertEqual(
            "Рис. 3-1; Табл. 2-1",
            _normalize_anchor_text("Рис. 3\u00ad1; Табл. 2\u20111"),
        )

    def test_empty_translated_page_fails(self) -> None:
        src = self._pdf("src.pdf", [("测试源文本文档页面", 12, "han")])
        out = self._pdf("out.pdf", [("", 9, "target")])
        self.assertEqual(1, self._validate(src, out))
        self.assertTrue(any("Пустые" in line for line in self.logger.error_lines))

    def test_overflow_appendix_extra_page_is_allowed(self) -> None:
        src = self._pdf("src.pdf", [("测试源文本内容", 12, "han")])
        out = self._pdf("out.pdf", [
            ("Качественный перевод", 9, "target"),
            ("Продолжение перевода в приложении", 9, "target"),
        ])
        report = Path(str(out) + ".layout.json")
        report.write_text(json.dumps({"overflow": 1, "lost": 0, "notfit": 0}),
                          encoding="utf-8")
        self.assertEqual(0, self._validate(src, out))

    def test_reported_overflow_can_move_long_text_to_appendix(self) -> None:
        src_text = "测试源文本文档页面内容" * 10
        src = self._pdf("src.pdf", [(src_text, 12, "han")])
        out = self._pdf("out.pdf", [
            ("Перевод приведен в приложении", 9, "target"),
            ("Полный качественный перевод технического текста " * 8, 9, "target"),
        ])
        report = Path(str(out) + ".layout.json")
        report.write_text(json.dumps({
            "appendix_pages": 1,
            "overflow_blocks": 1,
            "notfit": 0,
            "lost": 0,
            "overflow": [{"page": 0, "marker_inserted": True,
                          "source_retained": False}],
        }), encoding="utf-8")
        self.assertEqual(0, self._validate(src, out))


if __name__ == "__main__":
    unittest.main()
