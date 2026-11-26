"""Permission-name and colour parsing, with strict validation."""

from __future__ import annotations

import discord


class ToolError(Exception):
    """An error reported back to Claude as an is_error tool result."""


# discord.py exposes several names for the same bit (e.g. read_messages / view_channel).
# Use one display name per bit, preferring the wording of Discord's own UI.
_PREFERRED = {
    "view_channel",
    "use_external_emojis",
    "use_external_stickers",
    "create_polls",
    "manage_roles",
    "manage_expressions",
}
_BY_VALUE: dict[int, list[str]] = {}
for _name, _value in discord.Permissions.VALID_FLAGS.items():
    _BY_VALUE.setdefault(_value, []).append(_name)

ALIASES: dict[str, str] = {}  # any accepted name -> display name
for _names in _BY_VALUE.values():
    _display = next((n for n in _names if n in _PREFERRED), sorted(_names)[0])
    for _name in _names:
        ALIASES[_name] = _display

VALID_PERMISSIONS: frozenset[str] = frozenset(ALIASES.values())

# Not valid in channel overwrites (guild-level only).
GUILD_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    {
        "administrator",
        "ban_members",
        "kick_members",
        "manage_guild",
        "view_audit_log",
        "view_guild_insights",
        "manage_nicknames",
        "change_nickname",
        "moderate_members",
        "manage_expressions",
        "create_expressions",
        "view_creator_monetization_analytics",
    }
) & VALID_PERMISSIONS


def normalize_names(names: list[str] | None) -> list[str]:
    """Normalize names like 'Manage Channels' / 'MANAGE_CHANNELS' and validate them."""
    if not names:
        return []
    out: list[str] = []
    bad: list[str] = []
    for name in names:
        key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
        display = ALIASES.get(key)
        if display is None:
            bad.append(str(name))
        elif display not in out:
            out.append(display)
    if bad:
        raise ToolError(
            f"Unknown permission name(s): {bad}. Valid names: {sorted(VALID_PERMISSIONS)}"
        )
    return out


def check_admin_grant(names: list[str], allow_administrator_grant: bool) -> None:
    if "administrator" in names and not allow_administrator_grant:
        raise ToolError(
            "Granting 'administrator' is blocked by the bot's configuration "
            "(ALLOW_ADMINISTRATOR_GRANT=false). Grant specific permissions instead."
        )


def permissions_from_names(names: list[str]) -> discord.Permissions:
    # Every display name is a valid discord.py keyword (canonical or alias).
    return discord.Permissions(**{n: True for n in names})


def enabled_permission_names(perms: discord.Permissions) -> list[str]:
    if perms.administrator:
        return ["administrator"]
    return sorted({ALIASES[name] for name, value in perms if value})


def parse_colour(value: str | None) -> discord.Colour | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lstrip("#")
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        number = int(text, 16)
    except ValueError as exc:
        raise ToolError(f"Colour must be a hex value like '#5865F2', got {value!r}") from exc
    if not 0 <= number <= 0xFFFFFF:
        raise ToolError("Colour must be between #000000 and #FFFFFF")
    return discord.Colour(number)


def parse_id(value: object, what: str) -> int:
    text = str(value).strip().lstrip("<#@&!").rstrip(">")
    if not text.isdigit():
        raise ToolError(f"{what} must be a numeric Discord ID, got {value!r}")
    return int(text)
