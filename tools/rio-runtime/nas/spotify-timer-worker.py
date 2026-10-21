#!/usr/bin/env python3
"""Single local pause worker; survives the terminal session, never retries writes."""
import fcntl
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.spotify_timer import TimerStore


def main():
    store = TimerStore()
    with store.database():
        pass
    fd = os.open(store.directory/'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover()
        while True:
            store.run_due()
            time.sleep(1)
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
