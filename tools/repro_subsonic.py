"""Reproduce Subsonic API responses for a library with tricky names.

Run with: .venv312\\Scripts\\python.exe tools\\repro_subsonic.py
"""
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "work" / "repro.db"
DB.parent.mkdir(exist_ok=True)
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + DB.as_posix()

BACKEND = ROOT / "backend"
if not (BACKEND / "app").exists():
    BACKEND = ROOT / "src" / "backend"
os.environ["PYTHONPATH"] = str(BACKEND)
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.db.session import engine  # noqa: E402

SONGS = [
    # (id, title, artist, album, fmt, dur, path)
    ("s1", "Deadend (活该)", "Tyson Yoshi、王嘉尔", "I'm Just Your Type (命中偏爱)", "FLAC", 200, "/Music/a.flac"),
    ("s2", "晴天", "周杰伦", "叶惠美", "MP3", 269, "/Music/b.mp3"),
    ("s3", "夜夜夜夜 (R&B版)", "一毫升", "夜夜夜夜（R&B版）", "FLAC", 240, "/Music/c.flac"),
    ("s4", "Back In Black", "AC/DC", "Back In Black", "MP3", 255, "/Music/d.mp3"),
    ("s5", "雨蝶", "李翊君", "经典再回首", "MP3", 260, "/Music/e.mp3"),
    ("s6", "雨蝶", "李翊君", "誓言谎言", "MP3", 260, "/Music/f.mp3"),
    ("s7", "Fading Light", "Ulaa（尤拉）", "Fading Light(渐暗的光)", "FLAC", 210, "/Music/g.flac"),
]

with engine.begin() as conn:
    for sid, title, artist, album, fmt, dur, path in SONGS:
        conn.execute(
            text(
                "INSERT INTO songs (id, title, artist, album, fmt, dur, path, plays) "
                "VALUES (:id, :title, :artist, :album, :fmt, :dur, :path, 0)"
            ),
            {"id": sid, "title": title, "artist": artist, "album": album, "fmt": fmt, "dur": dur, "path": path},
        )

client = TestClient(main.app)
SALT = "abc123"
TOKEN = hashlib.md5(("changeme" + SALT).encode()).hexdigest()
AUTH = {"u": "admin", "t": TOKEN, "s": SALT, "f": "json", "v": "1.16.1", "c": "Symfonium", "format": "json"}


def call(path, **params):
    q = dict(AUTH)
    q.update(params)
    r = client.get(f"/rest/{path}", params=q)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return r.status_code, body


print("=" * 70)
print("1) getOpenSubsonicExtensions")
st, body = call("getOpenSubsonicExtensions.view")
print(st)
print(json.dumps(body, ensure_ascii=False, indent=2)[:800])
ext = body.get("subsonic-response", {}).get("openSubsonicExtensions")
print("=> python type:", type(ext).__name__, "| is list:", isinstance(ext, list))

print("=" * 70)
print("2) getArtists -> getArtist(id) round-trip")
st, body = call("getArtists.view")
artists = []
for idx in body["subsonic-response"]["artists"]["index"]:
    artists.extend(idx.get("artist", []))
print(f"artists: {len(artists)}")
for a in artists:
    st2, b2 = call("getArtist.view", id=a["id"])
    ok = b2.get("subsonic-response", {}).get("status")
    print(f"  id={a['id']!r:42} name={a['name']!r:24} -> getArtist {ok}")

print("=" * 70)
print("3) getAlbumList2 -> getAlbum(id) round-trip")
st, body = call("getAlbumList2.view", type="alphabeticalByName", size=500, offset=0)
albums = body["subsonic-response"]["albumList2"]["album"]
print(f"albums: {len(albums)}")
for a in albums:
    st2, b2 = call("getAlbum.view", id=a["id"])
    ok = b2.get("subsonic-response", {}).get("status")
    msg = b2.get("subsonic-response", {}).get("error", {}).get("message", "")
    print(f"  id={a['id']!r:48} name={a['name']!r:32} -> getAlbum {ok} {msg}")

print("=" * 70)
print("4) getMusicDirectory album child id -> getMusicDirectory")
st, body = call("getMusicDirectory.view", id="music-001")
children = body["subsonic-response"]["directory"].get("child", [])
print(f"root children (artists): {len(children)}")
for c in children[:3]:
    st2, b2 = call("getMusicDirectory.view", id=c["id"])
    ok = b2.get("subsonic-response", {}).get("status")
    print(f"  artist {c['title']!r:24} id={c['id']!r:34} -> {ok}")
