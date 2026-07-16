"""Strict vision OCR / translation of text regions inside raster images."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import fitz
from openai import OpenAI
from PIL import Image

from pipeline.config.loader import ROOT
from pipeline.translate.translator import _critical_tokens


VISION_PROMPT_VERSION = "vision-regions-v4"
VISION_PROMPT = """You are a precise OCR engine for technical screenshots.
Follow the user's image-extraction instructions. Return only the JSON object
constrained by the supplied schema. Never emit reasoning, Markdown or prose."""
VISION_USER_PROMPT = """Inspect the complete image. Extract only confidently
readable visible {src_lang} text and translate it concisely into {tgt_lang}.
{target_style}
Each item is one complete semantic label or line, in reading order; do not
merge separate controls. bbox=[x0,y0,x1,y1] uses integer coordinates normalized
from 0 to 1000 for the full image, with x0<x1 and y0<y1. source_text is an exact
transcription. translation must preserve every number, URL, filename, model
name, identifier, unit and code. confidence is a number from 0 to 1. Exclude
icons, decorative marks, already translated text, Latin/code-only regions and
obscured text. Never infer missing text. If there is no translatable source
text, return exactly {{"regions":[]}}."""
VISION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "vision_regions",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "regions": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "properties": {
                            "bbox": {
                                "type": "array",
                                "items": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 1000,
                                },
                                "minItems": 4,
                                "maxItems": 4,
                            },
                            "source_text": {"type": "string", "minLength": 1},
                            "translation": {"type": "string", "minLength": 1},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                        "required": [
                            "bbox", "source_text", "translation", "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["regions"],
            "additionalProperties": False,
        },
    },
}


_SCRIPT_PATTERNS = {
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "ja": re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff]"),
    "ko": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"),
    "ru": re.compile(r"[\u0400-\u052f]"),
    "uk": re.compile(r"[\u0400-\u052f]"),
    "ar": re.compile(r"[\u0600-\u06ff\u0750-\u077f]"),
    "he": re.compile(r"[\u0590-\u05ff]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
    "th": re.compile(r"[\u0e00-\u0e7f]"),
}
_LATIN_LANGS = {
    "en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
    "sv", "no", "da", "fi", "tr", "ro", "hu", "id", "vi",
}
_LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
def _base_lang(lang: str) -> str:
    return (lang or "").lower().replace("_", "-").split("-", 1)[0]


def _has_source_script(text: str, source_lang: str) -> bool:
    lang = _base_lang(source_lang)
    if lang in {"", "auto", "unknown", "und"}:
        return any(ch.isalpha() for ch in text)
    pattern = _SCRIPT_PATTERNS.get(lang)
    if pattern is not None:
        return bool(pattern.search(text))
    if lang in _LATIN_LANGS:
        return bool(_LATIN_RE.search(text))
    return any(ch.isalpha() for ch in text)


def clean_region(region: Any, source_lang: str, target_lang: str) -> dict | None:
    """Validate one model region and normalize its values.

    Invalid geometry, missing visible source script and translations which lose
    numeric / code tokens are rejected instead of being painted onto an image.
    """
    if not isinstance(region, dict):
        return None
    # Qwen-VL иногда использует своё документированное имя ``bbox_2d`` даже
    # при явном JSON-контракте. Нормализуем только этот известный алиас; все
    # требования к диапазону, порядку и размеру координат остаются строгими.
    bbox = region.get("bbox")
    if bbox is None:
        bbox = region.get("bbox_2d")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        coords = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in coords):
        return None
    if any(value < 0.0 or value > 1000.0 for value in coords):
        return None
    x0, y0, x1, y1 = coords
    if x1 - x0 < 1.0 or y1 - y0 < 1.0:
        return None

    source_text = str(region.get("source_text") or "").strip()
    translation = str(region.get("translation") or "").strip()
    if not source_text or not translation:
        return None
    if not _has_source_script(source_text, source_lang):
        return None
    if not _has_source_script(translation, target_lang):
        return None
    if re.sub(r"\s+", "", source_text).casefold() == re.sub(
            r"\s+", "", translation).casefold():
        return None
    if _critical_tokens(source_text) - _critical_tokens(translation):
        return None
    # A very large expansion is a strong hallucination signal, while still
    # allowing concise source scripts (CJK) to expand naturally.
    if len(translation) > max(500, len(source_text) * 8 + 80):
        return None

    try:
        confidence = float(region.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    confidence = max(0.0, min(1.0, confidence))

    return {
        "bbox": [x0, y0, x1, y1],
        "source_text": source_text,
        "translation": translation,
        "confidence": confidence,
    }


def _decode_json_object(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.lstrip("\ufeff").strip()
    text = re.sub(
        r"<(?:think|analysis)>.*?</(?:think|analysis)>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text,
                          flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                candidate, _end = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and isinstance(
                    candidate.get("regions"), list):
                return candidate
        return None
    return value if isinstance(value, dict) else None


def parse_regions(raw: Any, source_lang: str, target_lang: str) -> list[dict]:
    """Parse the strict JSON object returned by the vision model."""
    payload = _decode_json_object(raw)
    if payload is None or not isinstance(payload.get("regions"), list):
        return []
    out: list[dict] = []
    seen: set[tuple] = set()
    for item in payload["regions"][:256]:
        cleaned = clean_region(item, source_lang, target_lang)
        if cleaned is None:
            continue
        key = (tuple(cleaned["bbox"]), cleaned["source_text"],
               cleaned["translation"])
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    out.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return out


class VisionCache:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vision_translations ("
            "key TEXT PRIMARY KEY, src_img_hash TEXT, src_lang TEXT, tgt_lang TEXT, "
            "dst TEXT, model TEXT, prompt_version TEXT DEFAULT '', ts TEXT)")
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(vision_translations)")}
        if "prompt_version" not in columns:
            self.conn.execute(
                "ALTER TABLE vision_translations ADD COLUMN prompt_version TEXT DEFAULT ''")
        self.conn.commit()

    @staticmethod
    def make_key(img_hash: str, src_lang: str, tgt_lang: str, model: str,
                 prompt_version: str = VISION_PROMPT_VERSION) -> str:
        return hashlib.sha256(
            f"{img_hash}\x00{src_lang}\x00{tgt_lang}\x00{model}\x00{prompt_version}"
            .encode("utf-8")
        ).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT dst FROM vision_translations WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def put(self, key: str, img_hash: str, src_lang: str, tgt_lang: str,
            dst: str, model: str,
            prompt_version: str = VISION_PROMPT_VERSION) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO vision_translations "
                "(key, src_img_hash, src_lang, tgt_lang, dst, model, prompt_version, ts) "
                "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (key, img_hash, src_lang, tgt_lang, dst, model, prompt_version))
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class VisionTranslator:
    """Vision client returning sanitized region dictionaries."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        base_url = cfg.get("vision_llm_base_url") or cfg.get("llm_base_url")
        api_key = cfg.get("vision_llm_api_key") or cfg.get(
            "llm_api_key", "not-needed")
        # Never send images to an implicitly inherited text-only model.
        self.model = cfg.get("vision_llm_model")
        self.enabled = bool(base_url and self.model)
        self.client = (OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=cfg.get("vision_request_timeout", 600),
        ) if self.enabled else None)
        self.max_tokens = int(cfg.get("vision_max_tokens", 2048))
        self.retry_max_tokens = max(
            self.max_tokens,
            int(cfg.get("vision_retry_max_tokens", 8192)),
        )
        self.max_attempts = max(1, int(cfg.get("vision_max_attempts", 2)))
        self.temperature = float(cfg.get("vision_temperature", 0.0))
        self.top_p = float(cfg.get("vision_top_p", 0.9))
        self.enable_thinking = bool(cfg.get(
            "vision_enable_thinking",
            cfg.get("enable_thinking", False),
        ))
        self.json_schema = bool(cfg.get("vision_json_schema", True))
        self.prompt_version = VISION_PROMPT_VERSION
        self.last_call: dict[str, Any] = {}

    @staticmethod
    def _image_png(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _response_text(response) -> str:
        content = response.choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
                elif getattr(item, "text", None):
                    chunks.append(str(item.text))
            return "".join(chunks).strip()
        return ""

    def translate_image(self, image: Image.Image, src_lang: str, tgt_lang: str,
                        cache: VisionCache | None = None) -> list[dict]:
        if not self.enabled or self.client is None:
            return []
        image_bytes = self._image_png(image)
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        key = VisionCache.make_key(
            image_hash, src_lang, tgt_lang, str(self.model), self.prompt_version)
        if cache:
            cached = cache.get(key)
            payload = _decode_json_object(cached) if cached is not None else None
            if (
                payload is not None
                and payload.get("prompt_version") == self.prompt_version
                and isinstance(payload.get("regions"), list)
            ):
                regions = parse_regions(payload, src_lang, tgt_lang)
                self.last_call = {
                    "cached": True,
                    "attempts": 0,
                    "retries": 0,
                    "raw_regions": len(payload["regions"]),
                    "accepted_regions": len(regions),
                    "rejected_regions": len(payload["regions"]) - len(regions),
                }
                return regions

        encoded = base64.b64encode(image_bytes).decode("ascii")
        last_error = ""
        last_raw = ""
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            instruction = VISION_USER_PROMPT.format(
                src_lang=src_lang,
                tgt_lang=tgt_lang,
                target_style=(
                    "For English, write natural, concise technical UI copy using "
                    "standard industry terminology; avoid literal Chinese calques."
                    if _base_lang(tgt_lang) == "en" else ""
                ),
            )
            if attempt > 1:
                instruction += (
                    "\nPrevious response was unusable. Return the schema JSON "
                    "immediately; do not reason or omit required fields."
                )
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": VISION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{encoded}",
                                },
                            },
                        ],
                    },
                ],
                "max_tokens": min(
                    self.retry_max_tokens,
                    self.max_tokens * (2 ** (attempt - 1)),
                ),
                "temperature": self.temperature,
                "top_p": self.top_p,
            }
            if self.json_schema:
                kwargs["response_format"] = VISION_RESPONSE_FORMAT
            if not self.enable_thinking:
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False}
                }
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_error = f"call_failed:{exc}"
                continue
            if not getattr(response, "choices", None):
                last_error = "no_choices"
                continue
            choice = response.choices[0]
            finish_reason = str(getattr(choice, "finish_reason", "") or "")
            raw = self._response_text(response)
            last_raw = raw
            if finish_reason == "length":
                last_error = "finish_reason=length"
                continue
            if not raw:
                last_error = f"empty_content finish_reason={finish_reason or 'unknown'}"
                continue
            payload = _decode_json_object(raw)
            if payload is None or not isinstance(payload.get("regions"), list):
                last_error = "invalid_regions_json"
                continue

            raw_regions = payload["regions"][:256]
            regions = parse_regions(payload, src_lang, tgt_lang)
            source_candidates = sum(
                1 for item in raw_regions
                if isinstance(item, dict)
                and _has_source_script(
                    str(item.get("source_text") or ""), src_lang
                )
            )
            if raw_regions and source_candidates and not regions:
                last_error = (
                    f"all_regions_rejected raw={len(raw_regions)} "
                    f"source_candidates={source_candidates}"
                )
                continue

            serialized = json.dumps({
                "prompt_version": self.prompt_version,
                "regions": regions,
            }, ensure_ascii=False, separators=(",", ":"))
            if cache:
                cache.put(key, image_hash, src_lang, tgt_lang, serialized,
                          str(self.model), self.prompt_version)
            self.last_call = {
                "cached": False,
                "attempts": attempts,
                "retries": attempts - 1,
                "raw_regions": len(raw_regions),
                "accepted_regions": len(regions),
                "rejected_regions": len(raw_regions) - len(regions),
            }
            return regions

        excerpt = re.sub(r"\s+", " ", last_raw).strip()[:800]
        self.last_call = {
            "cached": False,
            "attempts": attempts,
            "retries": max(0, attempts - 1),
            "raw_regions": 0,
            "accepted_regions": 0,
            "rejected_regions": 0,
        }
        detail = f": {excerpt}" if excerpt else ""
        raise RuntimeError(
            f"Vision LLM response failed after {attempts} attempts "
            f"({last_error}){detail}"
        )


