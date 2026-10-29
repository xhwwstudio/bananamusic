from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

SONGS: list[dict[str, Any]] = [
    {'id': 's001', 'title': '晴天', 'artist': '周杰伦', 'album': '叶惠美', 'fmt': 'FLAC', 'dur': 269, 'path': '/阿里云盘/Music/周杰伦/叶惠美/晴天.flac', 'plays': 128},
    {'id': 's002', 'title': '七里香', 'artist': '周杰伦', 'album': '七里香', 'fmt': 'FLAC', 'dur': 234, 'path': '/阿里云盘/Music/周杰伦/七里香/七里香.flac', 'plays': 102},
    {'id': 's003', 'title': '夜曲', 'artist': '周杰伦', 'album': '十一月的萧邦', 'fmt': 'FLAC', 'dur': 223, 'path': '/阿里云盘/Music/周杰伦/十一月的萧邦/夜曲.flac', 'plays': 94},
    {'id': 's004', 'title': '稻香', 'artist': '周杰伦', 'album': '魔杰座', 'fmt': 'MP3', 'dur': 223, 'path': '/阿里云盘/Music/周杰伦/魔杰座/稻香.mp3', 'plays': 87},
    {'id': 's005', 'title': 'Bohemian Rhapsody', 'artist': 'Queen', 'album': 'A Night at the Opera', 'fmt': 'FLAC', 'dur': 355, 'path': '/阿里云盘/Music/Queen/A Night at the Opera/Bohemian Rhapsody.flac', 'plays': 76},
    {'id': 's006', 'title': 'Hotel California', 'artist': 'Eagles', 'album': 'Hotel California', 'fmt': 'FLAC', 'dur': 391, 'path': '/阿里云盘/Music/Eagles/Hotel California/Hotel California.flac', 'plays': 71},
    {'id': 's007', 'title': 'Stairway to Heaven', 'artist': 'Led Zeppelin', 'album': 'Led Zeppelin IV', 'fmt': 'FLAC', 'dur': 482, 'path': '/阿里云盘/Music/Led Zeppelin/Led Zeppelin IV/Stairway to Heaven.flac', 'plays': 65},
    {'id': 's008', 'title': 'Someone Like You', 'artist': 'Adele', 'album': '21', 'fmt': 'AAC', 'dur': 285, 'path': '/阿里云盘/Music/Adele/21/Someone Like You.m4a', 'plays': 61},
    {'id': 's009', 'title': 'Shape of You', 'artist': 'Ed Sheeran', 'album': '÷', 'fmt': 'MP3', 'dur': 233, 'path': '/阿里云盘/Music/Ed Sheeran/divide/Shape of You.mp3', 'plays': 58},
    {'id': 's010', 'title': 'Blinding Lights', 'artist': 'The Weeknd', 'album': 'After Hours', 'fmt': 'OPUS', 'dur': 200, 'path': '/阿里云盘/Music/The Weeknd/After Hours/Blinding Lights.opus', 'plays': 54},
    {'id': 's011', 'title': 'Rolling in the Deep', 'artist': 'Adele', 'album': '21', 'fmt': 'FLAC', 'dur': 228, 'path': '/阿里云盘/Music/Adele/21/Rolling in the Deep.flac', 'plays': 50},
    {'id': 's012', 'title': 'Smells Like Teen Spirit', 'artist': 'Nirvana', 'album': 'Nevermind', 'fmt': 'FLAC', 'dur': 301, 'path': '/阿里云盘/Music/Nirvana/Nevermind/Smells Like Teen Spirit.flac', 'plays': 47},
    {'id': 's013', 'title': 'Hey Jude', 'artist': 'The Beatles', 'album': 'Past Masters', 'fmt': 'FLAC', 'dur': 431, 'path': '/阿里云盘/Music/The Beatles/Past Masters/Hey Jude.flac', 'plays': 43},
    {'id': 's014', 'title': 'Lose Yourself', 'artist': 'Eminem', 'album': '8 Mile', 'fmt': 'MP3', 'dur': 326, 'path': '/阿里云盘/Music/Eminem/8 Mile/Lose Yourself.mp3', 'plays': 40},
    {'id': 's015', 'title': 'Thriller', 'artist': 'Michael Jackson', 'album': 'Thriller', 'fmt': 'FLAC', 'dur': 358, 'path': '/阿里云盘/Music/Michael Jackson/Thriller/Thriller.flac', 'plays': 38},
    {'id': 's016', 'title': 'Superstition', 'artist': 'Stevie Wonder', 'album': 'Talking Book', 'fmt': 'FLAC', 'dur': 245, 'path': '/阿里云盘/Music/Stevie Wonder/Talking Book/Superstition.flac', 'plays': 35},
    {'id': 's017', 'title': 'Comfortably Numb', 'artist': 'Pink Floyd', 'album': 'The Wall', 'fmt': 'FLAC', 'dur': 382, 'path': '/阿里云盘/Music/Pink Floyd/The Wall/Comfortably Numb.flac', 'plays': 32},
    {'id': 's018', 'title': 'Like a Rolling Stone', 'artist': 'Bob Dylan', 'album': 'Highway 61', 'fmt': 'MP3', 'dur': 369, 'path': '/阿里云盘/Music/Bob Dylan/Highway 61/Like a Rolling Stone.mp3', 'plays': 29},
    {'id': 's019', 'title': 'Imagine', 'artist': 'John Lennon', 'album': 'Imagine', 'fmt': 'FLAC', 'dur': 187, 'path': '/阿里云盘/Music/John Lennon/Imagine/Imagine.flac', 'plays': 27},
    {'id': 's020', 'title': 'What a Wonderful World', 'artist': 'Louis Armstrong', 'album': 'What a Wonderful World', 'fmt': 'AAC', 'dur': 139, 'path': '/阿里云盘/Music/Louis Armstrong/What a Wonderful World.m4a', 'plays': 24},
]

