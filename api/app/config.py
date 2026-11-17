"""Process configuration. Every knob is env-driven so a run can be reproduced from its label."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://shodh:shodh_dev_password@postgres:5432/shodh"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # "docker" -> submitted code runs in a locked-down sibling container.
    # "local"  -> submitted code runs as a subprocess with rlimits only (no container isolation).
    exec_mode: str = "docker"

    # Judge protocol.
    lease_seconds: int = 30
    max_deliveries: int = 3
    reaper_interval_seconds: float = 2.0
    reaper_enabled: bool = True

    # Dev authentication fixture: static seeded bearer tokens. Never enable outside dev.
    dev_auth_enabled: bool = True

    # Comma separated failpoint names, see app.domain.failpoints.
    failpoints: str = ""

    log_level: str = "INFO"
    service_name: str = "api"
    commit_sha: str = "unknown"

    # Leaderboard serving path used when the client does not ask for a specific one.
    default_standings_source: str = "standings"

    @property
    def failpoint_set(self) -> set[str]:
        return {f.strip() for f in self.failpoints.split(",") if f.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
