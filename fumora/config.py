"""Process-level configuration from the environment.

Only things needed before the database is open live here. Everything a user
can change (API keys, paths to clients, schedules) lives in the `setting`
table and is edited on the Settings page.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    config_dir: Path
    port: int

    @property
    def db_path(self) -> Path:
        return self.config_dir / "fumora.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret.key"


def load_config() -> Config:
    config_dir = Path(os.environ.get("FUMORA_CONFIG_DIR", "/config"))
    port = int(os.environ.get("FUMORA_PORT", "8792"))
    return Config(config_dir=config_dir, port=port)
