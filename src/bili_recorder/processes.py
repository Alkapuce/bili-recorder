"""Cancellable child processes shared by capture and media processing."""
import signal
import subprocess
import threading

STOP_EVENT = threading.Event()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_command(cmd):
    with subprocess.Popen(cmd) as process:
        while process.poll() is None:
            if STOP_EVENT.wait(0.2):
                stop_process(process)
                return -signal.SIGTERM
        return process.returncode
