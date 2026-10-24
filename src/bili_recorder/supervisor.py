"""One owned recording process per room; configuration is the control plane."""
import asyncio
import configparser
import json
import logging
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time

from .config import read_config, room_config, room_ids
from .locking import exclusive


def run_room(config_path, room_id):
    from . import recorder

    config = room_config(read_config(config_path), room_id)
    folder = Path(config["General"]["SaveDir"])
    state = Path(config["General"]["StateDir"]) / str(room_id)
    state.mkdir(parents=True, exist_ok=True)
    recorder.STATUS_FILE = str(state / "status.json")
    recorder.STOP_EVENT.clear()
    with exclusive(folder / ".recorder.lock"):
        async def run():
            task = asyncio.create_task(recorder.main(config_path, room_id))
            def stop():
                recorder.STOP_EVENT.set()
                task.cancel()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop)
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                recorder.STOP_EVENT.set()
                pending = asyncio.all_tasks() - {asyncio.current_task()}
                for child in pending:
                    child.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        try:
            asyncio.run(run())
        finally:
            try:
                status = json.loads(Path(recorder.STATUS_FILE).read_text())
                status.update(is_recording=False, stopped=True, pid=os.getpid())
                recorder.write_status_atomic(status)
            except (OSError, ValueError):
                pass


def supervise(config_path):
    config = read_config(config_path)
    state = Path(config["General"]["StateDir"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    workers = {}
    stopping = set()
    restart_at = {}
    context = multiprocessing.get_context("spawn")
    with exclusive(state / "supervisor.lock"):
        try:
            while not stop.is_set():
                try:
                    wanted = set(room_ids(read_config(config_path), enabled_only=True))
                except (OSError, ValueError, configparser.Error) as error:
                    logging.error("配置无法读取，保持现有房间: %s", error)
                    stop.wait(2)
                    continue
                for room, process in list(workers.items()):
                    if room not in wanted and process.is_alive() and room not in stopping:
                        process.terminate()
                        stopping.add(room)
                    if not process.is_alive():
                        process.join()
                        del workers[room]
                        stopping.discard(room)
                        restart_at[room] = time.monotonic() + 5
                for room in sorted(wanted - workers.keys()):
                    if time.monotonic() < restart_at.get(room, 0):
                        continue
                    process = context.Process(target=run_room, args=(config_path, room), name=f"room-{room}")
                    process.start()
                    workers[room] = process
                stop.wait(2)
        finally:
            for process in workers.values():
                if process.is_alive():
                    process.terminate()
            # Workers terminate their FFmpeg children and keep durable sessions resumable.
            for process in workers.values():
                process.join()
