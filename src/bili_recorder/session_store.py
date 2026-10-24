"""Durable recording sessions, shared by the recorder and its post-processing worker."""
import configparser
import json
from pathlib import Path


class SessionStore:
    def __init__(self, save_dir):
        self.directory = Path(save_dir).resolve() / ".sessions"
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, session):
        path = self.directory / f"{session['tag']}.json"
        temporary = path.with_suffix(".tmp")
        snapshot = {**session, "version": 2,
                    "config": {section: dict(options) for section, options in session["config"].items()}}
        snapshot["config"]["General"]["savedir"] = "."
        if "PostProcess" in snapshot["config"]:
            snapshot["config"]["PostProcess"].pop("lockfile", None)
        snapshot["parts"] = [Path(part).resolve().relative_to(self.directory.parent).as_posix()
                             for part in session["parts"]]
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def load(self):
        # Fail closed on unreadable state so unfinished recordings cannot be silently discarded.
        sessions = []
        for path in sorted(self.directory.glob("*.json")):
            session = json.loads(path.read_text(encoding="utf-8"))
            if session['tag'] != path.stem or session['state'] not in {'recording', 'pending', 'complete'}:
                raise ValueError(f"场次记录无效，保留现场: {path}")
            if session.get("version") == 2:
                if any(Path(part).is_absolute() for part in session["parts"]):
                    raise ValueError(f"场次分段必须为相对路径: {path}")
                session["config"]["General"]["savedir"] = str(self.directory.parent)
                session["parts"] = [str(self.directory.parent / part) for part in session["parts"]]
            if Path(self.config(session)['General']['SaveDir']).resolve() != self.directory.parent:
                raise ValueError(f"场次目录与当前录制目录不一致: {path}")
            for part in session['parts']:
                Path(part).resolve().relative_to(self.directory.parent)
            sessions.append(session)
        return sessions

    def create(self, config, room_id, tag, title):
        session = {"tag": tag, "title": title, "room_id": room_id, "state": "recording",
                   "parts": [], "config": {section: dict(config[section]) for section in config.sections()}}
        if (self.directory / f"{tag}.json").exists():
            raise ValueError(f"场次名称冲突，保留已有文件: {tag}")
        self.save(session)
        return session

    def finish(self, session):
        session["parts"] = [p for p in session["parts"] if Path(p).is_file() and Path(p).stat().st_size]
        session["state"] = "pending"
        self.save(session)

    def remove(self, session):
        (self.directory / f"{session['tag']}.json").unlink()

    @staticmethod
    def config(session):
        config = configparser.ConfigParser(interpolation=None)
        config.read_dict(session["config"])
        return config
