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
    scheduler: bool = True
    download_monitor: bool = True

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
    scheduler = os.environ.get("FUMORA_SCHEDULER", "1") != "0"
    monitor = os.environ.get("FUMORA_DOWNLOAD_MONITOR", "1") != "0"
    return Config(config_dir=config_dir, port=port, scheduler=scheduler, download_monitor=monitor)
