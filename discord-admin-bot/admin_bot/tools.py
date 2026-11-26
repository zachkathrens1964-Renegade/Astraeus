"""Tool schemas Claude can call, and the executor that performs them on a Discord guild."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Awaitable, Callable

import discord

from .perms import (
    GUILD_ONLY_PERMISSIONS,
    ToolError,
    check_admin_grant,
    enabled_permission_names,
    normalize_names,
    parse_colour,
    parse_id,
    permissions_from_names,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_ID = {"type": "string", "description": "Numeric Discord ID (snowflake) as a string."}
_PERM_LIST = {
    "type": "array",
    "items": {"type": "string"},
    "description": "discord.py permission names, e.g. send_messages, view_channel, manage_messages.",
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


READ_TOOL_SCHEMAS: list[dict] = [
    _tool(
        "get_server_overview",
        "List the server's categories, channels (with IDs, types, positions) and roles "
        "(with IDs, positions, colours, permissions). Call this first to learn IDs; never guess IDs.",
        {},
        [],
    ),
    _tool(
        "get_channel_details",
        "Show one channel's settings and its permission overwrites (per role/member allow/deny).",
        {"channel_id": _ID},
        ["channel_id"],
    ),
    _tool(
        "get_role_details",
        "Show one role's settings, permissions and how many members have it.",
        {"role_id": _ID},
        ["role_id"],
    ),
    _tool(
        "search_members",
        "Find members by username/nickname prefix, or by exact user ID.",
        {
            "query": {"type": "string", "description": "Name prefix or numeric user ID."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        ["query"],
    ),
]

WRITE_TOOL_SCHEMAS: list[dict] = [
    _tool(
        "create_channel",
        "Create a channel or category.",
        {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": ["text", "voice", "category", "forum", "stage"]},
            "category_id": {**_ID, "description": "Parent category ID (not for categories)."},
            "topic": {"type": "string", "description": "Text/forum channels only."},
            "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
            "nsfw": {"type": "boolean"},
            "user_limit": {"type": "integer", "minimum": 0, "maximum": 99, "description": "Voice only."},
            "private": {
                "type": "boolean",
                "description": "If true, deny view_channel to @everyone at creation.",
            },
        },
        ["name", "type"],
    ),
    _tool(
        "edit_channel",
        "Edit a channel or category. Only include the fields to change.",
        {
            "channel_id": _ID,
            "name": {"type": "string"},
            "topic": {"type": "string"},
            "category_id": {**_ID, "description": "Move under this category. Use 'none' to remove from its category."},
            "position": {"type": "integer", "minimum": 0},
            "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
            "nsfw": {"type": "boolean"},
            "sync_permissions_with_category": {"type": "boolean"},
        },
        ["channel_id"],
    ),
    _tool(
        "delete_channel",
        "Permanently delete a channel or category (a category's channels are kept, uncategorised).",
        {"channel_id": _ID},
        ["channel_id"],
    ),
    _tool(
        "set_channel_permissions",
        "Change a channel's permission overwrite for one role or member. allow/deny/reset are merged "
        "into the existing overwrite; reset returns a permission to 'inherit'. remove_overwrite deletes "
        "the whole overwrite. To make a channel private: deny view_channel for @everyone (its role ID "
        "equals the server ID) and allow view_channel for the roles that should see it.",
        {
            "channel_id": _ID,
            "target_id": {**_ID, "description": "Role ID or member ID."},
            "target_type": {"type": "string", "enum": ["role", "member"]},
            "allow": _PERM_LIST,
            "deny": _PERM_LIST,
            "reset": _PERM_LIST,
            "remove_overwrite": {"type": "boolean"},
        },
        ["channel_id", "target_id", "target_type"],
    ),
    _tool(
        "create_role",
        "Create a role.",
        {
            "name": {"type": "string"},
            "colour": {"type": "string", "description": "Hex like #5865F2."},
            "hoist": {"type": "boolean", "description": "Show separately in the member list."},
            "mentionable": {"type": "boolean"},
            "permissions": _PERM_LIST,
        },
        ["name"],
    ),
    _tool(
        "edit_role",
        "Edit a role. Only include fields to change. grant/revoke add or remove individual permissions.",
        {
            "role_id": _ID,
            "name": {"type": "string"},
            "colour": {"type": "string"},
            "hoist": {"type": "boolean"},
            "mentionable": {"type": "boolean"},
            "grant_permissions": _PERM_LIST,
            "revoke_permissions": _PERM_LIST,
            "position": {"type": "integer", "minimum": 1, "description": "Higher is more senior."},
        },
        ["role_id"],
    ),
    _tool("delete_role", "Permanently delete a role.", {"role_id": _ID}, ["role_id"]),
    _tool(
        "add_role_to_member",
        "Give a member a role.",
        {"member_id": _ID, "role_id": _ID},
        ["member_id", "role_id"],
    ),
    _tool(
        "remove_role_from_member",
        "Take a role away from a member.",
        {"member_id": _ID, "role_id": _ID},
        ["member_id", "role_id"],
    ),
    _tool(
        "timeout_member",
        "Time a member out (they can't talk) for N minutes; 0 removes an existing timeout.",
        {
            "member_id": _ID,
            "minutes": {"type": "integer", "minimum": 0, "maximum": 40320},
            "reason": {"type": "string"},
        },
        ["member_id", "minutes"],
    ),
    _tool(
        "kick_member",
        "Kick a member from the server.",
        {"member_id": _ID, "reason": {"type": "string"}},
        ["member_id"],
    ),
    _tool(
        "ban_member",
        "Ban a user from the server.",
        {
            "user_id": _ID,
            "reason": {"type": "string"},
            "delete_message_days": {"type": "integer", "minimum": 0, "maximum": 7},
        },
        ["user_id"],
    ),
    _tool("unban_user", "Lift a ban.", {"user_id": _ID, "reason": {"type": "string"}}, ["user_id"]),
    _tool(
        "send_message",
        "Post a message in a text channel as the bot.",
        {"channel_id": _ID, "content": {"type": "string", "maxLength": 2000}},
        ["channel_id", "content"],
    ),
    _tool(
        "edit_server",
        "Rename the server.",
        {"name": {"type": "string"}},
        ["name"],
    ),
]

TOOL_SCHEMAS: list[dict] = READ_TOOL_SCHEMAS + WRITE_TOOL_SCHEMAS
READ_TOOLS: frozenset[str] = frozenset(t["name"] for t in READ_TOOL_SCHEMAS)
WRITE_TOOLS: frozenset[str] = frozenset(t["name"] for t in WRITE_TOOL_SCHEMAS)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

_CHANNEL_TYPE_NAMES = {
    discord.ChannelType.text: "text",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.category: "category",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.news: "announcement",
}


def _ctype(channel: Any) -> str:
    return _CHANNEL_TYPE_NAMES.get(getattr(channel, "type", None), str(getattr(channel, "type", "unknown")))


class DiscordToolExecutor:
    """Runs tool calls against one guild, with safety checks enforced in code."""

    def __init__(self, guild: discord.Guild, actor: str, *, allow_administrator_grant: bool = False):
        self.guild = guild
        self.actor = actor
        self.allow_administrator_grant = allow_administrator_grant
        self._handlers: dict[str, Callable[[dict], Awaitable[Any]]] = {
            name: getattr(self, f"_t_{name}") for name in READ_TOOLS | WRITE_TOOLS
        }

    # -- public -------------------------------------------------------------

    def reason(self) -> str:
        return f"Requested by {self.actor} via Claude admin bot"[:512]

    async def execute(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool {name!r}")
        try:
            result = await handler(args or {})
        except ToolError:
            raise
        except discord.Forbidden as exc:
            raise ToolError(f"Discord refused (missing permission or role hierarchy): {exc.text}") from exc
        except discord.NotFound as exc:
            raise ToolError(f"Not found: {exc.text}") from exc
        except discord.HTTPException as exc:
            raise ToolError(f"Discord API error {exc.status}: {exc.text}") from exc
        return json.dumps(result, ensure_ascii=False, default=str)

    def describe(self, name: str, args: dict) -> str:
        """Human-readable one-liner for the confirmation prompt."""
        a = args or {}

        def ch(key: str) -> str:
            return self._label_channel(a.get(key))

        def rl(key: str) -> str:
            return self._label_role(a.get(key))

        def mb(key: str) -> str:
            return self._label_member(a.get(key))

        if name == "create_channel":
            where = f" in {ch('category_id')}" if a.get("category_id") else ""
            extra = " (private)" if a.get("private") else ""
            return f"Create {a.get('type', 'text')} channel **{a.get('name')}**{where}{extra}"
        if name == "edit_channel":
            changes = {k: v for k, v in a.items() if k != "channel_id"}
            return f"Edit {ch('channel_id')}: {self._fmt_changes(changes)}"
        if name == "delete_channel":
            return f"🗑️ **Delete** channel {ch('channel_id')}"
        if name == "set_channel_permissions":
            target = rl("target_id") if a.get("target_type") == "role" else mb("target_id")
            if a.get("remove_overwrite"):
                return f"Remove all overrides for {target} in {ch('channel_id')}"
            parts = []
            for key, label in (("allow", "allow"), ("deny", "deny"), ("reset", "reset")):
                if a.get(key):
                    parts.append(f"{label} {', '.join(a[key])}")
            return f"Permissions in {ch('channel_id')} for {target}: {'; '.join(parts) or 'no change'}"
        if name == "create_role":
            perms = f" with {', '.join(a['permissions'])}" if a.get("permissions") else ""
            return f"Create role **{a.get('name')}**{perms}"
        if name == "edit_role":
            changes = {k: v for k, v in a.items() if k != "role_id"}
            return f"Edit role {rl('role_id')}: {self._fmt_changes(changes)}"
        if name == "delete_role":
            return f"🗑️ **Delete** role {rl('role_id')}"
        if name == "add_role_to_member":
            return f"Give {rl('role_id')} to {mb('member_id')}"
        if name == "remove_role_from_member":
            return f"Remove {rl('role_id')} from {mb('member_id')}"
        if name == "timeout_member":
            if not a.get("minutes"):
                return f"Remove timeout from {mb('member_id')}"
            return f"⏱️ Timeout {mb('member_id')} for {a.get('minutes')} min"
        if name == "kick_member":
            return f"👢 **Kick** {mb('member_id')}"
        if name == "ban_member":
            return f"🔨 **Ban** {mb('user_id')}"
        if name == "unban_user":
            return f"Unban user {a.get('user_id')}"
        if name == "send_message":
            preview = str(a.get("content", ""))
            preview = preview if len(preview) <= 120 else preview[:117] + "..."
            return f"Post in {ch('channel_id')}: “{preview}”"
        if name == "edit_server":
            return f"Rename server to **{a.get('name')}**"
        return f"{name} {json.dumps(a)[:200]}"

    # -- lookups ------------------------------------------------------------

    @staticmethod
    def _fmt_changes(changes: dict) -> str:
        if not changes:
            return "no changes"
        return ", ".join(f"{k}={v}" for k, v in changes.items())

    def _label_channel(self, value: Any) -> str:
        try:
            channel = self.guild.get_channel(parse_id(value, "channel_id"))
        except ToolError:
            channel = None
        return f"#{channel.name}" if channel else f"channel {value}"

    def _label_role(self, value: Any) -> str:
        try:
            role = self.guild.get_role(parse_id(value, "role_id"))
        except ToolError:
            role = None
        return f"@{role.name}" if role else f"role {value}"

    def _label_member(self, value: Any) -> str:
        try:
            member = self.guild.get_member(parse_id(value, "member_id"))
        except ToolError:
            member = None
        return f"{member.display_name} ({member.id})" if member else f"user {value}"

    def _channel(self, value: Any) -> Any:
        channel = self.guild.get_channel(parse_id(value, "channel_id"))
        if channel is None:
            raise ToolError(f"No channel with ID {value} in this server. Call get_server_overview.")
        return channel

    def _category(self, value: Any) -> Any:
        channel = self._channel(value)
        if not isinstance(channel, discord.CategoryChannel):
            raise ToolError(f"Channel {value} is not a category.")
        return channel

    def _role(self, value: Any) -> discord.Role:
        role = self.guild.get_role(parse_id(value, "role_id"))
        if role is None:
            raise ToolError(f"No role with ID {value} in this server. Call get_server_overview.")
        return role

    async def _member(self, value: Any) -> discord.Member:
        member_id = parse_id(value, "member_id")
        member = self.guild.get_member(member_id)
        if member is None:
            try:
                member = await self.guild.fetch_member(member_id)
            except discord.NotFound as exc:
                raise ToolError(f"No member with ID {value} in this server.") from exc
        return member

    # -- safety checks ------------------------------------------------------

    def _assert_role_manageable(self, role: discord.Role, action: str) -> None:
        me = self.guild.me
        if role.managed:
            raise ToolError(f"Role @{role.name} is managed by an integration/bot and can't be {action}.")
        if role >= me.top_role:
            raise ToolError(
                f"Role @{role.name} is at or above the bot's highest role (@{me.top_role.name}); "
                f"move the bot's role higher in Server Settings > Roles first."
            )

    def _assert_member_moderatable(self, member: discord.Member, action: str) -> None:
        me = self.guild.me
        if member.id == me.id:
            raise ToolError(f"The bot won't {action} itself.")
        if member.id == self.guild.owner_id:
            raise ToolError(f"The server owner can't be {action}.")
        if member.top_role >= me.top_role:
            raise ToolError(f"{member.display_name}'s highest role is at or above the bot's; can't {action} them.")

    # -- read tools ---------------------------------------------------------

    async def _t_get_server_overview(self, _: dict) -> dict:
        g = self.guild
        categories = []
        for category, channels in g.by_category():
            categories.append(
                {
                    "category": None
                    if category is None
                    else {"id": str(category.id), "name": category.name, "position": category.position},
                    "channels": [
                        {"id": str(c.id), "name": c.name, "type": _ctype(c), "position": c.position}
                        for c in channels
                    ],
                }
            )
        roles = [
            {
                "id": str(r.id),
                "name": r.name,
                "position": r.position,
                "colour": str(r.colour),
                "managed": r.managed,
                "is_everyone": r.is_default(),
                "permissions": enabled_permission_names(r.permissions),
            }
            for r in sorted(g.roles, key=lambda r: r.position, reverse=True)
        ]
        return {
            "server": {"id": str(g.id), "name": g.name, "member_count": g.member_count, "owner_id": str(g.owner_id)},
            "bot_top_role": {"id": str(g.me.top_role.id), "name": g.me.top_role.name, "position": g.me.top_role.position},
            "note": "@everyone's role ID equals the server ID. The bot can only manage roles below its top role.",
            "categories": categories,
            "roles": roles,
        }

    async def _t_get_channel_details(self, a: dict) -> dict:
        c = self._channel(a.get("channel_id"))
        overwrites = []
        for target, ow in c.overwrites.items():
            allow, deny = ow.pair()
            overwrites.append(
                {
                    "target_id": str(target.id),
                    "target_type": "role" if isinstance(target, discord.Role) else "member",
                    "target_name": getattr(target, "name", str(target.id)),
                    "allow": enabled_permission_names(allow),
                    "deny": enabled_permission_names(deny),
                }
            )
        info: dict[str, Any] = {
            "id": str(c.id),
            "name": c.name,
            "type": _ctype(c),
            "position": c.position,
            "category_id": str(c.category_id) if getattr(c, "category_id", None) else None,
            "permissions_synced_with_category": getattr(c, "permissions_synced", None),
            "overwrites": overwrites,
        }
        for attr in ("topic", "slowmode_delay", "nsfw", "user_limit", "bitrate"):
            if hasattr(c, attr):
                info[attr] = getattr(c, attr)
        if isinstance(c, discord.CategoryChannel):
            info["channels"] = [{"id": str(x.id), "name": x.name, "type": _ctype(x)} for x in c.channels]
        return info

    async def _t_get_role_details(self, a: dict) -> dict:
        r = self._role(a.get("role_id"))
        return {
            "id": str(r.id),
            "name": r.name,
            "position": r.position,
            "colour": str(r.colour),
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "managed": r.managed,
            "member_count": len(r.members),
            "permissions": enabled_permission_names(r.permissions),
            "bot_can_manage": (not r.managed) and r < self.guild.me.top_role,
        }

    async def _t_search_members(self, a: dict) -> dict:
        query = str(a.get("query", "")).strip()
        limit = max(1, min(int(a.get("limit") or 10), 25))
        if not query:
            raise ToolError("query is required")
        if query.strip("<@!>").isdigit():
            members = [await self._member(query)]
        else:
            members = await self.guild.query_members(query=query, limit=limit)
        return {
            "members": [
                {
                    "id": str(m.id),
                    "username": m.name,
                    "display_name": m.display_name,
                    "bot": m.bot,
                    "roles": [{"id": str(r.id), "name": r.name} for r in m.roles if not r.is_default()],
                    "timed_out": m.is_timed_out(),
                }
                for m in members
            ]
        }

    # -- write tools --------------------------------------------------------

    async def _t_create_channel(self, a: dict) -> dict:
        name = str(a.get("name", "")).strip()
        if not name:
            raise ToolError("name is required")
        ctype = a.get("type", "text")
        reason = self.reason()
        category = self._category(a["category_id"]) if a.get("category_id") and ctype != "category" else None
        overwrites = {}
        if a.get("private"):
            overwrites[self.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            overwrites[self.guild.me] = discord.PermissionOverwrite(view_channel=True, manage_channels=True)
        kwargs: dict[str, Any] = {"reason": reason}
        if overwrites:
            kwargs["overwrites"] = overwrites

        g = self.guild
        if ctype == "category":
            channel = await g.create_category(name, **kwargs)
        elif ctype == "voice":
            if a.get("user_limit") is not None:
                kwargs["user_limit"] = int(a["user_limit"])
            channel = await g.create_voice_channel(name, category=category, **kwargs)
        elif ctype == "stage":
            channel = await g.create_stage_channel(name, category=category, **kwargs)
        elif ctype == "forum":
            if a.get("topic"):
                kwargs["topic"] = a["topic"]
            channel = await g.create_forum(name, category=category, **kwargs)
        elif ctype == "text":
            for src, dst in (("topic", "topic"), ("slowmode_seconds", "slowmode_delay"), ("nsfw", "nsfw")):
                if a.get(src) is not None:
                    kwargs[dst] = a[src]
            channel = await g.create_text_channel(name, category=category, **kwargs)
        else:
            raise ToolError(f"Unsupported channel type {ctype!r}")
        return {"created": {"id": str(channel.id), "name": channel.name, "type": _ctype(channel)}}

    async def _t_edit_channel(self, a: dict) -> dict:
        c = self._channel(a.get("channel_id"))
        kwargs: dict[str, Any] = {}
        if a.get("name"):
            kwargs["name"] = a["name"]
        if "topic" in a and hasattr(c, "topic"):
            kwargs["topic"] = a["topic"]
        if "category_id" in a:
            value = a["category_id"]
            kwargs["category"] = None if str(value).lower() in {"none", "null", ""} else self._category(value)
        if a.get("position") is not None:
            kwargs["position"] = int(a["position"])
        if a.get("slowmode_seconds") is not None and hasattr(c, "slowmode_delay"):
            kwargs["slowmode_delay"] = int(a["slowmode_seconds"])
        if a.get("nsfw") is not None and hasattr(c, "nsfw"):
            kwargs["nsfw"] = bool(a["nsfw"])
        if a.get("sync_permissions_with_category") is not None:
            kwargs["sync_permissions"] = bool(a["sync_permissions_with_category"])
        if not kwargs:
            raise ToolError("Nothing to change (or the fields don't apply to this channel type).")
        await c.edit(reason=self.reason(), **kwargs)
        return {"edited": {"id": str(c.id), "name": kwargs.get("name", c.name), "changed": sorted(kwargs)}}

    async def _t_delete_channel(self, a: dict) -> dict:
        c = self._channel(a.get("channel_id"))
        name = c.name
        await c.delete(reason=self.reason())
        return {"deleted": {"id": str(c.id), "name": name}}

    async def _t_set_channel_permissions(self, a: dict) -> dict:
        c = self._channel(a.get("channel_id"))
        target_type = a.get("target_type")
        if target_type == "role":
            target: Any = self._role(a.get("target_id"))
        elif target_type == "member":
            target = await self._member(a.get("target_id"))
        else:
            raise ToolError("target_type must be 'role' or 'member'")

        if a.get("remove_overwrite"):
            await c.set_permissions(target, overwrite=None, reason=self.reason())
            return {"removed_overwrite_for": getattr(target, "name", str(target.id)), "channel": c.name}

        allow, deny, reset = (normalize_names(a.get(k)) for k in ("allow", "deny", "reset"))
        overlap = (set(allow) & set(deny)) | (set(allow) & set(reset)) | (set(deny) & set(reset))
        if overlap:
            raise ToolError(f"Permission(s) listed in more than one of allow/deny/reset: {sorted(overlap)}")
        guild_only = sorted((set(allow) | set(deny) | set(reset)) & GUILD_ONLY_PERMISSIONS)
        if guild_only:
            raise ToolError(
                f"{guild_only} can't be set per channel; they are server-wide role permissions (use edit_role)."
            )
        if not (allow or deny or reset):
            raise ToolError("Provide at least one of allow, deny, reset, or remove_overwrite.")

        overwrite = c.overwrites_for(target)
        for n in allow:
            setattr(overwrite, n, True)
        for n in deny:
            setattr(overwrite, n, False)
        for n in reset:
            setattr(overwrite, n, None)
        await c.set_permissions(
            target, overwrite=None if overwrite.is_empty() else overwrite, reason=self.reason()
        )
        new_allow, new_deny = overwrite.pair()
        return {
            "channel": c.name,
            "target": getattr(target, "name", str(target.id)),
            "now_allowed": enabled_permission_names(new_allow),
            "now_denied": enabled_permission_names(new_deny),
        }

    async def _t_create_role(self, a: dict) -> dict:
        name = str(a.get("name", "")).strip()
        if not name:
            raise ToolError("name is required")
        perms = normalize_names(a.get("permissions"))
        check_admin_grant(perms, self.allow_administrator_grant)
        kwargs: dict[str, Any] = {"name": name, "reason": self.reason()}
        if perms:
            kwargs["permissions"] = permissions_from_names(perms)
        else:
            kwargs["permissions"] = discord.Permissions.none()
        colour = parse_colour(a.get("colour"))
        if colour is not None:
            kwargs["colour"] = colour
        for key in ("hoist", "mentionable"):
            if a.get(key) is not None:
                kwargs[key] = bool(a[key])
        role = await self.guild.create_role(**kwargs)
        return {"created": {"id": str(role.id), "name": role.name, "position": role.position}}

    async def _t_edit_role(self, a: dict) -> dict:
        role = self._role(a.get("role_id"))
        self._assert_role_manageable(role, "edited")
        grant = normalize_names(a.get("grant_permissions"))
        revoke = normalize_names(a.get("revoke_permissions"))
        if set(grant) & set(revoke):
            raise ToolError(f"Permission(s) in both grant and revoke: {sorted(set(grant) & set(revoke))}")
        check_admin_grant(grant, self.allow_administrator_grant)

        kwargs: dict[str, Any] = {}
        if a.get("name"):
            kwargs["name"] = a["name"]
        colour = parse_colour(a.get("colour"))
        if colour is not None:
            kwargs["colour"] = colour
        for key in ("hoist", "mentionable"):
            if a.get(key) is not None:
                kwargs[key] = bool(a[key])
        if grant or revoke:
            perms = discord.Permissions(role.permissions.value)
            for n in grant:
                setattr(perms, n, True)
            for n in revoke:
                setattr(perms, n, False)
            kwargs["permissions"] = perms
        if a.get("position") is not None:
            position = int(a["position"])
            if position >= self.guild.me.top_role.position:
                raise ToolError(
                    f"Position must be below the bot's top role (position {self.guild.me.top_role.position})."
                )
            kwargs["position"] = position
        if not kwargs:
            raise ToolError("Nothing to change.")
        await role.edit(reason=self.reason(), **kwargs)
        return {
            "edited": {"id": str(role.id), "name": kwargs.get("name", role.name), "changed": sorted(kwargs)},
        }

    async def _t_delete_role(self, a: dict) -> dict:
        role = self._role(a.get("role_id"))
        if role.is_default():
            raise ToolError("@everyone can't be deleted.")
        self._assert_role_manageable(role, "deleted")
        name = role.name
        await role.delete(reason=self.reason())
        return {"deleted": {"id": str(role.id), "name": name}}

    async def _t_add_role_to_member(self, a: dict) -> dict:
        member = await self._member(a.get("member_id"))
        role = self._role(a.get("role_id"))
        if role.is_default():
            raise ToolError("Everyone already has @everyone.")
        self._assert_role_manageable(role, "assigned")
        if role.permissions.administrator and not self.allow_administrator_grant:
            raise ToolError("Assigning a role with Administrator is blocked (ALLOW_ADMINISTRATOR_GRANT=false).")
        await member.add_roles(role, reason=self.reason())
        return {"member": member.display_name, "added_role": role.name}

    async def _t_remove_role_from_member(self, a: dict) -> dict:
        member = await self._member(a.get("member_id"))
        role = self._role(a.get("role_id"))
        if role.is_default():
            raise ToolError("@everyone can't be removed.")
        self._assert_role_manageable(role, "removed")
        await member.remove_roles(role, reason=self.reason())
        return {"member": member.display_name, "removed_role": role.name}

    async def _t_timeout_member(self, a: dict) -> dict:
        member = await self._member(a.get("member_id"))
        self._assert_member_moderatable(member, "time out")
        minutes = int(a.get("minutes") or 0)
        reason = f"{self.reason()}: {a.get('reason', '')}".strip(": ")[:512]
        await member.timeout(timedelta(minutes=minutes) if minutes else None, reason=reason)
        return {"member": member.display_name, "timeout_minutes": minutes}

    async def _t_kick_member(self, a: dict) -> dict:
        member = await self._member(a.get("member_id"))
        self._assert_member_moderatable(member, "kick")
        reason = f"{self.reason()}: {a.get('reason', '')}".strip(": ")[:512]
        await member.kick(reason=reason)
        return {"kicked": member.display_name}

    async def _t_ban_member(self, a: dict) -> dict:
        user_id = parse_id(a.get("user_id"), "user_id")
        member = self.guild.get_member(user_id)
        if member is not None:
            self._assert_member_moderatable(member, "ban")
        elif user_id == self.guild.owner_id or user_id == self.guild.me.id:
            raise ToolError("Refusing to ban the server owner or the bot.")
        days = max(0, min(int(a.get("delete_message_days") or 0), 7))
        reason = f"{self.reason()}: {a.get('reason', '')}".strip(": ")[:512]
        await self.guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=days * 86400)
        return {"banned_user_id": str(user_id)}

    async def _t_unban_user(self, a: dict) -> dict:
        user_id = parse_id(a.get("user_id"), "user_id")
        await self.guild.unban(discord.Object(id=user_id), reason=self.reason())
        return {"unbanned_user_id": str(user_id)}

    async def _t_send_message(self, a: dict) -> dict:
        c = self._channel(a.get("channel_id"))
        if not hasattr(c, "send") or isinstance(c, discord.CategoryChannel):
            raise ToolError("That channel can't receive messages.")
        content = str(a.get("content", ""))
        if not content.strip():
            raise ToolError("content is empty")
        msg = await c.send(content[:2000], allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        return {"sent": {"channel": c.name, "message_id": str(msg.id)}}

    async def _t_edit_server(self, a: dict) -> dict:
        name = str(a.get("name", "")).strip()
        if not name:
            raise ToolError("name is required")
        await self.guild.edit(name=name, reason=self.reason())
        return {"server_renamed_to": name}
