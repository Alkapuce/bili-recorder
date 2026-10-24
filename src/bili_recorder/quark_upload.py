"""Resumable Quark upload, explicit mirroring and verified archival deletion."""
import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import time

from .cloud_sync import Alist, completed_files, finish_sync, reconcile
from .config import read_config


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def upload_pending(path, ledger, key=None):
    record = ledger.get(key or path.name, {})
    stat = path.stat()
    return not (record.get("status") == "uploaded" and record.get("size") == stat.st_size
                and record.get("mtime_ns") == stat.st_mtime_ns)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_interrupted_upload(path, options, record, session_id, state_dir):
    """Recover the CLI task ID if the worker stopped before its terminal response."""
    if not record.get("record_id") and not record.get("attempt_started_at"):
        return
    command = [options.get("Node", "node"), os.path.expanduser(options["QuarkCli"]),
               "upload", "list", "--session-id", session_id]
    result = subprocess.run(command, cwd=state_dir, capture_output=True, text=True, check=True)
    candidates = []
    for line in result.stdout.splitlines():
        event = json.loads(line)
        data = event.get("data") or {}
        same_record = record.get("record_id") and data.get("recordId") == record["record_id"]
        same_attempt = (not record.get("record_id") and data.get("fileName") == path.name
                        and data.get("fileSize") == record["size"]
                        and data.get("createdAt", 0) >= record["attempt_started_at"] * 1000)
        if event.get("type") == "list" and (same_record or same_attempt):
            candidates.append(data)
    if len(candidates) > 1:
        raise RuntimeError("同名同大小存在多个上传任务，需要核对，避免重复上传")
    if candidates:
        record["record_id"] = candidates[0]["recordId"]
        if candidates[0].get("state") == "success":
            # The CLI cannot resume completed tasks; reconcile the cloud file instead.
            def browse(parent=None):
                command = [options.get("Node", "node"), os.path.expanduser(options["QuarkCli"]),
                           "browse", "--all", "--task-dir", str(state_dir), "--session-id", session_id]
                if parent:
                    command += ["--parent-fid", parent]
                response = subprocess.run(command, cwd=state_dir, capture_output=True, text=True, check=True)
                result = next(json.loads(line) for line in response.stdout.splitlines()
                              if json.loads(line).get("type") == "result")
                if result.get("code") != 0 or result["data"].get("hasMore"):
                    raise RuntimeError("云端文件查询未完成，保留上传任务")
                return [json.loads(line) for line in Path(result["data"]["file_path"]).read_text().splitlines()]

            parent = options.get("ParentFid", "").strip()
            if not parent:
                raise ValueError("恢复已完成上传需要明确的 ParentFid，保留任务")
            matches = [r for r in browse(parent) if r.get("filename") == path.name
                       and r.get("size") == record["size"]]
            if len(matches) != 1:
                raise RuntimeError("已完成任务的云端文件无法唯一确认，保留任务")
            record.update(status="uploaded", file_id=matches[0]["fid"], uploaded_at=time.time())
    elif record.get("record_id"):
        raise RuntimeError("CLI 上传任务记录不存在，保留本地文件，需要核对云端状态")


def upload_file(path, options, record, session_id, state_dir):
    command = [options.get("Node", "node"), os.path.expanduser(options["QuarkCli"])]
    if record.get("record_id"):
        command += ["upload", "resume", "--record-id", record["record_id"], "--file-path", str(path)]
    else:
        command += ["upload", "--file-path", str(path)]
        parent = options.get("ParentFid", "").strip()
        if parent:
            command += ["--parent-fid", parent]
    command += ["--session-id", session_id]
    result = None
    uploaded = None
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, cwd=state_dir) as process:
        for line in process.stdout:
            event = json.loads(line)
            data = event.get("data") or {}
            if event.get("type") == "list":
                if data.get("recordId"):
                    record["record_id"] = data["recordId"]
                if event.get("code") == 0 and data.get("fileId"):
                    uploaded = data
            if event.get("type") == "result":
                result = event
        return_code = process.wait()
    if return_code != 0 or not result or result.get("code") != 0:
        raise RuntimeError((result or {}).get("msg", "夸克上传未返回成功结果"))
    if not uploaded or uploaded.get("fileSize") != path.stat().st_size:
        raise RuntimeError("夸克上传结果缺少文件确认，或大小不一致")
    record.update(status="uploaded", file_id=uploaded["fileId"], uploaded_at=time.time(),
                  remote_path=result.get("data", {}).get("fullPath", ""))


def validate_upload_config(config):
    mode = config.get("Upload", "Mode", fallback="archive").strip().lower()
    if mode not in {"archive", "mirror"}:
        raise ValueError("Upload Mode 只支持 archive 或 mirror")
    delete = config.getboolean("Upload", "DeleteLocal", fallback=False)
    credentials = config.get("Upload", "AlistCredentials", fallback="").strip()
    parent = config.get("Upload", "ParentFid", fallback="").strip()
    if delete and (mode != "archive" or not credentials or not parent or parent == "0"):
        raise ValueError("DeleteLocal 需要 archive 模式、AList 核验账号和非根目录 ParentFid")
    if config.getboolean("Upload", "Enabled", fallback=False) and not config.get("Upload", "QuarkCli", fallback="").strip():
        raise ValueError("启用上传需要指定 QuarkCli")
    return mode, delete


