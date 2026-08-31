"""腾讯云 COS 存储服务 —— 上传/下载/签名URL"""

import os
import time
from qcloud_cos import CosConfig, CosS3Client

# ── COS 配置 ──
# 存储桶: zhuanjia-1307167583
# 优先从环境变量读取 secret，未配置时降级为本地存储
BUCKET = os.getenv("COS_BUCKET", "zhuanjia-1307167583")
REGION = os.getenv("COS_REGION", "ap-guangzhou")
SECRET_ID = os.getenv("COS_SECRET_ID", "")
SECRET_KEY = os.getenv("COS_SECRET_KEY", "")
COS_ENABLED = bool(SECRET_ID and SECRET_KEY)

_client = None


def _get_client() -> CosS3Client:
    global _client
    if _client is None and COS_ENABLED:
        config = CosConfig(
            Region=REGION,
            SecretId=SECRET_ID,
            SecretKey=SECRET_KEY,
            Scheme='https',
        )
        _client = CosS3Client(config)
    return _client


def is_enabled() -> bool:
    return COS_ENABLED


def upload(file_path: str, cos_key: str, content_type: str = "application/octet-stream") -> str:
    """上传文件到 COS，返回 cos_key（不是 URL）。
    大文件自动走分片上传（SDK 内部处理）。
    调用方应把 cos_key 存入数据库，需要访问时调 presigned_url() 实时生成。
    """
    if not COS_ENABLED:
        return None
    client = _get_client()
    if client is None:
        return None
    client.upload_file(
        Bucket=BUCKET,
        Key=cos_key,
        LocalFilePath=file_path,
        ContentType=content_type,
        EnableMD5=False,
    )
    return cos_key


def upload_bytes(data: bytes, cos_key: str, content_type: str = "application/octet-stream") -> str:
    """上传字节数据到 COS，返回 cos_key。"""
    if not COS_ENABLED:
        return None
    client = _get_client()
    if client is None:
        return None
    client.put_object(
        Bucket=BUCKET,
        Key=cos_key,
        Body=data,
        ContentType=content_type,
    )
    return cos_key


def download_bytes(cos_key: str) -> bytes:
    """从 COS 下载文件到内存。"""
    if not COS_ENABLED:
        return None
    client = _get_client()
    if client is None:
        return None
    resp = client.get_object(Bucket=BUCKET, Key=cos_key)
    return resp["Body"].get_raw_stream().read()


def presigned_url(cos_key: str, expires: int = 3600) -> str:
    """生成预签名下载 URL（有效期默认 1 小时）。"""
    if not COS_ENABLED:
        return None
    client = _get_client()
    if client is None:
        return None
    return client.get_presigned_download_url(
        Bucket=BUCKET,
        Key=cos_key,
        Expired=expires,
    )
