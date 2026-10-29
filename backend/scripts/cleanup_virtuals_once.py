from app.db.session import engine
from app.services.db_data import cleanup_virtual_songs
from sqlalchemy import text

with engine.begin() as conn:
    deleted = cleanup_virtual_songs(conn)
    rows = conn.execute(text("SELECT id,title,artist,album,fmt,dur,path FROM songs WHERE title ILIKE '%伤心的酒吧%' OR artist ILIKE '%孙艺琪%' ORDER BY fmt, title")).mappings().all()

print({'deleted_virtuals': deleted, 'rows': [dict(r) for r in rows]})
