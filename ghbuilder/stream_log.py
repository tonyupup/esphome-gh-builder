"""Runs inside the workflow: ship a growing log file as redacted chunks on the job branch.

usage: stream_log.py <logfile> <donefile> <redact.json>
Every ~3s any new bytes are redacted (known secret values -> ***), committed as logs/NNNNNN.txt and
pushed to $JOB_BRANCH. GitHub's own job log is live-less until the job ends; this makes it live.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

log, done = Path(sys.argv[1]), Path(sys.argv[2])
redact = sorted({v for v in json.loads(Path(sys.argv[3]).read_text()) if len(v) >= 4}, key=len, reverse=True)
CHUNK = 48 * 1024
Path("logs").mkdir(exist_ok=True)
offset = seq = 0
pending = b""


def git(*a):
    subprocess.run(["git", *a], check=True, capture_output=True)


git("config", "user.name", "ghbuilder")
git("config", "user.email", "ghbuilder@users.noreply.github.com")


def scrub(text: str) -> str:
    for secret in redact:
        text = text.replace(secret, "***")
    return text


def flush(final: bool) -> bool:
    """Send complete lines only (so a secret is never split across chunks); all of it when *final*."""
    global offset, seq, pending
    data = log.read_bytes() if log.exists() else b""
    if len(data) > offset:
        pending += data[offset:]
        offset = len(data)
    if not pending:
        return False
    cut = len(pending) if final else max(pending.rfind(b"\n"), pending.rfind(b"\r")) + 1
    if cut <= 0:
        return False
    chunk, pending = pending[:CHUNK] if cut > CHUNK else pending[:cut], pending[min(cut, CHUNK) :]
    seq += 1
    Path(f"logs/{seq:06d}.txt").write_text(scrub(chunk.decode(errors="replace")))
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
    while flush(finished):
        pass
    if finished:
        break
    time.sleep(3)
