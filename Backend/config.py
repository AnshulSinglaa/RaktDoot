from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "LifeLink"
    environment: str = "local"              # local | staging | production
    min_donation_interval_days: int = 90    # policy knob: confirm against blood-bank/NBTC rules

    database_url: str                       # required: no default, so a missing value fails fast
    test_database_url: str | None = None    # tests only; must point at a *_test database
    pool_size: int = 5
    max_overflow: int = 5
    sql_echo: bool = False                  # True prints every SQL statement (learning/debugging)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LIFELINK_", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
