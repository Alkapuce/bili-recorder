import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote

from bili_recorder import media, recorder
from bili_recorder.session_store import SessionStore


class ArchiveTest(unittest.TestCase):
    def test_real_media_to_cli_archive_failure_verification_delete_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "recordings"
            folder.mkdir()
            cloud = root / "cloud"
            cloud.mkdir()
            shutil.copyfile(Path(__file__).parent / "fixtures/quark_cli.py", root / "quark.py")
            faults = {"corrupt": False, "change_local": False, "wrong_target": False}
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(self.path)
                    if self.path == "/api/auth/login":
                        result = {"token": "test"}
                    elif self.path == "/api/fs/list":
                        rows = [{"name": p.name, "size": p.stat().st_size, "is_dir": False} for p in cloud.iterdir()]
                        result = {"total": len(rows), "content": rows}
                    elif self.path == "/api/fs/get":
                        name = data["path"].lstrip("/")
                        result = {"id": "wrong" if faults["wrong_target"] else "target"} if not name else {
                            "id": name, "size": (cloud / name).stat().st_size,
                            "raw_url": f"http://127.0.0.1:{self.server.server_port}/{name}"}
                    else:
                        self.send_error(500, "Archive must never mutate cloud files")
                        return
                    payload = json.dumps({"code": 200, "data": result}).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(payload)

                def do_GET(self):
                    name = unquote(self.path.lstrip("/"))
                    payload = (cloud / name).read_bytes()
                    if faults["change_local"]:
                        original = (folder / name).stat()
                        (folder / name).write_bytes(b"X" * original.st_size)
                        os.utime(folder / name, ns=(original.st_atime_ns, original.st_mtime_ns))
                    if faults["corrupt"]:
                        payload = b"!" * len(payload)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            credentials = root / "alist.json"
            credentials.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                                               "username": "test", "password": "test"}))
            config = recorder.read_config("config/example.ini")
            for section in list(config.sections()):
                if section.startswith("Room:"):
                    config.remove_section(section)
            config["General"].update(RoomId="123456", SaveDir=str(folder), StateDir=str(root / "runtime"))
            config["PostProcess"].update(AutoPreferNvenc="false", Preset="10", MinValidDurationSeconds="1")
            config["Upload"].update(Enabled="true", Mode="archive", DeleteLocal="true",
                                    QuarkCli=str(root / "quark.py"), Node=sys.executable,
                                    ParentFid="target", AlistCredentials=str(credentials))
            config_path = root / "local.ini"
            with config_path.open("w") as stream:
                config.write(stream)
            # Produce a complete session through the same durable worker as recording.
            parts = [folder / f"live_part{n:03d}.flv" for n in (1, 2)]
            for part in parts:
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                "testsrc2=size=160x96:rate=24:duration=1.5", "-f", "lavfi", "-i",
                                "sine=sample_rate=48000:duration=1.5", "-c:v", "libx264",
                                "-preset", "ultrafast", "-c:a", "aac", str(part)], check=True)
            audio = b"".join(subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(part),
                                                     "-map", "0:a", "-c", "copy", "-f", "data", "-"]) for part in parts)
            store = SessionStore(folder)
            session = store.create(config, 123456, "session", "title")
            session["parts"] = list(map(str, parts))
            store.finish(session)
            # Container and host mount the same data at different absolute paths.
            mounted = root / "mounted"
            folder.rename(mounted)
            folder = mounted
            parts = [folder / part.name for part in parts]
            store = SessionStore(folder)
            config["General"]["SaveDir"] = str(folder)
            with config_path.open("w") as target:
                config.write(target)
            async def finish():
                worker = asyncio.create_task(recorder.process_pending_sessions(store, asyncio.Event()))
                try:
                    async def wait():
                        while store.load():
                            await asyncio.sleep(0.05)
                    await asyncio.wait_for(wait(), 30)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            asyncio.run(finish())
            final = folder / "session_title.mp4"
            original = final.read_bytes()
            self.assertEqual(media.ffprobe_video_info(str(final))["codec_name"], "av1")
            self.assertEqual(audio, subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(final),
                                                            "-map", "0:a", "-c", "copy", "-f", "data", "-"]))
            self.assertFalse(any(part.exists() for part in parts))
            def upload():
                return subprocess.run([sys.executable, "-m", "bili_recorder", "--config", str(config_path), "upload"],
                                      capture_output=True, text=True, timeout=20)
            (root / "offline").touch()
            self.assertNotEqual(upload().returncode, 0)
            self.assertTrue(final.exists())
            (root / "offline").unlink()
            faults["wrong_target"] = True
            self.assertNotEqual(upload().returncode, 0)
            self.assertFalse((cloud / final.name).exists())
            faults["wrong_target"] = False
            faults["corrupt"] = True
            self.assertNotEqual(upload().returncode, 0)
            self.assertTrue(final.exists())
            faults["corrupt"] = False
            faults["change_local"] = True
            self.assertNotEqual(upload().returncode, 0)
            self.assertTrue(final.exists())
            faults["change_local"] = False
            final.write_bytes(original)
            # Retain the original metadata to resume verification without re-uploading.
            ledger_path = folder / ".quark-upload/manifest.json"
            ledger = json.loads(ledger_path.read_text())
            os.utime(final, ns=(final.stat().st_atime_ns, ledger[final.name]["mtime_ns"]))
            result = upload()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(final.exists())
            self.assertEqual((cloud / final.name).read_bytes(), original)
            self.assertEqual(json.loads(ledger_path.read_text())[final.name]["status"], "archived")
            self.assertEqual(len((root / "uploads.jsonl").read_text().splitlines()), 1)
            # Simulate a crash after unlink but before the archived receipt was saved.
            ledger = json.loads(ledger_path.read_text())
            ledger[final.name]["status"] = "uploaded"
            ledger_path.write_text(json.dumps(ledger))
            self.assertEqual(upload().returncode, 0)
            self.assertEqual(upload().returncode, 0)
            self.assertTrue((cloud / final.name).exists())
            self.assertFalse(any("remove" in request or "rename" in request for request in requests))
            manifest = {"codec": "av1", "audio_preserved": True, "local_deleted": not final.exists(),
                        "cloud_content_matches": hashlib.sha256((cloud / final.name).read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest(),
                        "receipt": json.loads(ledger_path.read_text())[final.name]["status"]}
            results = Path(__file__).resolve().parents[1] / ".runtime/test-results"
            results.mkdir(parents=True, exist_ok=True)
            (results / "archive.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
