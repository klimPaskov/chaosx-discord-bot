import sys
sys.path.insert(0, "src")
from chaosx_bot.db import connect
c = connect("/home/ubuntu/chaosx/chaosx.db")
row = c.execute("select value from index_meta where key='commit_sha'").fetchone()
print("index commit:", (row[0] if row else "")[:50])
print("docs:", c.execute("select count(*) from source_docs").fetchone()[0])
c.close()
from chaosx_bot.video_context import ytdlp_available, stt_available
print("yt-dlp:", ytdlp_available(), "| whisper STT:", stt_available())
