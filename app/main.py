"""Entry point: reads env config only, then builds the app (port of main.go)."""

from __future__ import annotations

import os

from . import api, pddikti


def _env(key: str, default: str) -> str:
    return os.getenv(key, "").strip() or default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise SystemExit(f"[-] {key} harus bilangan bulat >= 0, dapat {raw!r}")
    return value


def _list(key: str) -> list[str]:
    return [part.strip() for part in os.getenv(key, "").split(",") if part.strip()]


def create_app(client: pddikti.Client | None = None, cfg: api.Config | None = None) -> api.FastAPI:
    cfg = cfg or api.Config(
        api_key=_env("API_KEY", ""),
        cors_origins=_list("CORS_ORIGINS"),
        rate_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 30),
        min_query_len=_env_int("SEARCH_MIN_QUERY_LEN", 3),
        trusted_proxies=_list("TRUSTED_PROXIES"),
    )
    return api.build_app(client or pddikti.Client(timeout=10.0), cfg)


app = create_app()
