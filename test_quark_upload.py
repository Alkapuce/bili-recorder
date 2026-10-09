import configparser
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import quark_upload


class UploadTest(unittest.TestCase):
    def test_retry_resume_and_no_duplicate_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cli = folder / "fake_cli.py"
            cli.write_text('''import json, pathlib, sys
if sys.argv[1:3] == ['upload','list']:
 print(json.dumps({'type':'list','code':0,'data':{'recordId':'resume-id','state':'failed'}}))
 print(json.dumps({'type':'result','code':0,'data':{}})); sys.exit(0)
path = pathlib.Path(sys.argv[sys.argv.index('--file-path') + 1])
calls = path.parent / 'calls.json'
args = json.loads(calls.read_text()) if calls.exists() else []
args.append(sys.argv[1:]); calls.write_text(json.dumps(args))
failed = len(args) == 1
print(json.dumps({'type':'list', 'code':-1 if failed else 0,
 'data':{'recordId':'resume-id','fileId':'file-id','fileSize':path.stat().st_size}}))
print(json.dumps({'type':'result','code':-204 if failed else 0,'msg':'test failure' if failed else 'ok','data':{}}))
sys.exit(1 if failed else 0)
''')
            config = configparser.ConfigParser()
            config.read_dict({"General": {"SaveDir": tmp}, "Upload": {
                "Enabled": "true", "QuarkCli": str(cli), "Node": sys.executable,
                "ParentFid": ""
            }})
            config_path = folder / "config.ini"
            with config_path.open("w") as f:
                config.write(f)
            final = folder / "session.transcoded.mp4"
            final.write_bytes(b"completed")
            (folder / "session.transcoded.partial.mp4").write_bytes(b"not complete")
            with self.assertRaisesRegex(RuntimeError, "1 个文件待重试"):
                quark_upload.main(str(config_path))
            state = json.loads((folder / ".quark-upload" / "manifest.json").read_text())
            self.assertEqual(state[final.name]["record_id"], "resume-id")
            self.assertTrue(quark_upload.upload_pending(final, state))
            self.assertTrue(final.exists())
            quark_upload.main(str(config_path))
            quark_upload.main(str(config_path))
            args = json.loads((folder / "calls.json").read_text())
            self.assertEqual(len(args), 2)
            self.assertEqual(args[1][:4], ["upload", "resume", "--record-id", "resume-id"])
            self.assertNotIn("--parent-fid", args[0])
            state = json.loads((folder / ".quark-upload" / "manifest.json").read_text())
            self.assertFalse(quark_upload.upload_pending(final, state))
            self.assertEqual(state[final.name]["file_id"], "file-id")
            self.assertTrue(final.exists())

    def test_enqueue_archives_and_continue_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            folder = Path(tmp)
            cli = folder / "fake_cli.py"
            cli.write_text('''import json, pathlib, sys
if sys.argv[1:3] == ['upload','list']:
 for path in pathlib.Path(__file__).parent.glob('*/*.mp4'):
  print(json.dumps({'type':'list','code':0,'data':{'recordId':str(path),'state':'failed'}}))
 print(json.dumps({'type':'result','code':0,'data':{}})); sys.exit(0)
path = pathlib.Path(sys.argv[sys.argv.index('--file-path') + 1])
failed = path.parent.name == 'failed'
print(json.dumps({'type':'list','code':-1 if failed else 0,
 'data':{'recordId':str(path),'fileId':'file-id','fileSize':path.stat().st_size}}))
print(json.dumps({'type':'result','code':-204 if failed else 0,'msg':'test failure','data':{}}))
sys.exit(1 if failed else 0)
''')
            for sub in ["failed", "success"]:
                (folder / sub).mkdir()
                (folder / sub / "same.mp4").write_bytes(b"archive")
            (folder / "active.mp4").write_bytes(b"still recording")
            config = configparser.ConfigParser()
            config.read_dict({"General": {"SaveDir": tmp}, "Upload": {
                "Enabled": "true", "QuarkCli": str(cli), "Node": sys.executable}})
            config_path = folder / "config.ini"
            with config_path.open("w") as f:
                config.write(f)
            # Enqueue subdirectories only; top-level raw recordings must stay invisible.
            quark_upload.main(str(config_path), enqueue=str(folder / "failed"))
            quark_upload.main(str(config_path), enqueue=str(folder / "success"))
            with self.assertRaisesRegex(RuntimeError, "1 个文件待重试"):
                quark_upload.main(str(config_path))
            state = json.loads((folder / ".quark-upload" / "manifest.json").read_text())
            self.assertEqual(state["failed/same.mp4"]["status"], "pending")
            self.assertEqual(state["success/same.mp4"]["status"], "uploaded")
            self.assertNotIn("active.mp4", state)
            with self.assertRaises(ValueError):
                quark_upload.main(str(config_path), enqueue=outside)

    def test_reconcile_completed_task_after_worker_stopped_before_ledger_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'session.transcoded.mp4'
            path.write_bytes(b'complete')
            root = folder / 'root.jsonl'
            root.write_text(json.dumps({'filename':'Uploads','category':0,'fid':'parent'}))
            files = folder / 'files.jsonl'
            files.write_text(json.dumps({'filename':path.name,'size':8,'fid':'cloud-file'}))
            record = {'status':'pending','size':8,'attempt_started_at':1}
            responses = [
                {'type':'list','data':{'recordId':'task','fileName':path.name,'fileSize':8,'createdAt':2000,'state':'success'}},
                {'type':'result','code':0,'data':{'file_path':str(root),'hasMore':False}},
                {'type':'result','code':0,'data':{'file_path':str(files),'hasMore':False}},
            ]
            with patch.object(quark_upload.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps(response)) for response in responses]) as run:
                quark_upload.recover_interrupted_upload(path, {'Node':'node','QuarkCli':'cli'}, record, 'session', folder)
            self.assertEqual(record['status'], 'uploaded')
            self.assertEqual(record['file_id'], 'cloud-file')
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ['upload','browse','browse'])


if __name__ == "__main__":
    unittest.main()
