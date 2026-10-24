import configparser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


class SupervisorTest(unittest.TestCase):
    def test_two_real_recorders_independent_stop_start_and_duplicate_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = Path(__file__).parent / "fixtures/bilibili_entry.py"
            stream = root / "live.flv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=24:duration=12", "-f", "lavfi", "-i",
                            "sine=sample_rate=48000:duration=12", "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "aac", str(stream)], check=True)
            clients = {"active": 0, "max": 0}
            mutex = threading.Lock()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    with mutex:
                        clients["active"] += 1
                        clients["max"] = max(clients["max"], clients["active"])
                    try:
                        self.send_response(200)
                        self.send_header("Content-Length", str(stream.stat().st_size))
                        self.end_headers()
                        with stream.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024), b""):
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                time.sleep(0.01)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        with mutex:
                            clients["active"] -= 1
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            world = root / "world.json"
            world.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/live.flv"}))
            config = configparser.ConfigParser(interpolation=None)
            config.read("config/example.ini")
            config["General"].update(SaveDir=str(root / "recordings"), StateDir=str(root / "runtime"), CheckInterval="1")
            config["Recording"].update(ProcessPollIntervalSeconds="1", RecoverDelaySeconds="1")
            config["Room:123456"]["Enabled"] = "true"
            config["Room:654321"]["Enabled"] = "true"
            config_path = root / "local.ini"
            with config_path.open("w") as target:
                config.write(target)
            env = {**os.environ, "BILI_TEST_WORLD": str(world)}
            command = [sys.executable, str(fixture), "--config", str(config_path)]
            log = (root / "worker.log").open("w")
            process = subprocess.Popen(command + ["run"], env=env, stdout=log, stderr=log)
            def status(room):
                try:
                    return json.loads((root / f"runtime/{room}/status.json").read_text())
                except (OSError, ValueError):
                    return {}
            def wait(predicate):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.fail((root / "worker.log").read_text())
            try:
                wait(lambda: all(status(room).get("is_recording") for room in (123456, 654321)))
                wait(lambda: clients["max"] >= 2)
                statuses = [status(room) for room in (123456, 654321)]
                self.assertNotEqual(statuses[0]["pid"], statuses[1]["pid"])
                self.assertNotEqual(statuses[0]["current_file"], statuses[1]["current_file"])
                duplicate = subprocess.run(command + ["run", "--room", "123456"], env=env,
                                           capture_output=True, text=True, timeout=10)
                self.assertNotEqual(duplicate.returncode, 0)
                control = [sys.executable, "-m", "bili_recorder", "--config", str(config_path)]
                self.assertEqual(subprocess.run(control + ["stop", "123456"], capture_output=True).returncode, 0)
                wait(lambda: status(123456).get("stopped"))
                self.assertEqual(status(654321)["pid"], statuses[1]["pid"])
                self.assertFalse(status(654321).get("stopped"))
                self.assertEqual(subprocess.run(control + ["start", "123456"], capture_output=True).returncode, 0)
                wait(lambda: status(123456).get("is_recording") and status(123456).get("pid") != statuses[0]["pid"])
                session = list((root / "recordings/123456/.sessions").glob("*.json"))
                self.assertEqual(len(session), 1)
                self.assertGreaterEqual(len(json.loads(session[0].read_text())["parts"]), 2)
                results = Path(__file__).resolve().parents[1] / ".runtime/test-results"
                results.mkdir(parents=True, exist_ok=True)
                (results / "supervisor.json").write_text(json.dumps({"two_streams_overlap": clients["max"] >= 2,
                     "independent_pids": True, "duplicate_rejected": True, "stop_start_resumes_session": True}, sort_keys=True))
            finally:
                process.terminate()
                process.wait(timeout=20)
                log.close()
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertTrue(status(654321)["stopped"])

    def test_configuration_rejects_overlapping_or_malformed_rooms(self):
        from bili_recorder.config import read_config, room_ids
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.ini"
            for rooms in ("[Room:01]\nEnabled=true", "[Room:1]\nSaveDir=one\n[Room:2]\nSaveDir=one/nested"):
                path.write_text("[General]\nSaveDir=videos\n" + rooms)
                with self.assertRaises(ValueError):
                    room_ids(read_config(path))
            path.write_text("[General]\nSaveDir=videos\n[Room:1]\n[Room:2]\n[Upload]\nMode=mirror\n")
            with self.assertRaises(ValueError):
                room_ids(read_config(path))