def main(config_path=None, enqueue=None, config=None):
    config = config if config is not None else read_config(config_path)
    mode, delete_local = validate_upload_config(config)
    if not config.getboolean("Upload", "Enabled", fallback=False):
        return
    folder = Path(config["General"]["SaveDir"]).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    state_dir = folder / ".quark-upload"
    state_dir.mkdir(exist_ok=True)
    with (state_dir / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if enqueue:
                raise RuntimeError("上传服务正在运行，请稍后加入历史文件队列")
            return
        ledger_path = state_dir / "manifest.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        for key in ledger:
            candidate = folder / key
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(folder) or key.startswith("."):
                raise ValueError("上传清单包含不安全路径，保留现场")
        if enqueue:
            archive = Path(enqueue).resolve()
            archive.relative_to(folder)
            for path in sorted(archive.rglob("*.mp4") if archive.is_dir() else [archive]):
                if path.is_symlink() or not path.is_file() or path.suffix != ".mp4" or ".partial." in path.name:
                    raise ValueError(f"只允许加入 recordings 内的完整 MP4: {path}")
                key = path.resolve().relative_to(folder).as_posix()
                if upload_pending(path, ledger, key):
                    ledger.setdefault(key, {})["status"] = "pending"
            save_json(ledger_path, ledger)
            return
        paths = completed_files(folder)
        paths.update(folder / key for key, record in ledger.items()
                     if record.get("status") not in {"uploaded", "archived"}
                     or (delete_local and record.get("status") == "uploaded"))
        credentials = config["Upload"].get("AlistCredentials", "").strip()
        cloud = Alist(credentials) if credentials else None
        if cloud and not config["Upload"].get("ParentFid", "").strip():
            raise ValueError("镜像同步必须指定夸克目标目录 ParentFid")
        if cloud:
            target_id = cloud.target_id()
            if target_id in ("", "0") or target_id != config["Upload"]["ParentFid"].strip():
                raise ValueError("AList 账号基本路径必须指向同一个夸克子目录，禁止同步根目录")
        remote = cloud.files() if cloud and mode == "mirror" else None
        snapshots = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                     for p in completed_files(folder)}
        session_id = f"{int(time.time())}-{secrets.token_hex(3)}"
        failures = []
        for path in sorted(paths):
            key = path.resolve().relative_to(folder).as_posix()
            if not path.is_file():
                record = ledger.get(key, {})
                if delete_local and record.get("delete_verified_at"):
                    record["status"] = "archived"
                    save_json(ledger_path, ledger)
                    continue
                if cloud and mode == "mirror":
                    continue
                failures.append(key)
                logging.error("待上传文件缺失，保留任务: %s", key)
                continue
            if not cloud and not upload_pending(path, ledger, key):
                continue
            stat = path.stat()
            previous = ledger.get(key, {})
            digest = previous.get("sha256") if (previous.get("size"), previous.get("mtime_ns")) == (
                stat.st_size, stat.st_mtime_ns) else None
            if not digest:
                digest = sha256_file(path)
            if cloud and mode == "mirror":
                reconcile(path, digest, ledger, cloud, remote)
                save_json(ledger_path, ledger)
                if not upload_pending(path, ledger, key):
                    continue
            record = ledger.setdefault(key, {})
            if record.get("sha256") != digest:
                record.clear()
            record.update(sha256=digest, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                          status=record.get("status", "pending"))
            save_json(ledger_path, ledger)
            try:
                if record.get("status") != "uploaded":
                    recover_interrupted_upload(path, config["Upload"], record, session_id, state_dir)
                if record.get("status") != "uploaded":
                    record.setdefault("attempt_started_at", time.time())
                    save_json(ledger_path, ledger)
                    upload_file(path, config["Upload"], record, session_id, state_dir)
                if path.stat().st_size != stat.st_size or path.stat().st_mtime_ns != stat.st_mtime_ns:
                    raise RuntimeError("上传期间本地文件发生变化")
                if delete_local:
                    cloud.verify(path, record)
                    # Check content again, not only metadata, immediately before deletion.
                    current_digest = sha256_file(path)
                    current = path.lstat()
                    if (path.is_symlink() or current.st_ino != stat.st_ino
                            or current.st_size != stat.st_size or current.st_mtime_ns != stat.st_mtime_ns
                            or current_digest != digest):
                        raise RuntimeError("核验期间本地文件变化，取消删除")
                    record["delete_verified_at"] = time.time()
                    save_json(ledger_path, ledger)
                    path.unlink()
                    record["status"] = "archived"
                record.pop("last_error", None)
                save_json(ledger_path, ledger)
                logging.info("%s: %s", "已云端回读核验并归档" if delete_local else "已上传并确认大小", key)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                record.update(last_error=str(error)[:200])
                save_json(ledger_path, ledger)
                logging.error("上传失败，保留本地文件，下次重试: %s: %s", key, str(error)[:200])
                failures.append(key)
        if failures:
            raise RuntimeError(f"{len(failures)} 个文件待重试: {', '.join(failures)}")
        if cloud and mode == "mirror":
            finish_sync(folder, snapshots, ledger, cloud)
            save_json(ledger_path, ledger)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enqueue", help="将 recordings 内已整理的 MP4 文件或目录加入上传队列")
    args = parser.parse_args()
    main(enqueue=args.enqueue)
