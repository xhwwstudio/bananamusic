"""End-to-end compliance test for the MusicOn Subsonic layer.

Simulates the call sequence used by Symfonium when adding a server and
syncing the library, and asserts the JSON/XML shapes required by the
Subsonic 1.16.1 + OpenSubsonic specification.

Run:  .venv312\\Scripts\\python.exe tools\\test_subsonic_compliance.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "work" / "compliance.db"
DB.parent.mkdir(exist_ok=True)
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + DB.as_posix()

BACKEND = ROOT / "backend"
if not (BACKEND / "app").exists():
    BACKEND = ROOT / "src" / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.db.session import engine  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


ARTISTS_ALBUMS = [
    ("Tyson Yoshi、王嘉尔", "I'm Just Your Type (命中偏爱)"),
    ("AC/DC", "Back In Black"),
    ("Ulaa（尤拉）", "Fading Light(渐暗的光)"),
    ("一毫升", "夜夜夜夜（R&B版）"),
    ("周杰伦", "叶惠美"),
    ("李翊君", "经典再回首"),
    ("李翊君", "誓言谎言"),
    ("The Beatles", "Abbey Road"),
    ("小野リサ", "旅 そして ふるさと"),
    ("A&B", "C++ / D#"),
]

songs = []
for artist, album in ARTISTS_ALBUMS:
    for index in range(3):
        songs.append(
            {
                "id": f"id-{len(songs):03d}",
                "title": f"Track {index + 1} of {album}",
                "artist": artist,
                "album": album,
                "fmt": "FLAC" if index % 2 == 0 else "MP3",
                "dur": 180 + index,
                "path": f"/Music/{artist}/{album}/{index + 1}.flac",
            }
        )

with engine.begin() as conn:
    for song in songs:
        conn.execute(
            text(
                "INSERT INTO songs (id, title, artist, album, fmt, dur, path, plays) "
                "VALUES (:id, :title, :artist, :album, :fmt, :dur, :path, 0)"
            ),
            song,
        )

client = TestClient(main.app)
SALT = "deadbeef"
PASSWORD = "changeme"
TOKEN = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()


def call(path: str, fmt: str = "json", **params):
    query = {"u": "admin", "t": TOKEN, "s": SALT, "v": "1.16.1", "c": "Symfonium", "f": fmt}
    query.update(params)
    response = client.get(f"/rest/{path}", params=query)
    if fmt == "json":
        try:
            return response.status_code, response.json()
        except Exception:
            return response.status_code, response.text
    return response.status_code, response.text


def body(payload):
    return payload.get("subsonic-response", {}) if isinstance(payload, dict) else {}


def parse_xml(xml_text: str):
    root = ET.fromstring(xml_text)
    for element in root.iter():
        if isinstance(element.tag, str):
            element.tag = element.tag.split("}")[-1]
    return root


UNIQUE_ARTISTS = {artist for artist, _ in ARTISTS_ALBUMS}


def walk_types(value, path="$"):
    """Catch the classic Subsonic JSON type errors."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "starred":
                check(isinstance(item, str), f"{path}.starred must be an ISO string, got {type(item).__name__}")
            if key in {"created", "changed", "lastPlayed"} and item is not None:
                check(isinstance(item, str), f"{path}.{key} must be a string, got {type(item).__name__}")
            walk_types(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk_types(item, f"{path}[{index}]")


print("[1] ping / extensions / indexes")
status, payload = call("ping.view")
check(status == 200 and body(payload).get("status") == "ok", "ping failed")

status, payload = call("getOpenSubsonicExtensions.view")
extensions = body(payload).get("openSubsonicExtensions")
check(isinstance(extensions, list), f"openSubsonicExtensions must be a JSON array, got {type(extensions).__name__}")
walk_types(payload)

status, payload = call("getIndexes.view")
indexes = body(payload).get("indexes", {})
check(isinstance(indexes.get("shortcut"), list), "indexes.shortcut must be a JSON array")
check(isinstance(indexes.get("index"), list), "indexes.index must be a JSON array")
for group in indexes.get("index", []):
    check(group.get("name") in [chr(c) for c in range(65, 91)] + ["#"], f"bad index letter {group.get('name')!r}")
    check(isinstance(group.get("artist"), list), "index.artist must be a list")

status, payload = call("getArtists.view")
check(status == 200 and body(payload).get("status") == "ok", "getArtists failed")
walk_types(payload)

print("[2] artist id round-trip (getArtists -> getArtist / getMusicDirectory)")
artist_ids = []
for group in body(payload).get("artists", {}).get("index", []):
    for artist in group.get("artist", []):
        artist_ids.append((artist["id"], artist["name"]))
check(len(artist_ids) == len(UNIQUE_ARTISTS), f"expected {len(UNIQUE_ARTISTS)} artists, got {len(artist_ids)}")
for artist_id, name in artist_ids:
    status, detail = call("getArtist.view", id=artist_id)
    check(body(detail).get("status") == "ok", f"getArtist({artist_id!r}) failed for {name!r}")
    if body(detail).get("status") == "ok":
        artist = body(detail)["artist"]
        check(artist["name"] == name, f"getArtist({artist_id!r}) returned {artist['name']!r} instead of {name!r}")
        expected = 3 * sum(1 for artist_name, _ in ARTISTS_ALBUMS if artist_name == name)
        check(len(artist.get("song", [])) == expected, f"getArtist({name!r}) should return all {expected} songs, got {len(artist.get('song', []))}")
        for album in artist.get("album", []):
            check(album["artistId"] == artist_id, "album.artistId must match the artist id")
    status, directory = call("getMusicDirectory.view", id=artist_id)
    check(body(directory).get("status") == "ok", f"getMusicDirectory({artist_id!r}) failed for artist {name!r}")
    status, info = call("getArtistInfo2.view", id=artist_id)
    check(body(info).get("status") == "ok", f"getArtistInfo2({artist_id!r}) failed")

print("[3] album paging + album id round-trip (getAlbumList2 -> getAlbum)")
status, payload = call("getAlbumList2.view", type="alphabeticalByName", size=3, offset=0)
check(status == 200 and body(payload).get("status") == "ok", "getAlbumList2 failed")
walk_types(payload)
albums: list[dict] = []
offset = 0
while True:
    status, page = call("getAlbumList2.view", type="alphabeticalByName", size=3, offset=offset)
    rows = body(page).get("albumList2", {}).get("album", [])
    if not rows:
        break
    albums.extend(rows)
    offset += 3
    if offset > 100:
        break
check(len(albums) == len(ARTISTS_ALBUMS), f"paged album count {len(albums)} != {len(ARTISTS_ALBUMS)}")

for album in albums:
    status, detail = call("getAlbum.view", id=album["id"])
    check(body(detail).get("status") == "ok", f"getAlbum({album['id']!r}) failed for {album['name']!r}")
    if body(detail).get("status") == "ok":
        songs_in_album = body(detail)["album"].get("song", [])
        check(len(songs_in_album) == album["songCount"], f"getAlbum({album['name']!r}) songCount mismatch")
        for song in songs_in_album:
            check(song["parent"] == album["id"], f"song.parent {song['parent']!r} != album id {album['id']!r}")
            check(song["album"] == album["name"], "song.album mismatch")
        cover_id = body(detail)["album"].get("coverArt")
        if cover_id:
            status, cover = call("getCoverArt.view", id=cover_id)
            check(status == 404, "cover art for a song without artwork should 404 cleanly")

print("[4] music folder browsing")
status, payload = call("getMusicDirectory.view", id="music-001")
root_children = body(payload).get("directory", {}).get("child", [])
check(len(root_children) == len(UNIQUE_ARTISTS), f"root folder children {len(root_children)} != {len(UNIQUE_ARTISTS)}")
for child in root_children:
    status, payload = call("getMusicDirectory.view", id=child["id"])
    check(body(payload).get("status") == "ok", f"artist folder {child['id']!r} unreadable")

print("[5] genres / search / random / similar / top")
status, payload = call("getGenres.view")
check(isinstance(body(payload).get("genres", {}).get("genre"), list), "genres.genre must be a list")
status, payload = call("getSongsByGenre.view", genre="", count=10)
check(status == 200, "getSongsByGenre failed")
status, payload = call("search3.view", query="Track", artistCount=10, albumCount=10, songCount=10)
check(status == 200 and "searchResult3" in body(payload), "search3 failed")
walk_types(payload)
status, payload = call("getRandomSongs.view", size=10)
check(len(body(payload).get("randomSongs", {}).get("song", [])) == 10, "getRandomSongs size mismatch")
first_song_id = songs[0]["id"]
status, payload = call("getSimilarSongs2.view", id=first_song_id, count=5)
check(body(payload).get("status") == "ok", "getSimilarSongs2 failed")
status, payload = call("getTopSongs.view", artist="周杰伦", count=5)
check(body(payload).get("status") == "ok", "getTopSongs failed")
status, payload = call("getArtistInfo2.view", id=artist_ids[0][0])
check(body(payload).get("status") == "ok", "getArtistInfo2 failed")

print("[6] star / rating / bookmark / scrobble / lyrics")
status, payload = call("star.view", id=first_song_id)
check(body(payload).get("status") == "ok", "star failed")
status, payload = call("getStarred2.view")
starred_songs = body(payload).get("starred2", {}).get("song", [])
check(len(starred_songs) == 1, f"expected 1 starred song, got {len(starred_songs)}")
if starred_songs:
    check(isinstance(starred_songs[0].get("starred"), str), "starred song must carry an ISO `starred` string")
walk_types(payload)
status, payload = call("unstar.view", id=first_song_id)
check(body(payload).get("status") == "ok", "unstar failed")
status, payload = call("setRating.view", id=first_song_id, rating=4)
check(body(payload).get("status") == "ok", "setRating failed")
status, payload = call("saveBookmark.view", id=first_song_id, position=12345)
check(body(payload).get("status") == "ok", "saveBookmark failed")
status, payload = call("getBookmarks.view")
check(len(body(payload).get("bookmarks", {}).get("bookmark", [])) == 1, "bookmark missing")
status, payload = call("scrobble.view", id=first_song_id, submission="true")
check(body(payload).get("status") == "ok", "scrobble failed")
status, payload = call("getNowPlaying.view")
check(body(payload).get("status") == "ok", "getNowPlaying failed")
status, payload = call("search3.view", query="周杰伦", artistCount=5, albumCount=5, songCount=5)
check(body(payload).get("status") == "ok", "search3.view failed")
status, payload = call("search2.view", query="周杰伦", artistCount=5, albumCount=5, songCount=5)
check(body(payload).get("status") == "ok", "search2.view failed")
status, payload = call("getLyrics.view", artist="周杰伦", title="Track 1 of 叶惠美")
check(body(payload).get("status") == "ok", "getLyrics failed")
status, payload = call("getLyricsBySongId.view", id=first_song_id)
check(isinstance(body(payload).get("lyricsList", {}).get("structuredLyrics"), list), "lyricsList.structuredLyrics must be a list")

print("[7] playlists (create / read / replace / delete)")
status, payload = call("createPlaylist.view", name="Sync Test", songId=[songs[0]["id"], songs[1]["id"]])
check(body(payload).get("status") == "ok", "createPlaylist failed")
playlist_id = body(payload).get("playlist", {}).get("id")
status, payload = call("getPlaylist.view", id=playlist_id)
entries = body(payload).get("playlist", {}).get("entry", [])
check(len(entries) == 2, f"playlist should have 2 entries, got {len(entries)}")
walk_types(payload)
status, payload = call("createPlaylist.view", playlistId=playlist_id, name="Sync Test 2", songId=[songs[2]["id"]])
check(body(payload).get("status") == "ok", "createPlaylist with playlistId failed")
status, payload = call("getPlaylist.view", id=playlist_id)
check(len(body(payload).get("playlist", {}).get("entry", [])) == 1, "playlist replace did not apply")
status, payload = call("getPlaylists.view")
playlists = body(payload).get("playlists", {}).get("playlist", [])
check(
    len([item for item in playlists if str(item.get("name") or "").startswith("Sync Test")]) == 1,
    "getPlaylists should list the created playlist exactly once",
)
status, payload = call("deletePlaylist.view", id=playlist_id)
check(body(payload).get("status") == "ok", "deletePlaylist failed")

print("[8] scan status / auth variants")
status, payload = call("getScanStatus.view")
check(body(payload).get("scanStatus", {}).get("scanning") is False, "getScanStatus should not claim a running scan")
status, payload = call("startScan.view")
check(body(payload).get("scanStatus", {}).get("scanning") is False, "startScan must not hang clients in a fake scanning state")

plain = client.get("/rest/ping.view", params={"u": "admin", "p": PASSWORD, "v": "1.16.1", "c": "test", "f": "json"})
check(plain.status_code == 200, "plaintext p= auth failed")
hexed = client.get("/rest/ping.view", params={"u": "admin", "p": PASSWORD.encode().hex(), "v": "1.16.1", "c": "test", "f": "json"})
check(hexed.status_code == 200, "hex p= auth failed")
anonymous = client.get("/rest/ping.view", params={"u": "admin", "v": "1.16.1", "c": "test", "f": "json"})
check(anonymous.status_code == 401, "missing credentials should be rejected")

print("[9] XML shapes")
status, xml_text = call("getOpenSubsonicExtensions.view", fmt="xml")
root = parse_xml(xml_text)
check(root.tag == "subsonic-response", "xml root tag")
check(root.find("openSubsonicExtensions") is not None, "xml wrapper element missing")
status, xml_text = call("getIndexes.view", fmt="xml")
root = parse_xml(xml_text)
check(root.find("indexes") is not None, "xml indexes missing")
check(len(root.findall("indexes/index")) > 0, "xml indexes empty")
status, xml_text = call("getArtists.view", fmt="xml")
root = parse_xml(xml_text)
check(len(root.findall("artists/index/artist")) == len(UNIQUE_ARTISTS), "xml artist count")
status, xml_text = call("getAlbum.view", fmt="xml", id=albums[0]["id"])
root = parse_xml(xml_text)
check(root.find("album") is not None, "xml album missing")
status, xml_text = call("getAlbumList2.view", fmt="xml", type="newest", size=5)
root = parse_xml(xml_text)
check(root.find("albumList2") is not None, "xml albumList2 missing")
status, xml_text = call("getStarred2.view", fmt="xml")
root = parse_xml(xml_text)
check(root.find("starred2") is not None, "xml starred2 missing")

print()
print(f"checks: {CHECKS}, failures: {len(FAILURES)}")
if FAILURES:
    for failure in FAILURES[:40]:
        print(" -", failure)
    sys.exit(1)
print("ALL COMPLIANCE CHECKS PASSED")
