from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz

import app.cli as cli
import pipeline.io.artifacts as artifacts


class ArtifactResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(artifacts, "ROOT", self.root)
        self.root_patch.start()

        for rel in {
            rel
            for files in artifacts._STAGE_FILES.values()
            for rel in files
        }:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {rel}\n", encoding="utf-8")

        self.glossary = self.root / "glossary.csv"
        self.glossary.write_text("zh,ru\n软件,ПО\n", encoding="utf-8")
        self.cfg = {
            "tmp_dir": "intermediate",
            "log_dir": "log",
            "source_lang": "zh",
            "target_lang": "ru",
            "anchors": {"fig": "Рис."},
            "bullet_chars": ["•"],
            "bullet_replace": "-",
            "remove_pua": True,
            "section_patterns": [r"^第.+章$"],
            "llm_base_url": "http://test.invalid/v1",
            "llm_model": "model-a",
            "temperature": 0.1,
            "top_p": 0.9,
            "max_tokens": 1024,
            "enable_thinking": False,
            "llm_batch_max_items": 8,
            "llm_batch_max_chars": 4000,
            "translation_max_attempts": 3,
            "llm_max_attempts": 3,
            "glossary_path": str(self.glossary),
            "markdown_max_tokens": 2048,
            "markdown_temperature": 0.2,
            "markdown_top_p": 0.8,
            "markdown_system_prompt": "Translate exactly.",
            "pdf_path": str(self.root / "source.pdf"),
            "out_path": str(self.root / "translated.pdf"),
        }
        self.src_hash = "0123456789abcdef"

    def tearDown(self) -> None:
        self.root_patch.stop()
        self.tmp.cleanup()

    def test_atomic_save_keeps_original_and_removes_failed_temp(self) -> None:
        target = self.root / "state.json"
        artifacts.save_json({"state": "good"}, target)

        with self.assertRaises(TypeError):
            artifacts.save_json({"bad": object()}, target)

        self.assertEqual({"state": "good"}, artifacts.load_json(target))
        self.assertEqual([], list(self.root.glob("state.json.*.tmp")))

    def test_signatures_invalidate_on_model_language_glossary_and_code(self) -> None:
        parse_sig = artifacts.stage_signature(self.cfg, self.src_hash, "parse")
        segment_sig = artifacts.stage_signature(self.cfg, self.src_hash, "segment")
        translate_sig = artifacts.stage_signature(self.cfg, self.src_hash, "translate")
        markdown_sig = artifacts.stage_signature(self.cfg, self.src_hash, "markdown")

        model_cfg = dict(self.cfg, llm_model="model-b")
        self.assertEqual(
            segment_sig,
            artifacts.stage_signature(model_cfg, self.src_hash, "segment"),
        )
        self.assertNotEqual(
            translate_sig,
            artifacts.stage_signature(model_cfg, self.src_hash, "translate"),
        )
        self.assertNotEqual(
            markdown_sig,
            artifacts.stage_signature(model_cfg, self.src_hash, "markdown"),
        )

        lang_cfg = dict(self.cfg, target_lang="en")
        self.assertEqual(
            parse_sig,
            artifacts.stage_signature(lang_cfg, self.src_hash, "parse"),
        )
        self.assertNotEqual(
            segment_sig,
            artifacts.stage_signature(lang_cfg, self.src_hash, "segment"),
        )
        self.assertNotEqual(
            translate_sig,
            artifacts.stage_signature(lang_cfg, self.src_hash, "translate"),
        )
        self.assertNotEqual(
            markdown_sig,
            artifacts.stage_signature(lang_cfg, self.src_hash, "markdown"),
        )

        self.glossary.write_text("zh,ru\n软件,программа\n", encoding="utf-8")
        self.assertNotEqual(
            translate_sig,
            artifacts.stage_signature(self.cfg, self.src_hash, "translate"),
        )
        self.assertEqual(
            segment_sig,
            artifacts.stage_signature(self.cfg, self.src_hash, "segment"),
        )

        translator_path = self.root / "pipeline/translate/translator.py"
        translate_before_code = artifacts.stage_signature(
            self.cfg, self.src_hash, "translate"
        )
        translator_path.write_text("# changed translator\n", encoding="utf-8")
        self.assertNotEqual(
            translate_before_code,
            artifacts.stage_signature(self.cfg, self.src_hash, "translate"),
        )

        builder_path = self.root / "pipeline/pdf/builder.py"
        segment_before_builder = artifacts.stage_signature(
            self.cfg, self.src_hash, "segment"
        )
        translate_before_builder = artifacts.stage_signature(
            self.cfg, self.src_hash, "translate"
        )
        builder_path.write_text("# changed marker normalizer\n", encoding="utf-8")
        self.assertNotEqual(
            segment_before_builder,
            artifacts.stage_signature(self.cfg, self.src_hash, "segment"),
        )
        self.assertNotEqual(
            translate_before_builder,
            artifacts.stage_signature(self.cfg, self.src_hash, "translate"),
        )

        patterns_cfg = dict(self.cfg, section_patterns=[r"^Chapter \d+$"])
        self.assertNotEqual(
            artifacts.stage_signature(self.cfg, self.src_hash, "segment"),
            artifacts.stage_signature(patterns_cfg, self.src_hash, "segment"),
        )

    def test_manifest_checks_digest_and_accepts_legacy_entry(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        artifacts.save_json([{"id": 1, "ru": "Перевод"}], ap["segments_ru"])
        artifacts.mark_stage_done(self.cfg, self.src_hash, "translate")
        self.assertTrue(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

        artifacts.save_json([{"id": 1, "ru": "Изменено"}], ap["segments_ru"])
        self.assertFalse(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

        legacy = {
            "source_hash": self.src_hash,
            "stages": {
                "translate": {
                    "signature": artifacts.stage_signature(
                        self.cfg, self.src_hash, "translate"
                    )
                }
            },
        }
        artifacts.save_json(legacy, ap["manifest"])
        self.assertTrue(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

        legacy["source_hash"] = "different-source"
        artifacts.save_json(legacy, ap["manifest"])
        self.assertFalse(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

    def test_mark_done_recovers_from_non_mapping_manifest(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        artifacts.save_json([], ap["segments"])
        artifacts.save_json([], ap["manifest"])

        artifacts.mark_stage_done(self.cfg, self.src_hash, "segment")

        manifest = artifacts.load_json(ap["manifest"])
        self.assertEqual(self.src_hash, manifest["source_hash"])
        self.assertIs(manifest["stages"]["segment"]["complete"], True)
        self.assertTrue(artifacts.stage_done(self.cfg, self.src_hash, "segment"))

    def test_incomplete_marker_disables_resume(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        artifacts.save_json([], ap["segments_ru"])
        artifacts.mark_stage_done(self.cfg, self.src_hash, "translate")
        artifacts.mark_stage_incomplete(
            self.cfg, self.src_hash, "translate", reason="limit", limit=1
        )

        self.assertFalse(artifacts.stage_done(self.cfg, self.src_hash, "translate"))
        manifest = artifacts.load_json(ap["manifest"])
        entry = manifest["stages"]["translate"]
        self.assertIs(entry["complete"], False)
        self.assertEqual({"reason": "limit", "limit": 1}, entry["details"])

    def test_limited_translate_keeps_shape_and_invalidates_old_completion(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        segments = [
            {"id": 1, "text": "一"},
            {"id": 2, "text": "二"},
            {"id": 3, "text": "三"},
        ]
        artifacts.save_json(segments, ap["segments"])
        artifacts.save_json(
            [dict(item, ru=f"old-{item['id']}") for item in segments],
            ap["segments_ru"],
        )
        artifacts.mark_stage_done(self.cfg, self.src_hash, "translate")
        self.assertTrue(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

        translated_ids: list[int] = []

        class FakeTranslator:
            def __init__(self, *_args, **_kwargs):
                pass

            def translate_all(self, selected):
                translated_ids.extend(item["id"] for item in selected)
                return {item["id"]: f"RU-{item['id']}" for item in selected}

            def close(self):
                pass

        args = SimpleNamespace(inp=None, out=None, limit=1)
        with patch.object(
            cli, "_mod", return_value=SimpleNamespace(Translator=FakeTranslator)
        ):
            ok = cli.run_stage(
                "translate", self.cfg, Mock(), args, ap, self.src_hash
            )

        self.assertTrue(ok)
        self.assertEqual([1], translated_ids)
        saved = artifacts.load_json(ap["segments_ru"])
        self.assertEqual(3, len(saved))
        self.assertEqual(["RU-1", "二", "三"], [item["ru"] for item in saved])
        self.assertFalse(artifacts.stage_done(self.cfg, self.src_hash, "translate"))

    def test_translate_failures_stop_pipeline_and_mark_stage_incomplete(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        artifacts.save_json([
            {"id": 1, "text": "一"},
            {"id": 2, "text": "二"},
        ], ap["segments"])

        class FakeTranslator:
            def __init__(self, *_args, **_kwargs):
                self.last_stats = {
                    "ok": 1, "cached": 0, "fail": 1, "batches": 1,
                    "failed_ids": [2],
                }

            def translate_all(self, _selected):
                return {1: "Один", 2: "二"}

            def close(self):
                pass

        args = SimpleNamespace(inp=None, out=None, limit=0)
        with patch.object(
            cli, "_mod", return_value=SimpleNamespace(Translator=FakeTranslator)
        ):
            ok = cli.run_stage(
                "translate", self.cfg, Mock(), args, ap, self.src_hash
            )

        self.assertFalse(ok)
        saved = artifacts.load_json(ap["segments_ru"])
        self.assertEqual(["Один", "二"], [item["ru"] for item in saved])
        manifest = artifacts.load_json(ap["manifest"])
        entry = manifest["stages"]["translate"]
        self.assertIs(entry["complete"], False)
        self.assertEqual("failed_segments", entry["details"]["reason"])
        self.assertEqual(1, entry["details"]["count"])
        self.assertEqual([2], entry["details"]["ids"])

    def test_markdown_resume_is_invalidated_by_model_change(self) -> None:
        ap = artifacts.artifact_paths(self.cfg, self.src_hash)
        artifacts.save_json({"0": "Старый перевод"}, ap["pages_md"])
        artifacts.mark_stage_done(self.cfg, self.src_hash, "markdown")

        changed_cfg = dict(self.cfg, llm_model="model-b")
        args = SimpleNamespace(inp=None, out=None, resume=True, limit=0)
        translate_pdf = Mock(return_value={0: "Новый перевод"})
        build_pdf = Mock()
        validate = Mock(return_value=0)
        with (
            patch("pipeline.markdown.translator.translate_pdf", translate_pdf),
            patch("pipeline.markdown.builder.build_pdf", build_pdf),
            patch("pipeline.pdf.validator.validate", validate),
        ):
            ok = cli.run_markdown(
                changed_cfg, Mock(), args, ap, self.src_hash
            )

        self.assertTrue(ok)
        self.assertIs(translate_pdf.call_args.kwargs["resume"], False)
        self.assertTrue(
            artifacts.stage_done(changed_cfg, self.src_hash, "markdown")
        )


class ImagePostprocessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"vision_llm_model": "vision-model"}
        self.logger = Mock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _make_pdf(path: Path, pages: int) -> None:
        document = fitz.open()
        for _ in range(pages):
            document.new_page()
        document.save(path)
        document.close()

    def test_image_postprocess_emits_json_progress_and_validates_output(self) -> None:
        source = self.root / "base.pdf"
        output = self.root / "result.pdf"
        self._make_pdf(source, 2)

        def fake_postprocess(input_pdf, out_pdf, _cfg, _logger, progress):
            document = fitz.open(input_pdf)
            document.save(out_pdf)
            document.close()
            progress({
                "stage": "vision_images",
                "current": 1,
                "total": 1,
            })
            return {"processed": 1}

        args = SimpleNamespace(
            image_postprocess=str(source), out=str(output)
        )
        stdout = io.StringIO()
        with (
            patch(
                "pipeline.vision.image_overlay.postprocess_images",
                side_effect=fake_postprocess,
            ) as postprocess,
            redirect_stdout(stdout),
        ):
            ok = cli.run_image_postprocess(
                self.cfg, self.logger, args
            )

        self.assertTrue(ok)
        self.assertTrue(output.is_file())
        postprocess.assert_called_once()
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("@@VISION@@") for line in lines))
        self.assertEqual(
            {"stage": "vision_images", "current": 1, "total": 1},
            json.loads(lines[0].removeprefix("@@VISION@@")),
        )
        self.assertEqual(
            "summary",
            json.loads(lines[1].removeprefix("@@VISION@@"))["event"],
        )

    def test_image_postprocess_rejects_unsafe_or_unconfigured_output(self) -> None:
        source = self.root / "base.pdf"
        self._make_pdf(source, 1)

        cases = [
            (
                {},
                SimpleNamespace(image_postprocess=str(source), out=str(self.root / "a.pdf")),
            ),
            (
                self.cfg,
                SimpleNamespace(image_postprocess=str(source), out=str(source)),
            ),
        ]
        existing = self.root / "existing.pdf"
        self._make_pdf(existing, 1)
        cases.append((
            self.cfg,
            SimpleNamespace(image_postprocess=str(source), out=str(existing)),
        ))

        with patch(
            "pipeline.vision.image_overlay.postprocess_images"
        ) as postprocess:
            for cfg, args in cases:
                with self.subTest(cfg=cfg, out=args.out):
                    self.assertFalse(
                        cli.run_image_postprocess(cfg, self.logger, args)
                    )
            postprocess.assert_not_called()

    def test_image_postprocess_rejects_page_count_change(self) -> None:
        source = self.root / "base.pdf"
        output = self.root / "result.pdf"
        self._make_pdf(source, 2)

        def fake_postprocess(_input_pdf, out_pdf, _cfg, _logger, progress):
            self._make_pdf(Path(out_pdf), 1)
            return {}

        args = SimpleNamespace(
            image_postprocess=str(source), out=str(output)
        )
        with patch(
            "pipeline.vision.image_overlay.postprocess_images",
            side_effect=fake_postprocess,
        ):
            ok = cli.run_image_postprocess(self.cfg, self.logger, args)

        self.assertFalse(ok)
        self.assertFalse(output.exists())
        self.logger.exception.assert_called()

    def test_image_postprocess_rejects_partial_report_by_default(self) -> None:
        source = self.root / "base.pdf"
        output = self.root / "result.pdf"
        self._make_pdf(source, 1)

        def fake_postprocess(input_pdf, out_pdf, _cfg, _logger, progress):
            document = fitz.open(input_pdf)
            document.save(out_pdf)
            document.close()
            report_path = Path(str(out_pdf) + ".vision.json")
            report_path.write_text(
                json.dumps({"errors": [{"xref": 7}], "processed": 1}),
                encoding="utf-8",
            )
            return {
                "errors": [{"xref": 7}],
                "processed": 1,
                "report_path": str(report_path),
            }

        args = SimpleNamespace(
            image_postprocess=str(source), out=str(output)
        )
        with patch(
            "pipeline.vision.image_overlay.postprocess_images",
            side_effect=fake_postprocess,
        ):
            ok = cli.run_image_postprocess(self.cfg, self.logger, args)

        self.assertFalse(ok)
        self.assertFalse(output.exists())
        self.assertTrue(Path(str(output) + ".vision.json").exists())

    def test_main_handles_image_mode_before_source_hash_with_exit_0_or_2(self) -> None:
        for result, expected_exit in ((True, 0), (False, 2)):
            with self.subTest(result=result):
                logger = Mock()
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "app.cli",
                            "--image-postprocess", "base.pdf",
                            "--out", "result.pdf",
                        ],
                    ),
                    patch.object(cli, "load_config", return_value=self.cfg),
                    patch.object(cli, "ensure_dirs"),
                    patch.object(cli, "setup_logger", return_value=logger),
                    patch.object(
                        cli, "run_image_postprocess", return_value=result
                    ) as run_image,
                    patch.object(cli, "source_hash") as source_hash,
                ):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()
                self.assertEqual(expected_exit, raised.exception.code)
                run_image.assert_called_once()
                source_hash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
