# -*- coding: utf-8 -*-
import json, re

segs = json.load(open(r'W:\_python\TRANS\intermediate\149c1e5ce40ebb66\segments_ru.json', encoding='utf-8'))

# 1. сегменты с пустым ru но непустым text
print("=== segments with empty ru (had text) ===")
empty_ru = [s for s in segs if (s.get('text') or '').strip() and not (s.get('ru') or '').strip()]
print(f"count: {len(empty_ru)}")
for s in empty_ru[:20]:
    print(f"  p{s['page']} type={s['type']:12s} bbox={s['bbox']} orig_size={s.get('size')}")
    print(f"    text: {(s['text'] or '')[:80]!r}")

# 2. сегменты где ru == text (не переведены)
print("\n=== segments where ru == text (untranslated) ===")
notrans = [s for s in segs if (s.get('ru') or '') and s['ru'] == s['text']]
print(f"count: {len(notrans)}")
for s in notrans[:20]:
    print(f"  p{s['page']} type={s['type']:12s} | {(s['text'] or '')[:80]!r}")

# 3. китайские иероглифы в ru
cjk = re.compile(r'[\u4e00-\u9fff]')
print("\n=== CJK still in ru ===")
cjk_ru = [s for s in segs if cjk.search(s.get('ru') or '')]
print(f"count: {len(cjk_ru)}")
for s in cjk_ru[:20]:
    print(f"  p{s['page']} type={s['type']:12s} {(s.get('ru') or '')[:80]!r}")

# 4. постранично: сколько сегментов, сколько с пустым ru
from collections import Counter
pages_total = Counter(s['page'] for s in segs)
pages_empty = Counter(s['page'] for s in empty_ru)
print(f"\n=== pages with most empty-ru segments ===")
for p in sorted(pages_empty, key=lambda x: -pages_empty[x]):
    print(f"  p{p+1}: empty_ru={pages_empty[p]}/{pages_total[p]}")
