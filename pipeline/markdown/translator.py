"""Перевод страниц PDF в Markdown через LLM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz
from openai import OpenAI
from tqdm import tqdm

from pipeline.config.loader import (ROOT, ensure_dirs, load_config,
                                     resolve_path, setup_logger)
from pipeline.io.artifacts import artifact_paths, load_json, save_json, source_hash


DEFAULT_SYSTEM_PROMPT = (
    "SYSTEM: Ты — эксперт по анализу документов и профессиональный переводчик с китайского на русский. \n"
    "Тебе передан текст страницы PDF, извлеченный парсером. Из-за особенностей формата структура таблиц и абзацев могла немного нарушиться.\n\n"
    "Твоя задача:\n"
    "1. Визуально восстанови логику документа. Найди, где были таблицы, заголовки и списки.\n"
    "2. Переведи весь текст на русский язык, сохраняя технический/бизнес контекст.\n"
    "3. Оформи результат строго в Markdown:\n"
    "   - Все таблицы собери в классические Markdown-таблицы (| Заголовок | Заголовок |).\n"
    "   - Заголовки разметь как #, ##, ###.\n"
    "4. Не добавляй никаких своих мыслей, пояснений или вводных слов. Только готовый Markdown-перевод."
)


def _clean_markdown(md: str) -> str:
    """Удаляет частые вводные слова, которые LLM добавляет несмотря на запрет."""
    lines = md.splitlines()
    # Удаляем пустые строки в начале
    while lines and not lines[0].strip():
        lines.pop(0)
    # Убираем заголовки-обёртки
    drop_prefixes = [
        "текст страницы", "перевод", "markdown-перевод",
        "перевод на русский", "русский перевод",
    ]
    while lines:
        first = lines[0].strip().lower().lstrip("# ")
        if any(first.startswith(p) for p in drop_prefixes):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        else:
            break
    # Убираем разделители --- в начале/конце
    while lines and lines[0].strip() == "---":
        lines.pop(0)
    while lines and lines[-1].strip() == "---":
        lines.pop()
    return "\n".join(lines).strip()


def _page_text(page: fitz.Page) -> str:
    """Извлекает текст страницы в формате, удобном для LLM.

    PyMuPDF 1.24+ умеет отдавать markdown-разметку страницы; если доступна,
    используем её, иначе — обычный plain text.
    """
    try:
        text = page.get_text("markdown")
    except Exception:
        text = ""
    if not text or not text.strip():
        text = page.get_text("text")
    return text or ""


def _make_user_prompt(text: str, page_num: int, total: int) -> str:
    return (
        f"Страница {page_num + 1} из {total}.\n\n"
        f"{text}\n\n"
        "Переведи и оформи результат строго в Markdown. "
        "Не добавляй заголовков вроде 'Текст страницы', 'Перевод' и т.п."
    )


class MarkdownTranslator:
    """Переводит страницы PDF в Markdown через OpenAI-совместимую LLM."""

    def __init__(self, cfg: dict, logger, cache_db_path: str | None = None):
        self.cfg = cfg
        self.logger = logger
        self.client = OpenAI(
            base_url=cfg["llm_base_url"],
            api_key=cfg.get("llm_api_key", "not-needed"),
            timeout=cfg.get("request_timeout", 300),
        )
        self.model = cfg["llm_model"]
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self.max_tokens = int(cfg.get("markdown_max_tokens", cfg.get("max_tokens", 4096)))
        self.temperature = float(cfg.get("markdown_temperature", cfg.get("temperature", 0.2)))
        self.top_p = float(cfg.get("markdown_top_p", cfg.get("top_p", 0.9)))
        self.system_prompt = cfg.get("markdown_system_prompt", DEFAULT_SYSTEM_PROMPT)

    def translate_page(self, text: str, page_num: int, total: int) -> str:
        """Один LLM-вызов на страницу. Возвращает Markdown."""
        if not text.strip():
            return ""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": _make_user_prompt(text, page_num, total)},
        ]
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
        )
        if not self.enable_thinking:
            try:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            except Exception:
                pass
        resp = self.client.chat.completions.create(**kwargs)
        return _clean_markdown(resp.choices[0].message.content or "")


def translate_pdf(pdf_path: str, cfg: dict, logger,
                  out_md_json: str | None = None,
                  limit: int = 0,
                  resume: bool = False) -> dict[int, str]:
    """Переводит все страницы PDF в Markdown.

    Возвращает словарь {page_number: markdown_text}.
    Результат сохраняется в ``out_md_json`` (pages_md.json) если передан.
    При ``resume=True`` пропускает страницы, уже присутствующие в pages_md.json.
    """
    pdf_path = resolve_path(pdf_path)
    logger.info("[markdown] Открываю PDF: %s", pdf_path)
    doc = fitz.open(str(pdf_path))
    total = doc.page_count

    existing: dict[int, str] = {}
    if resume and out_md_json and Path(out_md_json).exists():
        try:
            existing_raw = load_json(out_md_json)
            existing = {int(k): v for k, v in existing_raw.items()}
            logger.info("[markdown] Загружено %d переведённых страниц", len(existing))
        except Exception as e:
            logger.warning("[markdown] Не удалось прочитать существующий pages_md.json: %s", e)

    translator = MarkdownTranslator(cfg, logger)
    results: dict[int, str] = {}
    results.update(existing)

    indices = list(range(total))
    if limit and limit > 0:
        indices = indices[:limit]

    for pno in tqdm(indices, desc="[markdown] translate pages", unit="page"):
        if pno in results and results[pno].strip():
            logger.info("[markdown] Страница %d/%d уже переведена — skip", pno + 1, total)
            continue
        page = doc.load_page(pno)
        text = _page_text(page)
        try:
            md = translator.translate_page(text, pno, total)
        except Exception as e:
            logger.exception("[markdown] Ошибка перевода страницы %d: %s", pno + 1, e)
            md = ""
        results[pno] = md
        # Промежуточное сохранение после каждой страницы
        if out_md_json:
            save_json({str(k): v for k, v in results.items()}, out_md_json)

    doc.close()
    if out_md_json:
        save_json({str(k): v for k, v in results.items()}, out_md_json)
        logger.info("[markdown] Сохранено: %s", out_md_json)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Перевод PDF -> Markdown (pages_md.json)")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)

    src_pdf = args.inp or cfg["pdf_path"]
    sh = source_hash(src_pdf)
    apaths = artifact_paths(cfg, sh)
    out = args.out or str(apaths.get("pages_md") or (ROOT / cfg.get("tmp_dir", "intermediate") / sh / "pages_md.json"))

    translate_pdf(src_pdf, cfg, logger, out_md_json=out, limit=args.limit, resume=args.resume)
    logger.info("[markdown] Готово: %s", out)


if __name__ == "__main__":
    main()
