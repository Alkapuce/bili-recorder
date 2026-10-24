import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from bili_recorder import recorder, media


class HeartbeatTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_during_recording_merge_and_transcode(self):
        config = recorder.read_config("config/example.ini")
        config["General"]["RoomId"] = "123456"
        loop_thread = threading.get_ident()
        real_sleep = asyncio.sleep
        stages = []
        processed = asyncio.Event()
        checks = 0

        async def fast_sleep(seconds):
            await real_sleep(0.01)

        def check_heartbeat(stage):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            with open(recorder.STATUS_FILE) as f:
                before = json.load(f)
            time.sleep(0.08)
            with open(recorder.STATUS_FILE) as f:
                after = json.load(f)
            self.assertGreater(after["heartbeat_ts"], before["heartbeat_ts"])
            for key in ("room_id", "is_recording", "current_file", "last_error"):
                self.assertEqual(after[key], before[key])
            stages.append(stage)

        def record(*args, file_path):
            with open(file_path, "wb") as f:
                f.write(b"test")
            check_heartbeat("record")
            return True, "completed", file_path

        def merge(parts, *args):
            check_heartbeat("merge")
            return parts[0], "single_part"

        def transcode(*args):
            check_heartbeat("transcode")
            loop.call_soon_threadsafe(processed.set)
            return True, "ok"

        loop = asyncio.get_running_loop()

        async def live_status(*args):
            nonlocal checks
            checks += 1
            if checks == 1:
                return True, "test", ""
            if checks == 2:
                return False, "", ""
            await processed.wait()
            raise RuntimeError("test complete")

        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            with patch.object(recorder, "STATUS_FILE", os.path.join(tmp, "status.json")), \
                 patch.object(recorder, "read_config", return_value=config), \
                 patch.object(recorder, "check_live_status_with_retry", live_status), \
                 patch.object(recorder, "get_live_url_with_retry", AsyncMock(return_value=("test", ""))), \
                 patch.object(recorder, "cleanup_recordings_if_needed"), \
                 patch.object(recorder, "record_stream", record), \
                 patch.object(media, "merge_recording_parts_if_needed", merge), \
                 patch.object(media, "transcode_after_recording_if_enabled", transcode), \
                 patch.object(recorder.asyncio, "sleep", fast_sleep):
                existing_tasks = asyncio.all_tasks()
                try:
                    with self.assertRaisesRegex(RuntimeError, "test complete"):
                        await asyncio.wait_for(recorder.main(), timeout=3)
                    self.assertEqual(stages, ["record", "merge", "transcode"])
                finally:
                    tasks = asyncio.all_tasks() - existing_tasks
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
