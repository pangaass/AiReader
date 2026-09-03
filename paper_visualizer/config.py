"""Configuration with environment-only secret resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    artifacts_dir: Path
    cache_dir: Path
    output_dir: Path
    max_download_mb: int = 80
    request_timeout_seconds: int = 45
    max_attempts: int = 3
    api_key_env_names: tuple[str, ...] = field(default=("OPENAI_API_KEY", "AMINER_API_KEY"))

    @classmethod
    def from_project_root(cls, project_root: Path) -> "Settings":
        root = project_root.resolve()
        return cls(root, root / "artifacts", root / "cache", root / "output")

    def secret_available(self, env_name: str) -> bool:
        if env_name not in self.api_key_env_names:
            raise ValueError(f"Environment variable is not allowlisted: {env_name}")
        return bool(os.environ.get(env_name))


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: "***" if any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value

