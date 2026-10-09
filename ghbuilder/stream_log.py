"""Runs inside the workflow: ship a growing log file as age-encrypted chunks on the job branch.

usage: stream_log.py <logfile> <donefile> <age-recipient>
Every ~3s any new bytes are encrypted to the recipient, committed as logs/NNNNNN.age and pushed.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pyrage

log, done, recipient = Path(sys.argv[1]), Path(sys.argv[2]), pyrage.x25519.Recipient.from_str(sys.argv[3])
CHUNK = 48 * 1024
Path("logs").mkdir(exist_ok=True)
offset = seq = 0


def git(*a):
    subprocess.run(["git", *a], check=True, capture_output=True)


git("config", "user.name", "ghbuilder")
git("config", "user.email", "ghbuilder@users.noreply.github.com")


def flush() -> bool:
    global offset, seq
    data = log.read_bytes() if log.exists() else b""
    if len(data) <= offset:
        return False
    chunk = data[offset : offset + CHUNK]
    offset += len(chunk)
    seq += 1
    Path(f"logs/{seq:06d}.age").write_bytes(pyrage.encrypt(chunk, [recipient]))
    git("add", "logs")
    git("commit", "-qm", f"log {seq}")
    for attempt in range(3):
        try:
            git("push", "-q", "origin", f"HEAD:refs/heads/{os.environ['JOB_BRANCH']}")
            break
        except subprocess.CalledProcessError:
            time.sleep(1 + attempt)
    return True


while True:
    finished = done.exists()
    while flush():
        pass
    if finished:
        break
    time.sleep(3)
