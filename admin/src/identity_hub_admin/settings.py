"""Typed runtime settings loaded from `IH_ADMIN_*` variables."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    TOKEN = "token"
    PASSWORD = "password"
    TRUSTED_PROXY = "trusted_proxy"


def packaged_static_dir() -> Path:
    return Path(__file__).with_name("static")


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IH_ADMIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    database_url: str | None = None
    hub_factory: str | None = None
    db_schema: str | None = None
    kek_keys: SecretStr | None = None
    kek_active_id: str | None = None
    auth_mode: AuthMode = AuthMode.TOKEN
    bearer_tokens: SecretStr | None = None
    password_hash: SecretStr | None = None
    password_username: str = "admin"
    password_role: Literal["viewer", "operator", "admin"] = "admin"
    password_allowed_spaces: tuple[str, ...] = ("*",)
    session_secret: SecretStr | None = None
    session_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    login_rate_limit: int = Field(default=5, ge=1, le=100)
    reveal_rate_limit: int = Field(default=5, ge=1, le=100)
    external_timeout_seconds: float = Field(default=15.0, ge=0.1, le=300.0)
    shutdown_timeout_seconds: float = Field(default=10.0, ge=0.1, le=120.0)
    trusted_proxy_cidrs: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = "INFO"
    base_path: str = ""
    static_dir: Path = Field(default_factory=packaged_static_dir)
    run_dispatcher: bool = True
    demo_mode: bool = False
    library_version_spec: str = ">=0.1.0.dev0,<2"

    @field_validator("base_path")
    @classmethod
    def validate_base_path(cls, value: str) -> str:
        if value in {"", "/"}:
            return ""
        if not value.startswith("/") or value.endswith("/") or ".." in value:
            raise ValueError("base_path must start with '/', have no trailing '/', and contain no '..'")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    def startup_errors(self) -> list[str]:
        errors: list[str] = []
        if self.environment == Environment.PRODUCTION:
            if self.demo_mode:
                errors.append("IH_ADMIN_DEMO_MODE is forbidden in production")
            if self.database_url is None:
                errors.append("IH_ADMIN_DATABASE_URL is required in production")
            if self.hub_factory is None:
                errors.append("IH_ADMIN_HUB_FACTORY is required in production")
            if self.kek_keys is None or self.kek_active_id is None:
                errors.append("IH_ADMIN_KEK_KEYS and IH_ADMIN_KEK_ACTIVE_ID are required in production")
            if self.auth_mode == AuthMode.TOKEN and self.bearer_tokens is None:
                errors.append("IH_ADMIN_BEARER_TOKENS is required for token auth in production")
            if self.auth_mode == AuthMode.PASSWORD and (
                self.password_hash is None or self.session_secret is None
            ):
                errors.append(
                    "IH_ADMIN_PASSWORD_HASH and IH_ADMIN_SESSION_SECRET are required "
                    "for password auth in production"
                )
            if self.auth_mode == AuthMode.TRUSTED_PROXY and not self.trusted_proxy_cidrs:
                errors.append("IH_ADMIN_TRUSTED_PROXY_CIDRS is required for trusted proxy auth in production")
            if not (self.static_dir / "index.html").is_file():
                errors.append(f"production SPA is missing: {self.static_dir / 'index.html'}")
        return errors
