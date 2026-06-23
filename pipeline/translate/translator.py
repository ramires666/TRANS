"""Этап 3 — Перевод сегментов через LLM (OpenAI-совместимый API).

Обобщён на любую языковую пару через source_lang/target_lang в конфиге.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI
from tqdm import tqdm

from pipeline.anchors import compiled_anchors
from pipeline.config.loader import (ROOT, ensure_dirs, load_config,
                                     resolve_path, setup_logger)
from pipeline.glossary.glossary import load_glossary
from pipeline.io.artifacts import load_json, save_json

LANG_NAMES = {
    "zh": "китайского", "en": "английского", "ja": "японского",
    "de": "немецкого", "fr": "французского", "es": "испанского",
}
TARGET_NAMES = {
    "ru": "русский", "en": "английский", "de": "немецкий",
    "fr": "французский", "es": "испанский", "ja": "японский",
}


def translate_filename_stem(stem: str, cfg: dict, logger=None) -> str:
    """Переводит имя файла (stem) через один LLM-вызов.

    Возвращает переведённую строку. При любой ошибке/пустоте — возвращает
    исходный ``stem`` (очищенный от job_id-префикса).
    """
    import re as _re
    # срезаем job_id-префикс от upload
    m = _re.match(r"^[0-9a-f]{12}_(.+)$", stem)
    clean = m.group(1) if m else stem
    # отбрасываем версию/расширение-хвосты вида V3.0.4, v2, (1), _RU
    clean = _re.sub(r"\s*[Vv]\d[\d.]*\s*", " ", clean).strip()
    if not clean:
        return stem

    src = cfg.get("source_lang", "zh")
    tgt = cfg.get("target_lang", "ru")
    src_name = LANG_NAMES.get(src, src)
    tgt_name = TARGET_NAMES.get(tgt, tgt)

    system = (
        f"Ты — переводчик названий технических документов с {src_name} "
        f"на {tgt_name}. Переведи название файла руководства. "
        "Выведи ТОЛЬКО перевод, без кавычек, пояснений и комментариев. "
        "Сохрани технические термины и цифры. Не добавляй расширение."
    )
    user = f"Переведи: {clean}"

    try:
        client = OpenAI(
            base_url=cfg["llm_base_url"],
            api_key=cfg.get("llm_api_key", "not-needed"),
            timeout=cfg.get("request_timeout", 300),
        )
        kwargs = dict(
            model=cfg["llm_model"], messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2, top_p=0.9, max_tokens=200,
        )
        if not cfg.get("enable_thinking", False):
            try:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            except Exception:
                pass
        resp = client.chat.completions.create(**kwargs)
        out = (resp.choices[0].message.content or "").strip()
        # чистим от возможных кавычек/мусора
        out = out.strip('"\'`«» ')
        out = out.replace("\n", " ").strip()
        if logger:
            logger.info("Имя файла переведено: %r -> %r", clean, out)
        return out or clean
    except Exception as e:
        if logger:
            logger.warning("Перевод имени файла не удался: %s — fallback", e)
        return clean


def _anchor_rules(cfg: dict) -> str:
    a = compiled_anchors(cfg)
    rules = []
    if a["fig"]["src"] and a["fig"]["dst_label"]:
        rules.append(
            f"   - ссылки на рисунки ({a['fig']['src'].pattern!r}) -> "
            f"«{a['fig']['dst_label']} N-M» (сохраняй N-M);")
    if a["tab"]["src"] and a["tab"]["dst_label"]:
        rules.append(
            f"   - ссылки на таблицы ({a['tab']['src'].pattern!r}) -> "
            f"«{a['tab']['dst_label']} N-N» (сохраняй N-N);")
    rules.append(
        "   - номера разделов «N.M», «N.M.K», маркеры глав — сохраняй числовую часть;")
    rules.append(
        "   - имена параметров интерфейса на английском (Acquisition Control, "
        "Trigger Mode и подобные) — НЕ переводи;")
    rules.append(
        "   - маркеры списка и нумерацию шагов — сохраняй;")
    rules.append("   - литералы вида {{...}} сохраняй дословно.")
    # Жёсткие требования по сохранению форматирования
    rules.append(
        "   - КАЖДЫЙ перенос строки оригинала обязан присутствовать в переводе "
        "на том же логическом месте (по числу строк 1-в-1).")
    rules.append(
        "   - табуляции и ведущие пробелы строк (отступы под-пунктов) — сохраняй "
        "дословно тем же количеством символов того же типа.")
    rules.append(
        "   - маркеры списка («•», «-», «1.», «1)» и т.п.) и нумерацию шагов — "
        "сохраняй на тех же строках в той же позиции.")
    rules.append(
        "   - пустые строки внутри фрагмента — сохраняй.")
    rules.append(
        "   - НЕ реорганизуй строки, НЕ объединяй соседние строки, НЕ разрывай одну "
        "строку на несколько, НЕ переупорядочивай пункты.")
    return "\n".join(rules)


def build_system_prompt(cfg: dict) -> str:
    src = cfg.get("source_lang", "zh")
    tgt = cfg.get("target_lang", "ru")
    src_name = LANG_NAMES.get(src, src)
    tgt_name = TARGET_NAMES.get(tgt, tgt)
    rules = _anchor_rules(cfg)
    return (
        f"Ты — профессиональный переводчик технической документации "
        f"с {src_name} на {tgt_name} язык. "
        f"Переводи фрагмент технического руководства.\n"
        "Правила:\n"
        f"1. Переводи на грамотный {tgt_name} технический язык.\n"
        "2. Соблюдай терминологию из глоссария (если применимо).\n"
        "3. НЕ переводи и не изменяй следующие токены:\n"
        f"{rules}\n"
        "4. Не добавляй пояснений и комментариев. Выведи только перевод.\n"
        "5. ФОРМАТИРОВАНИЕ: сохраняй дословно ВСЕ переносы строк, табуляции, "
        "ведущие пробелы-отступы, маркеры списка и нумерацию — РОВНО как в оригинале. "
        "Число строк в переводе обязано совпадать с числом строк в оригинале.\n"
    )


CONTEXT_TMPL = ("Контекст: тип фрагмента={stype}; раздел={section}. "
                "Переведи фрагмент, строго сохранив переносы строк, "
                "табуляции, отступы и маркеры списка по форме оригинала.")
RETRY_TMPL = (
    "В предыдущем переводе потеряны или изменены якоря/номера или нарушено "
    "форматирование. Повтори перевод, строго сохранив все числовые якоря, имена "
    "параметров, переносы строк (по числу строк 1-в-1), табуляции, отступы и "
    "маркеры списка. Выведи только перевод."
)


class Cache:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS translations ("
            "key TEXT PRIMARY KEY, src TEXT, dst TEXT, ts TEXT, model TEXT, "
            "prompt_hash TEXT, accepts_review INTEGER DEFAULT 1)")
        self.conn.commit()

    @staticmethod
    def make_key(text: str, model: str, prompt_hash: str) -> str:
        return hashlib.sha256(
            (text + "\x00" + model + "\x00" + prompt_hash).encode("utf-8")
        ).hexdigest()

    def get(self, key: str):
        with self._lock:
            cur = self.conn.execute("SELECT dst FROM translations WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def put(self, key: str, src: str, dst: str, model: str, prompt_hash: str,
            accepts_review: int = 1):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO translations "
                "(key, src, dst, ts, model, prompt_hash, accepts_review) "
                "VALUES (?,?,?,?,datetime('now'),?,?)",
                (key, src, dst, model, prompt_hash, accepts_review))
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()


class Translator:
    def __init__(self, cfg: dict, logger, cache_db_path: str, errors_path: str):
        self.cfg = cfg
        self.logger = logger
        self.client = OpenAI(
            base_url=cfg["llm_base_url"],
            api_key=cfg.get("llm_api_key", "not-needed"),
            timeout=cfg.get("request_timeout", 300),
        )
        self.model = cfg["llm_model"]
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self.max_tokens = int(cfg.get("max_tokens", 4096))
        self.temperature = float(cfg.get("temperature", 0.2))
        self.top_p = float(cfg.get("top_p", 0.9))
        self.workers = int(cfg.get("workers", 2))
        self.cache = Cache(cache_db_path)
        self.glossary = load_glossary(cfg["glossary_path"])
        self.system_prompt = build_system_prompt(cfg)
        self.compiled_anch = compiled_anchors(cfg)
        self.prompt_hash = self._compute_prompt_hash()
        self.errors_path = Path(errors_path)
        self.errors_path.parent.mkdir(parents=True, exist_ok=True)
        self.errors_fh = open(self.errors_path, "a", encoding="utf-8")

    def _compute_prompt_hash(self) -> str:
        sig = (self.system_prompt + "|"
               + json.dumps(self.glossary, ensure_ascii=False) + "|"
               + self.model)
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]

    def _glossary_str(self) -> str:
        return ", ".join(f"{k}→{v}" for k, v in list(self.glossary.items())[:60])

    def _build_messages(self, seg: dict) -> list[dict]:
        glossary = self._glossary_str()
        src = self.cfg.get("source_lang", "zh").upper()
        tgt = self.cfg.get("target_lang", "ru").upper()
        context = CONTEXT_TMPL.format(stype=seg["type"],
                                      section=seg.get("section_id") or "—")
        user = (f"{context}\nГлоссарий: {glossary}\n\n"
                f"Фрагмент ({src}):\n\"\"\"\n{seg['text']}\n\"\"\"\n\n"
                f"Выведи только перевод на {tgt}.")
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]

    def _call_llm(self, messages: list[dict], hint: str | None = None) -> str:
        msgs = messages + [{"role": "user", "content": hint}] if hint else messages
        kwargs = dict(model=self.model, messages=msgs,
                     temperature=self.temperature, top_p=self.top_p,
                     max_tokens=self.max_tokens)
        if not self.enable_thinking:
            try:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            except Exception:
                pass
        resp = self.client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    def _check_anchors(self, src_text: str, dst_text: str) -> bool:
        ok = True
        for kind, spec in self.compiled_anch.items():
            s_re = spec["src"]
            d_re = spec["dst"]
            if not s_re:
                continue
            for m in s_re.finditer(src_text):
                num = m.group(1)
                if num in dst_text:
                    continue
                if d_re and d_re.search(dst_text):
                    continue
                ok = False
                break
            if not ok:
                break
        return ok

    def translate_one(self, seg: dict) -> dict:
        text = seg["text"]
        if not text.strip():
            return {"id": seg["id"], "src": text, "dst": text,
                    "cached": True, "ok": True}
        key = Cache.make_key(text, self.model, self.prompt_hash)
        cached = self.cache.get(key)
        if cached is not None:
            return {"id": seg["id"], "src": text, "dst": cached,
                    "cached": True, "ok": True}

        messages = self._build_messages(seg)
        last_err = None
        ru = ""
        for attempt in range(2):
            try:
                ru = self._call_llm(messages, hint=RETRY_TMPL if attempt == 1 else None)
            except Exception as e:
                last_err = str(e)
                continue
            if self._check_anchors(text, ru):
                self.cache.put(key, text, ru, self.model, self.prompt_hash, 1)
                return {"id": seg["id"], "src": text, "dst": ru,
                        "cached": False, "ok": True}

        if ru:
            self.cache.put(key, text, ru, self.model, self.prompt_hash, 0)
            self._log_error(seg, text, ru, "anchors_lost")
            return {"id": seg["id"], "src": text, "dst": ru,
                    "cached": False, "ok": False, "warn": "anchors_lost"}
        self._log_error(seg, text, "", f"llm_error:{last_err}")
        return {"id": seg["id"], "src": text, "dst": text,
                "cached": False, "ok": False, "error": last_err}

    def _log_error(self, seg, src, dst, reason):
        rec = {"id": seg["id"], "page": seg["page"], "type": seg["type"],
               "section": seg.get("section_id"), "reason": reason,
               "src": src, "dst": dst}
        self.errors_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.errors_fh.flush()

    def translate_all(self, segments: list[dict]) -> dict[int, str]:
        results: dict[int, str] = {}
        ok_cnt = cached_cnt = fail_cnt = 0
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self.translate_one, s): s for s in segments}
            for fut in tqdm(as_completed(futs), total=len(futs),
                            desc="translate", unit="seg"):
                seg = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    self._log_error(seg, seg["text"], "", f"exception:{e}")
                    fail_cnt += 1
                    results[seg["id"]] = seg["text"]
                    continue
                results[seg["id"]] = res["dst"]
                if res.get("cached"):
                    cached_cnt += 1
                if res.get("ok"):
                    ok_cnt += 1
                else:
                    fail_cnt += 1
        self.logger.info("Перевод завершён: ok=%d cached=%d fail=%d",
                         ok_cnt, cached_cnt, fail_cnt)
        return results

    def close(self):
        try:
            self.errors_fh.close()
        except Exception:
            pass
        self.cache.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Перевод segments.json -> segments_ru.json")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--config")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    logger = setup_logger(cfg)
    from pipeline.io.artifacts import source_hash, artifact_paths
    src_pdf = cfg["pdf_path"]
    sh = source_hash(src_pdf)
    ap = artifact_paths(cfg, sh)

    segs = load_json(args.inp or str(ap["segments"]))
    if args.limit:
        segs = segs[:args.limit]
    tr = Translator(cfg, logger, str(ap["cache_db"]), str(ap["errors"]))
    try:
        translations = tr.translate_all(segs)
    finally:
        tr.close()
    for s in segs:
        s["ru"] = translations.get(s["id"], s["text"])
    save_json(segs, args.out or str(ap["segments_ru"]))
    logger.info("Готово: %s", args.out or str(ap["segments_ru"]))


if __name__ == "__main__":
    main()