def _pixmap_to_image(pix: fitz.Pixmap) -> Image.Image:
    if pix.colorspace is not None and pix.colorspace.n > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    if pix.alpha and pix.n == 4:
        return Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    if pix.colorspace is not None and pix.colorspace.n == 1:
        return Image.frombytes("L", (pix.width, pix.height), pix.samples).convert("RGB")
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def extract_images_from_pdf(pdf_path: str, min_size: int = 100) -> list[dict[str, Any]]:
    """Extract raster images with page, xref, placement bbox and PIL image."""
    doc = fitz.open(str(pdf_path))
    results: list[dict[str, Any]] = []
    try:
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            for item in page.get_images(full=True):
                xref = item[0]
                try:
                    image = _pixmap_to_image(fitz.Pixmap(doc, xref))
                    if image.width < min_size or image.height < min_size:
                        continue
                    rects = page.get_image_rects(xref)
                    bbox = ([rects[0].x0, rects[0].y0,
                             rects[0].x1, rects[0].y1] if rects else None)
                    results.append({
                        "page": page_number,
                        "xref": xref,
                        "bbox": bbox,
                        "image": image,
                    })
                except Exception:
                    continue
    finally:
        doc.close()
    return results


def extract_and_translate_images(pdf_path: str, cfg: dict,
                                 cache_path: str | None = None) -> list[dict[str, Any]]:
    """Compatibility helper returning translated regions for extracted images."""
    source_lang = cfg.get("source_lang", "zh")
    target_lang = cfg.get("target_lang", "ru")
    translator = VisionTranslator(cfg)
    if not translator.enabled:
        return []
    cache = VisionCache(cache_path) if cache_path else None
    try:
        output = []
        for item in extract_images_from_pdf(pdf_path):
            try:
                regions = translator.translate_image(
                    item["image"], source_lang, target_lang, cache)
            except Exception:
                continue
            if regions:
                output.append({
                    "page": item["page"],
                    "xref": item["xref"],
                    "bbox": item["bbox"],
                    "regions": regions,
                })
        return output
    finally:
        if cache:
            cache.close()


if __name__ == "__main__":
    import argparse
    from pipeline.config.loader import load_config

    argument_parser = argparse.ArgumentParser(description="Vision OCR test")
    argument_parser.add_argument("--pdf", required=True)
    argument_parser.add_argument("--config")
    arguments = argument_parser.parse_args()
    configuration = load_config(arguments.config)
    result = extract_and_translate_images(
        arguments.pdf,
        configuration,
        cache_path=str(ROOT / "intermediate" / "vision_cache.db"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
