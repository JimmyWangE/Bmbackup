"""照片和语音的存储。App 拿到预签名地址后直接上传/下载，不占 API 服务的带宽。

- local：存在服务器磁盘上（docker 卷），由本服务的 /v1/media/blob 接口收发，单台服务器部署最省事。
- s3：AWS S3、Cloudflare R2、MinIO 等 S3 兼容服务。
"""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import quote

from app.config import get_settings
from app.security import sign_blob


class Storage:
    def upload_url(self, key: str, content_type: str) -> str: ...
    def download_url(self, key: str) -> str: ...
    async def size_of(self, key: str) -> int | None: ...
    async def read(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalStorage(Storage):
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("非法路径")
        return p

    def _url(self, key: str, method: str) -> str:
        s = get_settings()
        exp = int(time.time()) + s.media_url_seconds
        sig = sign_blob(key, method, exp)
        return f"{s.public_base_url.rstrip('/')}/v1/media/blob/{quote(key)}?exp={exp}&sig={sig}"

    def upload_url(self, key: str, content_type: str) -> str:
        return self._url(key, "PUT")

    def download_url(self, key: str) -> str:
        return self._url(key, "GET")

    async def size_of(self, key: str) -> int | None:
        p = self.path(key)
        return p.stat().st_size if p.exists() else None

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path(key).read_bytes)

    async def write(self, key: str, data: bytes) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        await asyncio.to_thread(tmp.write_bytes, data)
        os.replace(tmp, p)

    async def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


class S3Storage(Storage):
    def __init__(self) -> None:
        import boto3

        s = get_settings()
        self.bucket = s.s3_bucket
        self.client = boto3.client(
            "s3",
            region_name=s.s3_region or None,
            endpoint_url=s.s3_endpoint_url or None,
            aws_access_key_id=s.s3_access_key_id or None,
            aws_secret_access_key=s.s3_secret_access_key or None,
        )

    def upload_url(self, key: str, content_type: str) -> str:
        return self.client.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=get_settings().media_url_seconds)

    def download_url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=get_settings().media_url_seconds)

    async def size_of(self, key: str) -> int | None:
        def head() -> int | None:
            try:
                return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
            except self.client.exceptions.ClientError:
                return None
        return await asyncio.to_thread(head)

    async def read(self, key: str) -> bytes:
        def get() -> bytes:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return await asyncio.to_thread(get)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = S3Storage() if s.storage_backend == "s3" else LocalStorage(s.local_storage_dir)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
