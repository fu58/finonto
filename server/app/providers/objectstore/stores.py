"""对象存储适配：本地文件系统（默认）/ 阿里云 OSS / S3 兼容。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..base import ProviderError

_SAFE = re.compile(r"[^A-Za-z0-9._/\u4e00-\u9fa5-]")


def normalize_key(key: str) -> str:
    """拒绝路径穿越，保留可读的中文键名。"""
    cleaned = _SAFE.sub("_", key.strip().lstrip("/"))
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("非法的对象键名")
    return "/".join(parts)


class LocalFSObjectStore:
    name = "local_fs"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / normalize_key(key)).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("对象键名越界")
        return path

    def put(self, key: str, data: bytes, content_type: str = "") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if content_type:
            (path.parent / (path.name + ".meta")).write_text(content_type, encoding="utf-8")
        return f"localfs://{normalize_key(key)}"

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            path.unlink()
            meta = path.parent / (path.name + ".meta")
            if meta.exists():
                meta.unlink()
            return True
        return False

    def path_for(self, key: str) -> Path | None:
        return self._path(key)

    def list(self, prefix: str = "") -> list[str]:
        base = self.root
        out: list[str] = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith(".meta"):
                rel = p.relative_to(base).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return out

    @staticmethod
    def key_for_bytes(data: bytes, ext: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()[:24]
        return f"blobs/{digest}{ext}"


class OSSObjectStore:
    """阿里云 OSS / S3 兼容（需 oss2 或 boto3）。"""

    name = "oss"

    def __init__(self, *, bucket: str, endpoint: str, access_key_id: str, access_key_secret: str) -> None:
        try:
            import oss2  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "未安装 oss2，请 pip install oss2 并配置 FINONTO_OBJECT_STORE=oss 及密钥"
            ) from exc
        auth = oss2.Auth(access_key_id, access_key_secret)
        self._bucket = oss2.Bucket(auth, endpoint, bucket)

    def put(self, key: str, data: bytes, content_type: str = "") -> str:
        self._bucket.put_object(normalize_key(key), data, headers={"Content-Type": content_type} if content_type else None)
        return f"oss://{key}"

    def get(self, key: str) -> bytes:  # pragma: no cover
        return self._bucket.get_object(normalize_key(key)).read()

    def exists(self, key: str) -> bool:  # pragma: no cover
        return bool(self._bucket.object_exists(normalize_key(key)))

    def delete(self, key: str) -> bool:  # pragma: no cover
        self._bucket.delete_object(normalize_key(key))
        return True

    def path_for(self, key: str) -> Path | None:
        return None
