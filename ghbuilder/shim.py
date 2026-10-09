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
import sys
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

import pyrage
import requests

API = "https://api.github.com"
RUNNER_DATA_DIR = "/home/runner/esphome-data"
POLL_SECONDS = 3
WORKFLOW = "build.yml"
EXCLUDE_TOP = {".esphome", "__pycache__", ".git"}


def log(msg: str) -> None:
    sys.stdout.write(msg if msg.endswith("\n") else msg + "\n")
    sys.stdout.flush()


class GitHub:
    """Minimal GitHub REST client for one repository."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 202, 204), **kw) -> requests.Response:
        url = f"{API}/repos/{self.repo}" + (f"/{path}" if path else "")
        resp = self.s.request(method, url, timeout=60, **kw)
        if resp.status_code not in ok:
            raise RuntimeError(f"GitHub {method} {path.split('?')[0]} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw).json()

    # -- bundle upload -------------------------------------------------------------------
    def push_bundle(self, job: str, encrypted: bytes) -> str:
        """Upload the encrypted bundle to a new branch; return the commit sha (anchor of the log check run)."""
        default = self.get("")["default_branch"]
        sha = self.get(f"git/ref/heads/{default}")["object"]["sha"]
        self.call("POST", "git/refs", json={"ref": f"refs/heads/job/{job}", "sha": sha})
        resp = self.call(
            "PUT", "contents/bundle.tar.gz.age",
            json={"message": f"job {job}", "branch": f"job/{job}", "content": base64.b64encode(encrypted).decode()},
        )
        return resp.json()["commit"]["sha"]

    # -- run lifecycle -------------------------------------------------------------------
    def dispatch(self, job: str, version: str, reply_pubkey: str, project: str) -> int:
        self.call(
            "POST", f"actions/workflows/{WORKFLOW}/dispatches",
            json={"ref": "main", "inputs": {"job": job, "esphome_version": version,
                                            "reply_pubkey": reply_pubkey, "project": project}},
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            runs = self.get(f"actions/workflows/{WORKFLOW}/runs", params={"event": "workflow_dispatch", "per_page": 30})
            for run in runs["workflow_runs"]:
                if run["display_title"] == f"build {job}":
                    return int(run["id"])
            time.sleep(2)
        raise RuntimeError("workflow run did not appear within 120s")

    def cancel(self, run_id: int) -> None:
        self.call("POST", f"actions/runs/{run_id}/cancel", ok=(202, 409, 404))

    def cleanup(self, job: str, run_id: int | None, succeeded: bool = False) -> None:
        """Delete the job branch; delete the run too unless GHB_KEEP_RUNS says to keep it.

        GHB_KEEP_RUNS: ``failed`` (default; keep failed/cancelled runs for debugging), ``all`` or ``none``.
        """
        keep = os.environ.get("GHB_KEEP_RUNS", "failed")
        try:
            self.call("DELETE", f"git/refs/heads/job/{job}", ok=(204, 404, 422))
            if run_id and keep != "all" and (keep == "none" or succeeded):
                self.call("DELETE", f"actions/runs/{run_id}", ok=(204, 404, 409))
        except Exception:  # noqa: BLE001 — best effort
            pass

    # -- logs ----------------------------------------------------------------------------
    def read_log_window(self, sha: str, job: str) -> tuple[int, str] | None:
        """Return ``(end, text)`` of the live-log check run, or None if it does not exist yet.

        ``text`` is the tail of the redacted log and ``end`` the total characters emitted, so the
        window covers ``[end - len(text), end)``.
        """
        runs = self.get(f"commits/{sha}/check-runs", params={"check_name": f"log-{job}"})["check_runs"]
        if not runs:
            return None
        out = runs[0].get("output") or {}
        title = out.get("title") or ""
        if not title.startswith("end="):
            return None
        return int(title[4:]), out.get("text") or ""

    # -- artefacts -----------------------------------------------------------------------
    def download_result(self, run_id: int, job: str) -> bytes:
        arts = self.get(f"actions/runs/{run_id}/artifacts")["artifacts"]
        art = next((a for a in arts if a["name"] == f"result-{job}"), None)
        if art is None:
            raise RuntimeError("result artifact not found")
        resp = self.call("GET", f"actions/artifacts/{art['id']}/zip", allow_redirects=True)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            return zf.read("result.tar.gz.age")

    # -- caches --------------------------------------------------------------------------
    def delete_caches(self, key_prefix: str | None = None) -> int:
        deleted = 0
        while True:
            params = {"per_page": 100}
            if key_prefix:
                params["key"] = key_prefix
            caches = self.get("actions/caches", params=params)["actions_caches"]
            if not caches:
                return deleted
            for c in caches:
                self.call("DELETE", f"actions/caches/{c['id']}", ok=(200, 204, 404))
                deleted += 1


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


def project_key(config_path: str) -> str:
    """Cache-key-safe name of a config (its YAML stem)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", Path(config_path).stem)[:60] or "default"


