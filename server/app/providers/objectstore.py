"""对象存储组件：本地文件系统（默认）+ S3/OSS 兼容。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any


class LocalFSObjectStore:
    """``var/objects/<k1>/<k2>/<full-key>``，两级目录避免单目录文件过多。"""

    name = "local_fs"
    kind = "object_store"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def info(self) -> dict[str, Any]:
        total, size = 0, 0
        for file in self.root.rglob("*"):
            if file.is_file():
                total += 1
                size += file.stat().st_size
        return {
            "name": self.name,
            "provider": "local_fs",
            "root": str(self.root),
            "objects": total,
            "bytes": size,
            "needs_key": False,
        }

    def _path(self, key: str) -> Path:
        safe = key.replace("\\", "/").lstrip("/")
        parts = [p for p in safe.split("/") if p not in ("", ".", "..")]
        if not parts:
            raise ValueError("对象 key 不能为空")
        digest = hashlib.sha1("/".join(parts).encode("utf-8")).hexdigest()[:2]
        target = self.root / digest / "/".join(parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def put(self, key: str, data: bytes, content_type: str = "") -> str:
        path = self._path(key)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def url_for(self, key: str) -> str:
        return f"/api/objects/{key}"


class S3CompatibleObjectStore:
    """S3 协议（阿里云 OSS / MinIO / AWS S3）。需要 ``boto3``。"""

    name = "s3"
    kind = "object_store"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OSS/S3 对象存储需要 boto3：pip install boto3（本地实现 local_fs 无需任何依赖）"
            ) from exc
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region,
        )

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "s3", "bucket": self.bucket, "needs_key": True}

    def put(self, key: str, data: bytes, content_type: str = "") -> str:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return key

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def delete(self, key: str) -> bool:
        self._client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def url_for(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )


def copy_tree(src: Path, dst: Path) -> int:
    """导出备份用的小工具。"""
    count = 0
    for file in Path(src).rglob("*"):
        if file.is_file():
            target = Path(dst) / file.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            count += 1
    return count
