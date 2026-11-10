"""Runtime configuration, read from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str
    payment_api_key: str
    payment_timeout_s: float
    export_dir: str
    session_ttl_s: int


def load_settings() -> Settings:
    return Settings(
        database_path=os.environ.get("SHOP_DB", "shop.sqlite3"),
        payment_api_key=os.environ["PAYMENT_API_KEY"],
        payment_timeout_s=float(os.environ.get("PAYMENT_TIMEOUT_S", "10")),
        export_dir=os.environ.get("SHOP_EXPORT_DIR", "/var/lib/shop/exports"),
        session_ttl_s=int(os.environ.get("SESSION_TTL_S", "3600")),
    )
