import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    postgres_user: str = "capturio"
    postgres_password: str = "changeme"
    postgres_db: str = "capturio"
    postgres_host: str = "db"
    postgres_port: int = 5432

    secret_key: str = "changeme"
    algorithm: str = "HS256"
    access_token_expire_days: int = 7

    admin_username: str = "admin"
    admin_password_hash: str = ""

    photos_dir: str = "/photos"        # fixed container path, do not override
    cache_dir: str = "/app/cache"      # fixed container path, do not override

    trusted_proxies: str = ""
    # Comma-separated IPs/CIDRs of trusted reverse proxies (e.g. "127.0.0.1,172.16.0.0/12")
    # Empty = no proxy headers trusted (direct connection mode)

    admin_ip_allowlist: str = ""
    # Comma-separated IPs/CIDRs allowed to access /admin and /auth/login
    # Empty = all IPs allowed (default, backwards-compatible)

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def thumbs_dir(self) -> str:
        return os.path.join(self.cache_dir, "thumbs")

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
