from sqlalchemy import text
from app.db.session import engine
from app.services.db_data import cleanup_virtual_songs

REAL_PATH = '/quark/music/Vendredi sur mer/Écoute Chérie/Vendredi sur mer - Écoute Chérie.flac'
FIX_TITLE = 'Écoute Chérie'
FIX_ARTIST = 'Vendredi sur mer'
FIX_ALBUM = 'Écoute Chérie'

with engine.begin() as conn:
    conn.execute(
        text("""
        UPDATE songs
        SET title = :title,
            artist = :artist,
            album = :album,
            updated_at = NOW()
        WHERE path = :path
        """),
        {
            'title': FIX_TITLE,
            'artist': FIX_ARTIST,
            'album': FIX_ALBUM,
            'path': REAL_PATH,
        }
    )
    deleted = cleanup_virtual_songs(conn)
    rows = conn.execute(
        text("SELECT id, title, artist, album, fmt, path FROM songs WHERE artist = :artist ORDER BY fmt, title"),
        {'artist': FIX_ARTIST},
    ).mappings().all()

print({'deleted_virtuals': deleted, 'rows': [dict(r) for r in rows]})
