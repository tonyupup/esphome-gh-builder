"""A fake ``esphome`` CLI that compiles on GitHub Actions.

Invoked by the device-builder receiver as::

    python -m ghbuilder.shim --esphome-version V --dashboard [cache args] compile <config.yaml>

It mirrors what the real ``esphome compile`` does from the receiver's point of view:
build output on stdout, exit code 0/1, and the build artefacts written under
``$ESPHOME_DATA_DIR`` (``idedata/``, ``storage/``, ``build/``).

Environment:
    GHB_REPO          owner/name of the build repository
    GHB_PUBKEY        age recipient of the build repository (encrypts the bundle)
    GH_TOKEN          token with contents:write + actions:write on GHB_REPO
    ESPHOME_DATA_DIR  where the receiver expects artefacts (set by the receiver)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import pyrage

RUNNER_DATA_DIR = "/home/runner/esphome-data"
POLL_SECONDS = 4
WORKFLOW = "build.yml"
EXCLUDE_TOP = {".esphome", "__pycache__", ".git"}


def log(msg: str) -> None:
    sys.stdout.write(msg if msg.endswith("\n") else msg + "\n")
    sys.stdout.flush()


def gh(*args: str, check: bool = True, input_: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["gh", *args], capture_output=True, input=input_, check=False)
    if check and proc.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def gh_json(*args: str):
    return json.loads(gh(*args).stdout or b"null")


def make_bundle(config_path: Path) -> bytes:
    """Tar the receiver's extracted config dir and tell the workflow which YAML to build."""
    root = config_path.parent
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in sorted(root.iterdir()):
            if entry.name in EXCLUDE_TOP:
                continue
            tar.add(entry, arcname=entry.name)
        meta = json.dumps({"configuration": config_path.name}).encode()
        info = tarfile.TarInfo(".ghbuild.json")
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
    return buf.getvalue()


def push_bundle(repo: str, job: str, encrypted: bytes) -> None:
    default = gh_json("api", f"repos/{repo}", "-q", ".")["default_branch"]
    sha = gh("api", f"repos/{repo}/git/ref/heads/{default}", "-q", ".object.sha").stdout.decode().strip()
    gh("api", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/job/{job}", "-f", f"sha={sha}")
    gh(
        "api", "-X", "PUT", f"repos/{repo}/contents/bundle.tar.gz.age",
        "-f", f"message=job {job}",
        "-f", f"branch=job/{job}",
        "-f", f"content={base64.b64encode(encrypted).decode()}",
    )


def dispatch(repo: str, job: str, version: str, reply_pubkey: str) -> int:
    started = time.time()
    gh(
        "workflow", "run", WORKFLOW, "-R", repo, "--ref", "main",
        "-f", f"job={job}", "-f", f"esphome_version={version}", "-f", f"reply_pubkey={reply_pubkey}",
    )
    deadline = started + 120
    while time.time() < deadline:
        runs = gh_json(
            "run", "list", "-R", repo, "--workflow", WORKFLOW, "--event", "workflow_dispatch",
            "--json", "databaseId,displayTitle", "-L", "30",
        )
        for run in runs or []:
            if run["displayTitle"] == f"build {job}":
                return int(run["databaseId"])
        time.sleep(2)
    raise RuntimeError("workflow run did not appear within 120s")


def stream_logs(repo: str, run_id: int, state: dict) -> str:
    """Follow the run, printing new log lines; return the final conclusion."""
    printed = 0
    final = False
    while True:
        run = gh_json("api", f"repos/{repo}/actions/runs/{run_id}", "-q", ".")
        jobs = gh_json("api", f"repos/{repo}/actions/runs/{run_id}/jobs", "-q", ".jobs") or []
        if jobs:
            job_id = jobs[0]["id"]
            state["job_id"] = job_id
            proc = gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs", check=False)
            if proc.returncode == 0:
                lines = proc.stdout.decode(errors="replace").splitlines()
                for line in lines[printed:]:
                    log(re.sub(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", line))
                printed = len(lines)
        if run["status"] == "completed":
            if final:
                return run["conclusion"] or "failure"
            final = True  # one more pass so the tail of the log is flushed
            continue
        time.sleep(POLL_SECONDS)


def fetch_result(repo: str, run_id: int, job: str, identity: str, data_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gh("run", "download", str(run_id), "-R", repo, "-n", f"result-{job}", "-D", tmp)
        blob = (Path(tmp) / "result.tar.gz.age").read_bytes()
    plain = pyrage.decrypt(blob, [pyrage.x25519.Identity.from_str(identity)])
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(data_dir, filter="data")
    # idedata/storage embed absolute runner paths; point them at the local data dir.
    for pattern in ("idedata/*.json", "storage/*.json"):
        for p in data_dir.glob(pattern):
            p.write_text(p.read_text().replace(RUNNER_DATA_DIR, str(data_dir)))


def cleanup(repo: str, job: str, run_id: int | None) -> None:
    gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/job/{job}", check=False)
    if run_id and not os.environ.get("GHB_KEEP_RUN"):
        gh("api", "-X", "DELETE", f"repos/{repo}/actions/runs/{run_id}", check=False)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="esphome")
    ap.add_argument("--esphome-version", required=True)
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--mdns-address-cache", action="append")
    ap.add_argument("--dns-address-cache", action="append")
    ap.add_argument("command")
    ap.add_argument("configuration")
    args, _ = ap.parse_known_args(argv)
    repo = os.environ["GHB_REPO"]
    if args.command == "clean":
        log("INFO ghbuilder: nothing to clean locally; builds run on GitHub Actions")
        return 0
    if args.command == "clean-all":
        proc = gh("cache", "delete", "--all", "-R", repo, check=False)
        out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
        log(f"INFO ghbuilder: cleared GitHub Actions caches of {repo}" + (f" ({out})" if out else ""))
        return proc.returncode
    if args.command != "compile":
        log(f"ghbuilder: unsupported command {args.command!r}; only compile/clean/clean-all are offloaded")
        return 2
    pubkey = os.environ["GHB_PUBKEY"]
    data_dir = Path(os.environ["ESPHOME_DATA_DIR"])
    config_path = Path(args.configuration).resolve()
    job = uuid.uuid4().hex[:12]
    reply = pyrage.x25519.Identity.generate()
    state: dict = {}
    run_id: int | None = None

    def on_term(*_):
        if run_id:
            gh("run", "cancel", str(run_id), "-R", repo, check=False)
        cleanup(repo, job, None)
        os._exit(143)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        log(f"INFO ghbuilder: bundling {config_path.name} for esphome {args.esphome_version}")
        bundle = make_bundle(config_path)
        encrypted = pyrage.encrypt(bundle, [pyrage.x25519.Recipient.from_str(pubkey)])
        log(f"INFO ghbuilder: uploading encrypted bundle ({len(encrypted) // 1024} KiB) to {repo}")
        push_bundle(repo, job, encrypted)
        run_id = dispatch(repo, job, args.esphome_version, str(reply.to_public()))
        log(f"INFO ghbuilder: GitHub run {run_id} started")
        conclusion = stream_logs(repo, run_id, state)
        if conclusion != "success":
            log(f"ERROR ghbuilder: GitHub run finished with {conclusion}")
            return 1
        log("INFO ghbuilder: downloading build artefacts")
        fetch_result(repo, run_id, job, str(reply), data_dir)
        log("INFO ghbuilder: done")
        return 0
    except Exception as exc:  # noqa: BLE001 — surface any failure as a failed build
        log(f"ERROR ghbuilder: {exc}")
        return 1
    finally:
        cleanup(repo, job, run_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