TASKS: list[dict[str, Any]] = [
    {'id': 'T-007', 'path': '/阿里云盘/Music', 'mode': '增量', 'count': 342, 'total': 342, 'processed': 342, 'duration': '4m 12s', 'status': 'done', 'end': '2026-04-17 22:31', 'logs': [{'level': 'ok', 'message': '[DONE] 扫描完成！新增 342 首，跳过 1 首，总耗时 4m 12s'}]},
    {'id': 'T-006', 'path': '/阿里云盘/Music', 'mode': '全量', 'count': 15284, 'total': 15284, 'processed': 15284, 'duration': '18m 07s', 'status': 'done', 'end': '2026-04-10 14:22', 'logs': [{'level': 'ok', 'message': '[DONE] 全量构建完成'}]},
]

SUBSONIC_ITEMS: list[dict[str, str]] = [
    {'method': 'GET', 'path': '/rest/ping', 'desc': '心跳检测', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getIndexes', 'desc': '获取艺术家索引', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getMusicFolders', 'desc': '获取音乐文件夹', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getAlbumList', 'desc': '获取专辑列表（旧版）', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getAlbumList2', 'desc': '获取专辑列表', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getAlbum', 'desc': '专辑详情', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getSong', 'desc': '单曲信息', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getLicense', 'desc': '许可证检查', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getUser', 'desc': '用户信息', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getGenres', 'desc': '流派列表', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/search2', 'desc': '兼容搜索', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/search3', 'desc': '全文搜索', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getArtist', 'desc': '艺术家详情', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getCoverArt', 'desc': '封面图获取', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getPlaylists', 'desc': '获取歌单列表', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/getPlaylist', 'desc': '获取单个歌单', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/createPlaylist', 'desc': '创建歌单', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/updatePlaylist', 'desc': '更新歌单', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/deletePlaylist', 'desc': '删除歌单', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/stream', 'desc': '302 重定向播放流', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/download', 'desc': '302 重定向下载', 'status': 'done'},
    {'method': 'GET', 'path': '/rest/scrobble', 'desc': '播放记录上报', 'status': 'done'},
]


def get_stats() -> dict[str, Any]:
    albums = {song['album'] for song in SONGS}
    artists = {song['artist'] for song in SONGS}
    total = len(SONGS)
    fmt_counts: dict[str, int] = {}
    for song in SONGS:
        fmt_counts[song['fmt']] = fmt_counts.get(song['fmt'], 0) + 1
    fmt_colors = {'FLAC': '#34d399', 'MP3': '#a78bfa', 'AAC': '#fbbf24', 'OPUS': '#38bdf8'}
    formats = [
        {
            'name': name,
            'value': round(count / total * 100),
            'color': fmt_colors.get(name, '#38bdf8'),
        }
        for name, count in sorted(fmt_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    return {
        'total_songs': total,
        'total_albums': len(albums),
        'total_artists': len(artists),
        'monthly_plays': sum(song['plays'] for song in SONGS),
        'formats': formats,
    }


def list_songs() -> list[dict[str, Any]]:
    return deepcopy(SONGS)


def list_tasks() -> list[dict[str, Any]]:
    return deepcopy(TASKS)


def create_scan_task(path: str, mode: str, concurrency: int, batch_size: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    task = {
        'id': f"T-{now.strftime('%H%M%S')}",
        'path': path,
        'mode': '全量' if mode == 'full' else '增量',
        'count': 20,
        'total': 20,
        'processed': 20,
        'duration': '12s',
        'status': 'done',
        'end': now.strftime('%Y-%m-%d %H:%M UTC'),
        'logs': [
            {'level': 'info', 'message': f'[INIT] 路径 {path}，并发 {concurrency}，批大小 {batch_size}'},
            {'level': 'ok', 'message': '[OK] 已创建最小验证扫描任务'},
            {'level': 'ok', 'message': '[DONE] 当前为第一包验证态，任务直接完成并回写记录'},
        ],
    }
    TASKS.insert(0, task)
    return task


def get_subsonic_items() -> list[dict[str, str]]:
    return deepcopy(SUBSONIC_ITEMS)


def build_indexes() -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for artist in sorted({song['artist'] for song in SONGS}):
        letter = artist[0].upper() if artist and artist[0].isalpha() else '#'
        grouped.setdefault(letter, []).append({'id': artist.lower().replace(' ', '-'), 'name': artist, 'albumCount': len({song['album'] for song in SONGS if song['artist'] == artist})})
    return {'lastModified': 0, 'ignoredArticles': 'The', 'index': [{'name': key, 'artist': value} for key, value in grouped.items()]}


def build_album_list() -> list[dict[str, Any]]:
    seen = []
    for song in SONGS:
        if song['album'] not in [item['name'] for item in seen]:
            seen.append({'id': song['album'].lower().replace(' ', '-'), 'name': song['album'], 'artist': song['artist'], 'songCount': len([s for s in SONGS if s['album'] == song['album']])})
    return seen[:10]


def get_song(song_id: str) -> dict[str, Any] | None:
    for song in SONGS:
        if song['id'] == song_id:
            return deepcopy(song)
    return None
