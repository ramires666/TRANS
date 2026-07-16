# PDF-переводчик технической документации

Переводит PDF через OpenAI-совместимую LLM и сохраняет исходную геометрию страниц, изображения, векторную графику, таблицы и закладки. Основной режим работает по текстовым сегментам; языковая пара задаётся в `config/config.yaml` (по умолчанию ZH → RU).

## Что реализовано

- Парсинг текста и таблиц PyMuPDF с привязкой сегментов к физическим ячейкам.
- Подбор TTF-шрифта целевого языка и, при `match_fonts: true`, близкого семейства/начертания.
- Определение стиля по значимым символам: пробелы, маркеры списков и PUA-глифы не должны навязывать соседнему тексту случайный мелкий шрифт.
- Пакетный перевод LLM со строгим JSON-контрактом, глоссарием, проверкой кодов, чисел, маркеров и ссылок.
- Кэш переводов и безопасный `--resume`, учитывающий исходный PDF, конфигурацию, промпт, глоссарий и код стадии.
- Читаемые минимальные кегли, приложение для не поместившихся переводов, отчёт вёрстки и автоматическая валидация.
- CLI и веб-интерфейс.
- Отдельная необязательная обработка текста внутри растровых изображений через vision LLM.

## Установка и конфигурация

```powershell
pip install -r requirements.txt
```

Минимальные настройки текстовой LLM:

```yaml
source_lang: "zh"
target_lang: "ru"

llm_base_url: "http://127.0.0.1:8080/v1"
llm_api_key: "not-needed"
llm_model: "имя-текстовой-модели"

workers: 2
llm_batch_max_items: 24
llm_batch_max_chars: 4000
translation_max_attempts: 3

glossary_path: "config/glossary.csv"
target_font: ""       # пусто = авто-поиск подходящего TTF
match_fonts: true
```

Глоссарий хранится в `config/glossary.csv` в формате `source,target`. Полный набор параметров и комментарии находятся в `config/config.yaml`.

## Основной конвейер

```text
PDF → parse → segment → translate → build → validate → базовый переведённый PDF
      │       │          │           │
      │       │          │           └─ RESULT.pdf.layout.json
      │       │          └─ segments_ru.json + translations.db
      │       └─ segments.json
      └─ parse.json
```

Промпт переводчика рассматривает входной текст как данные, требует вернуть только `items[{id, translation}]`, применяет переданный глоссарий и запрещает изменять коды, числа, единицы и номера ссылок. Сегменты объединяются в пакеты по `llm_batch_max_items` и `llm_batch_max_chars`; если пакет или отдельный перевод не прошёл разбор/проверки, повторяются только проблемные элементы.

Промежуточные файлы находятся в `intermediate/<hash-исходника>/`. `manifest.json` хранит завершённость, сигнатуру и digest артефакта каждой стадии. `--resume` пропускает только полностью завершённые совместимые стадии; частичный запуск с `--limit` не помечается завершённым.

## Читаемость и переполнение

Основные пределы по умолчанию:

```yaml
builder_min_fontsize: 8.5
builder_table_min_fontsize: 7.5
builder_caption_min_fontsize: 8.0
builder_heading_min_fontsize: 9.0
builder_overflow_policy: "appendix"
builder_overflow_fontsize: 9.0
validator_min_readable_fontsize: 7.5
validator_max_residual_source_chars: 0
```

Если перевод не помещается при читаемом минимуме, он не ужимается до микрошрифта: на исходной странице ставится маркер `[T…]`, а полный текст переносится в раздел «Продолжение перевода». Если безопасно поставить маркер нельзя, исходный блок сохраняется и фиксируется в отчёте.

Сборщик создаёт sidecar-отчёт `RESULT.pdf.layout.json` с минимальным использованным кеглем, переполнениями, добавленными страницами и ошибками размещения. Валидатор использует этот отчёт и проверяет, среди прочего, потерю страниц/изображений/закладок, пустые страницы, слишком мелкий текст, остаточный исходный Han-текст и `lost/notfit`.

