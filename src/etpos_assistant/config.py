from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_backup_dir() -> str:
    if os.getenv("ETPOS_ENV", "development").strip().lower() == "production":
        return "/var/lib/etpos-assistant/backups"
    return "./backups"


_load_local_env()


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("ETPOS_ENV", "development")
    host: str = os.getenv("ETPOS_HOST", "127.0.0.1")
    port: int = int(os.getenv("ETPOS_PORT", "8787"))
    data_dir: Path = Path(os.getenv("ETPOS_DATA_DIR", "./data"))
    snapshot_dir: Path = Path(os.getenv("ETPOS_SNAPSHOT_DIR", "./snapshots"))
    backup_dir: Path = Path(os.getenv("ETPOS_BACKUP_DIR", _default_backup_dir()))
    backup_retention_days: int = int(os.getenv("ETPOS_BACKUP_RETENTION_DAYS", "30"))
    docs_history_keep: int = int(os.getenv("ETPOS_DOCS_HISTORY_KEEP", "0"))
    cookie_secure: bool = _as_bool(os.getenv("ETPOS_COOKIE_SECURE"), False)
    public_origin: str = os.getenv("ETPOS_PUBLIC_ORIGIN", "").strip().rstrip("/")
    session_hours: int = int(os.getenv("ETPOS_SESSION_HOURS", "12"))
    provider: str = os.getenv("ETPOS_PROVIDER", "mock").strip().lower()
    codex_binary: str = os.getenv("CODEX_BINARY", "codex")
    codex_model: str = os.getenv("CODEX_MODEL", "").strip()
    codex_home: Path | None = Path(os.environ["CODEX_HOME"]) if os.getenv("CODEX_HOME") else None
    codex_timeout_seconds: int = int(os.getenv("CODEX_TIMEOUT_SECONDS", "120"))

    @property
    def app_db(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def docs_db(self) -> Path:
        return self.data_dir / "docs.db"

    @property
    def docs_candidates_dir(self) -> Path:
        return self.data_dir / "docs-candidates"

    @property
    def docs_history_dir(self) -> Path:
        return self.data_dir / "docs-history"

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
