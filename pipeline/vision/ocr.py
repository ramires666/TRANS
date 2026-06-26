"""Vision/OCR pipeline for translating text in PDF images.

Uses an OpenAI-compatible vision model to extract and translate text
embedded in images (diagrams, screenshots, photos with labels).

Configuration (config.yaml):
    vision_llm_base_url: "http://127.0.0.1:8080/v1"
    vision_llm_api_key: "not-needed"
    vision_llm_model: "llava"  # or "qwen-vl", "gpt-4-vision", etc.
    vision_max_tokens: 2048
    vision_temperature: 0.1
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import fitz
from openai import OpenAI
from PIL import Image

from pipeline.config.loader import ROOT


VISION_PROMPT = (
    "You are an expert technical document translator. "
    "Look at the provided image and translate any visible text from {src_lang} to {tgt_lang}. "
    "Preserve numbers, labels, arrows, and layout meaning. "
    "Output ONLY the translated text, one item per line, in the same spatial order as the original. "
    "If there is no text, output an empty string."
)


class VisionCache:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vision_translations ("
            "key TEXT PRIMARY KEY, src_img_hash TEXT, src_lang TEXT, tgt_lang TEXT, "
            "dst TEXT, model TEXT, ts TEXT)")
        self.conn.commit()

    @staticmethod
    def make_key(img_hash: str, src_lang: str, tgt_lang: str, model: str) -> str:
        return hashlib.sha256(
            f"{img_hash}\x00{src_lang}\x00{tgt_lang}\x00{model}".encode("utf-8")
        ).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT dst FROM vision_translations WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def put(self, key: str, img_hash: str, src_lang: str, tgt_lang: str,
            dst: str, model: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO vision_translations "
                "(key, src_img_hash, src_lang, tgt_lang, dst, model, ts) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (key, img_hash, src_lang, tgt_lang, dst, model))
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()


class VisionTranslator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        base_url = cfg.get("vision_llm_base_url") or cfg.get("llm_base_url")
        api_key = cfg.get("vision_llm_api_key") or cfg.get("llm_api_key", "not-needed")
        self.model = cfg.get("vision_llm_model") or cfg.get("llm_model")
        self.client = OpenAI(base_url=base_url, api_key=api_key,
                             timeout=cfg.get("vision_request_timeout", 600))
        self.max_tokens = int(cfg.get("vision_max_tokens", 2048))
        self.temperature = float(cfg.get("vision_temperature", 0.1))
        self.top_p = float(cfg.get("vision_top_p", 0.9))
        self.enabled = bool(base_url and self.model)

    def _encode_image(self, image: Image.Image) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def translate_image(self, image: Image.Image, src_lang: str, tgt_lang: str,
                        cache: VisionCache | None = None) -> str:
        if not self.enabled:
            return ""
        img_bytes = io.BytesIO()
        image.save(img_bytes, format="PNG")
        img_hash = hashlib.sha256(img_bytes.getvalue()).hexdigest()
        key = VisionCache.make_key(img_hash, src_lang, tgt_lang, self.model)
        if cache:
            cached = cache.get(key)
            if cached is not None:
                return cached

        prompt = VISION_PROMPT.format(src_lang=src_lang, tgt_lang=tgt_lang)
        b64 = self._encode_image(image)
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]},
                ],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
            )
            out = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            out = ""
            # Re-raise so caller can log
            raise RuntimeError(f"Vision LLM call failed: {e}") from e

        if cache:
            cache.put(key, img_hash, src_lang, tgt_lang, out, self.model)
        return out


def extract_images_from_pdf(pdf_path: str, min_size: int = 100) -> list[dict[str, Any]]:
    """Extract images from PDF with their page number and bbox.

    Returns list of dicts: {"page", "xref", "bbox", "image": PIL.Image}
    """
    doc = fitz.open(str(pdf_path))
    results: list[dict[str, Any]] = []
    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                if pil_img.width >= min_size and pil_img.height >= min_size:
                    rects = page.get_image_rects(xref)
                    bbox = [rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1] if rects else None
                    results.append({"page": pno, "xref": xref, "bbox": bbox,
                                    "image": pil_img})
                pix = None
            except Exception:
                continue
    doc.close()
    return results


def extract_and_translate_images(pdf_path: str, cfg: dict,
                                 cache_path: str | None = None) -> list[dict[str, Any]]:
    """High-level helper: extract all images, translate text in them.

    Returns list of translation records with page, bbox, translated text.
    """
    src_lang = cfg.get("source_lang", "zh")
    tgt_lang = cfg.get("target_lang", "ru")
    translator = VisionTranslator(cfg)
    if not translator.enabled:
        return []

    cache = VisionCache(cache_path) if cache_path else None
    try:
        images = extract_images_from_pdf(pdf_path)
        out = []
        for item in images:
            try:
                translated = translator.translate_image(
                    item["image"], src_lang, tgt_lang, cache)
                if translated:
                    out.append({
                        "page": item["page"],
                        "xref": item["xref"],
                        "bbox": item["bbox"],
                        "text": translated,
                    })
            except Exception:
                continue
        return out
    finally:
        if cache:
            cache.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Vision OCR test")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--config")
    args = ap.parse_args()
    from pipeline.config.loader import load_config
    cfg = load_config(args.config)
    results = extract_and_translate_images(args.pdf, cfg,
                                           cache_path=str(ROOT / "intermediate" / "vision_cache.db"))
    print(json.dumps(results, ensure_ascii=False, indent=2))
