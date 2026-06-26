"""Markdown-режим перевода PDF.

Этот режим:
1. Извлекает текст каждой страницы исходного PDF.
2. Переводит страницу целиком в Markdown через локальную LLM.
3. Сохраняет Markdown-артефакты.
4. Собирает итоговый PDF как копию исходника с заменой текста
   на переведённый Markdown-рендер (overlay-подход).
"""
from __future__ import annotations

from pipeline.markdown.translator import MarkdownTranslator, translate_pdf
from pipeline.markdown.builder import build_pdf

__all__ = ["MarkdownTranslator", "translate_pdf", "build_pdf"]
