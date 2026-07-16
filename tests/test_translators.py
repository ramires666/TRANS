from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pipeline.markdown.translator import (
    DEFAULT_SYSTEM_PROMPT,
    MarkdownTranslator,
    _make_user_prompt,
)
from pipeline.translate.translator import (
    Cache,
    TranslationResponseError,
    Translator,
    _critical_tokens,
    _logical_bullets,
    normalize_soft_wraps,
)
from pipeline.anchors import compiled_anchors
from pipeline.config.loader import configure_target_language


def _response(content: str, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Unexpected LLM call")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class TranslatorTestCase(unittest.TestCase):
    def _write_glossary(self, root: str) -> Path:
        path = Path(root) / "glossary.csv"
        path.write_text(
            "zh,ru\n"
            "图,рис.\n"
            "图像,изображение\n"
            "像,образ\n"
            "软件,ПО\n"
            "配置,конфигурация\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _cfg(glossary: Path) -> dict:
        return {
            "llm_base_url": "http://test.invalid/v1",
            "llm_model": "fake-model",
            "glossary_path": str(glossary),
            "source_lang": "zh",
            "target_lang": "ru",
            "enable_thinking": False,
            "max_tokens": 512,
            "temperature": 0.0,
            "top_p": 1.0,
            "workers": 1,
            "llm_batch_max_items": 10,
            "llm_batch_max_chars": 1000,
            "llm_max_attempts": 2,
        }

    def _translator(self, root: str, responses) -> tuple[Translator, FakeClient]:
        glossary = self._write_glossary(root)
        fake = FakeClient(responses)
        with patch("pipeline.translate.translator.OpenAI", return_value=fake):
            translator = Translator(
                self._cfg(glossary),
                Mock(),
                str(Path(root) / "cache.db"),
                str(Path(root) / "errors.jsonl"),
            )
        return translator, fake

    def test_cache_returns_only_reviewed_rows(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(str(Path(root) / "cache.db"))
            try:
                cache.put("bad", "源", "плохо", "model", "prompt", 0)
                cache.put("good", "源", "хорошо", "model", "prompt", 1)
                cache.put("soft", "源", "длинно", "model", "prompt", 2)
                self.assertIsNone(cache.get("bad"))
                self.assertEqual(cache.get("good"), "хорошо")
                self.assertEqual(cache.get("soft"), "длинно")
                cache.reject("good")
                self.assertIsNone(cache.get("good"))
            finally:
                cache.close()

    def test_relevant_glossary_prefers_longest_non_overlapping_term(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                self.assertEqual(
                    translator._relevant_glossary(["图像"]),
                    [("图像", "изображение")],
                )
                self.assertEqual(
                    translator._relevant_glossary(["图像和图"]),
                    [("图像", "изображение"), ("图", "рис.")],
                )
            finally:
                translator.close()

    def test_exact_anchor_numbers_and_added_reference_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                source = "图1-1 и 图1-2"
                self.assertTrue(
                    translator._check_anchors(
                        source, "Рис. 1-1 и Рис. 1-2"
                    )
                )
                self.assertFalse(
                    translator._check_anchors(
                        source, "Рис. 1-1 и Рис. 1-1"
                    )
                )
                self.assertFalse(
                    translator._check_anchors(
                        "как показано ниже", "как показано на Рис. 2.3.2"
                    )
                )
            finally:
                translator.close()

    def test_codes_numbers_and_urls_must_be_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                record = translator._prepare_record({
                    "id": 1, "type": "paragraph", "section_id": "1",
                    "text": "设备 XG-200 写入 DM101 值 17751，见 https://a.test/x?y=2",
                })
                valid = (
                    "Устройство XG-200: записать в DM101 значение 17751; "
                    "см. https://a.test/x?y=2"
                )
                invalid = valid.replace("DM101", "DM102").replace("17751", "17715")
                self.assertNotIn(
                    "critical_tokens_mismatch",
                    translator._validate_translation(record, valid),
                )
                self.assertIn(
                    "critical_tokens_mismatch",
                    translator._validate_translation(record, invalid),
                )
            finally:
                translator.close()

    def test_critical_tokens_allow_safe_typographic_translation_changes(self):
        source = "Windows7/10/11 X86 64 3.4GHz CPU i7-8 万兆"
        translated = (
            "Windows 7/10/11, x86, 64-разрядная система, частота 3,4 ГГц, "
            "процессор i7 8-го поколения, 10 GigE"
        )
        self.assertFalse(_critical_tokens(source) - _critical_tokens(translated))

    def test_bullets_without_space_are_logical_bullets(self):
        self.assertEqual(["•", "▪"], _logical_bullets("•项目\n▪ Второй"))
        self.assertEqual([], _logical_bullets("-option"))

    def test_chinese_punctuation_is_normalized_for_russian_output(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                self.assertEqual(
                    "Параметры: CPU, RAM. Готово!",
                    translator._normalize_translation_text(
                        "Параметры：CPU，RAM。Готово！"
                    ),
                )
            finally:
                translator.close()

    def test_cache_signature_includes_context(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                first = translator._prepare_record(
                    {"id": 1, "type": "heading", "section_id": "1", "text": "配置"}
                )
                second = translator._prepare_record(
                    {"id": 2, "type": "cell", "section_id": "2", "text": "配置"}
                )
                self.assertNotEqual(first["prompt_hash"], second["prompt_hash"])
                self.assertNotEqual(first["cache_key"], second["cache_key"])
            finally:
                translator.close()

    def test_document_neighbours_are_sent_as_translation_context(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                segments = [
                    {
                        "id": 1, "page": 0, "type": "heading",
                        "section_id": "2", "text": "触发设置",
                    },
                    {
                        "id": 2, "page": 0, "type": "paragraph",
                        "section_id": "2", "text": "模式",
                    },
                    {
                        "id": 3, "page": 0, "type": "paragraph",
                        "section_id": "2", "text": "选择外触发",
                    },
                ]
                translator._active_document_contexts = (
                    translator._document_contexts(segments)
                )
                records = [translator._prepare_record(seg) for seg in segments]
                payload = json.loads(
                    translator._build_batch_messages(records)[1]["content"]
                )
                middle = payload["items"][1]["context"]
                self.assertEqual(middle["section_title"], "触发设置")
                self.assertEqual(middle["previous_text"], "触发设置")
                self.assertEqual(middle["next_text"], "选择外触发")
            finally:
                translator.close()

    def test_neighbour_context_changes_cache_signature(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(root, [])
            try:
                first_set = [
                    {"id": 1, "section_id": "1", "text": "网络设置"},
                    {"id": 2, "section_id": "1", "text": "模式"},
                ]
                second_set = [
                    {"id": 1, "section_id": "1", "text": "曝光设置"},
                    {"id": 2, "section_id": "1", "text": "模式"},
                ]
                translator._active_document_contexts = (
                    translator._document_contexts(first_set)
                )
                first = translator._prepare_record(first_set[1])
                translator._active_document_contexts = (
                    translator._document_contexts(second_set)
                )
                second = translator._prepare_record(second_set[1])
                self.assertNotEqual(first["prompt_hash"], second["prompt_hash"])
                self.assertNotEqual(first["cache_key"], second["cache_key"])
            finally:
                translator.close()

    def test_english_target_uses_english_anchors_and_glossary_path(self):
        cfg = {
            "source_lang": "zh",
            "target_lang": "ru",
            "glossary_path": "ru.csv",
            "glossary_paths": {"ru": "ru.csv", "en": "en.csv"},
        }
        configure_target_language(cfg, "en")
        anchors = compiled_anchors(cfg)
        self.assertEqual(cfg["glossary_path"], "en.csv")
        self.assertEqual(anchors["fig"]["dst_label"], "Fig.")
        self.assertEqual(anchors["tab"]["dst_label"], "Table")

    def test_batch_json_retries_only_invalid_item(self):
        batch = {
            "items": [
                {"id": "1", "translation": "Рис. 1-1 Установка"},
                {"id": "2", "translation": "Настройка 软件"},
            ]
        }
        retry = {"items": [{"id": "2", "translation": "Настройка ПО"}]}
        wrapped_batch = "Ответ:\n```json\n" + json.dumps(batch, ensure_ascii=False) + "\n```"

        with tempfile.TemporaryDirectory() as root:
            translator, fake = self._translator(
                root,
                [_response(wrapped_batch), _response(json.dumps(retry, ensure_ascii=False))],
            )
            try:
                segments = [
                    {
                        "id": 1,
                        "page": 0,
                        "type": "caption_fig",
                        "section_id": "1",
                        "text": "图1-1 安装",
                    },
                    {
                        "id": 2,
                        "page": 0,
                        "type": "paragraph",
                        "section_id": "1",
                        "text": "软件配置",
                    },
                ]
                translated = translator.translate_all(segments)
                self.assertEqual(translated[1], "Рис. 1-1 Установка")
                self.assertEqual(translated[2], "Настройка ПО")
                self.assertEqual(len(fake.completions.calls), 2)

                retry_payload = json.loads(
                    fake.completions.calls[1]["messages"][1]["content"]
                )
                self.assertEqual(
                    [item["id"] for item in retry_payload["items"]], ["2"]
                )
                self.assertEqual(
                    retry_payload["violations"]["2"],
                    [
                        "residual_han: переведи или удали весь оставшийся "
                        "китайский текст; в translation не должно быть "
                        "Han-иероглифов. Особенно проверь фрагменты: "
                        "['软件配置']"
                    ],
                )

                first_record = translator._prepare_record(segments[0])
                second_record = translator._prepare_record(segments[1])
                self.assertEqual(
                    translator.cache.get(first_record["cache_key"]),
                    "Рис. 1-1 Установка",
                )
                self.assertEqual(
                    translator.cache.get(second_record["cache_key"]),
                    "Настройка ПО",
                )
            finally:
                translator.close()

    def test_finish_reason_length_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            translator, _fake = self._translator(
                root, [_response('{"items": []}', finish_reason="length")]
            )
            try:
                with self.assertRaises(TranslationResponseError):
                    translator._call_llm(
                        [{"role": "user", "content": "{}"}]
                    )
            finally:
                translator.close()

    def test_soft_wraps_join_visual_lines_but_keep_logical_bullets(self):
        source = "第一行\n第二行\n\n• 项目\n续行\n• 第二项"
        self.assertEqual(
            normalize_soft_wraps(source),
            "第一行第二行\n\n• 项目续行\n• 第二项",
        )

    def test_soft_wraps_keep_chinese_bullets_without_spaces(self):
        self.assertEqual(
            normalize_soft_wraps("•第一项\n续行\n•第二项"),
            "•第一项续行\n•第二项",
        )

    def test_invalid_cached_translation_is_retranslated(self):
        with tempfile.TemporaryDirectory() as root:
            translator, fake = self._translator(
                root,
                [_response(json.dumps({
                    "items": [{"id": "1", "translation": "Настройка ПО"}]
                }, ensure_ascii=False))],
            )
            try:
                seg = {
                    "id": 1, "page": 0, "type": "paragraph",
                    "section_id": "1", "text": "软件配置",
                }
                record = translator._prepare_record(seg)
                translator.cache.put(
                    record["cache_key"], record["source"], "Настройка 软件",
                    translator.model, record["prompt_hash"],
                )

                result = translator.translate_one(seg)

                self.assertTrue(result["ok"])
                self.assertFalse(result["cached"])
                self.assertEqual("Настройка ПО", result["dst"])
                self.assertEqual(1, len(fake.completions.calls))
            finally:
                translator.close()

    def test_residual_han_fragment_gets_targeted_repair(self):
        draft = {
            "items": [{
                "id": "1",
                "translation": "Пакет разделён на 3 части,具体如下:",
            }]
        }
        repair = {
            "replacements": [{
                "source": "具体如下",
                "target": "как указано ниже",
            }]
        }
        with tempfile.TemporaryDirectory() as root:
            translator, fake = self._translator(
                root,
                [
                    _response(json.dumps(draft, ensure_ascii=False)),
                    _response(json.dumps(repair, ensure_ascii=False)),
                ],
            )
            try:
                result = translator.translate_one({
                    "id": 1, "page": 0, "type": "paragraph",
                    "section_id": "1",
                    "text": "软件安装包分为3个，具体如下：",
                })
                self.assertTrue(result["ok"])
                self.assertTrue(result["repaired_residual_han"])
                self.assertEqual(
                    "Пакет разделён на 3 части,как указано ниже:",
                    result["dst"],
                )
                self.assertEqual(2, len(fake.completions.calls))
            finally:
                translator.close()


class MarkdownTranslatorTestCase(unittest.TestCase):
    def test_empty_custom_prompt_uses_language_general_default(self):
        fake = FakeClient([_response("Ответ:\n```markdown\n# Заголовок\n```")])
        cfg = {
            "llm_base_url": "http://test.invalid/v1",
            "llm_model": "fake-model",
            "source_lang": "zh",
            "target_lang": "ru",
            "markdown_system_prompt": "",
            "enable_thinking": False,
        }
        with patch("pipeline.markdown.translator.OpenAI", return_value=fake):
            translator = MarkdownTranslator(cfg, Mock())

        self.assertEqual(
            translator.system_prompt,
            DEFAULT_SYSTEM_PROMPT.format(source_language="zh", target_language="ru"),
        )
        self.assertNotIn("Страница 3 из 9", _make_user_prompt("текст", 2, 9))
        self.assertEqual(translator.translate_page("源文本", 2, 9), "# Заголовок")
        sent_user = fake.completions.calls[0]["messages"][1]["content"]
        self.assertNotIn("Страница 3 из 9", sent_user)
        self.assertIn("<document_page>", sent_user)

    def test_english_default_prompt_requires_natural_technical_english(self):
        fake = FakeClient([])
        cfg = {
            "llm_base_url": "http://test.invalid/v1",
            "llm_model": "fake-model",
            "source_lang": "zh",
            "target_lang": "en",
            "markdown_system_prompt": "",
            "enable_thinking": False,
        }
        with patch("pipeline.markdown.translator.OpenAI", return_value=fake):
            translator = MarkdownTranslator(cfg, Mock())
        self.assertIn("стандартную отраслевую терминологию", translator.system_prompt)
        self.assertIn("дословных кальк", translator.system_prompt)


if __name__ == "__main__":
    unittest.main()
