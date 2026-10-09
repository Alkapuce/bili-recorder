import asyncio
import os
import subprocess
import tempfile
import unittest
import json
import functools
import http.server
import struct
import threading
from unittest.mock import AsyncMock, patch

import recorder


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_parts_survive_clean_exit_retry_limit_and_status_error(self):
        config = recorder.read_config("config.example.ini")
        config["General"]["RoomId"] = "123456"
        config["Recording"]["MaxRecoverRetries"] = "0"
        checks = 0
        files = []
        completed = asyncio.Event()
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0.01)

        async def live_status(*args):
            nonlocal checks
            checks += 1
            if checks in (1, 3, 4):
                return True, f"title {checks}", ""
            if checks == 2:
                return False, "", "check_live_status_352"
            if checks == 5:
                return False, "", ""
            await completed.wait()
            raise RuntimeError("test complete")

        def record(*args, file_path):
            with open(file_path, "wb") as f:
                f.write(b"test")
            files.append(file_path)
            return len(files) != 2, "completed", file_path

        async def process(parts, cfg, tag, title, previous=None):
            self.assertEqual(parts, files)
            self.assertEqual(len(parts), 3)
            self.assertEqual(title, "title 1")
            self.assertTrue(files[1].endswith("_part002.flv"))
            self.assertTrue(files[2].endswith("_part003.flv"))
            completed.set()
            return True

        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            with patch.object(recorder, "STATUS_FILE", os.path.join(tmp, "status.json")), \
                 patch.object(recorder, "read_config", return_value=config), \
                 patch.object(recorder, "check_live_status_with_retry", live_status), \
                 patch.object(recorder, "get_live_url_with_retry", AsyncMock(return_value=("test", ""))), \
                 patch.object(recorder, "cleanup_recordings_if_needed"), \
                 patch.object(recorder, "record_stream", record), \
                 patch.object(recorder, "process_recording_session", process), \
                 patch.object(recorder.asyncio, "sleep", fast_sleep):
                existing = asyncio.all_tasks()
                try:
                    with self.assertRaisesRegex(RuntimeError, "test complete"):
                        await asyncio.wait_for(recorder.main(), 3)
                    self.assertTrue(completed.is_set())
                finally:
                    tasks = asyncio.all_tasks() - existing
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_restart_recovers_recording_and_next_part_number(self):
        config = recorder.read_config("config.example.ini")
        config["General"]["RoomId"] = "123456"
        config["Recording"]["MaxRecoverRetries"] = "0"
        real_sleep = asyncio.sleep
        recorded = []
        finished = asyncio.Event()

        async def fast_sleep(_):
            await real_sleep(0.01)

        def record(*args, file_path):
            with open(file_path, "wb") as f:
                f.write(b"recoverable")
            recorded.append(file_path)
            if len(recorded) == 1:
                raise RuntimeError("simulated crash")
            return True, "completed", file_path

        async def process(parts, *args):
            self.assertEqual(parts, recorded)
            self.assertTrue(parts[1].endswith("_part002.flv"))
            finished.set()
            return True

        async def end_status(*args):
            await finished.wait()
            raise RuntimeError("test complete")

        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            with patch.object(recorder, "STATUS_FILE", os.path.join(tmp, "status.json")), \
                 patch.object(recorder, "read_config", return_value=config), \
                 patch.object(recorder, "get_live_url_with_retry", AsyncMock(return_value=("test", ""))), \
                 patch.object(recorder, "cleanup_recordings_if_needed"), \
                 patch.object(recorder, "record_stream", record), \
                 patch.object(recorder, "process_recording_session", process), \
                 patch.object(recorder.asyncio, "sleep", fast_sleep):
                for restart in [False, True]:
                    calls = 0

                    async def status(*args):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            return True, "same live", ""
                        if calls == 2:
                            return False, "", ""
                        return await end_status()

                    existing = asyncio.all_tasks()
                    try:
                        with patch.object(recorder, "check_live_status_with_retry", status):
                            with self.assertRaisesRegex(RuntimeError, "test complete" if restart else "simulated crash"):
                                await asyncio.wait_for(recorder.main(), 3)
                    finally:
                        tasks = asyncio.all_tasks() - existing
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                self.assertTrue(finished.is_set())
                self.assertEqual(recorder.SessionStore(tmp).load(), [])

    async def test_pending_job_survives_failure_and_worker_restart(self):
        config = recorder.read_config("config.example.ini")
        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            store = recorder.SessionStore(tmp)
            session = store.create(config, 123456, "test", "title")
            part = os.path.join(tmp, 'part.flv')
            with open(part, 'wb') as f:
                f.write(b'recording')
            session['parts'] = [part]
            store.finish(session)
            wake = asyncio.Event()
            for success in [False, True]:
                with patch.object(recorder, 'process_recording_session', AsyncMock(return_value=success)) as process:
                    worker = asyncio.create_task(recorder.process_pending_sessions(store, wake))
                    try:
                        for _ in range(100):
                            if process.await_count:
                                break
                            await asyncio.sleep(0.01)
                        self.assertEqual(process.await_count, 1)
                    finally:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                remaining = store.load()
                if not success:
                    self.assertTrue(os.path.exists(part))
                    self.assertGreater(remaining[0]['retry_at'], 0)
                    remaining[0]['retry_at'] = 0
                    store.save(remaining[0])
                else:
                    self.assertEqual(remaining, [])
                    self.assertFalse(os.path.exists(part))


