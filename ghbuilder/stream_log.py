"""Runs inside the workflow: publish a growing log file through a GitHub check run.

usage: stream_log.py <logfile> <donefile> <redact.json>
Creates check run ``log-<job>`` on the job branch's commit and, every ~3s, PATCHes its output with a
rolling window of the (redacted) log. ``output.title`` carries ``end=<N>``: the total number of
characters emitted so far, so a reader polling the window can tell exactly which part is new.
GitHub's own job log only becomes readable once the job ends; this makes the output live without
creating commits.

Env: GITHUB_TOKEN (checks: write), GITHUB_REPOSITORY, JOB_ID
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

log, done = Path(sys.argv[1]), Path(sys.argv[2])
redact = sorted({v for v in json.loads(Path(sys.argv[3]).read_text()) if len(v) >= 4}, key=len, reverse=True)
WINDOW = 50_000  # chars kept in output.text (GitHub caps it at 65535)
API = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
NAME = f"log-{os.environ['JOB_ID']}"
S = requests.Session()
S.headers.update(
    {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
     "X-GitHub-Api-Version": "2022-11-28"}
)
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def call(method: str, url: str, **kw):
    for attempt in range(4):
        r = S.request(method, url, timeout=30, **kw)
        if r.status_code < 500 and r.status_code != 403:
            r.raise_for_status()
            return r.json()
        time.sleep(2 * (attempt + 1))
    r.raise_for_status()


check_id = call("POST", f"{API}/check-runs", json={"name": NAME, "head_sha": sha, "status": "in_progress"})["id"]


def scrub(text: str) -> str:
    for secret in redact:
        text = text.replace(secret, "***")
    return text


text_all = ""  # redacted text emitted so far
offset = 0  # bytes of the raw log consumed
pending = b""


def publish(final: bool) -> bool:
    """Move complete lines (everything when *final*) from the raw log into ``text_all``; PATCH if changed."""
    global text_all, offset, pending
    data = log.read_bytes() if log.exists() else b""
    if len(data) > offset:
        pending += data[offset:]
        offset = len(data)
    cut = len(pending) if final else max(pending.rfind(b"\n"), pending.rfind(b"\r")) + 1
    if cut <= 0:
        return False
    text_all += scrub(pending[:cut].decode(errors="replace"))
    pending = pending[cut:]
    body = {"output": {"title": f"end={len(text_all)}", "summary": "live log", "text": text_all[-WINDOW:]}}
    if final:
        body.update(status="completed", conclusion="neutral")
    call("PATCH", f"{API}/check-runs/{check_id}", json=body)
    return True


while True:
    finished = done.exists()
    publish(finished)
    if finished:
        break
    time.sleep(3)
# Nothing was ever logged: still close the check run so readers see it end.
if not text_all:
    call("PATCH", f"{API}/check-runs/{check_id}", json={"status": "completed", "conclusion": "neutral",
                                                         "output": {"title": "end=0", "summary": "live log"}})
