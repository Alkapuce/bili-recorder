"""Replace only the external SDK; use the real CLI/supervisor/recorder/FFmpeg."""
import json
import os
from pathlib import Path
from bilibili_api import live
from bili_recorder.cli import main


class LiveRoom:
    def __init__(self, room_id):
        self.room_id = room_id

    async def get_room_info(self):
        return {"room_info": {"live_status": 1, "title": "same title"}}

    async def get_room_play_url(self):
        world = json.loads(Path(os.environ["BILI_TEST_WORLD"]).read_text())
        return {"durl": [{"url": world["url"]}]}


live.LiveRoom = LiveRoom

if __name__ == "__main__":
    raise SystemExit(main())
