"""External Quark process double; persists bytes so HTTP verification is independent."""
import json
from pathlib import Path
import shutil
import sys

root = Path(__file__).parent
cloud = root / "cloud"
cloud.mkdir(exist_ok=True)
if sys.argv[1:3] == ["upload", "list"]:
    print(json.dumps({"type": "result", "code": 0, "data": {}}))
    raise SystemExit(0)
source = Path(sys.argv[sys.argv.index("--file-path") + 1])
if (root / "offline").exists():
    print(json.dumps({"type": "result", "code": -1, "msg": "offline"}))
    raise SystemExit(1)
shutil.copyfile(source, cloud / source.name)
with (root / "uploads.jsonl").open("a") as log:
    log.write(json.dumps({"name": source.name}) + "\n")
print(json.dumps({"type": "list", "code": 0,
                  "data": {"fileId": source.name, "fileSize": source.stat().st_size}}))
print(json.dumps({"type": "result", "code": 0, "data": {}}))
