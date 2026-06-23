# PDF-переводчик технических инструкций (ZH → RU)

Автоматизированный перевод PDF-документов с китайского на русский язык с сохранением
структуры, изображений, векторной графики, таблиц, оглавления и перекрёстных ссылок.
Перевод выполняется локальной LLM по OpenAI-совместимому протоколу.

## Возможности

- Сохранение полной структуры: заголовки 4 уровней, нумерованные списки, оглавление-закладки.
- Сохранение изображений и векторной графики на исходных местах.
- Обработка «нарисованных» линиями таблиц.
- Перевод перекрёстных ссылок `图N-M` → `Рис. N-M`, `表N-N` → `Табл. N-N` с сохранением номеров.
- Кириллический шрифт (DejaVuSans/Arial) с автоподбором размера.
- SQLite-кэш переводов — идемпотентность и возможность докрутки.
- Параллельный перевод, resume по стадиям.
- Веб-интерфейс (одностраничник) для загрузки PDF и запуска конвейера.

## Стек

| Назначение | Библиотека |
|---|---|
| Парсинг/сборка PDF | PyMuPDF (fitz) |
| Нейросетевой перевод | локальная LLM (OpenAI-совместимый API) |
| HTTP-клиент к LLM | `openai` |
| Веб-интерфейс | FastAPI + uvicorn |
| Конфиг/глоссарий | YAML / CSV |

## Установка

```powershell
pip install pymupdf openai tqdm pyyaml fastapi uvicorn python-multipart
```

LLM-сервер (например, llama.cpp / LM Studio) должен слушать `http://127.0.0.1:8080/v1`.
Модель и параметры задаются в `config.yaml`.

## Конфигурация

`config.yaml`:
```yaml
pdf_path: "source.pdf"
out_path: "_RU.pdf"
llm_base_url: "http://127.0.0.1:8080/v1"
llm_model: "Qwen3.6-35B-A3B-UD-Q3_K_M.gguf"
enable_thinking: false
max_tokens: 4096
workers: 2
glossary_path: "glossary.csv"
metadata:
  title: "..."
  author: "..."
  subject: "..."
```

Терминологический словарь — в `glossary.csv` (формат `zh,ru`).

## Конвейер

```
PDF (ZH) ─► [parse] ─► [segment] ─► [translate] ─► [build] ─► PDF (RU)
              │            │              │             │
         parse.json   segments.json   translations.db  _RU.pdf
```

| Этап | Модуль | Результат |
|---|---|---|
| 1. Парсинг | `src/parser.py` | `intermediate/parse.json` — блоки/спаны/изображения/drawings/таблицы |
| 2. Сегментация | `src/segmenter.py` | `intermediate/segments.json` — логические сегменты с типами и якорями |
| 3. Перевод | `src/translator.py` | `intermediate/segments_ru.json` + кэш в `translations.db` |
| 4. Сборка | `src/builder.py` | `_RU.pdf` — копия оригинала с русским текстом |
| 5. Валидация | `src/validator.py` | проверка страниц/изображений/TOC/якорей/метаданных |

## Запуск из CLI

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"

# Сводка по PDF
python src\inspect_pdf.py --in "file.pdf"

# Полный прогон
python run.py --in "file.pdf" --out "_RU.pdf" --resume

# Только перевод (parse/segment уже готовы)
python run.py --from-stage translate --resume

# Только валидация
python run.py --validate "_RU.pdf"
```

## Веб-интерфейс

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
python webapp.py
```

Откроется `http://127.0.0.1:8765`. Перетащите PDF в окно, нажмите «Перевести»,
наблюдайте прогресс в реальном времени, скачайте `_RU.pdf` по готовности.

## Структура проекта

```
├─ config.yaml          # параметры запуска
├─ glossary.csv         # терминологический словарь zh→ru
├─ run.py               # CLI-оркестратор конвейера
├─ webapp.py            # веб-интерфейс (FastAPI, одностраничник)
├─ src\
│  ├─ utils.py          # конфиг, лог, JSON, шрифты, глоссарий
│  ├─ inspect_pdf.py    # сводка по PDF
│  ├─ parser.py         # этап 1
│  ├─ segmenter.py      # этап 2
│  ├─ translator.py     # этап 3 (LLM + кэш)
│  ├─ builder.py        # этап 4
│  └─ validator.py      # этап 5
├─ intermediate\        # parse.json, segments.json, translations.db
├─ log\                 # translate.log, errors.jsonl
└─ _RU.pdf              # результат
```

## Риски и митигация

| Риск | Решение |
|---|---|
| Длинные русские фразы не вписываются в ширину | автосжатие `fontsize` через `insert_textbox` |
| Картинки/вектор затираются redactions | `images=fitz.PDF_REDACT_IMAGE_NONE` + redact строго по bbox блока |
| LLM теряет якоря/термины | запрет в промпте + постпроверка + повторный запрос |
| Helvetica не знает кириллицу | регистрация TTF DejaVuSans/Arial |
| Долгий прогон | SQLite-кэш + параллелизм + resume по стадиям |
