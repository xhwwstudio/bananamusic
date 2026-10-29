from __future__ import annotations

import hashlib
import json
import random
import re
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from mutagen import File as MutagenFile

from app.core.config import settings
from app.core.slugs import slugify
from app.db.compat import dt_to_iso
from app.services.storage import get_storage_service
from app.services.app_settings import get_runtime_settings
from app.services.cover_cache import save_cover_bytes
from app.services.db_data import (
    build_album_list,
    build_indexes,
    create_playlist,
    delete_playlist,
    get_bookmark,
    get_playlist,
    get_song,
    get_song_rating,
    get_starred_at,
    get_subsonic_user,
    get_task,
    is_starred_item,
    list_bookmarks,
    list_genres,
    list_newest_songs,
    list_playlists,
    list_random_songs,
    list_recent_plays,
    list_songs,
    list_songs_by_genre,
    list_starred_items,
    list_tasks,
    record_play_event,
    save_bookmark,
    set_song_rating,
    set_starred_item,
    update_playlist,
    update_song,
)


MAX_SEARCH_RESULTS = 20
SUBSONIC_API_VERSION = '1.16.1'
SUBSONIC_SERVER_TYPE = 'musicon'
DIRTY_ARTISTS = {'unknown artist'}
DIRTY_ALBUMS = {'unknown album'}
DEFAULT_MUSIC_FOLDER_ID = 'music-001'
DEFAULT_MUSIC_FOLDER_NAME = 'MusicOn Library'


def _library_stats() -> dict[str, int]:
    songs = list_songs()
    album_count = len({str(song.get('album') or '').strip() for song in songs if str(song.get('album') or '').strip()})
    artist_count = len({str(song.get('artist') or '').strip() for song in songs if str(song.get('artist') or '').strip()})
    return {
        'count': len(songs),
        'albumCount': album_count,
        'artistCount': artist_count,
        'folderCount': 1,
    }


def _is_admin(auth: dict | None) -> bool:
    return str((auth or {}).get('role') or 'user') == 'admin'


def _can_view_playlist(auth: dict | None, playlist: dict | None) -> bool:
    if not playlist:
        return False
    if _is_admin(auth):
        return True
    username = str((auth or {}).get('username') or '')
    if str(playlist.get('scope') or '') == 'public_system':
        return True
    return bool(playlist.get('public')) or str(playlist.get('owner') or '') == username


def _can_modify_playlist(auth: dict | None, playlist: dict | None) -> bool:
    if not playlist:
        return False
    if _is_admin(auth):
        return True
    username = str((auth or {}).get('username') or '')
    if bool(playlist.get('is_readonly')) or str(playlist.get('scope') or '') == 'public_system':
        return False
    return str(playlist.get('owner') or '') == username

router = APIRouter(prefix='/rest', tags=['subsonic'])
response_format_var: ContextVar[str] = ContextVar('subsonic_response_format', default='xml')
current_auth_var: ContextVar[dict | None] = ContextVar('subsonic_current_auth', default=None)


def _xml_scalar(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _xml_append(parent: Element, key: str, value):
    if value is None:
        return

    # OpenSubsonic defines `openSubsonicExtensions` as a JSON array whose XML
    # representation is a wrapper element containing repeated
    # `openSubsonicExtension` children.  The generic list serializer would
    # repeat the wrapper instead, so handle it explicitly.
    if key == 'openSubsonicExtensions':
        holder = SubElement(parent, key)
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict):
                continue
            extension = SubElement(holder, 'openSubsonicExtension')
            extension.set('name', _xml_scalar(item.get('name', '')))
            versions = item.get('versions') or []
            if isinstance(versions, (list, tuple)):
                versions = ','.join(str(version) for version in versions)
            extension.set('versions', _xml_scalar(versions))
        return

    if isinstance(value, list):
        for item in value:
            _xml_append(parent, key, item)
        return

    if isinstance(value, dict):
        child = SubElement(parent, key)
        primitive_items = {}
        complex_items = {}
        for sub_key, sub_value in value.items():
            if sub_value is None:
                continue
            if isinstance(sub_value, (dict, list)):
                complex_items[sub_key] = sub_value
            else:
                primitive_items[sub_key] = sub_value

        if key == 'lyrics':
            for attr_key in ('artist', 'title'):
                if attr_key in primitive_items:
                    child.set(attr_key, _xml_scalar(primitive_items.pop(attr_key)))
            if 'value' in primitive_items:
                value_child = SubElement(child, 'value')
                value_child.text = _xml_scalar(primitive_items.pop('value'))
            for attr_key, attr_value in primitive_items.items():
                child.set(attr_key, _xml_scalar(attr_value))
            for sub_key, sub_value in complex_items.items():
                _xml_append(child, sub_key, sub_value)
            return

        if primitive_items and not complex_items:
            for attr_key, attr_value in primitive_items.items():
                child.set(attr_key, _xml_scalar(attr_value))
            return

        for attr_key, attr_value in primitive_items.items():
            child.set(attr_key, _xml_scalar(attr_value))
        for sub_key, sub_value in complex_items.items():
            _xml_append(child, sub_key, sub_value)
        return

    child = SubElement(parent, key)
    child.text = _xml_scalar(value)


def _subsonic_xml(payload: dict) -> Response:
    body = payload.get('subsonic-response', {})
    root = Element('subsonic-response')
    root.set('xmlns', 'http://subsonic.org/restapi')
    root.set('status', str(body.get('status', 'ok')))
    root.set('version', str(body.get('version', SUBSONIC_API_VERSION)))
    root.set('type', str(body.get('type', SUBSONIC_SERVER_TYPE)))
    root.set('serverVersion', str(body.get('serverVersion', SUBSONIC_API_VERSION)))
    root.set('openSubsonic', 'true' if bool(body.get('openSubsonic', True)) else 'false')
    for key, value in body.items():
        if key in {'status', 'version', 'type', 'serverVersion', 'openSubsonic'}:
            continue
        _xml_append(root, key, value)
    xml_bytes = tostring(root, encoding='utf-8', xml_declaration=True)
    return Response(content=xml_bytes, media_type='application/xml; charset=utf-8')


def _finalize_subsonic(payload: dict):
    if response_format_var.get() == 'json':
        return payload
    return _subsonic_xml(payload)


_HEX_ONLY = re.compile(r'^[0-9a-fA-F]+$')


def _password_candidates(value: str) -> list[str]:
    """Subsonic sends `p` hex-encoded; tolerate plain-text senders as well."""
    raw = str(value or '')
    candidates = [raw]
    if raw and len(raw) % 2 == 0 and _HEX_ONLY.match(raw):
        try:
            decoded = bytes.fromhex(raw).decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            decoded = ''
        if decoded and decoded not in candidates:
            candidates.insert(0, decoded)
    return candidates


def _subsonic_credentials() -> tuple[str, str]:
    runtime = get_runtime_settings()
    return (
        str(runtime.get('subsonic_user') or settings.subsonic_user),
        str(runtime.get('subsonic_pass') or settings.subsonic_pass),
    )


