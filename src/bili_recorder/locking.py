"""Advisory process ownership; all entry points use the same lock files."""
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"已有进程持有锁: {path}") from None
        yield lock
