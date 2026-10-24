"""CLI for recording, individual room controls and host-side upload workers."""
import argparse
import configparser
import json
import logging
import os
from pathlib import Path
import sys

from .config import get_config_path, read_config, room_config, room_ids
from .locking import exclusive


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=get_config_path())
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="监督所有已启用房间，或独立运行一个房间")
    run.add_argument("--room", type=int)
    for action in ("start", "stop"):
        command = sub.add_parser(action, help="修改房间启用状态，由正在运行的监督进程执行")
        command.add_argument("room", type=int)
    sub.add_parser("status", help="显示每个房间的独立状态")
    sub.add_parser("health", help="检查已启用房间的心跳")
    sub.add_parser("check", help="校验配置，不连接外部服务")
    upload = sub.add_parser("upload", help="上传所有房间已完成录像；包含停用房间的待处理文件")
    upload.add_argument("--room", type=int)
    upload.add_argument("--enqueue")
    args = parser.parse_args(argv)
    config_path = str(Path(args.config).expanduser().resolve())
    try:
        config = read_config(config_path)
        rooms = room_ids(config)
        if args.command in ("start", "stop"):
            if args.room not in rooms:
                raise ValueError(f"未配置房间: {args.room}")
            with exclusive(config_path + ".lock"):
                settings = configparser.ConfigParser(interpolation=None)
                settings.read(config_path, encoding="utf-8")
                section = f"Room:{args.room}" if settings.has_section(f"Room:{args.room}") else "General"
                settings[section]["Enabled"] = str(args.command == "start").lower()
                temporary = Path(config_path + ".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    settings.write(target)
                temporary.replace(config_path)
            print(f"房间 {args.room}: {args.command}（需有 run 监督进程）")
        elif args.command == "run":
            from .supervisor import run_room, supervise
            if args.room is not None:
                run_room(config_path, args.room)
            else:
                supervise(config_path)
        elif args.command == "upload":
            from .quark_upload import main as upload_room
            if args.enqueue and args.room is None and len(rooms) != 1:
                raise ValueError("多个房间时 --enqueue 必须同时指定 --room")
            selected = [args.room] if args.room is not None else rooms
            failures = []
            for room in selected:
                options = room_config(config, room)
                folder = Path(options["General"]["SaveDir"])
                if not folder.exists() and not args.enqueue:
                    continue
                try:
                    upload_room(config=options, enqueue=args.enqueue)
                except (OSError, ValueError, RuntimeError) as error:
                    logging.error("房间 %s 上传失败: %s", room, error)
                    failures.append(room)
            if failures:
                raise RuntimeError(f"房间上传待重试: {failures}")
        elif args.command == "check":
            from .recorder import validate_startup_config
            from .quark_upload import validate_upload_config
            for room in rooms:
                options = room_config(config, room)
                validate_startup_config(options)
                validate_upload_config(options)
            print(f"配置有效，{len(rooms)} 个房间，已启用 {len(room_ids(config, True))} 个")
        else:
            statuses = []
            healthy = True
            enabled = room_ids(config, True)
            import time
            for room in rooms:
                path = Path(config["General"]["StateDir"]) / str(room) / "status.json"
                try:
                    status = json.loads(path.read_text())
                except (OSError, ValueError):
                    status = {}
                alive = not status.get("stopped", False) and time.time() - status.get("heartbeat_ts", 0) < 180
                healthy &= room not in enabled or alive
                statuses.append({**status, "room_id": room, "enabled": room in enabled, "healthy": alive})
            if args.command == "health":
                return 0 if healthy else 1
            print(json.dumps(statuses, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, configparser.Error) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
