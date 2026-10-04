"""Layered settings: defaults < config/tuningpad.yaml < TUNINGPAD_* env < init kwargs."""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "tuningpad.yaml"


class _YamlSource(PydanticBaseSettingsSource):
    """Reads the operator's yaml file (missing file = no overrides)."""

    def __init__(self, settings_cls: type[BaseSettings], path: Path):
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text()) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{path} must contain a mapping")
            self._data = loaded

    def get_field_value(self, field, field_name):  # pragma: no cover - required by ABC
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TUNINGPAD_", extra="ignore")

    # --- runtime ---
    run_mode: str = "dev"  # dev | prod
    host: str = "127.0.0.1"
    port: int = 8100
    data_dir: Path = REPO_ROOT / "data"
    database_url: str = ""  # default: sqlite under data_dir
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5180"])

    # --- auth (single operator) ---
    password: str = ""  # empty = login disabled, loopback-only access
    session_secret: str = ""  # empty = random per process (sessions reset on restart)
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    # --- AWS ---
    default_region: str = "us-east-1"
    resource_tag: str = "tuningpad"  # value of the Project tag on everything we create
    # AgentCore Runtime platform version for new deploys: auto (V2 where offered) | V1 | V2
    agent_platform_version: str = "auto"

    # --- toolkit ---
    toolkit_path: Path = REPO_ROOT.parent / "agentcore-rl-toolkit"

    # --- job engine ---
    max_concurrent_jobs: int = 12  # run monitors are long-lived
    reconcile_interval_s: int = 20

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        path = Path(os.environ.get("TUNINGPAD_CONFIG_FILE", DEFAULT_CONFIG_FILE))
        return (init_settings, env_settings, _YamlSource(settings_cls, path))

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'tuningpad.db'}"

    @property
    def secret(self) -> str:
        return self.session_secret or _process_secret()

    @property
    def auth_required(self) -> bool:
        return bool(self.password)


@lru_cache(maxsize=1)
def _process_secret() -> str:
    return secrets.token_urlsafe(32)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