def verify_subsonic_auth(
    u: str = Query(...),
    t: str | None = Query(None),
    s: str | None = Query(None),
    p: str | None = Query(None),
    f: str | None = Query(None),
) -> dict:
    response_format_var.set('json' if (f or '').strip().lower() == 'json' else 'xml')

    candidates: list[tuple[str, str, str]] = []
    user = get_subsonic_user(u)
    if user and bool(user.get('enabled')):
        candidates.append(
            (str(user.get('username') or u), str(user.get('password') or ''), str(user.get('role') or 'user'))
        )
    runtime_user, runtime_pass = _subsonic_credentials()
    candidates.append((runtime_user, runtime_pass, 'admin'))

    token = str(t or '').lower()
    salt = str(s or '')
    plain_candidates = _password_candidates(p) if p is not None else []

    for username, password, role in candidates:
        if username != u:
            continue
        authenticated = False
        if token and salt:
            authenticated = token == hashlib.md5((password + salt).encode()).hexdigest()
        if not authenticated:
            authenticated = any(candidate == password for candidate in plain_candidates)
        if authenticated:
            auth = {'username': username, 'role': role, 'enabled': True}
            current_auth_var.set(auth)
            return auth

    raise HTTPException(status_code=401, detail='Wrong username or password')


def ok(data: dict | None = None):
    payload = {
        'subsonic-response': {
            'status': 'ok',
            'version': SUBSONIC_API_VERSION,
            'type': SUBSONIC_SERVER_TYPE,
            'serverVersion': SUBSONIC_API_VERSION,
            'openSubsonic': True,
        }
    }
    if data:
        payload['subsonic-response'].update(data)
    return _finalize_subsonic(payload)


def fail(message: str, code: int = 70):
    payload = {
        'subsonic-response': {
            'status': 'failed',
            'version': SUBSONIC_API_VERSION,
            'type': SUBSONIC_SERVER_TYPE,
            'serverVersion': SUBSONIC_API_VERSION,
            'openSubsonic': True,
            'error': {'code': code, 'message': message},
        }
    }
    return _finalize_subsonic(payload)


def _is_dirty_song(song: dict) -> bool:
    artist = str(song.get('artist') or '').strip().lower()
    album = str(song.get('album') or '').strip().lower()
    path = str(song.get('path') or '').strip()
    title = str(song.get('title') or '').strip()
    if artist in DIRTY_ARTISTS or album in DIRTY_ALBUMS:
        return True
    if path.startswith('/') and path.count('/') <= 1:
        return True
    if ' _ ' in title and artist in DIRTY_ARTISTS:
        return True
    return False


