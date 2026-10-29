import asyncio
import logging
import uuid
import re
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from app.db.session import engine
from app.db.compat import db_json_type, db_json_value_expr, db_now_expr, db_timestamp_type, json_dumps, utc_days_ago
from app.services.app_settings import get_runtime_settings
from app.services.lx_runtime import lx_embedded_manager

async def import_lx_playlist(url: str, owner: str = 'admin'):
    """
    Parse a playlist URL and import it via LX-Server into local playlists.
    """
    source = None
    playlist_id = None
    
    import httpx
    
    # Try to resolve short links (e.g. 163cn.tv, q音短链)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            res = await client.get(url)
            url = str(res.url)
    except Exception:
        pass
        
    if "music.163.com" in url or "163cn.tv" in url:
        source = "wy"
        m = re.search(r'id=(\d+)', url)
        if m:
            playlist_id = m.group(1)
    elif "y.qq.com" in url or "c.y.qq.com" in url:
        source = "tx"
        # Support https://y.qq.com/n/ryqq/playlist/7311100223
        m = re.search(r'playlist/(\d+)', url)
        if m:
            playlist_id = m.group(1)
        else:
            m = re.search(r'id=(\d+)', url)
            if m:
                playlist_id = m.group(1)
    elif "kugou.com" in url:
        source = "kg"
        m = re.search(r'special/single/(\d+)', url)
        if m:
            playlist_id = m.group(1)
            
    if not source or not playlist_id:
        raise Exception("无法解析该平台链接或暂不支持该平台。请尝试输入网易云音乐或QQ音乐的歌单分享链接。")
        
    if lx_embedded_manager.configured_mode() == 'embedded':
        status = lx_embedded_manager.status()
        if status.get('embedded_enabled') and status.get('embedded_available') and not status.get('embedded_running'):
            await lx_embedded_manager.start()

    lx_url = (lx_embedded_manager.configured_base_url() or '').rstrip('/')
    if not lx_url:
        raise Exception("LX 服务地址为空，无法抓取外部歌单。")
        
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            res = await client.get(f"{lx_url}/api/music/songList/detail?source={source}&id={playlist_id}&page=1")
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            raise Exception(f"请求 LX-Server 失败: {e}")
            
    if "list" not in data:
        # Fallback to leaderboard parsing if it happens to be a leaderboard id
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.get(f"{lx_url}/api/music/leaderboard/list?source={source}&bangid={playlist_id}&page=1")
                data = res.json()
                if "data" in data and "list" in data["data"]:
                    data = data["data"]
        except Exception:
            pass
            
    if "list" not in data:
        raise Exception(f"LX-Server 无法解析该歌单内容，或者该歌单不可用。返回: {data.get('error', 'Unknown')}")
        
    playlist_name = str(data.get("info", {}).get("name") or f"导入歌单 ({source}-{playlist_id})")
    desc = str(data.get("info", {}).get("desc") or "通过链接从外部平台导入")
    items = data["list"]
    
    if not items:
        raise Exception("该歌单中没有可用的曲目。")
        
    with engine.begin() as conn:
        pl_id = f"imp-{uuid.uuid4().hex[:8]}"
        conn.execute(
            text("""
                INSERT INTO playlists (id, name, comment, owner, public, is_readonly, scope)
                VALUES (:id, :name, :comment, :owner, FALSE, FALSE, 'private_user')
            """),
            {
                "id": pl_id,
                "name": playlist_name,
                "comment": desc,
                "owner": owner
            }
        )
        
        track_inserts = []
        for i, item in enumerate(items):
            title = normalize_title(item.get("name", ""))
            artist = normalize_artist(item.get("singer", ""))
            if not title:
                continue
                
            # 1. Match real
            local_song = conn.execute(
                text("SELECT id FROM songs WHERE LOWER(title) = :t AND LOWER(artist) LIKE :a AND fmt != 'VIRTUAL' LIMIT 1"),
                {"t": title, "a": f"%{artist}%"}
            ).fetchone()
            
            song_id = None
            if local_song:
                song_id = local_song[0]
            else:
                # 2. Virtual match
                virtual_song = conn.execute(
                    text("SELECT id FROM songs WHERE LOWER(title) = :t AND LOWER(artist) LIKE :a AND fmt = 'VIRTUAL' LIMIT 1"),
                    {"t": title, "a": f"%{artist}%"}
                ).fetchone()
                
                lx_item = item.copy()
                lx_item["source"] = source
                import json
                raw_url_json = json.dumps(lx_item, ensure_ascii=False)
                pic = item.get("pic") or item.get("img", "")
                dur = _parse_interval(item.get("interval", 0))
                
                if virtual_song:
                    song_id = virtual_song[0]
                    conn.execute(
                        text("UPDATE songs SET raw_url = :raw_url, cover_art = :cover WHERE id = :id"),
                        {"raw_url": raw_url_json, "cover": pic, "id": song_id}
                    )
                else:
                    song_id = f"lxv-{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        text("""
                            INSERT INTO songs (id, title, artist, album, fmt, dur, path, raw_url, cover_art)
                            VALUES (:id, :title, :artist, :album, 'VIRTUAL', :dur, :path, :raw_url, :cover)
                        """),
                        {
                            "id": song_id,
                            "title": item.get("name", ""),
                            "artist": item.get("singer", "") or "Unknown",
                            "album": item.get("albumName") or item.get("album", "") or "Unknown",
                            "dur": dur,
                            "path": f"lx://virtual/{song_id}",
                            "raw_url": raw_url_json,
                            "cover": pic
                        }
                    )
            
            track_inserts.append({
                "playlist_id": pl_id,
                "song_id": song_id,
                "sort_order": i + 1
            })
            
        if track_inserts:
            conn.execute(
                text("""
                    INSERT INTO playlist_tracks (playlist_id, song_id, sort_order)
                    VALUES (:playlist_id, :song_id, :sort_order)
                """),
                track_inserts
            )

        # Refresh playlist summary fields so list view song_count / duration stay in sync
        from app.services.db_data import _refresh_playlist_meta
        _refresh_playlist_meta(conn, pl_id)
            
    return {"id": pl_id, "name": playlist_name, "count": len(track_inserts)}
from app.services.lx_client import LxMusicClient

logger = logging.getLogger(__name__)


def ensure_curation_history_table() -> None:
    with engine.begin() as conn:
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS curation_update_history (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                playlist_id TEXT NOT NULL,
                playlist_name TEXT NOT NULL,
                trigger_source TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'done',
                track_count INTEGER NOT NULL DEFAULT 0,
                added_count INTEGER NOT NULL DEFAULT 0,
                removed_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                summary {db_json_type()} NOT NULL DEFAULT '{json_dumps({}, {})}',
                error TEXT NOT NULL DEFAULT '',
                created_at {db_timestamp_type()} NOT NULL DEFAULT {db_now_expr()}
            )
        '''))


def _ensure_curation_history_table() -> None:
    ensure_curation_history_table()


def _record_curation_history(
    *,
    run_id: str,
    playlist_id: str,
    playlist_name: str,
    trigger_source: str,
    status: str,
    track_count: int,
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    top_tracks: list[dict[str, Any]],
    error: str = '',
) -> None:
    preview_limit = 20
    summary = {
        'top_tracks': top_tracks[:preview_limit],
        'added': added[:preview_limit],
        'removed': removed[:preview_limit],
    }
    with engine.begin() as conn:
        conn.execute(
            text(f'''
                INSERT INTO curation_update_history (
                    id, run_id, playlist_id, playlist_name, trigger_source, status,
                    track_count, added_count, removed_count, changed_count, summary, error
                ) VALUES (
                    :id, :run_id, :playlist_id, :playlist_name, :trigger_source, :status,
                    :track_count, :added_count, :removed_count, :changed_count, {db_json_value_expr('summary')}, :error
                )
            '''),
            {
                'id': f'ch-{uuid.uuid4().hex[:12]}',
                'run_id': run_id,
                'playlist_id': playlist_id,
                'playlist_name': playlist_name,
                'trigger_source': trigger_source,
                'status': status,
                'track_count': track_count,
                'added_count': len(added),
                'removed_count': len(removed),
                'changed_count': len(added) + len(removed),
                'summary': json_dumps(summary, {}),
                'error': error,
            }
        )

# Define the 5 fixed playlists
CURATED_PLAYLISTS = [
    {
        "id": "lx-hot",
        "name": "全网热歌榜",
        "comment": "全网最火的国民级热歌（腾讯+网易云+酷狗）",
        "sources": [("tx", "26"), ("wy", "3778678"), ("kg", "8888")]
    },
    {
        "id": "lx-new",
        "name": "全网新歌速递",
        "comment": "华语乐坛最新动态（腾讯+网易云）",
        "sources": [("tx", "27"), ("wy", "3779629")]
    },
    {
        "id": "lx-viral",
        "name": "网络爆款风向",
        "comment": "下沉市场洗脑神曲（腾讯网络+酷狗抖音）",
        "sources": [("tx", "28"), ("kg", "52144")]
    },
    {
        "id": "lx-classic",
        "name": "岁月经典回声",
        "comment": "华语老歌（腾讯K歌金曲+酷我经典）",
        "sources": [("tx", "36"), ("kw", "26")]
    },
    {
        "id": "lx-tiktok",
        "name": "抖音神曲榜",
        "comment": "当下最火的短视频BGM（酷狗+酷我+网易云飙升）",
        "sources": [("kg", "52144"), ("kw", "158"), ("wy", "19723756")]
    }
]

def _ensure_playlists_exist():
    """Ensure the 5 fixed playlists exist in the DB and are public system playlists."""
    with engine.begin() as conn:
        for p in CURATED_PLAYLISTS:
            row = conn.execute(
                text("SELECT id FROM playlists WHERE id = :id"),
                {"id": p["id"]}
            ).fetchone()
            if not row:
                conn.execute(
                    text("""
                        INSERT INTO playlists (id, name, comment, owner, public, is_readonly, scope)
                        VALUES (:id, :name, :comment, 'system', TRUE, TRUE, 'public_system')
                    """),
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "comment": p["comment"]
                    }
                )
            else:
                conn.execute(
                    text("""
                        UPDATE playlists
                        SET name = :name, comment = :comment, public = TRUE, is_readonly = TRUE, owner = 'system', scope = 'public_system'
                        WHERE id = :id
                    """),
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "comment": p["comment"]
                    }
                )


def ensure_curated_playlists() -> None:
    _ensure_playlists_exist()

SOURCE_WEIGHTS = {
    "tx": 1.1,
    "wy": 1.1,
    "kg": 1.0,
    "kw": 1.0,
    "mg": 0.9
}

def normalize_title(t: str) -> str:
    return t.lower().strip() if t else ""

def normalize_artist(a: str) -> str:
    return a.lower().strip() if a else ""

def _parse_interval(val: Any) -> int:
    if not val:
        return 0
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if ':' in s:
        parts = s.split(':')
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    try:
        return int(float(s))
    except ValueError:
        return 0

async def generate_curated_playlists(trigger_autofill: bool = False, trigger_source: str = 'manual'):
    logger.info(f"Starting background task: generate_curated_playlists (trigger_autofill={trigger_autofill}, trigger_source={trigger_source})")
    settings = get_runtime_settings()
    lx_url = settings.get("lx_server_url", "")
    run_id = f'cr-{uuid.uuid4().hex[:12]}'
    
    _ensure_playlists_exist()
    _ensure_curation_history_table()

    client = LxMusicClient(base_url=lx_url)
    
    try:
        for p in CURATED_PLAYLISTS:
            logger.info(f"Generating playlist: {p['name']}")
            
            # Dictionary to aggregate scores
            # key: (title, artist)
            # value: {"score": float, "meta": dict}
            song_scores = defaultdict(lambda: {"score": 0.0, "meta": None})
            
            for source, board_id in p["sources"]:
                try:
                    items = await client.get_leaderboard(source, board_id)
                except Exception as e:
                    logger.warning(f"Failed to fetch {source}-{board_id}: {e}")
                    continue
                
                weight = SOURCE_WEIGHTS.get(source, 1.0)
                
                for idx, item in enumerate(items):
                    rank = idx + 1
                    # Base score logic: Top 1 = 100, Top 100 = 1
                    base_score = max(100 - rank + 1, 1)
                    weighted_score = base_score * weight
                    
                    title = normalize_title(item.get("name", ""))
                    artist = normalize_artist(item.get("singer", ""))
                    
                    if not title:
                        continue
                        
                    key = (title, artist)
                    # Use max score across platforms instead of sum
                    if weighted_score > song_scores[key]["score"]:
                        song_scores[key]["score"] = weighted_score
                        
                    if song_scores[key]["meta"] is None:
                        song_scores[key]["meta"] = {
                            "title": item.get("name", ""),
                            "artist": item.get("singer", ""),
                            "album": item.get("albumName") or item.get("album", ""),
                            "dur": _parse_interval(item.get("interval", 0)),
                            "pic": item.get("pic") or item.get("img", ""),
                            "lx_source": source,
                            "lx_item": item  # Store raw item for stream resolution later
                        }

            # Sort by score descending
            sorted_songs = sorted(song_scores.values(), key=lambda x: x["score"], reverse=True)
            top_100 = sorted_songs[:100]
            top_preview = [
                {
                    'title': s_data['meta'].get('title', ''),
                    'artist': s_data['meta'].get('artist', ''),
                    'album': s_data['meta'].get('album', ''),
                }
                for s_data in top_100[:20]
            ]
            
            if not top_100:
                logger.info(f"No songs fetched for {p['name']}, skipping DB update.")
                _record_curation_history(
                    run_id=run_id,
                    playlist_id=p['id'],
                    playlist_name=p['name'],
                    trigger_source=trigger_source,
                    status='empty',
                    track_count=0,
                    added=[],
                    removed=[],
                    top_tracks=[],
                    error='no songs fetched',
                )
                continue

            # Map to DB and update playlist_tracks
            with engine.begin() as conn:
                previous_tracks = conn.execute(
                    text('''
                        SELECT s.title, s.artist, s.album
                        FROM playlist_tracks pt
                        JOIN songs s ON s.id = pt.song_id
                        WHERE pt.playlist_id = :pid
                        ORDER BY pt.sort_order ASC
                    '''),
                    {'pid': p['id']}
                ).mappings().all()
                previous_keys = {
                    (normalize_title(str(row.get('title') or '')), normalize_artist(str(row.get('artist') or ''))): {
                        'title': row.get('title') or '',
                        'artist': row.get('artist') or '',
                        'album': row.get('album') or '',
                    }
                    for row in previous_tracks
                }
                next_keys = {
                    (normalize_title(s_data['meta'].get('title', '')), normalize_artist(s_data['meta'].get('artist', ''))): {
                        'title': s_data['meta'].get('title', ''),
                        'artist': s_data['meta'].get('artist', ''),
                        'album': s_data['meta'].get('album', ''),
                    }
                    for s_data in top_100
                    if s_data.get('meta')
                }
                added_items = [value for key, value in next_keys.items() if key not in previous_keys]
                removed_items = [value for key, value in previous_keys.items() if key not in next_keys]

                # Clear existing tracks
                conn.execute(
                    text("DELETE FROM playlist_tracks WHERE playlist_id = :pid"),
                    {"pid": p["id"]}
                )
                
                track_inserts = []
                for i, s_data in enumerate(top_100):
                    meta = s_data["meta"]
                    title_norm = normalize_title(meta["title"])
                    artist_norm = normalize_artist(meta["artist"])
                    
                    # 1. Try to find local match (only REAL songs)
                    local_song = conn.execute(
                        text("SELECT id FROM songs WHERE LOWER(title) = :t AND LOWER(artist) LIKE :a AND fmt != 'VIRTUAL' LIMIT 1"),
                        {"t": title_norm, "a": f"%{artist_norm}%"}
                    ).fetchone()
                    
                    song_id = None
                    if local_song:
                        song_id = local_song[0]
                    else:
                        # 2. Check if a virtual song already exists for this track
                        virtual_song = conn.execute(
                            text("SELECT id FROM songs WHERE LOWER(title) = :t AND LOWER(artist) LIKE :a AND fmt = 'VIRTUAL' LIMIT 1"),
                            {"t": title_norm, "a": f"%{artist_norm}%"}
                        ).fetchone()
                        
                        import json
                        lx_item = meta["lx_item"].copy()
                        lx_item["source"] = meta["lx_source"]
                        raw_url_json = json.dumps(lx_item, ensure_ascii=False)
                        
                        if virtual_song:
                            song_id = virtual_song[0]
                            conn.execute(
                                text("UPDATE songs SET raw_url = :raw_url, cover_art = :cover WHERE id = :id"),
                                {"raw_url": raw_url_json, "cover": meta["pic"], "id": song_id}
                            )
                        else:
                            song_id = f"lxv-{uuid.uuid4().hex[:12]}"
                            conn.execute(
                                text("""
                                    INSERT INTO songs (id, title, artist, album, fmt, dur, path, raw_url, cover_art)
                                    VALUES (:id, :title, :artist, :album, 'VIRTUAL', :dur, :path, :raw_url, :cover)
                                """),
                                {
                                    "id": song_id,
                                    "title": meta["title"],
                                    "artist": meta["artist"] or "Unknown",
                                    "album": meta["album"] or "Unknown",
                                    "dur": meta["dur"],
                                    "path": f"lx://virtual/{song_id}",
                                    "raw_url": raw_url_json,
                                    "cover": meta["pic"]
                                }
                            )
                    
                    track_inserts.append({
                        "playlist_id": p["id"],
                        "song_id": song_id,
                        "sort_order": i + 1
                    })
                
                if track_inserts:
                    conn.execute(
                        text("""
                            INSERT INTO playlist_tracks (playlist_id, song_id, sort_order)
                            VALUES (:playlist_id, :song_id, :sort_order)
                        """),
                        track_inserts
                    )
                    
                    # Update playlist metadata
                    conn.execute(
                        text("""
                            UPDATE playlists 
                            SET song_count = :cnt, updated_at = :updated_at 
                            WHERE id = :pid
                        """),
                        {"cnt": len(track_inserts), "pid": p["id"], "updated_at": utc_days_ago(0)}
                    )
            
            logger.info(f"Successfully updated {p['name']} with {len(track_inserts)} tracks.")
            _record_curation_history(
                run_id=run_id,
                playlist_id=p['id'],
                playlist_name=p['name'],
                trigger_source=trigger_source,
                status='done',
                track_count=len(track_inserts),
                added=added_items,
                removed=removed_items,
                top_tracks=top_preview,
            )

    finally:
        await client.close()
        logger.info("Finished background task: generate_curated_playlists")

