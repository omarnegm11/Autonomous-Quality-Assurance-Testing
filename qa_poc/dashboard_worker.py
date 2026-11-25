"""Isolated runner. Secrets arrive on stdin, never in process arguments or files."""
import json
import sys
import threading
import time
from .cli import run_one
from .config import Settings


def main():
    payload = json.loads(sys.stdin.readline())
    cfg = Settings(**payload["settings"])
    control = cfg.live_stream_dir
    finished = threading.Event()
    def heartbeat():
        while not finished.is_set():
            (control / "worker.json").write_text(json.dumps({"status": "RUNNING", "heartbeat": time.time()}), encoding="utf-8")
            finished.wait(1)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        run_one(cfg, payload["mode"])
    finally:
        finished.set()
        thread.join(timeout=2)
        (control / "worker.json").write_text(json.dumps({"status": "FINISHED", "heartbeat": time.time()}), encoding="utf-8")


if __name__ == "__main__":
    main()
