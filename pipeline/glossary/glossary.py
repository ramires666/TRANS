"""Загрузка глоссария из CSV. Поддерживает любой формат `source,target`.

Первая строка — заголовок (опционально). Если первая строка выглядит как
header (`zh,ru`, `source,target`, `en,ru`) — пропускаем, иначе парсим сразу.
"""
from __future__ import annotations

import csv
from pathlib import Path

from pipeline.config.loader import resolve_path


def load_glossary(path: str | Path) -> dict[str, str]:
    p = resolve_path(path)
    g: dict[str, str] = {}
    if not p.exists():
        return g
    source_headers = {
        "zh", "source", "src", "source_language",
        "en", "ja", "de", "fr",
    }
    target_headers = {
        "ru", "target", "dst", "target_language",
        "en", "de", "fr",
    }
    first_data_row = True
    # utf-8-sig прозрачно снимает BOM; newline="" требуется модулю csv для
    # корректной обработки quoted-полей и переводов строк внутри кавычек.
    with open(p, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) < 2:
                continue
            source = row[0].strip()
            # Сохраняем прежнюю терпимость к неэкранированным запятым в target,
            # но корректный CSV с quoted comma остаётся предпочтительным.
            target = ",".join(row[1:]).strip()
            if first_data_row:
                first_data_row = False
                if (source.lower() in source_headers
                        and target.lower() in target_headers):
                    continue
            if source and target:
                g[source] = target
    return g