class PostprocessTest(unittest.TestCase):
    def test_final_name_collision_preserves_original_recording(self):
        config = recorder.read_config('config.example.ini')
        with tempfile.TemporaryDirectory() as tmp:
            config['General']['SaveDir'] = tmp
            source = os.path.join(tmp, 'test_title.mp4')
            with open(source, 'wb') as stream:
                stream.write(b'original recording')
            self.assertFalse(asyncio.run(recorder.process_recording_session([source], config, 'test', 'title')))
            with open(source, 'rb') as stream:
                self.assertEqual(stream.read(), b'original recording')

    def test_session_state_rejects_paths_outside_recordings(self):
        config = recorder.read_config('config.example.ini')
        with tempfile.TemporaryDirectory() as tmp:
            config['General']['SaveDir'] = tmp
            store = recorder.SessionStore(tmp)
            session = store.create(config, 123456, 'test', 'title')
            session['parts'] = [os.path.join(tmp, '..', 'unrelated.flv')]
            store.save(session)
            with self.assertRaises(ValueError):
                store.load()

    def test_real_http_recording_and_durable_postprocess_with_missing_flv_duration(self):
        config = recorder.read_config("config.example.ini")
        config['PostProcess']['AutoPreferNvenc'] = 'false'
        config['PostProcess']['Preset'] = '10'
        config['PostProcess']['MinValidDurationSeconds'] = '1'
        config['Recording']['ProcessPollIntervalSeconds'] = '1'
        with tempfile.TemporaryDirectory() as tmp:
            config['General']['SaveDir'] = tmp
            original = os.path.join(tmp, 'live.flv')
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30:duration=2',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', original
            ], check=True)
            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=tmp))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            recording = os.path.join(tmp, 'test_title.flv')
            try:
                ok, reason, path = recorder.record_stream(
                    f'http://127.0.0.1:{server.server_port}/live.flv', 'title', config, recording)
                self.assertTrue(ok, reason)
                self.assertEqual(path, recording)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            def audio_bytes(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a',
                                                '-c', 'copy', '-f', 'data', '-'])
            audio = audio_bytes(recording)
            self.assertEqual(audio, audio_bytes(original))
            with open(recording, 'rb') as f:
                data = bytearray(f.read())
            duration_pos = data.index(b'duration') + len(b'duration')
            self.assertEqual(data[duration_pos], 0)
            data[duration_pos + 1:duration_pos + 9] = struct.pack('>d', 0)
            with open(recording, 'wb') as f:
                f.write(data)
            self.assertGreater(recorder.ffprobe_media_info(recording)[0], 1.9)
            store = recorder.SessionStore(tmp)
            session = store.create(config, 123456, 'test', 'title')
            session['parts'] = [recording]
            store.finish(session)
            async def process():
                worker = asyncio.create_task(recorder.process_pending_sessions(store, asyncio.Event()))
                try:
                    for _ in range(300):
                        if not store.load():
                            return
                        await asyncio.sleep(0.05)
                    self.fail('durable worker did not finish')
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            asyncio.run(process())
            output = os.path.join(tmp, 'test_title.mp4')
            self.assertEqual(recorder.ffprobe_video_info(output)['codec_name'], 'av1')
            self.assertEqual(audio, audio_bytes(output))
            self.assertFalse(os.path.exists(recording))

    def test_real_mixed_video_normalized_to_one_av1_with_copied_audio(self):
        config = recorder.read_config("config.example.ini")
        config['PostProcess']['AutoPreferNvenc'] = 'false'
        config['PostProcess']['KeepSource'] = 'true'
        config['PostProcess']['Preset'] = '10'
        with tempfile.TemporaryDirectory() as tmp:
            config['General']['SaveDir'] = tmp
            parts = [os.path.join(tmp, f'part{n}.mkv') for n in range(2)]
            for n, part in enumerate(parts):
                subprocess.run([
                    'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    f'testsrc2=size={320 * (n+1)}x{180 * (n+1)}:rate=24:duration=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', part
                ], check=True)
            self.assertEqual(recorder.merge_recording_parts_if_needed(parts, config, 'test', 'mixed'),
                             (None, 'incompatible_video_streams'))
            self.assertTrue(asyncio.run(recorder.process_recording_session(parts, config, 'test', 'mixed')))
            output = os.path.join(tmp, 'test_mixed.mp4')
            self.assertEqual(recorder.ffprobe_video_info(output)['codec_name'], 'av1')
            def audio_bytes(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a',
                                                '-c', 'copy', '-f', 'data', '-'])
            self.assertEqual(audio_bytes(output), b''.join(audio_bytes(p) for p in parts))
            self.assertTrue(all(os.path.exists(p) for p in parts))
    def test_cleanup_keeps_pending_uploads(self):
        config = recorder.read_config("config.example.ini")
        config["Upload"]["Enabled"] = "true"
        config["Maintenance"]["KeepDays"] = "1"
        config["Maintenance"]["KeepCount"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            final = os.path.join(tmp, "session.mp4")
            with open(final, "wb") as f:
                f.write(b"uploaded")
            os.utime(final, (1, 1))
            recorder.cleanup_recordings_if_needed(config)
            self.assertTrue(os.path.exists(final))
            state_dir = os.path.join(tmp, ".quark-upload")
            os.mkdir(state_dir)
            stat = os.stat(final)
            with open(os.path.join(state_dir, "manifest.json"), "w") as f:
                json.dump({"session.mp4": {
                    "status": "uploaded", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns
                }}, f)
            recorder.cleanup_recordings_if_needed(config)
            self.assertFalse(os.path.exists(final))

    def test_real_merge_av1_and_audio_copy(self):
        config = recorder.read_config("config.example.ini")
        config["PostProcess"]["AutoPreferNvenc"] = "false"
        config["PostProcess"]["KeepSource"] = "true"
        config["PostProcess"]["MinValidDurationSeconds"] = "1"
        config["PostProcess"]["Preset"] = "10"
        with tempfile.TemporaryDirectory() as tmp:
            config["General"]["SaveDir"] = tmp
            parts = [os.path.join(tmp, f"part'{n}.flv") for n in range(2)]
            for part in parts:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=24:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", part
                ], check=True)
            # A crash leaves FLV duration metadata at zero; concat must use recovered packet times.
            with open(parts[0], 'rb') as f:
                data = bytearray(f.read())
            pos = data.index(b'duration') + len(b'duration') + 1
            data[pos:pos+8] = struct.pack('>d', 0)
            with open(parts[0], 'wb') as f:
                f.write(data)
            merged, reason = recorder.merge_recording_parts_if_needed(parts, config, "test", "title")
            self.assertEqual(reason, "merged")
            self.assertTrue(all(os.path.exists(p) for p in parts))
            self.assertEqual(recorder.resolve_transcode_attempts(config), [("libsvtav1", "34", "10")])
            ok, reason = recorder.transcode_after_recording_if_enabled(merged, config)
            self.assertTrue(ok, reason)
            output = os.path.splitext(merged)[0] + ".transcoded.mp4"
            codec = subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=codec_name", "-of", "csv=p=0", output
            ], text=True).strip()
            self.assertEqual(codec, "av1")
            def audio_hash(path):
                return subprocess.check_output([
                    "ffmpeg", "-v", "error", "-i", path, "-map", "0:a", "-c:a", "copy",
                    "-f", "hash", "-hash", "sha256", "-"
                ])
            self.assertEqual(audio_hash(merged), audio_hash(output))
            self.assertTrue(os.path.exists(merged))
            valid, reason = recorder.validate_transcoded_file(output, source_files=[merged, merged])
            self.assertFalse(valid)
            self.assertEqual(reason, "output_duration_mismatch")
            with patch.object(recorder, "ffprobe_media_info", side_effect=[(4, []), (4, [("aac", "48000", 1)])]):
                self.assertEqual(recorder.validate_transcoded_file(output, source_files=[merged]),
                                 (False, "output_audio_mismatch"))


if __name__ == "__main__":
    unittest.main()
