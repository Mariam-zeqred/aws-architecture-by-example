"""One tiny abstraction over "a prefix in S3" and "a directory on disk".

The pipeline is the same code in the Fargate task and on a laptop; only the
root changes: ``s3://bucket/prefix`` or ``./some/dir``. boto3 is imported
lazily so the local run and the tests never need it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


class Store:
    """``get``/``put``/``list``/``exists`` on keys relative to a root."""

    def __init__(self, root: str, client=None) -> None:
        self.root = root.rstrip("/")
        self._client = client
        if root.startswith("s3://"):
            bucket, _, prefix = root[5:].partition("/")
            self.bucket, self.prefix = bucket, prefix.strip("/")
        else:
            self.bucket, self.prefix = None, ""

    # --- helpers -----------------------------------------------------------
    @property
    def is_s3(self) -> bool:
        return self.bucket is not None

    @property
    def client(self):
        if self._client is None:
            import boto3  # the only place the pipeline touches the AWS SDK

            self._client = boto3.client("s3")
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _path(self, key: str) -> Path:
        return Path(self.root) / key

    # --- the four operations -----------------------------------------------
    def get(self, key: str) -> bytes:
        if self.is_s3:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes) -> None:
        if self.is_s3:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        if self.is_s3:
            try:
                self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            except self.client.exceptions.ClientError:
                return False
            return True
        return self._path(key).exists()

    def list(self, prefix: str) -> Iterator[str]:
        """Keys under ``prefix`` (relative to the root), sorted."""
        if self.is_s3:
            paginator = self.client.get_paginator("list_objects_v2")
            full = self._key(prefix)
            keys = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"][len(self.prefix) + 1 :] if self.prefix else obj["Key"])
            yield from sorted(keys)
            return
        base = self._path(prefix)
        if not base.exists():
            return
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            yield str(path.relative_to(self.root))
