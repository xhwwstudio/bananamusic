import httpx
from sqlalchemy import text
from app.db.session import engine
from app.services.db_data import _relink_play_history

ALIST_URL = 'http://10.0.0.173:5244'
ALIST_TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VybmFtZSI6ImFkbWluIiwicHdkX3RzIjoxNzc2NjEyNzYzLCJleHAiOjE3NzcxOTM0OTUsIm5iZiI6MTc3NzAyMDY5NSwiaWF0IjoxNzc3MDIwNjk1fQ.O4QOaxWRj5LDNY_laTGem5oCg2Q_f6gMjlm4zYfxQwM'

PAIRS = [
    # B 组
    {'keep_id':'alist-a0480e909c9ad980','delete_id':'alist-31a5a28e7c6fd260','delete_path':'/quark/music/群星/Bad Angel (Explicit)/Anyma、LISA - Bad Angel (Explicit).flac'},
    {'keep_id':'alist-c226e1b3b55e39f3','delete_id':'alist-2362c61f6ca7eb5c','delete_path':'/quark/music/群星/孤独Person (你对我的了解 无声的告别)/Herme4、WhyAce、Zy - 孤独Person (你对我的了解 无声的告别).flac'},
    # A 组
    {'keep_id':'alist-2b7d777879e8590a','delete_id':'alist-dbcd1f5db805a061','delete_path':'/quark/music/群星/Deadend (活该)/Tyson Yoshi、王嘉尔 - Deadend(活该).flac'},
    {'keep_id':'alist-7724c4cafaa1c38a','delete_id':'alist-e006ef84b7aa1509','delete_path':'/quark/music/Ulaa（尤拉）/Fading Light（渐暗的光）/Ulaa（尤拉） - Fading Light(渐暗的光).flac'},
    {'keep_id':'alist-2a6bcb955cc7ad1a','delete_id':'alist-a94cb84c1a7115d5','delete_path':'/quark/music/sienna Miller（希娜）/I\'m Just Your Type(命中偏爱)/sienna Miller（希娜） - I\'m Just Your Type (命中偏爱).flac'},
    {'keep_id':'alist-fe0b116691b1ee5b','delete_id':'alist-9d52bd51949261d8','delete_path':'/quark/music/一毫升/夜夜夜夜（R&B版）/一毫升 - 夜夜夜夜 (R&B版).flac'},
    {'keep_id':'alist-2b540615a9418e80','delete_id':'alist-34daa8eac406000a','delete_path':'/quark/music/群星/尘/薛之谦、刘惜君 - 聊表心意.flac'},
    {'keep_id':'alist-8c871a36dbebbf4b','delete_id':'alist-520bc854300ead64','delete_path':'/quark/music/林雪儿/俩俩相忘（拈朵微笑的花）/林雪儿 - 俩俩相忘 (拈朵微笑的花).flac'},
    {'keep_id':'alist-b5bc3efce806c12d','delete_id':'alist-5ae2d29649544390','delete_path':'/quark/music/江辰/瘾/江辰 - 锈 (我们两个都拉过勾).flac'},
    {'keep_id':'alist-1f7da53228ec6ff8','delete_id':'alist-f1d2483e96032959','delete_path':'/quark/music/鱼多余/剑魂 (鱼多余DJ版)/鱼多余 - 剑魂(鱼多余DJ版).flac'},
]

headers={'Authorization': ALIST_TOKEN}
results=[]
with httpx.Client(timeout=30.0) as client:
    with engine.begin() as conn:
        for pair in PAIRS:
            keep_id=pair['keep_id']
            delete_id=pair['delete_id']
            delete_path=pair['delete_path']
            # remove source file first
            remove_ok=False
            remove_msg=''
            try:
                res=client.post(f'{ALIST_URL}/api/fs/remove', headers=headers, json={'dir': delete_path.rsplit('/',1)[0], 'names':[delete_path.rsplit('/',1)[1]]})
                remove_ok = res.status_code==200 and res.json().get('code')==200
                remove_msg = res.text[:300]
            except Exception as e:
                remove_msg = str(e)
            # relink references then delete duplicate DB row
            _relink_play_history(conn, delete_id, keep_id)
            conn.execute(text('DELETE FROM playlist_tracks WHERE song_id = :song_id'), {'song_id': delete_id})
            conn.execute(text('DELETE FROM ratings WHERE song_id = :song_id'), {'song_id': delete_id})
            conn.execute(text('DELETE FROM bookmarks WHERE song_id = :song_id'), {'song_id': delete_id})
            conn.execute(text('DELETE FROM songs WHERE id = :song_id'), {'song_id': delete_id})
            results.append({'keep_id':keep_id,'delete_id':delete_id,'delete_path':delete_path,'remove_ok':remove_ok,'remove_msg':remove_msg})
print(results)
