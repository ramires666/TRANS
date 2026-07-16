from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fitz
from fastapi.testclient import TestClient

from app import web


def _make_pdf(path: Path, pages: int = 1, label: str = "PDF") -> None:
    doc = fitz.open()
    for page_no in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{label} {page_no + 1}")
    doc.save(path)
    doc.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FakeProcess:
    def __init__(self, lines=(), returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.terminated = False

    def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class WebImagePostprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.uploads = Path(self.temp.name) / "uploads"
        self.uploads.mkdir()
        self.upload_patch = patch.object(web, "UPLOADS", self.uploads)
        self.upload_patch.start()
        self.sem_patch = patch.object(
            web, "IMAGE_POST_SEMAPHORE", threading.Semaphore(1))
        self.sem_patch.start()
        with web.JOBS_LOCK:
            web.JOBS.clear()
        self.client = TestClient(web.app)

    def tearDown(self):
        with web.JOBS_LOCK:
            jobs = list(web.JOBS.values())
            web.JOBS.clear()
        for job in jobs:
            proc = job.get("image_post", {}).get("proc")
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.sem_patch.stop()
        self.upload_patch.stop()
        self.temp.cleanup()

    def _job(self, job_id: str = "abc123", *, state: str = "done",
             base: Path | None = None) -> dict:
        base = base or (self.uploads / f"{job_id}_translated.pdf")
        if not base.exists():
            _make_pdf(base)
        job = {
            "job_id": job_id, "src": str(base), "out_path": str(base),
            "state": state, "stage": "validate", "progress": 1, "total": 1,
            "ok": 1, "cached": 0, "fail": 0, "pages": 1, "images": 1,
            "logs": [], "result_path": str(base),
            "base_result_path": str(base), "proc": None, "cancel": False,
            "image_post": web._new_image_post_state(),
        }
        with web.JOBS_LOCK:
            web.JOBS[job_id] = job
        return job

    def _queue(self, job: dict) -> tuple[Path, Path]:
        base = Path(job["base_result_path"])
        final, partial, download_name = web._image_output_paths(job["job_id"], base)
        image_post = web._new_image_post_state()
        image_post.update({
            "state": "queued", "phase": "queue", "base_path": str(base),
            "partial_path": str(partial), "final_path": str(final),
            "download_name": download_name,
        })
        job["image_post"] = image_post
        return final, partial

    def test_capability_requires_explicit_vision_model_and_import(self):
        with patch.object(web.importlib, "import_module") as importer:
            available, reason = web._image_postprocess_capability(
                {"llm_model": "text-model", "vision_llm_model": ""})
        self.assertFalse(available)
        self.assertIn("vision_llm_model", reason)
        importer.assert_not_called()

        with patch.object(web.importlib, "import_module", return_value=object()):
            self.assertEqual(
                web._image_postprocess_capability({"vision_llm_model": "vision"}),
                (True, ""),
            )
        with patch.object(web.importlib, "import_module", side_effect=ImportError):
            available, _ = web._image_postprocess_capability(
                {"vision_llm_model": "vision"})
        self.assertFalse(available)

    def test_main_job_paths_are_unique_and_download_name_is_clean(self):
        first_id = "a" * 12
        second_id = "b" * 12
        source = self.uploads / f"{first_id}_manual.pdf"
        _make_pdf(source)
        with patch.object(web, "load_config", return_value={"target_lang": "ru"}):
            first = web._new_job(str(source), first_id)
            second = web._new_job(str(source), second_id)

        self.assertNotEqual(first["out_path"], second["out_path"])
        self.assertEqual(Path(first["out_path"]).parent, self.uploads.resolve())
        self.assertTrue(Path(first["out_path"]).name.startswith(first_id + "_"))
        self.assertEqual(first["download_name"], "manual_RU.pdf")

    def test_main_start_rejects_paths_not_owned_by_upload_job(self):
        job_id = "c" * 12
        owned = self.uploads / f"{job_id}_manual.pdf"
        wrong = self.uploads / f"{'d' * 12}_manual.pdf"
        outside = Path(self.temp.name) / f"{job_id}_outside.pdf"
        for path in (owned, wrong, outside):
            _make_pdf(path)
        with patch.object(web, "_run_pipeline") as runner:
            accepted = self.client.post(
                "/api/start", json={"job": job_id, "src": str(owned)})
            wrong_job = self.client.post(
                "/api/start", json={"job": job_id, "src": str(wrong)})
            outside_result = self.client.post(
                "/api/start", json={"job": job_id, "src": str(outside)})

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(wrong_job.status_code, 403)
        self.assertEqual(outside_result.status_code, 404)
        runner.assert_called_once()

    def test_start_uses_only_server_paths_and_rejects_duplicate(self):
        job = self._job()
        runner = Mock()
        with patch.object(web, "_image_postprocess_capability",
                          return_value=(True, "")), \
             patch.object(web, "_run_image_postprocess", runner):
            response = self.client.post(
                "/api/jobs/abc123/image-postprocess",
                json={"base": "../../evil.pdf", "out": "C:/evil.pdf"},
            )
            duplicate = self.client.post("/api/jobs/abc123/image-postprocess")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(duplicate.status_code, 409)
        image_post = job["image_post"]
        self.assertEqual(image_post["state"], "queued")
        self.assertEqual(Path(image_post["final_path"]).parent, self.uploads.resolve())
        self.assertIn("abc123_", Path(image_post["final_path"]).name)
        self.assertNotIn("evil", image_post["final_path"].lower())
        self.assertEqual(job["result_path"], job["base_result_path"])
        runner.assert_called_once_with(job)

    def test_start_guards_main_state_capability_and_upload_boundary(self):
        self._job("running", state="running")
        with patch.object(web, "_image_postprocess_capability",
                          return_value=(True, "")):
            response = self.client.post("/api/jobs/running/image-postprocess")
        self.assertEqual(response.status_code, 409)

        outside = Path(self.temp.name) / "outside.pdf"
        _make_pdf(outside)
        self._job("outside", base=outside)
        with patch.object(web, "_image_postprocess_capability",
                          return_value=(True, "")):
            response = self.client.post("/api/jobs/outside/image-postprocess")
        self.assertEqual(response.status_code, 403)

        self._job("disabled")
        with patch.object(web, "_image_postprocess_capability",
                          return_value=(False, "not configured")):
            response = self.client.post("/api/jobs/disabled/image-postprocess")
        self.assertEqual(response.status_code, 503)

    def test_runner_promotes_valid_pdf_and_keeps_base_immutable(self):
        job = self._job()
        final, partial = self._queue(job)
        base = Path(job["base_result_path"])
        base_digest = _sha256(base)
        calls = []

        def popen(cmd, **kwargs):
            calls.append((cmd, kwargs))
            _make_pdf(Path(cmd[-1]), label="enhanced")
            Path(str(cmd[-1]) + ".vision.json").write_text(
                '{"processed": 1}', encoding="utf-8")
            return _FakeProcess([
                '@@VISION@@{"event":"progress","phase":"ocr",'
                '"current":1,"total":2,"ok":1,"cached":1,"failed":0}',
                "image progress 2/2",
            ])

        with patch.object(web.subprocess, "Popen", side_effect=popen):
            web._run_image_postprocess(job)

        self.assertEqual(job["state"], "done")
        self.assertFalse(job["cancel"])
        self.assertEqual(job["result_path"], str(base))
        self.assertEqual(_sha256(base), base_digest)
        self.assertEqual(job["image_post"]["state"], "done")
        self.assertEqual(job["image_post"]["phase"], "done")
        self.assertEqual(job["image_post"]["progress"], 2)
        self.assertEqual(job["image_post"]["cached"], 1)
        self.assertEqual(job["image_post"]["result_path"], str(final))
        self.assertTrue(final.is_file())
        self.assertTrue(Path(str(final) + ".vision.json").is_file())
        self.assertFalse(partial.exists())
        self.assertFalse(Path(str(partial) + ".vision.json").exists())
        self.assertEqual(calls[0][0], [
            web.sys.executable, "-m", "app.cli", "--image-postprocess",
            str(base), "--out", str(partial),
        ])

    def test_invalid_pdf_is_not_promoted(self):
        job = self._job()
        final, partial = self._queue(job)
        base = Path(job["base_result_path"])
        base_digest = _sha256(base)

        def popen(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"not a pdf")
            return _FakeProcess(["phase=validate"])

        with patch.object(web.subprocess, "Popen", side_effect=popen):
            web._run_image_postprocess(job)

        self.assertEqual(job["image_post"]["state"], "error")
        self.assertFalse(final.exists())
        self.assertFalse(partial.exists())
        self.assertEqual(_sha256(base), base_digest)
        self.assertEqual(job["state"], "done")

    def test_image_pdf_validation_rejects_added_pages(self):
        base = self.uploads / "base-one-page.pdf"
        candidate = self.uploads / "candidate-two-pages.pdf"
        _make_pdf(base, label="base")
        document = fitz.open()
        document.new_page()
        document.new_page()
        document.save(candidate)
        document.close()

        valid, reason, pages = web._validate_image_pdf(base, candidate)

        self.assertFalse(valid)
        self.assertIn("changed page count", reason)
        self.assertEqual(2, pages)

    def test_cancel_is_nested_and_does_not_cancel_main_job(self):
        job = self._job()
        proc = Mock()
        job["image_post"].update({"state": "running", "proc": proc})

        response = self.client.post(
            "/api/jobs/abc123/image-postprocess/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(job["image_post"]["cancel"])
        proc.terminate.assert_called_once_with()
        self.assertEqual(job["state"], "done")
        self.assertFalse(job["cancel"])
        self.assertEqual(job["result_path"], job["base_result_path"])

    def test_result_variants_and_status_do_not_expose_derivative_path(self):
        job = self._job()
        missing = self.client.get(
            "/api/jobs/abc123/result?variant=images")
        self.assertEqual(missing.status_code, 404)

        final, _ = self._queue(job)
        _make_pdf(final, label="enhanced")
        job["image_post"].update({"state": "done", "result_path": str(final)})
        base_response = self.client.get(
            "/api/jobs/abc123/result?variant=base&disposition=inline")
        image_response = self.client.get(
            "/api/jobs/abc123/result?variant=images&disposition=attachment")
        status = asyncio.run(web.status("abc123"))

        self.assertEqual(base_response.status_code, 200)
        self.assertTrue(base_response.headers["content-disposition"].startswith("inline"))
        self.assertEqual(image_response.status_code, 200)
        self.assertIn("attachment", image_response.headers["content-disposition"])
        self.assertTrue(status["image_post"]["result_ready"])
        self.assertNotIn("result_path", status["image_post"])

    def test_global_semaphore_serializes_two_subprocesses(self):
        first = self._job("first")
        second = self._job("second")
        self._queue(first)
        self._queue(second)
        gate = threading.Event()
        entered = threading.Event()
        guard = threading.Lock()
        counters = {"calls": 0, "active": 0, "maximum": 0}

        class BlockingProcess(_FakeProcess):
            def __init__(self, out_path: Path):
                out_path.write_bytes(b"candidate")
                with guard:
                    counters["calls"] += 1
                    counters["active"] += 1
                    counters["maximum"] = max(
                        counters["maximum"], counters["active"])
                entered.set()
                self.returncode = 0
                self.terminated = False

                def lines():
                    yield "phase=ocr"
                    gate.wait(2)

                self.stdout = lines()

            def wait(self):
                with guard:
                    counters["active"] -= 1
                return 0

        def popen(cmd, **kwargs):
            return BlockingProcess(Path(cmd[-1]))

        with patch.object(web.subprocess, "Popen", side_effect=popen), \
             patch.object(web, "_validate_image_pdf", return_value=(True, "", 1)):
            t1 = threading.Thread(target=web._run_image_postprocess, args=(first,))
            t2 = threading.Thread(target=web._run_image_postprocess, args=(second,))
            t1.start()
            self.assertTrue(entered.wait(1))
            t2.start()
            time.sleep(0.1)
            with guard:
                self.assertEqual(counters["calls"], 1)
            gate.set()
            t1.join(2)
            t2.join(2)

        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(counters["calls"], 2)
        self.assertEqual(counters["maximum"], 1)
        self.assertEqual(first["image_post"]["state"], "done")
        self.assertEqual(second["image_post"]["state"], "done")

    def test_ui_requires_base_preview_before_enabling_image_pass(self):
        self.assertIn('id="previewLink"', web.HTML_PAGE)
        self.assertIn('id="imagePostBtn" disabled', web.HTML_PAGE)
        self.assertIn("basePreviewed = true", web.HTML_PAGE)
        self.assertIn("imagePostBtn.disabled = active || state === 'done' || !basePreviewed",
                      web.HTML_PAGE)
        self.assertIn("/image-postprocess/cancel", web.HTML_PAGE)


if __name__ == "__main__":
    unittest.main()
