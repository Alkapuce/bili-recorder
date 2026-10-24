"""Mirror finalized videos through a directory-scoped AList account."""
import json
import hashlib
from pathlib import Path
import re
import secrets
import urllib.request

from .session_store import SessionStore


def completed_files(folder):
    active = {Path(p).resolve() for session in SessionStore(str(folder)).load()
              for p in session["parts"]}
    return {p for p in folder.glob("*.mp4") if p.is_file() and not p.is_symlink()
            and p.resolve() not in active
            and not re.search(r"\.(partial|transcoded|merged)\.|_part\d+\.", p.name)}


class Alist:
    def __init__(self, credentials):
        settings = json.loads(Path(credentials).expanduser().read_text())
        self.url = settings["url"].rstrip("/")
        self.token = ""
        self.token = self.call("auth/login", {"username": settings["username"],
                                              "password": settings["password"]})["token"]

    def call(self, endpoint, data):
        request = urllib.request.Request(self.url + "/api/" + endpoint,
                                         data=json.dumps(data).encode(),
                                         headers={"Authorization": self.token,
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
        if result.get("code") != 200:
            raise RuntimeError(f"AList {endpoint}: {result.get('message', '操作失败')}")
        return result.get("data")

    def files(self):
        files = {}
        page = 1
        count = 0
        while True:
            result = self.call("fs/list", {"path": "/", "refresh": True,
                                           "page": page, "per_page": 1000})
            rows = result.get("content") or []
            for row in rows:
                if row["name"] in files:
                    raise RuntimeError("云端存在重复文件名，停止同步")
                files[row["name"]] = row
            count += len(rows)
            if count >= result["total"]:
                return files
            if not rows:
                raise RuntimeError("云端分页结果不完整，停止同步")
            page += 1

    def target_id(self):
        return self.call("fs/get", {"path": "/"})["id"]

    def verify(self, path, record):
        """Read cloud bytes independently of the upload response before local deletion."""
        row = self.files().get(path.name, {})
        if row.get("is_dir") or row.get("size") != record["size"]:
            raise RuntimeError("云端文件不存在或大小不一致，保留本地文件")
        details = self.call("fs/get", {"path": "/" + path.name, "refresh": True})
        if details.get("id") != record.get("file_id") or details.get("size") != record["size"]:
            raise RuntimeError("云端文件身份或大小不一致，保留本地文件")
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(details["raw_url"], timeout=120) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != record["size"] or digest.hexdigest() != record["sha256"]:
            raise RuntimeError("云端回读 SHA256 不一致，保留本地文件")

    def rename(self, old, new):
        for name in (old, new):
            if Path(name).name != name or name in (".", ".."):
                raise ValueError("云端改名只允许当前目录的文件")
        self.call("fs/rename", {"path": "/" + old, "name": new})

    def remove(self, names):
        if any(Path(name).name != name or name in (".", "..") for name in names):
            raise ValueError("云端删除只允许当前目录的文件")
        self.call("fs/remove", {"dir": "/", "names": names})


def reconcile(path, digest, ledger, cloud, remote):
    """Reuse renamed uploads; preserve replaced cloud content until sync succeeds."""
    key = path.name
    record = ledger.get(key, {})
    if not record:
        matches = [(old, item) for old, item in ledger.items()
                   if not (path.parent / old).exists() and Path(old).name == old
                   and item.get("status") == "uploaded" and item.get("sha256") == digest
                   and old in remote and remote[old]["size"] == path.stat().st_size]
        if len(matches) == 1 and key not in remote:
            old, record = matches[0]
            cloud.rename(old, key)
            remote[key] = remote.pop(old)
            ledger[key] = ledger.pop(old)
            record["mtime_ns"] = path.stat().st_mtime_ns
    existing = remote.get(key)
    unchanged = (record.get("status") == "uploaded" and record.get("sha256") == digest
                 and existing and existing["size"] == path.stat().st_size)
    if unchanged:
        record["mtime_ns"] = path.stat().st_mtime_ns
        return
    if existing:
        # Keep the old version recoverable if upload or verification fails.
        backup = f".previous-{secrets.token_hex(6)}-{key}"
        cloud.rename(key, backup)
        remote[backup] = remote.pop(key)
    if record.get("status") == "uploaded":
        record.clear()


def finish_sync(folder, snapshots, ledger, cloud):
    current = completed_files(folder)
    if {p.name for p in current} != set(snapshots):
        raise RuntimeError("同步期间本地文件集合变化，下轮重试，暂不删除云端文件")
    remote = cloud.files()
    for path in current:
        stat = path.stat()
        if snapshots[path.name] != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("同步期间本地文件变化，暂不删除云端文件")
        record = ledger.get(path.name, {})
        row = remote.get(path.name, {})
        if record.get("status") != "uploaded" or row.get("is_dir") or row.get("size") != stat.st_size:
            raise RuntimeError(f"云端文件未确认，暂不删除: {path.name}")
    stale = [name for name, row in remote.items()
             if not row["is_dir"] and name.endswith(".mp4") and name not in snapshots]
    if stale:
        cloud.remove(stale)
    remaining = cloud.files()
    if any(name in remaining for name in stale):
        raise RuntimeError("云端删除未完成，下轮重试")
    for key in list(ledger):
        if key not in snapshots and not (folder / key).exists():
            del ledger[key]
