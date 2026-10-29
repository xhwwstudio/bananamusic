import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from app.db.session import engine
from sqlalchemy import text


def norm_text(s: str) -> str:
    s = unicodedata.normalize('NFKC', str(s or '')).strip().lower()
    s = s.replace('（', '(').replace('）', ')')
    s = re.sub(r'\s+', ' ', s)
    return s


def split_artists(s: str):
    s = norm_text(s)
    parts = re.split(r'\s*[/,，&、;；]\s*', s)
    return tuple(sorted(p for p in parts if p))


def title_core(s: str) -> str:
    s = norm_text(s)
    s = re.sub(r'\([^)]*\)', '', s)
    s = re.sub(r'（[^）]*）', '', s)
    s = re.sub(r'\b(dj|remix|mix|live|版|伴奏|cover|demo)\b', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' -_')
    return s

with engine.connect() as conn:
    rows = conn.execute(text("SELECT id,title,artist,album,fmt,dur,path,created_at,updated_at FROM songs ORDER BY artist,title,fmt,path")).mappings().all()

songs = [dict(r) for r in rows]

# 1) real + virtual coexist exact-ish
coexist = []
by_key = defaultdict(list)
for s in songs:
    key = (title_core(s['title']), split_artists(s['artist']))
    by_key[key].append(s)

for key, group in by_key.items():
    real = [x for x in group if x['fmt'] != 'VIRTUAL']
    virt = [x for x in group if x['fmt'] == 'VIRTUAL']
    if real and virt:
        coexist.append({
            'key': {'title_core': key[0], 'artists': list(key[1])},
            'real': [{k: x[k] for k in ('id','title','artist','album','fmt','dur','path')} for x in real],
            'virtual': [{k: x[k] for k in ('id','title','artist','album','fmt','dur','path')} for x in virt],
        })

# 2) similar real-real duplicates
real_songs = [s for s in songs if s['fmt'] != 'VIRTUAL']
similar = []
for i in range(len(real_songs)):
    a = real_songs[i]
    for j in range(i+1, len(real_songs)):
        b = real_songs[j]
        if split_artists(a['artist']) != split_artists(b['artist']):
            continue
        ta, tb = title_core(a['title']), title_core(b['title'])
        if not ta or not tb:
            continue
        ratio = SequenceMatcher(None, ta, tb).ratio()
        dur_a = int(a.get('dur') or 0)
        dur_b = int(b.get('dur') or 0)
        dur_diff = abs(dur_a - dur_b) if dur_a and dur_b else None
        album_same = norm_text(a['album']) == norm_text(b['album'])
        # exact same core title but different paths always interesting
        if ta == tb:
            similar.append({
                'type': 'same-core-title',
                'ratio': 1.0,
                'dur_diff': dur_diff,
                'album_same': album_same,
                'a': {k: a[k] for k in ('id','title','artist','album','fmt','dur','path')},
                'b': {k: b[k] for k in ('id','title','artist','album','fmt','dur','path')},
            })
            continue
        # high similarity
        if ratio >= 0.92 and (dur_diff is None or dur_diff <= 15):
            similar.append({
                'type': 'high-similarity',
                'ratio': round(ratio, 3),
                'dur_diff': dur_diff,
                'album_same': album_same,
                'a': {k: a[k] for k in ('id','title','artist','album','fmt','dur','path')},
                'b': {k: b[k] for k in ('id','title','artist','album','fmt','dur','path')},
            })

# dedupe and sort
similar.sort(key=lambda x: (x['type'] != 'same-core-title', x['ratio'] * -1 if isinstance(x['ratio'], (int,float)) else -1, x['dur_diff'] if x['dur_diff'] is not None else 9999))

report = {
    'total_songs': len(songs),
    'virtual_count': sum(1 for s in songs if s['fmt']=='VIRTUAL'),
    'real_count': sum(1 for s in songs if s['fmt']!='VIRTUAL'),
    'real_virtual_coexist_count': len(coexist),
    'real_virtual_coexist': coexist[:50],
    'similar_real_duplicates_count': len(similar),
    'similar_real_duplicates': similar[:80],
}
print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
