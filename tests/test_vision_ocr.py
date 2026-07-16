import hashlib
import json

from PIL import Image

from pipeline.vision.ocr import (VISION_PROMPT_VERSION, VisionCache,
                                 VisionTranslator, parse_regions)


def test_parse_regions_cleans_bounds_confidence_and_rejects_hallucinations():
    raw = "```json\n" + json.dumps({
        "regions": [
            {
                "bbox": [10, 20, 990, 800],
                "source_text": "\u8bbe\u5907 XG-200",
                "translation": "\u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e XG-200",
                "confidence": 1.4,
            },
            {
                "bbox": [1100, -20, -10, 800],
                "source_text": "\u8bbe\u5907",
                "translation": "\u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e",
                "confidence": 0.9,
            },
            {
                "bbox": [10, 10, 200, 100],
                "source_text": "\u578b\u53f7 XG-300",
                "translation": "\u041c\u043e\u0434\u0435\u043b\u044c",
                "confidence": 0.9,
            },
            {
                "bbox": [10, 10, 10, 100],
                "source_text": "\u6587\u672c",
                "translation": "\u0422\u0435\u043a\u0441\u0442",
                "confidence": 0.9,
            },
            {
                "bbox": [10, 10, 200, 100],
                "source_text": "English only",
                "translation": "\u0422\u0435\u043a\u0441\u0442",
                "confidence": 0.9,
            },
            {
                "bbox": [10, 10, 200, 100],
                "source_text": "\u6587\u672c",
                "translation": "\u672a\u7ffb\u8bd1",
                "confidence": 0.9,
            },
        ],
    }, ensure_ascii=False) + "\n```"

    regions = parse_regions(raw, "zh", "ru")

    assert regions == [{
        "bbox": [10.0, 20.0, 990.0, 800.0],
        "source_text": "\u8bbe\u5907 XG-200",
        "translation": "\u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e XG-200",
        "confidence": 1.0,
    }]


def test_text_model_fallback_does_not_enable_vision():
    translator = VisionTranslator({
        "llm_base_url": "http://127.0.0.1:8080/v1",
        "llm_model": "text-only-model",
        "llm_api_key": "test",
    })

    assert translator.enabled is False
    assert translator.model is None


def test_qwen_bbox_2d_alias_is_normalized_without_relaxing_geometry():
    payload = {
        "regions": [{
            "bbox_2d": [214, 306, 786, 563],
            "source_text": "设备状态 XG-200",
            "translation": "Состояние устройства XG-200",
            "confidence": 0.98,
        }]
    }

    assert parse_regions(payload, "zh", "ru") == [{
        "bbox": [214.0, 306.0, 786.0, 563.0],
        "source_text": "设备状态 XG-200",
        "translation": "Состояние устройства XG-200",
        "confidence": 0.98,
    }]


def test_cache_key_contains_prompt_version_and_cached_regions_avoid_llm(tmp_path):
    image = Image.new("RGB", (32, 20), "white")
    translator = VisionTranslator({
        "vision_llm_base_url": "http://127.0.0.1:1/v1",
        "vision_llm_api_key": "test",
        "vision_llm_model": "mock-vision",
    })
    image_hash = hashlib.sha256(translator._image_png(image)).hexdigest()
    key = VisionCache.make_key(
        image_hash, "zh", "ru", "mock-vision", VISION_PROMPT_VERSION)
    assert key != VisionCache.make_key(
        image_hash, "zh", "ru", "mock-vision", "older-prompt")

    cache = VisionCache(str(tmp_path / "vision.sqlite3"))
    payload = json.dumps({
        "prompt_version": VISION_PROMPT_VERSION,
        "regions": [{
            "bbox": [100, 100, 900, 900],
            "source_text": "\u6587\u672c 42",
            "translation": "\u0422\u0435\u043a\u0441\u0442 42",
            "confidence": 0.95,
        }],
    }, ensure_ascii=False)
    cache.put(key, image_hash, "zh", "ru", payload, "mock-vision")

    class _MustNotRun:
        def create(self, **kwargs):  # pragma: no cover - failure guard
            raise AssertionError("LLM must not be called on a cache hit")

    translator.client.chat.completions = _MustNotRun()
    try:
        regions = translator.translate_image(image, "zh", "ru", cache)
    finally:
        cache.close()

    assert len(regions) == 1
    assert regions[0]["translation"] == "\u0422\u0435\u043a\u0441\u0442 42"
