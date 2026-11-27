"""Interactive first-run setup: python -m admin_bot setup

Asks for the two secrets only a human can create (Discord bot token, Anthropic API key),
checks both, works out your user ID and server ID automatically, prints the invite link,
and writes .env.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (claude-admin-bot, 1.0)"

# Every permission except Administrator (bit 3). Discord only lets a bot grant permissions it has.
RECOMMENDED_PERMISSIONS = 8866461766385655
MINIMAL_PERMISSIONS = 1099780131894

# Application flags: https://discord.com/developers/docs/resources/application#application-object-application-flags
FLAG_GUILD_MEMBERS = 1 << 14
FLAG_GUILD_MEMBERS_LIMITED = 1 << 15

ENV_ORDER = [
    "DISCORD_TOKEN",
    "ANTHROPIC_API_KEY",
    "OWNER_IDS",
    "GUILD_IDS",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "ENABLE_FALLBACKS",
    "AUTO_APPROVE",
    "ALLOW_ADMINISTRATOR_GRANT",
    "CONFIRM_TIMEOUT_SECONDS",
    "SESSION_TTL_SECONDS",
    "MAX_ITERATIONS",
    "MAX_HISTORY_MESSAGES",
]
DEFAULTS = {
    "CLAUDE_MODEL": "claude-opus-5-5",
    "CLAUDE_EFFORT": "medium",
    "ENABLE_FALLBACKS": "true",
    "AUTO_APPROVE": "false",
    "ALLOW_ADMINISTRATOR_GRANT": "false",
    "CONFIRM_TIMEOUT_SECONDS": "300",
    "SESSION_TTL_SECONDS": "1800",
    "MAX_ITERATIONS": "25",
    "MAX_HISTORY_MESSAGES": "60",
}


class SetupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def invite_url(application_id: str, permissions: int = RECOMMENDED_PERMISSIONS) -> str:
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot%20applications.commands&permissions={permissions}"
    )


def owner_ids_from_application(app: dict) -> list[tuple[str, str]]:
    """(id, name) of the person/people who own the Discord application."""
    team = app.get("team")
    if team:
        members = team.get("members") or []
        return [(str(m["user"]["id"]), m["user"].get("username", "?")) for m in members if m.get("user")]
    owner = app.get("owner") or {}
    if owner.get("id"):
        return [(str(owner["id"]), owner.get("username", "?"))]
    return []


def has_members_intent(app: dict) -> bool:
    flags = int(app.get("flags") or 0)
    return bool(flags & (FLAG_GUILD_MEMBERS | FLAG_GUILD_MEMBERS_LIMITED))


def read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            values[key.strip()] = value
    return values


def render_env(values: dict[str, str]) -> str:
    merged = {**DEFAULTS, **{k: v for k, v in values.items() if v != ""}}
    lines = [
        "# Written by `python -m admin_bot setup`. Keep this file secret: it holds your tokens.",
        "# See .env.example for what each setting does.",
    ]
    for key in ENV_ORDER:
        lines.append(f"{key}={merged.get(key, '')}")
    for key in sorted(set(merged) - set(ENV_ORDER)):
        lines.append(f"{key}={merged[key]}")
    return "\n".join(lines) + "\n"


def write_env(path: str, values: dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_env(values))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: chmod is mostly a no-op


# ---------------------------------------------------------------------------
# Network checks
# ---------------------------------------------------------------------------


def discord_get(path: str, token: str) -> Any:
    request = urllib.request.Request(
        DISCORD_API + path,
        headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SetupError("Discord rejected that token. In the Developer Portal: Bot tab > Reset Token, copy it again.")
        raise SetupError(f"Discord API error {exc.code} on {path}") from exc
    except urllib.error.URLError as exc:
        raise SetupError(f"Couldn't reach Discord ({exc.reason}). Check your internet connection.") from exc


def check_anthropic_key(key: str) -> None:
    import anthropic

    try:
        anthropic.Anthropic(api_key=key, max_retries=1).models.list(limit=1)
    except anthropic.AuthenticationError as exc:
        raise SetupError("Anthropic rejected that API key. Create one at console.anthropic.com > API Keys.") from exc
    except anthropic.APIConnectionError as exc:
        raise SetupError("Couldn't reach the Anthropic API. Check your internet connection.") from exc


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str = "", secret: bool = False, input_fn: Callable[[str], str] = input) -> str:
    suffix = f" [{default}]" if default and not secret else (" [keep current]" if default else "")
    reader = getpass.getpass if secret and input_fn is input else input_fn
    value = reader(f"{prompt}{suffix}: ").strip()
    return value or default


def _yes(prompt: str, input_fn: Callable[[str], str] = input) -> bool:
    return input_fn(f"{prompt} [Y/n]: ").strip().lower() in {"", "y", "yes"}


def run_wizard(env_path: str = ".env", input_fn: Callable[[str], str] = input) -> int:
    print("\n=== Claude Discord Admin Bot: setup ===\n")
    values = read_env(env_path)

    # 1. Discord token -> application info
    print("Step 1/4: Discord bot token")
    print("  https://discord.com/developers/applications > New Application > Bot tab > Reset Token")
    print("  (Pasted text is hidden. That's normal.)")
    while True:
        token = _ask("  Bot token", values.get("DISCORD_TOKEN", ""), secret=True, input_fn=input_fn)
        if not token:
            continue
        try:
            app = discord_get("/oauth2/applications/@me", token)
            break
        except SetupError as exc:
            print(f"  ✗ {exc}")
            values.pop("DISCORD_TOKEN", None)
    values["DISCORD_TOKEN"] = token
    app_id = str(app["id"])
    print(f"  ✓ Token works for app “{app.get('name', app_id)}”")

    if app.get("bot_public"):
        print("  ! Your bot is Public, so anyone with the link can add it. Turn off 'Public Bot' on the Bot tab.")
    while not has_members_intent(app):
        print("  ! 'Server Members Intent' is off. Bot tab > Privileged Gateway Intents > turn it ON > Save.")
        input_fn("    Press Enter after saving to re-check...")
        app = discord_get("/oauth2/applications/@me", token)
    print("  ✓ Server Members Intent is on")

    # 2. Owner ID from the application owner
    print("\nStep 2/4: Who is allowed to give the bot orders")
    owners = owner_ids_from_application(app)
    if owners and _yes(
        f"  Allow {', '.join(f'{name} ({oid})' for oid, name in owners)} (the app's owner)?", input_fn
    ):
        values["OWNER_IDS"] = ",".join(oid for oid, _ in owners)
    else:
        print("  Discord Settings > Advanced > Developer Mode ON, then long-press your name > Copy User ID")
        while True:
            raw = _ask("  Your user ID(s), comma-separated", values.get("OWNER_IDS", ""), input_fn=input_fn)
            if raw and all(p.strip().isdigit() for p in raw.split(",")):
                values["OWNER_IDS"] = ",".join(p.strip() for p in raw.split(","))
                break
            print("  ✗ IDs are numbers only")
    print(f"  ✓ Owner(s): {values['OWNER_IDS']}")

    # 3. Anthropic key
    print("\nStep 3/4: Claude API key")
    print("  https://console.anthropic.com > API Keys > Create Key")
    while True:
        key = _ask("  API key", values.get("ANTHROPIC_API_KEY", ""), secret=True, input_fn=input_fn)
        if not key:
            continue
        try:
            check_anthropic_key(key)
            break
        except SetupError as exc:
            print(f"  ✗ {exc}")
            values.pop("ANTHROPIC_API_KEY", None)
    values["ANTHROPIC_API_KEY"] = key
    print("  ✓ API key works")

    # Save progress now so a later Ctrl+C doesn't lose the secrets.
    write_env(env_path, values)

    # 4. Invite + pick the server
    print("\nStep 4/4: Add the bot to your server")
    print("  Open this link, pick your server, click Authorize:\n")
    print(f"  {invite_url(app_id)}\n")
    print("  (It asks for every permission except Administrator. Discord only lets a bot hand out")
    print("   permissions it has itself.)")
    while True:
        input_fn("  Press Enter once you've added it...")
        guilds = discord_get("/users/@me/guilds", token)
        if guilds:
            break
        print("  ✗ The bot isn't in any server yet.")
    if len(guilds) == 1:
        chosen = [guilds[0]]
    else:
        for i, g in enumerate(guilds, 1):
            print(f"   {i}. {g['name']}")
        while True:
            raw = input_fn("  Which server(s) should it manage? Numbers, comma-separated: ").strip()
            try:
                picks = [int(p) for p in raw.split(",") if p.strip()]
                chosen = [guilds[p - 1] for p in picks if 1 <= p <= len(guilds)]
            except ValueError:
                chosen = []
            if chosen:
                break
            print("  ✗ Pick at least one number from the list")
    values["GUILD_IDS"] = ",".join(str(g["id"]) for g in chosen)
    print(f"  ✓ Managing: {', '.join(g['name'] for g in chosen)}")

    write_env(env_path, values)
    print(f"\n✓ Saved settings to {os.path.abspath(env_path)}")
    print("\nLast manual step: Server Settings > Roles > drag the bot's role ABOVE the roles it should manage.")
    print("Then start the bot (start-windows.bat, ./start.sh, or `python -m admin_bot`) and type /claude in your server.\n")
    return 0


def main() -> int:
    try:
        return run_wizard()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        return 1
    except SetupError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