def follow_run(gh: GitHub, run_id: int, job: str, sha: str) -> str:
    """Print step progress and live log text until the run ends; return its conclusion."""
    pos = 0  # characters of the log already printed
    started_steps: set[str] = set()
    final = False
    while True:
        run = gh.get(f"actions/runs/{run_id}")
        jobs = gh.get(f"actions/runs/{run_id}/jobs")["jobs"]
        for step in jobs[0]["steps"] if jobs else []:
            name = step["name"]
            if name in started_steps or name in ("Set up job", "Compile") or name.startswith(("Post ", "Complete")):
                continue
            if step["status"] in ("in_progress", "completed"):
                started_steps.add(name)
                log(f"INFO ghbuilder: {name}...")
        window = gh.read_log_window(sha, job)
        if window:
            end, text = window
            start = end - len(text)
            if pos < start:
                log(f"WARNING ghbuilder: skipped {start - pos} characters of log output")
                pos = start
            if end > pos:
                sys.stdout.write(text[pos - start :])
                sys.stdout.flush()
                pos = end
        if run["status"] == "completed":
            if final:
                return run["conclusion"] or "failure"
            final = True  # one more pass so the tail of the log is flushed
            continue
        time.sleep(POLL_SECONDS)


def unpack_result(blob: bytes, identity: pyrage.x25519.Identity, data_dir: Path) -> None:
    plain = pyrage.decrypt(blob, [identity])
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(data_dir, filter="data")
    # idedata/storage embed absolute runner paths; point them at the local data dir.
    for pattern in ("idedata/*.json", "storage/*.json"):
        for p in data_dir.glob(pattern):
            p.write_text(p.read_text().replace(RUNNER_DATA_DIR, str(data_dir)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="esphome")
    ap.add_argument("--esphome-version", required=True)
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--mdns-address-cache", action="append")
    ap.add_argument("--dns-address-cache", action="append")
    ap.add_argument("command")
    ap.add_argument("configuration")
    args, _ = ap.parse_known_args(argv)

    gh = GitHub(os.environ["GHB_REPO"], os.environ.get("GH_TOKEN") or os.environ["GITHUB_TOKEN"])
    project = project_key(args.configuration)

    if args.command == "clean":
        n = gh.delete_caches(f"build-{project}-")
        log(f"INFO ghbuilder: deleted {n} build cache(s) for {project}")
        return 0
    if args.command == "clean-all":
        n = gh.delete_caches()
        log(f"INFO ghbuilder: cleared {n} GitHub Actions cache(s) of {gh.repo}")
        return 0
    if args.command != "compile":
        log(f"ghbuilder: unsupported command {args.command!r}; only compile/clean/clean-all are offloaded")
        return 2

    pubkey = os.environ["GHB_PUBKEY"]
    data_dir = Path(os.environ["ESPHOME_DATA_DIR"])
    config_path = Path(args.configuration).resolve()
    job = uuid.uuid4().hex[:12]
    reply = pyrage.x25519.Identity.generate()
    run_id: int | None = None
    succeeded = False

    def on_term(*_):
        if run_id:
            gh.cancel(run_id)
        gh.cleanup(job, None)
        os._exit(143)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        bundle = make_bundle(config_path)
        encrypted = pyrage.encrypt(bundle, [pyrage.x25519.Recipient.from_str(pubkey)])
        sha = gh.push_bundle(job, encrypted)
        run_id = gh.dispatch(job, args.esphome_version, str(reply.to_public()), project)
        # The run id is the first thing the user sees, so a stuck build can be found on GitHub.
        log(f"INFO ghbuilder: GitHub run {run_id}: https://github.com/{gh.repo}/actions/runs/{run_id}")
        log(f"INFO ghbuilder: {config_path.name} with esphome {args.esphome_version} "
            f"(encrypted bundle {len(encrypted) // 1024} KiB)")
        conclusion = follow_run(gh, run_id, job, sha)
        if conclusion != "success":
            log(f"ERROR ghbuilder: GitHub run finished with {conclusion}")
            return 1
        log("INFO ghbuilder: downloading build artefacts")
        unpack_result(gh.download_result(run_id, job), reply, data_dir)
        succeeded = True
        log("INFO ghbuilder: done")
        return 0
    except Exception as exc:  # noqa: BLE001 — surface any failure as a failed build
        log(f"ERROR ghbuilder: {exc}")
        return 1
    finally:
        gh.cleanup(job, run_id, succeeded)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
