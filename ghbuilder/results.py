"""Build-result cache in S3: skip GitHub entirely when a bundle has already been built.

A result is the (decrypted) tarball of ``idedata/ storage/ build/`` produced by the workflow. It is
keyed by a content hash of the bundle + esphome version, so any change to a YAML, ``secrets.yaml``, an
included file or the esphome version produces a miss and a normal GitHub build.

Environment (feature is off unless endpoint, bucket and both keys are set):
    GHB_S3_ENDPOINT    e.g. https://s3.example.com
    GHB_S3_BUCKET      bucket name (must already exist)
    GHB_S3_ACCESS_KEY / GHB_S3_SECRET_KEY
    GHB_S3_REGION      default "us-east-1"
    GHB_S3_PREFIX      key prefix, default "ghbuilder"
    GHB_S3_KEEP        results kept per project, default 5

Results contain the firmware (and with it WiFi/API secrets), so keep the bucket private.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

SCHEMA = "v1"
EXCLUDE_TOP = {".esphome", "__pycache__", ".git"}


def content_hash(root: Path, configuration: str, esphome_version: str) -> str:
    """Hash every file under *root* (names + bytes, not mtimes) with the config name and esphome version."""
    h = hashlib.sha256()
    h.update(f"{SCHEMA}\0{configuration}\0{esphome_version}\0".encode())
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if rel.parts[0] in EXCLUDE_TOP:
            continue
        h.update(rel.as_posix().encode() + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


class ResultStore:
    """Results in an S3 bucket: ``<prefix>/<project>/<hash>.tar.gz``."""

    def __init__(self, client, bucket: str, prefix: str, keep: int) -> None:
        self.c, self.bucket, self.prefix, self.keep = client, bucket, prefix.strip("/"), keep

    @classmethod
    def from_env(cls) -> ResultStore | None:
        env = os.environ
        if not all(env.get(k) for k in ("GHB_S3_ENDPOINT", "GHB_S3_BUCKET", "GHB_S3_ACCESS_KEY", "GHB_S3_SECRET_KEY")):
            return None
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=env["GHB_S3_ENDPOINT"],
            aws_access_key_id=env["GHB_S3_ACCESS_KEY"],
            aws_secret_access_key=env["GHB_S3_SECRET_KEY"],
            region_name=env.get("GHB_S3_REGION", "us-east-1"),
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}, connect_timeout=10),
        )
        return cls(client, env["GHB_S3_BUCKET"], env.get("GHB_S3_PREFIX", "ghbuilder"), int(env.get("GHB_S3_KEEP", "5")))

    def _key(self, project: str, digest: str) -> str:
        return f"{self.prefix}/{project}/{digest}.tar.gz"

    def get(self, project: str, digest: str) -> bytes | None:
        try:
            return self.c.get_object(Bucket=self.bucket, Key=self._key(project, digest))["Body"].read()
        except self.c.exceptions.NoSuchKey:
            return None

    def put(self, project: str, digest: str, data: bytes) -> None:
        self.c.put_object(Bucket=self.bucket, Key=self._key(project, digest), Body=data)
        self._prune(project)

    def _list(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(page.get("Contents", []))
        return out

    def _prune(self, project: str) -> None:
        objs = sorted(self._list(f"{self.prefix}/{project}/"), key=lambda o: o["LastModified"], reverse=True)
        for o in objs[self.keep :]:
            self.c.delete_object(Bucket=self.bucket, Key=o["Key"])

    def delete(self, project: str | None = None) -> int:
        """Delete one project's results, or everything under the prefix."""
        objs = self._list(f"{self.prefix}/{project}/" if project else f"{self.prefix}/")
        for o in objs:
            self.c.delete_object(Bucket=self.bucket, Key=o["Key"])
        return len(objs)