## Запуск CLI

```powershell
$env:PYTHONUTF8="1"
$env:PYTHONIOENCODING="utf-8"
$env:PYTHONPATH="."

# информация о документе
python -m app.cli --inspect "manual.pdf"

# полный основной конвейер
python -m app.cli --in "manual.pdf" --out "manual_RU.pdf" --resume

# отдельная проверка готового результата относительно исходника
python -m app.cli --in "manual.pdf" --validate "manual_RU.pdf"
```

Для сложных документов также доступен альтернативный режим перевода страниц через Markdown:

```powershell
python -m app.cli --in "manual.pdf" --out "manual_RU.pdf" --mode markdown --resume
```

## Текст внутри изображений: только отдельный постпроцесс

Обычный конвейер не отправляет изображения в vision LLM. Сначала создайте и визуально проверьте базовый переведённый PDF, затем при необходимости запустите отдельную обработку:

```powershell
python -m app.cli --image-postprocess "manual_RU.pdf" --out "manual_RU_images.pdf"
```

`--out` обязателен, должен отличаться от входа и не должен указывать на существующий файл. Поэтому базовый `manual_RU.pdf` остаётся неизменным, а результат создаётся как отдельный производный PDF.

Vision-модель должна быть указана явно; значение `llm_model` не используется как неявная замена:

```yaml
vision_llm_base_url: ""            # пусто = использовать llm_base_url
vision_llm_api_key: ""
vision_llm_model: "имя-vision-модели"  # обязательно для постпроцесса
vision_max_tokens: 2048
vision_temperature: 0.0
vision_top_p: 0.9
vision_request_timeout: 600

vision_confidence_threshold: 0.65
vision_min_image_size: 64
vision_min_fontsize: 12
vision_overlay_padding: 3
vision_bbox_padding_ratio: 0.04  # закрывает крайние штрихи tight OCR bbox
vision_text_align: "center"        # center | left
vision_cache_path: "intermediate/vision_translations.db"
# vision_report_path: ""           # по умолчанию RESULT.pdf.vision.json
```

Vision LLM возвращает строгий JSON со списком областей и координатами. Некорректные координаты, низкая уверенность, потерянные коды/числа и неподходящий язык перевода отбрасываются; такие области изображения остаются без изменений. Повторяющиеся PDF-изображения обрабатываются один раз по `xref`.

Рядом с производным PDF создаётся `RESULT.pdf.vision.json` со статистикой, минимальным кеглем, причинами пропуска и ошибками. SQLite-кэш задаётся через `vision_cache_path`; его можно отключить пустым значением.

## Веб-интерфейс

```powershell
python -m app.web
```

Интерфейс доступен по `http://127.0.0.1:8765`. После завершения основного перевода сначала откройте «Просмотреть базовый PDF». Если настроен `vision_llm_model`, после просмотра станет доступна отдельная кнопка «Обработать изображения» с собственным прогрессом, отменой, просмотром и скачиванием производного файла.

На Windows можно также запустить `start.bat`.

## Основные файлы

```text
app/cli.py                       CLI и оркестрация стадий
app/web.py                       веб-интерфейс
pipeline/pdf/parser.py           извлечение структуры PDF
pipeline/text/segmenter.py       логические сегменты и ячейки таблиц
pipeline/translate/translator.py пакетный перевод, промпт и проверки
pipeline/pdf/builder.py          компоновка, шрифты, overflow-приложение
pipeline/pdf/validator.py        проверка PDF и layout-отчёта
pipeline/vision/ocr.py           строгий vision JSON и кэш
pipeline/vision/image_overlay.py отдельная растровая постобработка
config/config.yaml               основная конфигурация
```

Перевод всё равно требует визуального контроля: PDF может содержать нестандартные шрифты, сканы, сложные формы, подписи как векторные кривые или намеренно плотную вёрстку. Отчёты помогают найти такие места, но не заменяют просмотр готового документа.
