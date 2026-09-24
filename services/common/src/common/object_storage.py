"""可选 S3/MinIO 产物镜像后端。

本模块只负责把已写入本地产物卷的文件镜像到对象存储；本地文件仍是当前
ArtifactStore 的主读源，镜像失败会明确阻止当前产物判定为完成。未配置
`ARTIFACT_STORAGE_BACKEND=s3` 时不导入 boto3，保持本地开发零额外依赖。
"""

from __future__ import annotations

import mimetypes
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol


class ObjectStorageError(RuntimeError):
    """对象存储配置、上传或删除失败。"""


class ObjectStorage(Protocol):
    """ArtifactStore 所需的最小对象存储协议。"""

    def put_file(self, local_path: Path, object_key: str) -> str:
        """上传文件并返回不含凭据的 storage URI。"""

    def uri_for(self, object_key: str) -> str:
        """返回对象键对应的 URI，不发起网络请求。"""

    def exists(self, object_key: str) -> bool:
        """确认对象存在；网络/权限错误按 False 返回给完成判据。"""

    def get_file(self, object_key: str, destination: Path) -> None:
        """下载对象到本地临时文件；失败时抛出 ObjectStorageError。"""

    def healthcheck(self) -> bool:
        """执行只读探活；依赖不可用时抛出 ObjectStorageError。"""


class S3MirrorStorage:
    """S3 兼容对象存储镜像（AWS S3、MinIO、阿里云 OSS S3 兼容端点）。"""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        prefix: str = "llm-autotest",
        verify_tls: bool = True,
    ) -> None:
        if not bucket.strip():
            raise ObjectStorageError("对象存储 bucket 不能为空")
        try:
            boto3 = import_module("boto3")
            config_module = import_module("botocore.config")
            config = config_module.Config(s3={"addressing_style": "path"})
            self._client: Any = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                verify=verify_tls,
                config=config,
            )
        except Exception as exc:
            raise ObjectStorageError(
                "S3/MinIO 后端不可用，请安装 boto3 并检查对象存储配置"
            ) from exc
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def put_file(self, local_path: Path, object_key: str) -> str:
        """上传文件；异常不回显 access key/secret。"""
        if not local_path.is_file():
            raise ObjectStorageError(f"待上传文件不存在：{local_path.name}")
        key = self._key(object_key)
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        try:
            self._client.upload_file(
                str(local_path),
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except Exception as exc:
            raise ObjectStorageError(f"对象存储上传失败：{local_path.name}") from exc
        return self.uri_for(object_key)

    def uri_for(self, object_key: str) -> str:
        """返回不含访问密钥的稳定对象 URI。"""
        return f"s3://{self._bucket}/{self._key(object_key)}"

    def exists(self, object_key: str) -> bool:
        """用 HEAD 校验镜像对象存在，不把云端异常扩散到用户响应。"""
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(object_key))
        except Exception:
            return False
        return True

    def get_file(self, object_key: str, destination: Path) -> None:
        """下载对象到本地，供本地产物卷丢失后的断点恢复使用。"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self._bucket, self._key(object_key), str(destination))
        except Exception as exc:
            raise ObjectStorageError("对象存储下载失败") from exc

    def healthcheck(self) -> bool:
        """通过 HEAD bucket 验证 endpoint、凭据和权限。"""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            raise ObjectStorageError("对象存储探活失败") from exc
        return True

    def _key(self, object_key: str) -> str:
        normalized = object_key.strip("/")
        return f"{self._prefix}/{normalized}" if self._prefix else normalized


def build_object_storage(settings: Any) -> ObjectStorage | None:
    """按 Settings 构造可选镜像后端；local/空配置均返回 None。"""
    backend = str(getattr(settings, "artifact_storage_backend", "local") or "local").lower()
    if backend in {"", "local", "filesystem"}:
        return None
    if backend != "s3":
        raise ObjectStorageError(f"不支持的 ARTIFACT_STORAGE_BACKEND：{backend}")
    return S3MirrorStorage(
        bucket=str(getattr(settings, "artifact_storage_bucket", "") or ""),
        endpoint_url=getattr(settings, "artifact_storage_endpoint", None),
        region=str(getattr(settings, "artifact_storage_region", "us-east-1")),
        access_key=getattr(settings, "artifact_storage_access_key", None),
        secret_key=getattr(settings, "artifact_storage_secret_key", None),
        prefix=str(getattr(settings, "artifact_storage_prefix", "llm-autotest")),
        verify_tls=bool(getattr(settings, "artifact_storage_verify_tls", True)),
    )
