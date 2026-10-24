"""Public defaults and private per-installation/per-room settings."""
import configparser
import os
from pathlib import Path


CONFIG_ENV_VAR = "BILI_CONFIG_PATH"


def get_config_path():
    return os.environ.get(CONFIG_ENV_VAR, "config/local.ini")


def read_config(config_path=None):
    path = Path(config_path or get_config_path()).expanduser().resolve()
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"配置文件不存在: {path}，请复制 config/example.ini 为 config/local.ini")
    for section, key in (("General", "SaveDir"), ("General", "StateDir"),
                         ("Upload", "QuarkCli"), ("Upload", "AlistCredentials")):
        value = config.get(section, key, fallback="").strip()
        if value:
            target = Path(value).expanduser()
            config[section][key] = str(target if target.is_absolute() else path.parent / target)
    if "General" in config:
        config["General"].setdefault("StateDir", str(Path(config["General"].get("SaveDir", path.parent)) / ".runtime"))
    # Resolve explicit room paths before handing a snapshot to a worker.
    for section in config.sections():
        if section.startswith("Room:") and config[section].get("SaveDir"):
            target = Path(config[section]["SaveDir"]).expanduser()
            config[section]["SaveDir"] = str(target if target.is_absolute() else path.parent / target)
    return config


def room_ids(config, enabled_only=False):
    if not config.get("General", "SaveDir", fallback="").strip():
        raise ValueError("General SaveDir 不能为空")
    rooms = {}
    for section in config.sections():
        if not section.startswith("Room:"):
            continue
        raw = section[5:]
        if not raw.isdecimal() or int(raw) <= 0 or str(int(raw)) != raw:
            raise ValueError(f"房间段名称无效: {section}")
        unknown = set(config[section]) - {"enabled", "savedir", "checkinterval", "parentfid"}
        if unknown:
            raise ValueError(f"{section} 未知配置项: {', '.join(sorted(unknown))}")
        rooms[int(raw)] = config[section].getboolean("Enabled", fallback=True)
    if len(rooms) > 1 and config.get("Upload", "Mode", fallback="archive").strip().lower() == "mirror":
        raise ValueError("多房间配置只支持 archive；mirror 必须使用各自独立目录的单房间配置")
    if not rooms:
        raw = config.get("General", "RoomId", fallback="0")
        if not raw.isdecimal() or int(raw) <= 0:
            raise ValueError("请配置 [Room:直播间号] 或有效的 [General] RoomId")
        rooms[int(raw)] = config.getboolean("General", "Enabled", fallback=True)
    directories = []
    for room in rooms:
        section = f"Room:{room}"
        root = Path(config["General"]["SaveDir"]).resolve()
        directory = Path(config.get(section, "SaveDir", fallback=str(root / str(room)))) if config.has_section(section) else root
        directory = directory.resolve()
        if any(directory == other or directory in other.parents or other in directory.parents for other in directories):
            raise ValueError("各房间录制目录不能相同或互相嵌套")
        directories.append(directory)
    return [room for room, enabled in rooms.items() if enabled or not enabled_only]


def room_config(config, room_id):
    if room_id not in room_ids(config):
        raise ValueError(f"未配置房间: {room_id}")
    result = configparser.ConfigParser(interpolation=None)
    result.read_dict({section: dict(config[section]) for section in config.sections()
                      if not section.startswith("Room:")})
    result["General"]["RoomId"] = str(room_id)
    section = f"Room:{room_id}"
    if config.has_section(section):
        result["General"]["SaveDir"] = str(Path(config["General"]["SaveDir"]) / str(room_id))
        for key in ("SaveDir", "CheckInterval"):
            if key in config[section]:
                result["General"][key] = config[section][key]
        if "ParentFid" in config[section]:
            result["Upload"]["ParentFid"] = config[section]["ParentFid"]
    if result.has_section("PostProcess"):
        result["PostProcess"]["LockFile"] = str(Path(config["General"]["StateDir"]) / "postprocess.lock")
    return result