def _clean_search_matches(matched_songs: list[dict]) -> list[dict]:
    clean = [song for song in matched_songs if not _is_dirty_song(song)]
    pool = clean or matched_songs
    deduped: list[dict] = []
    seen = set()
    for song in pool:
        key = (
            str(song.get('title') or '').strip().lower(),
            str(song.get('artist') or '').strip().lower(),
            str(song.get('album') or '').strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(song)
    return deduped



def _slugify(value: str, fallback_prefix: str = 'item') -> str:
    # Single source of truth shared with the DB layer so ids returned by
    # listing endpoints always resolve in the detail endpoints.
    return slugify(value, fallback_prefix)


def _artist_slug(name: str) -> str:
    return _slugify(name, 'artist')



def _all_artist_names(songs: list[dict]) -> list[str]:
    names: list[str] = []
    seen = set()
    for song in songs:
        name = str(song.get('artist') or '').strip()
        if not name:
            continue
        slug = _artist_slug(name)
        if slug in seen:
            continue
        seen.add(slug)
        names.append(name)
    return names



def _resolve_artist_name(artist_id: str | None, songs: list[dict]) -> str | None:
    wanted = (artist_id or '').strip()
    if not wanted:
        names = _all_artist_names(songs)
        return names[0] if names else None
    for song in songs:
        if _artist_slug(song.get('artist') or '') == wanted:
            return song.get('artist')
    return None



def _artist_payload(artist_id: str | None, songs: list[dict], auth: dict | None = None) -> dict | None:
    artist_name = _resolve_artist_name(artist_id, songs)
    if not artist_name:
        return None
    artist_songs = [song for song in songs if str(song.get('artist') or '') == artist_name]
    albums = []
    seen = set()
    for song in artist_songs:
        album_id = _slugify(song.get('album') or '', 'album')
        if album_id in seen:
            continue
        seen.add(album_id)
        albums.append(to_subsonic_album(song, songs))
    return {
        'id': _artist_slug(artist_name),
        'name': artist_name,
        'albumCount': len(albums),
        'coverArt': next((song['id'] for song in artist_songs if song.get('cover_path')), ''),
        'album': albums,
        'song': [to_subsonic_song(song, auth) for song in artist_songs],
    }


def _lyrics_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        parts = [_lyrics_text(item) for item in value]
        return '\n'.join(part for part in parts if part).strip()
    text = getattr(value, 'text', value)
    if isinstance(text, list):
        parts = [_lyrics_text(item) for item in text]
        return '\n'.join(part for part in parts if part).strip()
    if isinstance(text, bytes):
        for encoding in ('utf-8', 'utf-16', 'utf-16le', 'utf-16be', 'gb18030', 'latin-1'):
            try:
                decoded = text.decode(encoding, 'ignore').strip('\x00\r\n ')
                if decoded:
                    return decoded
            except Exception:
                continue
        return ''
    return str(text).strip()


def _extract_embedded_lyrics(song: dict | None) -> str:
    if not song:
        return ''
    path = str(song.get('path') or '').strip()
    if not path:
        return ''
    try:
        storage = get_storage_service()
        ref = storage.make_ref(path)
        content = storage.read_range(ref, 0, 1024 * 1024 - 1)
        audio = MutagenFile(BytesIO(content))
        if audio is None:
            return ''
        tags = getattr(audio, 'tags', None)
        if not tags:
            return ''

        lyric_keys = ['USLT::eng', 'USLT::XXX', 'USLT', 'LYRICS', 'lyrics', 'LYRIC', '©lyr']
        for key in lyric_keys:
            if key in tags:
                text = _lyrics_text(tags.get(key))
                if text:
                    return text

        for key in tags.keys():
            upper = str(key).upper()
            if upper.startswith('USLT') or 'LYRICS' in upper or upper in {'LYRIC', '©LYR'}:
                text = _lyrics_text(tags.get(key))
                if text:
                    return text
        return ''
    except Exception:
        return ''


def _virtual_meta(song: dict | None) -> dict:
    if not song:
        return {}
    raw_url = song.get('raw_url') or ''
    if not raw_url:
        return {}
    try:
        data = json.loads(raw_url)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _virtual_cover_url(song: dict | None) -> str:
    meta = _virtual_meta(song)
    return str(meta.get('img') or meta.get('pic') or '').strip()


def _ensure_virtual_cover(song: dict | None) -> str:
    if not song:
        return ''
    existing = str(song.get('cover_path') or '').strip()
    if existing:
        path = Path(existing)
        if path.exists():
            return existing

    cover_url = _virtual_cover_url(song)
    if not cover_url:
        return ''

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            res = client.get(cover_url)
            res.raise_for_status()
            content_type = str(res.headers.get('content-type') or '').lower()
            suffix = 'png' if 'png' in content_type else 'jpg'
            saved_path = save_cover_bytes(res.content, suffix=suffix)
            if saved_path:
                update_song(str(song.get('id') or ''), {'cover_path': saved_path})
                song['cover_path'] = saved_path
                return saved_path
    except Exception:
        return ''
    return ''


def _fetch_virtual_lyrics(song: dict | None) -> str:
    if not song:
        return ''
    current = str(song.get('lyrics') or '').strip()
    if current:
        return current

    meta = _virtual_meta(song)
    source = str(meta.get('source') or '').strip()
    song_id = meta.get('songmid') or meta.get('songId') or meta.get('id')
    if not source or not song_id:
        return ''

    lx_url = str(get_runtime_settings().get('lx_server_url') or '').rstrip('/')
    if not lx_url:
        return ''

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            res = client.get(f"{lx_url}/api/music/lyric", params={'source': source, 'songId': song_id})
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                lyrics = str(data.get('lrc') or data.get('lyric') or data.get('lxlyric') or '').strip()
                if not lyrics and isinstance(data.get('data'), dict):
                    lyrics = str(data.get('data', {}).get('lrc') or '').strip()
            else:
                lyrics = ''
            if lyrics and lyrics not in ('暂无歌词', '[00:00.00]暂无歌词'):
                update_song(str(song.get('id') or ''), {'lyrics': lyrics})
                song['lyrics'] = lyrics
                return lyrics
    except Exception:
        return ''
    return ''


@router.api_route('/ping', methods=['GET', 'POST'])
@router.api_route('/ping.view', methods=['GET', 'POST'])
def ping(_: str = Query(None), auth: str = Query(None), user: str = Query(None), _ok: str = Query(None), __auth: str = Depends(verify_subsonic_auth)):
    return ok()


@router.api_route('/getLicense', methods=['GET', 'POST'])
@router.api_route('/getLicense.view', methods=['GET', 'POST'])
def get_license(__auth: str = Depends(verify_subsonic_auth)):
    return ok({'license': {'valid': True, 'email': 'musicon@local', 'licenseExpires': '2099-12-31T00:00:00Z'}})


@router.api_route('/getOpenSubsonicExtensions', methods=['GET', 'POST'])
@router.api_route('/getOpenSubsonicExtensions.view', methods=['GET', 'POST'])
def get_open_subsonic_extensions(__auth: str = Depends(verify_subsonic_auth)):
    # OpenSubsonic spec: `openSubsonicExtensions` is an ARRAY of objects
    # {"name": str, "versions": [int]}.  Returning an object here made strict
    # clients (Symfonium) abort the whole library sync with
    # "Expected BEGIN_ARRAY but was BEGIN_OBJECT".
    # Only advertise extensions that are actually implemented.
    return ok({'openSubsonicExtensions': []})


@router.api_route('/getMusicFolders', methods=['GET', 'POST'])
@router.api_route('/getMusicFolders.view', methods=['GET', 'POST'])
def get_music_folders(__auth: str = Depends(verify_subsonic_auth)):
    payload = {'musicFolders': {'musicFolder': [{'id': DEFAULT_MUSIC_FOLDER_ID, 'name': DEFAULT_MUSIC_FOLDER_NAME}]}}
    print(f"[subsonic:resp] getMusicFolders folders=1")
    return ok(payload)


@router.api_route('/getIndexes', methods=['GET', 'POST'])
@router.api_route('/getIndexes.view', methods=['GET', 'POST'])
def get_indexes(__auth: dict = Depends(verify_subsonic_auth)):
    payload = build_indexes()
    # `shortcut` is an array in the Subsonic schema; `{}` broke strict JSON
    # parsers with "Expected BEGIN_ARRAY but was BEGIN_OBJECT".
    payload['shortcut'] = []
    artist_total = sum(len(item.get('artist', [])) for item in payload.get('index', []))
    print(f"[subsonic:resp] getIndexes artists={artist_total} groups={len(payload.get('index', []))}")
    return ok({'indexes': payload})


@router.api_route('/getArtists', methods=['GET', 'POST'])
@router.api_route('/getArtists.view', methods=['GET', 'POST'])
def get_artists(__auth: dict = Depends(verify_subsonic_auth)):
    payload = build_indexes()
    artist_total = sum(len(item.get('artist', [])) for item in payload.get('index', []))
    print(f"[subsonic:resp] getArtists artists={artist_total} groups={len(payload.get('index', []))}")
    return ok({'artists': payload})


@router.api_route('/getGenres', methods=['GET', 'POST'])
@router.api_route('/getGenres.view', methods=['GET', 'POST'])
def get_genres(__auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs()
    genres = []
    for row in list_genres():
        genre_name = str(row.get('genre') or '').strip()
        album_count = len({str(song.get('album') or '').strip() for song in songs if str(song.get('genre') or '').strip() == genre_name and str(song.get('album') or '').strip()})
        genres.append({'value': genre_name, 'songCount': int(row.get('song_count') or 0), 'albumCount': album_count})
    return ok({'genres': {'genre': genres}})


@router.api_route('/getSongsByGenre', methods=['GET', 'POST'])
@router.api_route('/getSongsByGenre.view', methods=['GET', 'POST'])
def get_songs_by_genre(genre: str, count: int = Query(50), offset: int = Query(0), __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs_by_genre(genre, count, offset)
    return ok({'songsByGenre': {'song': [to_subsonic_song(song, __auth) for song in songs]}})


@router.api_route('/getUser', methods=['GET', 'POST'])
@router.api_route('/getUser.view', methods=['GET', 'POST'])
def get_user(username: str | None = None, __auth: str = Depends(verify_subsonic_auth)):
    target = username or settings.subsonic_user
    return ok({
        'user': {
            'username': target,
            'email': '',
            'scrobblingEnabled': True,
            'adminRole': True,
            'settingsRole': True,
            'downloadRole': True,
            'uploadRole': False,
            'playlistRole': True,
            'coverArtRole': True,
            'commentRole': False,
            'podcastRole': False,
            'streamRole': True,
            'jukeboxRole': False,
            'shareRole': False,
            'videoConversionRole': False,
            'transcodeRole': False,
            'avatarLastChanged': 0,
            'maxBitRate': 0,
            'folder': [DEFAULT_MUSIC_FOLDER_ID],
        }
    })


@router.api_route('/getAlbumList', methods=['GET', 'POST'])
@router.api_route('/getAlbumList.view', methods=['GET', 'POST'])
def get_album_list(
    type: str = Query('random'),
    size: int = Query(10),
    offset: int = Query(0),
    fromYear: int | None = Query(None),
    toYear: int | None = Query(None),
    genre: str | None = Query(None),
    musicFolderId: str | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    albums = build_album_list()
    normalized_type = (type or 'random').strip().lower()
    wanted_genre = str(genre or '').strip().lower()
    if wanted_genre:
        albums = [item for item in albums if str(item.get('genre') or '').strip().lower() == wanted_genre]

    if normalized_type in {'newest', 'recent'}:
        albums = sorted(albums, key=lambda item: str(item.get('created') or ''), reverse=True)
    elif normalized_type == 'alphabeticalbyartist':
        albums = sorted(albums, key=lambda item: (str(item.get('artist') or '').lower(), str(item.get('name') or '').lower()))
    elif normalized_type == 'alphabeticalbyname':
        albums = sorted(albums, key=lambda item: str(item.get('name') or '').lower())
    elif normalized_type in {'frequent', 'highest'}:
        song_map = {_slugify(song['album'], 'album'): int(song.get('plays') or 0) for song in list_songs()}
        albums = sorted(albums, key=lambda item: song_map.get(str(item.get('id') or ''), 0), reverse=True)
    elif normalized_type == 'random':
        albums = list(albums)
        random.shuffle(albums)

    _ = (fromYear, toYear, musicFolderId)
    safe_size = max(0, min(int(size or 0), 500))
    safe_offset = max(0, int(offset or 0))
    paged = albums[safe_offset:safe_offset + safe_size] if safe_size else []
    print(f"[subsonic:resp] getAlbumList type={normalized_type} total={len(albums)} returned={len(paged)} offset={safe_offset} size={safe_size}")
    return ok({'albumList': {'album': paged}})


@router.api_route('/startScan', methods=['GET', 'POST'])
@router.api_route('/startScan.view', methods=['GET', 'POST'])
def start_scan(__auth: dict = Depends(verify_subsonic_auth)):
    # Scanning is driven by the web console / scheduler.  Reporting a hard
    # coded `scanning: true` made clients poll forever waiting for a scan that
    # never finished, so just report the real task state.
    return get_scan_status(__auth)


@router.api_route('/getAlbumList2', methods=['GET', 'POST'])
@router.api_route('/getAlbumList2.view', methods=['GET', 'POST'])
def get_album_list2(
    type: str = Query('random'),
    size: int = Query(20),
    offset: int = Query(0),
    fromYear: int | None = Query(None),
    toYear: int | None = Query(None),
    genre: str | None = Query(None),
    musicFolderId: str | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    albums = build_album_list()
    normalized_type = (type or 'random').strip().lower()
    wanted_genre = str(genre or '').strip().lower()

    if wanted_genre:
        albums = [item for item in albums if str(item.get('genre') or '').strip().lower() == wanted_genre]

    if normalized_type in {'newest', 'recent'}:
        # Newest first; albums without a timestamp sort last.
        albums = sorted(albums, key=lambda item: str(item.get('created') or ''), reverse=True)
    elif normalized_type == 'frequent':
        song_map = {_slugify(song['album'], 'album'): int(song.get('plays') or 0) for song in list_songs()}
        albums = sorted(albums, key=lambda item: song_map.get(str(item.get('id') or ''), 0), reverse=True)
    elif normalized_type == 'highest':
        song_map = {_slugify(song['album'], 'album'): int(song.get('plays') or 0) for song in list_songs()}
        albums = sorted(albums, key=lambda item: song_map.get(str(item.get('id') or ''), 0), reverse=True)
    elif normalized_type == 'starred':
        starred_ids = {
            _slugify(str(song.get('album') or ''), 'album')
            for song in list_starred_items(str(__auth.get('username') or '')).get('song', [])
        }
        albums = [item for item in albums if str(item.get('id') or '') in starred_ids]
    elif normalized_type == 'alphabeticalbyartist':
        albums = sorted(albums, key=lambda item: (str(item.get('artist') or '').lower(), str(item.get('name') or '').lower()))
    elif normalized_type in {'alphabeticalbyname', 'alphabetical'}:
        albums = sorted(albums, key=lambda item: str(item.get('name') or '').lower())
    elif normalized_type == 'random':
        albums = list(albums)
        random.shuffle(albums)
    else:
        albums = list(albums)

    # `year` is not stored yet, so fromYear/toYear are accepted but ignored
    # instead of filtering every album out.
    _ = (fromYear, toYear, musicFolderId)

    safe_size = max(0, min(int(size or 0), 500))
    safe_offset = max(0, int(offset or 0))
    paged = albums[safe_offset:safe_offset + safe_size] if safe_size else []
    print(f"[subsonic:resp] getAlbumList2 type={normalized_type} total={len(albums)} returned={len(paged)} offset={safe_offset} size={safe_size}")
    return ok({'albumList2': {'album': paged}})


@router.api_route('/getAlbum', methods=['GET', 'POST'])
@router.api_route('/getAlbum.view', methods=['GET', 'POST'])
def get_album(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    songs = [song for song in list_songs() if _slugify(song['album'], 'album') == id]
    if not songs:
        return fail('Album not found')
    album_payload = to_subsonic_album(songs[0], songs)
    album_payload['song'] = [to_subsonic_song(song, __auth) for song in songs]
    return ok({'album': album_payload})


@router.api_route('/getArtist', methods=['GET', 'POST'])
@router.api_route('/getArtist.view', methods=['GET', 'POST'])
def get_artist(id: str = Query(''), __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs()
    payload = _artist_payload(id, songs, __auth)
    if not payload:
        return fail('Artist not found')
    return ok({'artist': payload})


@router.api_route('/getArtistInfo', methods=['GET', 'POST'])
@router.api_route('/getArtistInfo.view', methods=['GET', 'POST'])
def get_artist_info(id: str = Query(''), count: int = Query(20), includeNotPresent: bool = Query(False), __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs()
    payload = _artist_payload(id, songs, __auth)
    if not payload:
        return fail('Artist not found')
    safe_count = max(0, min(int(count or 0), 100))
    return ok({
        'artistInfo': {
            'biography': f"{payload.get('name', '')} · {payload.get('albumCount', 0)} albums",
            'musicBrainzId': '',
            'lastFmUrl': '',
            'smallImageUrl': '',
            'mediumImageUrl': '',
            'largeImageUrl': '',
            'similarArtist': [
                to_subsonic_artist(name, songs)
                for name in _all_artist_names(songs)
                if _artist_slug(name) != payload['id']
            ][:safe_count],
        }
    })


@router.api_route('/getArtistInfo2', methods=['GET', 'POST'])
@router.api_route('/getArtistInfo2.view', methods=['GET', 'POST'])
def get_artist_info2(id: str = Query(''), count: int = Query(20), includeNotPresent: bool = Query(False), __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs()
    payload = _artist_payload(id, songs, __auth)
    if not payload:
        return fail('Artist not found')
    safe_count = max(0, min(int(count or 0), 100))
    return ok({
        'artistInfo2': {
            'biography': f"{payload.get('name', '')} · {payload.get('albumCount', 0)} albums",
            'musicBrainzId': '',
            'lastFmUrl': '',
            'smallImageUrl': '',
            'mediumImageUrl': '',
            'largeImageUrl': '',
            'similarArtist': [
                to_subsonic_artist(name, songs)
                for name in _all_artist_names(songs)
                if _artist_slug(name) != payload['id']
            ][:safe_count],
        }
    })


@router.api_route('/getAlbumInfo', methods=['GET', 'POST'])
@router.api_route('/getAlbumInfo.view', methods=['GET', 'POST'])
def get_album_info(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    songs = [song for song in list_songs() if _slugify(song.get('album') or '', 'album') == (id or '').strip()]
    if not songs:
        return fail('Album not found')
    album = to_subsonic_album(songs[0], songs)
    notes = f"{album.get('artist', '')} · {album.get('songCount', 0)} tracks"
    return ok({
        'albumInfo': {
            'notes': notes,
            'musicBrainzId': '',
            'lastFmUrl': '',
            'smallImageUrl': '',
            'mediumImageUrl': '',
            'largeImageUrl': '',
        }
    })


@router.api_route('/getAlbumInfo2', methods=['GET', 'POST'])
@router.api_route('/getAlbumInfo2.view', methods=['GET', 'POST'])
def get_album_info2(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    return get_album_info(id, __auth)


@router.api_route('/getSong', methods=['GET', 'POST'])
@router.api_route('/getSong.view', methods=['GET', 'POST'])
def get_song_endpoint(id: str, __auth: str = Depends(verify_subsonic_auth)):
    song = get_song(id)
    if not song:
        return fail('Song not found')
    return ok({'song': to_subsonic_song(song, __auth)})


@router.api_route('/getMusicDirectory', methods=['GET', 'POST'])
@router.api_route('/getMusicDirectory.view', methods=['GET', 'POST'])
def get_music_directory(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_songs()

    if id in {'', DEFAULT_MUSIC_FOLDER_ID}:
        artist_children = []
        seen_artists = set()
        for song in songs:
            artist_name = str(song.get('artist') or '').strip()
            if not artist_name:
                continue
            artist_id = _artist_slug(artist_name)
            if artist_id in seen_artists:
                continue
            seen_artists.add(artist_id)
            artist_children.append({
                'id': artist_id,
                'title': artist_name,
                'artist': artist_name,
                'isDir': True,
                'coverArt': song['id'] if song.get('cover_path') else '',
            })
        return ok({'directory': {'id': DEFAULT_MUSIC_FOLDER_ID, 'name': DEFAULT_MUSIC_FOLDER_NAME, 'child': artist_children}})

    artist_name = None
    for song in songs:
        slug = _artist_slug(song['artist'])
        if slug == id:
            artist_name = song['artist']
            break

    if artist_name:
        album_children = []
        seen = set()
        for song in songs:
            if song['artist'] != artist_name:
                continue
            album_id = _slugify(song['album'], 'album')
            if album_id in seen:
                continue
            seen.add(album_id)
            album_children.append({
                'id': album_id,
                'title': song['album'],
                'artist': song['artist'],
                'isDir': True,
                'coverArt': song['id'] if song.get('cover_path') else '',
            })
        return ok({'directory': {'id': id, 'name': artist_name, 'child': album_children}})

    album_songs = [song for song in songs if _slugify(song['album'], 'album') == id]
    if album_songs:
        return ok({'directory': {'id': id, 'name': album_songs[0]['album'], 'child': [to_subsonic_song(song, __auth) for song in album_songs]}})

    return fail('Directory not found')


def _search_payload(
    query: str,
    artist_count: int,
    album_count: int,
    song_count: int,
    artist_offset: int = 0,
    album_offset: int = 0,
    song_offset: int = 0,
    auth: dict | None = None,
) -> dict:
    normalized_query = query.strip().strip('"').lower()
    songs = list_songs()
    matched_songs = [
        song for song in songs
        if not normalized_query or normalized_query in song['title'].lower() or normalized_query in song['artist'].lower() or normalized_query in song['album'].lower()
    ]
    matched_songs = _clean_search_matches(matched_songs)

    artist_names = []
    artist_seen = set()
    albums = []
    album_seen = set()
    for song in matched_songs:
        if song['artist'] not in artist_seen:
            artist_seen.add(song['artist'])
            artist_names.append(song['artist'])
        album_id = _slugify(song['album'], 'album')
        if album_id not in album_seen:
            album_seen.add(album_id)
            albums.append(to_subsonic_album(song, songs))

    safe_artist_count = max(0, min(int(artist_count or 0), 500))
    safe_album_count = max(0, min(int(album_count or 0), 500))
    safe_song_count = max(0, min(int(song_count or 0), 500))
    safe_artist_offset = max(0, int(artist_offset or 0))
    safe_album_offset = max(0, int(album_offset or 0))
    safe_song_offset = max(0, int(song_offset or 0))

    artists_full = [to_subsonic_artist(name, songs) for name in artist_names]
    songs_window = matched_songs[safe_song_offset:safe_song_offset + safe_song_count] if safe_song_count else []
    songs_limited = [to_subsonic_song(song, auth) for song in songs_window]

    artists_limited = artists_full[safe_artist_offset:safe_artist_offset + safe_artist_count] if safe_artist_count else []
    albums_limited = albums[safe_album_offset:safe_album_offset + safe_album_count] if safe_album_count else []
    return {
        'artist': artists_limited,
        'album': albums_limited,
        'song': songs_limited,
    }


@router.api_route('/search2', methods=['GET', 'POST'])
@router.api_route('/search2.view', methods=['GET', 'POST'])
def search2(
    query: str = Query(''),
    artistCount: int = Query(MAX_SEARCH_RESULTS),
    albumCount: int = Query(MAX_SEARCH_RESULTS),
    songCount: int = Query(MAX_SEARCH_RESULTS),
    artistOffset: int = Query(0),
    albumOffset: int = Query(0),
    songOffset: int = Query(0),
    __auth: dict = Depends(verify_subsonic_auth),
):
    payload = _search_payload(query, artistCount, albumCount, songCount, artistOffset, albumOffset, songOffset, __auth)
    return ok({'searchResult2': payload})


@router.api_route('/search3', methods=['GET', 'POST'])
@router.api_route('/search3.view', methods=['GET', 'POST'])
def search3(
    query: str = Query(''),
    artistCount: int = Query(MAX_SEARCH_RESULTS),
    albumCount: int = Query(MAX_SEARCH_RESULTS),
    songCount: int = Query(MAX_SEARCH_RESULTS),
    artistOffset: int = Query(0),
    albumOffset: int = Query(0),
    songOffset: int = Query(0),
    __auth: dict = Depends(verify_subsonic_auth),
):
    payload = _search_payload(query, artistCount, albumCount, songCount, artistOffset, albumOffset, songOffset, __auth)
    return ok({'searchResult3': payload})


@router.api_route('/getPlaylists', methods=['GET', 'POST'])
@router.api_route('/getPlaylists.view', methods=['GET', 'POST'])
def get_playlists(__auth: dict = Depends(verify_subsonic_auth)):
    playlists = [item for item in list_playlists() if _can_view_playlist(__auth, item)]
    playlist_items = [
        {
            'id': item['id'],
            'name': item['name'],
            'comment': item.get('comment') or '',
            'owner': item.get('owner') or 'admin',
            'public': bool(item.get('public')),
            'songCount': int(item.get('song_count') or 0),
            'duration': int(item.get('duration') or 0),
            'created': dt_to_iso(item.get('created_at')),
            'changed': dt_to_iso(item.get('updated_at')),
        }
        for item in playlists
    ]
    return ok({'playlists': {'playlist': playlist_items if playlist_items else []}})


@router.api_route('/getScanStatus', methods=['GET', 'POST'])
@router.api_route('/getScanStatus.view', methods=['GET', 'POST'])
def get_scan_status(__auth: str = Depends(verify_subsonic_auth)):
    tasks = list_tasks()
    latest = tasks[0] if tasks else None
    stats = _library_stats()
    scanning = bool(latest and latest.get('status') in {'queued', 'running'})
    count = int(latest.get('count') or latest.get('processed') or 0) if latest else int(stats['count'])
    print(f"[subsonic:resp] getScanStatus scanning={str(scanning).lower()} count={count} albums={int(stats['albumCount'])} artists={int(stats['artistCount'])} folders={int(stats['folderCount'])}")
    return ok({
        'scanStatus': {
            'scanning': scanning,
            'count': count,
            'albumCount': int(stats['albumCount']),
            'artistCount': int(stats['artistCount']),
            'folderCount': int(stats['folderCount']),
        }
    })


@router.api_route('/getStarred', methods=['GET', 'POST'])
@router.api_route('/getStarred.view', methods=['GET', 'POST'])
def get_starred(__auth: dict = Depends(verify_subsonic_auth)):
    starred = list_starred_items(str(__auth.get('username') or ''))
    return ok({'starred': {
        'song': [to_subsonic_song(song, __auth) for song in starred.get('song', [])],
        'album': [to_subsonic_album(song) for song in starred.get('album', [])],
        'artist': [to_subsonic_artist(str(song.get('artist') or ''), list_songs()) for song in starred.get('artist', [])],
    }})


@router.api_route('/getStarred2', methods=['GET', 'POST'])
@router.api_route('/getStarred2.view', methods=['GET', 'POST'])
def get_starred2(__auth: dict = Depends(verify_subsonic_auth)):
    starred = list_starred_items(str(__auth.get('username') or ''))
    return ok({'starred2': {
        'song': [to_subsonic_song(song, __auth) for song in starred.get('song', [])],
        'album': [to_subsonic_album(song) for song in starred.get('album', [])],
        'artist': [to_subsonic_artist(str(song.get('artist') or ''), list_songs()) for song in starred.get('artist', [])],
    }})


@router.api_route('/star', methods=['GET', 'POST'])
@router.api_route('/star.view', methods=['GET', 'POST'])
def star(
    id: list[str] | None = Query(None),
    albumId: list[str] | None = Query(None),
    artistId: list[str] | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    username = str(__auth.get('username') or '')
    for item_id in id or []:
        set_starred_item(username, 'song', item_id, True)
    for item_id in albumId or []:
        set_starred_item(username, 'album', item_id, True)
    for item_id in artistId or []:
        set_starred_item(username, 'artist', item_id, True)
    return ok()


@router.api_route('/unstar', methods=['GET', 'POST'])
@router.api_route('/unstar.view', methods=['GET', 'POST'])
def unstar(
    id: list[str] | None = Query(None),
    albumId: list[str] | None = Query(None),
    artistId: list[str] | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    username = str(__auth.get('username') or '')
    for item_id in id or []:
        set_starred_item(username, 'song', item_id, False)
    for item_id in albumId or []:
        set_starred_item(username, 'album', item_id, False)
    for item_id in artistId or []:
        set_starred_item(username, 'artist', item_id, False)
    return ok()


@router.api_route('/setRating', methods=['GET', 'POST'])
@router.api_route('/setRating.view', methods=['GET', 'POST'])
def set_rating(id: str, rating: int = Query(0), __auth: dict = Depends(verify_subsonic_auth)):
    if not get_song(id):
        return fail('Song not found')
    set_song_rating(str(__auth.get('username') or ''), id, rating)
    return ok()


@router.api_route('/saveBookmark', methods=['GET', 'POST'])
@router.api_route('/saveBookmark.view', methods=['GET', 'POST'])
def save_bookmark_endpoint(id: str, position: int = Query(0), comment: str | None = Query(None), __auth: dict = Depends(verify_subsonic_auth)):
    if not get_song(id):
        return fail('Song not found')
    save_bookmark(str(__auth.get('username') or ''), id, position, comment or '')
    return ok()


@router.api_route('/getBookmarks', methods=['GET', 'POST'])
@router.api_route('/getBookmarks.view', methods=['GET', 'POST'])
def get_bookmarks(__auth: dict = Depends(verify_subsonic_auth)):
    rows = list_bookmarks(str(__auth.get('username') or ''))
    return ok({'bookmarks': {'bookmark': [
        {
            'position': int(row.get('position_ms') or 0),
            'comment': str(row.get('comment') or ''),
            'created': dt_to_iso(row.get('updated_at')),
            'changed': dt_to_iso(row.get('updated_at')),
            'entry': to_subsonic_song(row, __auth),
        }
        for row in rows
    ]}})


@router.api_route('/getRandomSongs', methods=['GET', 'POST'])
@router.api_route('/getRandomSongs.view', methods=['GET', 'POST'])
def get_random_songs(size: int = Query(50), __auth: dict = Depends(verify_subsonic_auth)):
    pool = list_random_songs(max(0, min(size, MAX_SEARCH_RESULTS * 25)))
    return ok({'randomSongs': {'song': [to_subsonic_song(song, __auth) for song in pool]}})


@router.api_route('/getNewestPodcasts', methods=['GET', 'POST'])
@router.api_route('/getNewestPodcasts.view', methods=['GET', 'POST'])
def get_newest_podcasts(count: int = Query(20), __auth: dict = Depends(verify_subsonic_auth)):
    songs = list_newest_songs(count)
    return ok({'newestPodcasts': {'episode': [to_subsonic_song(song, __auth) for song in songs]}})


@router.api_route('/getTopSongs', methods=['GET', 'POST'])
@router.api_route('/getTopSongs.view', methods=['GET', 'POST'])
def get_top_songs(artist: str, count: int = Query(50), __auth: str = Depends(verify_subsonic_auth)):
    songs = [song for song in list_songs() if str(song.get('artist') or '') == (artist or '').strip()]
    safe_count = max(0, min(int(count or 0), 5000))
    songs = sorted(songs, key=lambda song: (-int(song.get('plays') or 0), str(song.get('title') or '').lower(), str(song.get('id') or '')))
    return ok({'topSongs': {'song': [to_subsonic_song(song, __auth) for song in songs[:safe_count]]}})


@router.api_route('/getPlaylist', methods=['GET', 'POST'])
@router.api_route('/getPlaylist.view', methods=['GET', 'POST'])
def get_playlist_endpoint(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    playlist = get_playlist(id)
    if not playlist or not _can_view_playlist(__auth, playlist):
        return fail('Playlist not found')
    return ok({
        'playlist': {
            'id': playlist['id'],
            'name': playlist['name'],
            'comment': playlist.get('comment') or '',
            'owner': playlist.get('owner') or 'admin',
            'public': bool(playlist.get('public')),
            'songCount': int(playlist.get('song_count') or 0),
            'duration': int(playlist.get('duration') or 0),
            'created': dt_to_iso(playlist.get('created_at')),
            'changed': dt_to_iso(playlist.get('updated_at')),
            'entry': [to_subsonic_song(entry, __auth) for entry in playlist.get('entries', [])],
        }
    })


@router.api_route('/createPlaylist', methods=['GET', 'POST'])
@router.api_route('/createPlaylist.view', methods=['GET', 'POST'])
def create_playlist_endpoint(
    playlistId: str | None = Query(None),
    name: str | None = Query(None),
    songId: list[str] | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    # Subsonic: when `playlistId` is supplied createPlaylist must *replace* the
    # tracks of that playlist instead of creating a duplicate playlist.
    if playlistId:
        existing = get_playlist(playlistId)
        if not existing:
            return fail('Playlist not found')
        if not _can_modify_playlist(__auth, existing):
            return fail('Permission denied')
        current_ids = [str(entry.get('id') or '') for entry in existing.get('entries', [])]
        playlist = update_playlist(
            playlist_id=playlistId,
            name=name or None,
            song_ids_to_add=songId or [],
            song_ids_to_remove=current_ids,
        )
        if not playlist:
            return fail('Playlist not found')
        return ok({'playlist': {'id': playlist['id'], 'name': playlist['name']}})

    if not str(name or '').strip():
        return fail('Missing playlist name')

    playlist = create_playlist(
        name=str(name),
        song_ids=songId or [],
        owner=str(__auth.get('username') or 'admin'),
        public=False,
        is_readonly=False,
        scope='private_user',
    )
    return ok({'playlist': {'id': playlist['id'], 'name': playlist['name']}})


@router.api_route('/updatePlaylist', methods=['GET', 'POST'])
@router.api_route('/updatePlaylist.view', methods=['GET', 'POST'])
def update_playlist_endpoint(
    playlistId: str = Query(...),
    name: str | None = Query(None),
    comment: str | None = Query(None),
    public: bool | None = Query(None),
    songIdToAdd: list[str] | None = Query(None),
    songIdToRemove: list[str] | None = Query(None),
    songIndexToRemove: list[int] | None = Query(None),
    __auth: dict = Depends(verify_subsonic_auth),
):
    existing = get_playlist(playlistId)
    if not existing:
        return fail('Playlist not found')
    if not _can_modify_playlist(__auth, existing):
        return fail('Permission denied')
    playlist = update_playlist(
        playlist_id=playlistId,
        name=name,
        comment=comment,
        public=public if _is_admin(__auth) else None,
        song_ids_to_add=songIdToAdd or [],
        song_ids_to_remove=songIdToRemove or [],
        song_indexes_to_remove=songIndexToRemove or [],
    )
    if not playlist:
        return fail('Playlist not found')
    return ok()


@router.api_route('/deletePlaylist', methods=['GET', 'POST'])
@router.api_route('/deletePlaylist.view', methods=['GET', 'POST'])
def delete_playlist_endpoint(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    existing = get_playlist(id)
    if not existing:
        return fail('Playlist not found')
    if not _can_modify_playlist(__auth, existing):
        return fail('Permission denied')
    deleted = delete_playlist(id)
    if not deleted:
        return fail('Playlist not found')
    return ok()


@router.api_route('/scrobble', methods=['GET', 'POST'])
@router.api_route('/scrobble.view', methods=['GET', 'POST'])
def scrobble(
    id: str,
    time: int | None = Query(None),
    submission: bool | None = Query(False),
    c: str | None = Query(''),
    __auth: str = Depends(verify_subsonic_auth),
):
    song = get_song(id)
    if not song:
        return fail('Song not found')
    updated_song = record_play_event(
        song_id=id,
        source='subsonic',
        submission=bool(submission),
        client=(c or '').strip(),
        amount=1,
    )
    return ok({
        'scrobble': {
            'id': id,
            'submission': bool(submission),
            'time': time or 0,
            'client': (c or '').strip(),
            'plays': int((updated_song or song).get('plays') or 0),
        }
    })


@router.api_route('/getNowPlaying', methods=['GET', 'POST'])
@router.api_route('/getNowPlaying.view', methods=['GET', 'POST'])
def get_now_playing(__auth: dict = Depends(verify_subsonic_auth)):
    recent = list_recent_plays(limit=20)
    unique_entries = []
    seen = set()
    for item in recent:
        song_id = str(item.get('song_id') or '')
        if not song_id or song_id in seen:
            continue
        seen.add(song_id)
        song = get_song(song_id)
        if not song:
            continue
        unique_entries.append({
            'username': str(__auth.get('username') or ''),
            'minutesAgo': 0,
            'playerId': item.get('client') or 'subsonic',
            'playerName': item.get('client') or 'Subsonic Client',
            **to_subsonic_song(song, __auth),
        })
        if len(unique_entries) >= 10:
            break
    return ok({'nowPlaying': {'entry': unique_entries}})


import json
from app.services.lx_client import LxMusicClient

def _resolve_song_url(song_id: str, file_path: str, raw_url: str) -> str:
    """
    Returns the real streaming URL.
    If it's a virtual song (lx://virtual/), it fetches the dynamic stream URL from LX-Server.
    """
    if file_path.startswith('lx://virtual/'):
        try:
            song_info = json.loads(raw_url)
            # Fetch dynamically
            settings = get_runtime_settings()
            lx_url = settings.get("lx_server_url", "")
            if not lx_url:
                return ""
            
            # Since this is synchronous, we use asyncio to run the coroutine
            import asyncio
            client = LxMusicClient(base_url=lx_url)
            
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are in an async context (FastAPI endpoint)
                # But subsonic routes are currently def (sync)
                # If they are sync, we can't await easily inside an event loop
                pass # Handled below by making the endpoint async or creating a task
        except Exception:
            return ""
    
    return raw_url


async def _proxy_stream_from_url(url: str, *, cookie: str = '', referer: str = 'https://pan.quark.cn', user_agent: str = ''):
    headers = {
        'Referer': referer,
        'User-Agent': user_agent or 'Mozilla/5.0',
    }
    if cookie:
        headers['Cookie'] = cookie

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    request = client.build_request('GET', url, headers=headers)
    upstream = await client.send(request, stream=True)
    upstream.raise_for_status()

    response_headers: dict[str, str] = {}
    for key in ('content-type', 'content-length', 'accept-ranges', 'content-range', 'last-modified', 'etag'):
        value = upstream.headers.get(key)
        if value:
            response_headers[key] = value

    async def iterator():
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(iterator(), status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get('content-type'))


@router.api_route('/stream', methods=['GET', 'POST'])
@router.api_route('/stream.view', methods=['GET', 'POST'])
async def stream(id: str, user_agent: str | None = Header(default=None, alias='User-Agent'), __auth: str = Depends(verify_subsonic_auth)):
    song = get_song(id)
    if not song:
        return fail('Song not found')
    path = song.get('path')
    if not path:
        return fail('Cloud path not ready')

    if path.startswith('lx://virtual/'):
        raw_url = song.get('raw_url') or ''
        try:
            import json
            import httpx
            
            song_info = json.loads(raw_url)
            settings = get_runtime_settings()
            lx_url = settings.get("lx_server_url", "")
            if not lx_url:
                return fail('LX-Server URL not configured')
            
            # Request dynamic streaming URL from LX-Server
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{lx_url.rstrip('/')}/api/music/url",
                    json={"songInfo": song_info, "quality": "128k"},
                    headers={"Content-Type": "application/json"}
                )
                res.raise_for_status()
                data = res.json()
                stream_url = data.get("url")
                if not stream_url:
                    return fail('Streaming URL not found in LX-Server response')
                
                # Redirect the client directly to the LX-Server's generated audio URL
                return RedirectResponse(url=stream_url, status_code=302)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error fetching virtual song stream: {e}")
            return fail(f'Failed to fetch virtual stream: {e}')

    runtime = get_runtime_settings()
    storage = get_storage_service()
    provider_id = str(song.get('provider_id') or '').strip()
    if not provider_id:
        raw_url = str(song.get('raw_url') or '')
        if 'quark.cn' in raw_url or 'pds.quark.cn' in raw_url:
            provider_id = str(runtime.get('last_scan_provider_id') or runtime.get('storage_default_provider_id') or '').strip()
    try:
        ref = storage.make_ref(path, provider_id=provider_id or None)
        provider = storage.get_provider(ref.provider_id)
        provider_type = str(getattr(provider, 'provider_type', '') or '')
        if provider_type == 'quark':
            fresh_url = await storage.get_download_url_async(ref)
            return await _proxy_stream_from_url(
                fresh_url,
                cookie=str(getattr(provider, 'cookie', '') or ''),
                referer=str(getattr(provider, '_referer', 'https://pan.quark.cn') or 'https://pan.quark.cn'),
                user_agent=str(getattr(provider, '_user_agent', '') or ''),
            )
        if provider_type == '115' and hasattr(provider, 'get_stream_redirect_url_async'):
            fresh_url = await provider.get_stream_redirect_url_async(ref, user_agent=user_agent)
        else:
            fresh_url = await storage.get_download_url_async(ref)
    except Exception:
        fresh_url = song.get('raw_url') or ''
    if not fresh_url:
        return fail('Direct stream URL not ready')
    return RedirectResponse(url=fresh_url, status_code=302)


@router.api_route('/download', methods=['GET', 'POST'])
@router.api_route('/download.view', methods=['GET', 'POST'])
async def download(id: str, __auth: str = Depends(verify_subsonic_auth)):
    return await stream(id=id, __auth=__auth)


@router.api_route('/getCoverArt', methods=['GET', 'POST'])
@router.api_route('/getCoverArt.view', methods=['GET', 'POST'])
def get_cover_art(id: str, __auth: str = Depends(verify_subsonic_auth)):
    song = get_song(id)
    songs = list_songs()

    if not song and id.startswith('al-'):
        album_id = id[3:]
        song = next((item for item in songs if _slugify(item['album'], 'album') == album_id and (item.get('cover_path') or _virtual_cover_url(item))), None)
    elif not song and id:
        song = next((item for item in songs if _slugify(item['album'], 'album') == id and (item.get('cover_path') or _virtual_cover_url(item))), None)

    if not song:
        raise HTTPException(status_code=404, detail='Song not found')

    cover_path = str(song.get('cover_path') or '').strip()
    if (not cover_path or not Path(cover_path).exists()) and str(song.get('fmt') or '') == 'VIRTUAL':
        cover_path = _ensure_virtual_cover(song)

    if not cover_path:
        raise HTTPException(status_code=404, detail='Cover not found')
    path = Path(cover_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail='Cover file missing')
    media_type = 'image/png' if path.suffix.lower() == '.png' else 'image/jpeg'
    return FileResponse(path, media_type=media_type)


@router.api_route('/getLyrics', methods=['GET', 'POST'])
@router.api_route('/getLyrics.view', methods=['GET', 'POST'])
def get_lyrics(artist: str | None = Query(None), title: str | None = Query(None), id: str | None = Query(None), __auth: str = Depends(verify_subsonic_auth)):
    song = get_song(id) if id else None
    if not song and (artist or title):
        wanted_artist = (artist or '').strip().lower()
        wanted_title = (title or '').strip().lower()
        song = next(
            (
                item for item in list_songs()
                if (not wanted_artist or str(item.get('artist') or '').strip().lower() == wanted_artist)
                and (not wanted_title or str(item.get('title') or '').strip().lower() == wanted_title)
            ),
            None,
        )
    lyrics = str((song or {}).get('lyrics') or '').strip()
    if not lyrics:
        lyrics = _extract_embedded_lyrics(song)
    if not lyrics and str((song or {}).get('fmt') or '') == 'VIRTUAL':
        lyrics = _fetch_virtual_lyrics(song)
    return ok({'lyrics': {'artist': artist or (song or {}).get('artist', ''), 'title': title or (song or {}).get('title', ''), 'value': lyrics}})


@router.api_route('/getLyricsBySongId', methods=['GET', 'POST'])
@router.api_route('/getLyricsBySongId.view', methods=['GET', 'POST'])
def get_lyrics_by_song_id(id: str, __auth: dict = Depends(verify_subsonic_auth)):
    """OpenSubsonic `songLyrics` shape used by newer clients."""
    song = get_song(id)
    if not song:
        return fail('Song not found')
    lyrics = str(song.get('lyrics') or '').strip()
    if not lyrics:
        lyrics = _extract_embedded_lyrics(song)
    if not lyrics and str(song.get('fmt') or '') == 'VIRTUAL':
        lyrics = _fetch_virtual_lyrics(song)
    lines = [{'value': line} for line in lyrics.splitlines() if line.strip()]
    structured = []
    if lines:
        structured.append({
            'displayArtist': str(song.get('artist') or ''),
            'displayTitle': str(song.get('title') or ''),
            'lang': '',
            'synced': False,
            'line': lines,
        })
    return ok({'lyricsList': {'structuredLyrics': structured}})


@router.api_route('/getSimilarSongs', methods=['GET', 'POST'])
@router.api_route('/getSimilarSongs.view', methods=['GET', 'POST'])
def get_similar_songs(id: str, count: int = Query(20), __auth: str = Depends(verify_subsonic_auth)):
    songs = [song for song in list_songs() if song.get('id') != id]
    safe_count = max(0, min(int(count or 0), 100))
    return ok({'similarSongs': {'song': [to_subsonic_song(song, __auth) for song in songs[:safe_count]]}})


@router.api_route('/getSimilarSongs2', methods=['GET', 'POST'])
@router.api_route('/getSimilarSongs2.view', methods=['GET', 'POST'])
def get_similar_songs2(id: str, count: int = Query(20), __auth: str = Depends(verify_subsonic_auth)):
    return get_similar_songs(id, count, __auth)


def to_subsonic_album(song: dict, songs: list[dict] | None = None) -> dict:
    source_songs = songs if songs is not None else list_songs()
    album_songs = [item for item in source_songs if item['album'] == song['album']]
    first_song = album_songs[0] if album_songs else song
    return {
        'id': _slugify(song['album'], 'album'),
        'name': song['album'],
        'title': song['album'],
        'artist': song['artist'],
        'artistId': _artist_slug(song.get('artist') or ''),
        'songCount': len(album_songs),
        'duration': sum(int(item.get('dur') or 0) for item in album_songs),
        'created': dt_to_iso(first_song.get('created_at')) or '',
        'year': 0,
        'genre': str(first_song.get('genre') or ''),
        'coverArt': next((item['id'] for item in album_songs if item.get('cover_path')), song['id'] if song.get('cover_path') else ''),
    }


def to_subsonic_artist(name: str, songs: list[dict]) -> dict:
    artist_songs = [song for song in songs if song['artist'] == name]
    return {
        'id': _artist_slug(name),
        'name': name,
        'albumCount': len({song['album'] for song in artist_songs}),
        'coverArt': next((song['id'] for song in artist_songs if song.get('cover_path')), ''),
    }


def to_subsonic_song(song: dict, auth: dict | None = None) -> dict:
    suffix_map = {'FLAC': 'flac', 'MP3': 'mp3', 'AAC': 'm4a', 'OPUS': 'opus'}
    content_map = {'FLAC': 'audio/flac', 'MP3': 'audio/mpeg', 'AAC': 'audio/aac', 'OPUS': 'audio/ogg'}
    suffix = suffix_map.get(song['fmt'], 'mp3')
    auth = auth or current_auth_var.get() or {}
    username = str(auth.get('username') or '')
    song_id = str(song.get('id') or '')
    bookmark = get_bookmark(username, song_id) if username else None
    # Subsonic models `starred` as an ISO-8601 date string (or omits it), never
    # as a boolean.  Sending `false` made strict JSON clients fail to parse the
    # whole song object.
    starred_at = get_starred_at(username, 'song', song_id) if username else ''
    has_cover = bool(song.get('cover_path')) or (str(song.get('fmt') or '') == 'VIRTUAL' and bool(_virtual_cover_url(song)))
    payload = {
        'id': song['id'],
        'parent': _slugify(song['album'], 'album'),
        'title': song['title'],
        'artist': song['artist'],
        'album': song['album'],
        'duration': song['dur'],
        'track': 1,
        'year': 0,
        'genre': str(song.get('genre') or ''),
        'coverArt': song['id'] if has_cover else '',
        'path': song.get('path', ''),
        'isDir': False,
        'type': 'music',
        'suffix': suffix,
        'contentType': content_map.get(song['fmt'], 'audio/mpeg'),
        'bitRate': 0,
        'size': 0,
        'userRating': get_song_rating(username, song_id) if username else 0,
        'bookmarkPosition': int((bookmark or {}).get('position_ms') or 0),
    }
    if starred_at:
        payload['starred'] = starred_at
    return payload
