"""Configuration loaded from environment variables (optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so there is no extra dependency. Real env vars win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.split(" #", 1)[0]  # allow trailing comments
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if value:  # an empty ANTHROPIC_API_KEY= would otherwise shadow other credentials
                os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_list(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(f"{name} must be a comma-separated list of numeric Discord IDs, got {part!r}")
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_ids: frozenset[int]
    owner_ids: frozenset[int]
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    enable_fallbacks: bool = True
    auto_approve: bool = False
    allow_administrator_grant: bool = False
    max_iterations: int = 25
    confirm_timeout_seconds: int = 300
    session_ttl_seconds: int = 1800
    max_history_messages: int = 60


VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def load_settings(dotenv_path: str = ".env") -> Settings:
    _load_dotenv(dotenv_path)

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is required")

    owners = _int_list("OWNER_IDS")
    if not owners:
        raise ConfigError("OWNER_IDS is required: your Discord user ID (Developer Mode > right-click you > Copy User ID)")

    guilds = _int_list("GUILD_IDS")
    if not guilds:
        raise ConfigError("GUILD_IDS is required: the server ID(s) the bot is allowed to manage")

    effort = os.environ.get("CLAUDE_EFFORT", "medium").strip().lower()
    if effort not in VALID_EFFORTS:
        raise ConfigError(f"CLAUDE_EFFORT must be one of {sorted(VALID_EFFORTS)}")

    # The Anthropic SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile) itself.
    return Settings(
        discord_token=token,
        guild_ids=guilds,
        owner_ids=owners,
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip(),
        effort=effort,
        enable_fallbacks=_bool("ENABLE_FALLBACKS", True),
        auto_approve=_bool("AUTO_APPROVE", False),
        allow_administrator_grant=_bool("ALLOW_ADMINISTRATOR_GRANT", False),
        max_iterations=int(os.environ.get("MAX_ITERATIONS", "25")),
        confirm_timeout_seconds=int(os.environ.get("CONFIRM_TIMEOUT_SECONDS", "300")),
        session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "1800")),
        max_history_messages=int(os.environ.get("MAX_HISTORY_MESSAGES", "60")),
    )
