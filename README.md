# PDF-переводчик технических руководств

Автоматизированный перевод PDF-документов с сохранением структуры,
изображений, векторной графики, таблиц, оглавления и перекрёстных ссылок.
Перевод выполняется локальной LLM по OpenAI-совместимому протоколу.

Языковая пара настраивается через `config/config.yaml`: по умолчанию ZH → RU,
но можно переключить на любую поддерживаемую (английский, немецкий и т.д.),
переопределив `source_lang`/`target_lang`/`anchors`.

## Возможности

- Сохранение полной структуры: заголовки 4 уровней, нумерованные списки, оглавление-закладки.
- Сохранение изображений и векторной графики на исходных местах.
- Обработка нарисованных линиями таблиц + `find_tables()`.
- Перевод перекрёстных ссылок (для ZH→RU: `图N-M` → `Рис. N-M`, `表N-N` → `Табл. N-N`).
- TTF-шрифт целевого языка (DejaVuSans/Arial/Noto) с автоподбором размера.
- SQLite-кэш переводов, привязанный к sha256 исходного PDF — корректный RESUME при смене файла.
- Параллельный перевод; запуск с произвольной стадии; resume по хэшу.
- Веб-интерфейс (FastAPI одностраничник) и CLI.

## Стек

| Назначение | Библиотека |
|---|---|
| Парсинг/сборка PDF | PyMuPDF (fitz) |
| Нейросетевой перевод | локальная LLM (OpenAI-совместимый API) |
| HTTP-клиент | `openai` |
| Веб-интерфейс | FastAPI + uvicorn |
| Конфиг/глоссарий | YAML / CSV |

## Установка

```powershell
pip install -r requirements.txt
```

LLM-сервер (llama.cpp / LM Studio) должен слушать `http://127.0.0.1:8080/v1`.
Модель и параметры — в `config/config.yaml`.

## Конфигурация

`config/config.yaml`:

```yaml
source_lang: "zh"
target_lang: "ru"

pdf_path: ""
out_path: "_RU.pdf"

llm_base_url: "http://127.0.0.1:8080/v1"
llm_model: "Qwen3.6-35B-A3B-UD-Q3_K_M.gguf"
workers: 2

glossary_path: "config/glossary.csv"
target_font: ""      # пусто = авто-поиск DejaVu/Arial

# anchors (необязательно, по умолчанию берутся из пресета source_lang)
# anchors:
#   fig: {src_regex: '图\s*(\d+-\d+)', dst_label: 'Рис.', dst_regex: 'Рис\.?\s*(\d+-\d+)'}
#   tab: {src_regex: '表\s*(\d+-\d+)', dst_label: 'Табл.', dst_regex: 'Табл\.?\s*(\d+-\d+)'}

metadata:
  title: ""
  author: ""
  subject: ""
```

Глоссарий — `config/glossary.csv` в формате `source,target` (заголовок необязателен).

## Конвейер

```
PDF ──► [parse] ──► [segment] ──► [translate] ──► [build] ──► _RU.pdf
              │           │              │            │
        parse.json   segments.json   translations.db  validate
```

Артефакты лежат в `intermediate/<sha256-исходника>/`, поэтому смена PDF
автоматически инвалидирует кэш стадий. `translations.db` — там же.

| Этап | Модуль | Результат |
|---|---|---|
| 1. Парсинг | `pipeline/pdf/parser.py` | `parse.json` — блоки/спаны/изображения/drawings/таблицы |
| 2. Сегментация | `pipeline/text/segmenter.py` | `segments.json` — логические сегменты с типами и якорями |
| 3. Перевод | `pipeline/translate/translator.py` | `segments_ru.json` + `translations.db` |
| 4. Сборка | `pipeline/pdf/builder.py` | `_RU.pdf` — копия оригинала с переведённым текстом |
| 5. Валидация | `pipeline/pdf/validator.py` | проверка страниц/изображений/TOC/якорей/метаданных |

## Запуск

### Windows

```powershell
start.bat
```
(поднимает веб-интерфейс на `http://127.0.0.1:8765`)

### CLI

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."

# сводка по PDF
python -m app.cli --inspect "file.pdf"

# полный прогон
python -m app.cli --in "file.pdf" --out "_RU.pdf" --resume

# только валидация готового
python -m app.cli --validate "_RU.pdf"
```

### Веб-интерфейс

```powershell
python -m app.web
```
Откроется `http://127.0.0.1:8765`. Перетащите PDF, нажмите «Перевести»,
наблюдайте прогресс, скачайте результат по готовности.

### Docker

```powershell
docker compose -f docker/docker-compose.yml up --build
```
`config/`, `assets/`, `intermediate/`, `log/`, `uploads/` монтируются.
Если LLM-сервер на хосте, укажи в `config.yaml`:
`llm_base_url: "http://host.docker.internal:8080/v1"`.

## Структура проекта

```
├─ app/
│  ├─ cli.py            # оркестратор конвейера
│  └─ web.py            # FastAPI веб-интерфейс
├─ pipeline/
│  ├─ anchors.py        # якоря перекрёстных ссылок (зависят от языковой пары)
│  ├─ config/loader.py  # конфиг + логгер
│  ├─ io/artifacts.py   # JSON I/O + RESUME по sha256 исходника
│  ├─ fonts/fonts.py    # поиск TTF
│  ├─ glossary/glossary.py
│  ├─ pdf/              # parser, builder, validator, inspect
│  ├─ text/segmenter.py
│  └─ translate/translator.py
├─ config/
│  ├─ config.yaml
│  └─ glossary.csv
├─ docker/
│  ├─ Dockerfile
│  └─ docker-compose.yml
├─ assets/              # TTF-шрифты (опционально)
├─ intermediate/        # артефакты по хэшу исходника
├─ log/                 # translate.log, errors.jsonl
├─ uploads/             # загруженные через веб PDF
├─ requirements.txt
├─ start.bat
└─ README.md
```

## Риски и митигация

| Риск | Решение |
|---|---|
| Длинные переведённые фразы не вписываются в ширину | автосжатие `fontsize` через `insert_textbox` |
| Картинки/вектор затираются redactions | `images=fitz.PDF_REDACT_IMAGE_NONE` + redact строго по bbox |
| LLM теряет якоря/термины | запрет в промпте + постпроверка + повторный запрос |
| Шрифт без глифов кириллицы | регистрация TTF DejaVuSans/Arial/Noto |
| Долгий прогон | SQLite-кэш по sha256 + параллелизм + resume |
| Кэш врёт при смене исходника | артефакты в `intermediate/<hash>/` — смена PDF сбрасывает кэш |