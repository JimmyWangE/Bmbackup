from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有配置都来自环境变量（或 .env 文件），代码里不放任何密钥。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://bm:bm@localhost:5432/bm"
    # 签发令牌、签名验证码和本地文件下载地址用。生产环境必须换成随机长字符串。
    secret_key: str = Field(default="dev-only-change-me-not-for-production-use", min_length=32)
    # 对外的根地址，生成照片/语音的上传下载链接时用，例如 https://api.example.com
    public_base_url: str = "http://localhost:8000"
    # 生产环境拒绝非 HTTPS 请求（前面的反向代理会带上 X-Forwarded-Proto）。
    require_https: bool = False

    access_token_minutes: int = 60
    refresh_token_days: int = 90

    # 邮箱验证码
    email_backend: Literal["console", "smtp", "memory"] = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False  # 465 端口用 True；587 端口用 STARTTLS（False）
    mail_from: str = "BM <no-reply@example.com>"
    code_ttl_minutes: int = 10
    code_max_attempts: int = 5
    code_resend_seconds: int = 60
    code_hourly_limit: int = 10

    # 照片和语音
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./data/media"
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""  # Cloudflare R2、MinIO 等 S3 兼容服务填这里
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    media_url_seconds: int = 900
    max_media_bytes: int = 30 * 1024 * 1024

    # 同步
    max_changes_per_push: int = 500
    max_payload_bytes: int = 512 * 1024
    pull_page_size: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
