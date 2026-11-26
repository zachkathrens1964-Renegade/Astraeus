from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace

import discord
import pytest

from admin_bot.agent import AdminAgent, PendingAction
from admin_bot.bot import chunk_text, format_actions
from admin_bot.config import ConfigError, Settings, load_settings
from admin_bot.perms import ToolError, normalize_names, parse_colour, parse_id
from admin_bot.tools import READ_TOOLS, TOOL_SCHEMAS, WRITE_TOOLS, DiscordToolExecutor


def settings(**overrides) -> Settings:
    base = dict(discord_token="x", guild_ids=frozenset({1}), owner_ids=frozenset({42}))
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(stop_reason, *content, stop_details=None):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), stop_details=stop_details)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **params):
        # Snapshot messages so later appends don't change what we recorded.
        self.calls.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.beta = SimpleNamespace(messages=FakeMessages(responses))


class RecordingExecutor:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    async def execute(self, name, args):
        self.calls.append((name, args))
        if name in self.fail:
            raise ToolError(self.fail[name])
        return json.dumps({"ok": name})

    def describe(self, name, args):
        return f"{name} {args}"


def approver(decision):
    seen = []

    async def approve(actions: list[PendingAction]):
        seen.append(actions)
        if decision == "all":
            return {a.tool_use_id for a in actions}
        return set()

    approve.seen = seen
    return approve


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


async def test_read_tools_run_without_approval_and_writes_need_it():
    client = FakeClient(
        [
            response("tool_use", tool_use("t1", "get_server_overview", {})),
            response(
                "tool_use",
                text("Creating."),
                tool_use("t2", "create_channel", {"name": "clips", "type": "text"}),
                tool_use("t3", "create_role", {"name": "Clippers"}),
            ),
            response("end_turn", text("- Created #clips\n- Created @Clippers")),
        ]
    )
    agent = AdminAgent(settings(), client=client)
    ex = RecordingExecutor()
    approve = approver("all")
    result = await agent.run((1, 42), "make #clips and a Clippers role", ex, approve)

    assert result.text.startswith("- Created #clips")
    assert [c[0] for c in ex.calls] == ["get_server_overview", "create_channel", "create_role"]
    assert len(approve.seen) == 1 and len(approve.seen[0]) == 2  # both writes batched into one prompt
    assert result.actions_run == 2 and result.actions_declined == 0

    # Tool results for parallel calls go back in ONE user message, in order.
    last_user = client.beta.messages.calls[2]["messages"][-1]
    assert last_user["role"] == "user"
    assert [r["tool_use_id"] for r in last_user["content"]] == ["t2", "t3"]


async def test_declined_writes_are_not_executed_and_reported_as_errors():
    client = FakeClient(
        [
            response("tool_use", tool_use("t1", "delete_channel", {"channel_id": "5"})),
            response("end_turn", text("Skipped deleting the channel.")),
        ]
    )
    agent = AdminAgent(settings(), client=client)
    ex = RecordingExecutor()
    result = await agent.run((1, 42), "delete general", ex, approver("none"))

    assert ex.calls == []
    assert result.actions_declined == 1
    tool_result = client.beta.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True and "declined" in tool_result["content"]


async def test_auto_approve_skips_callback():
    client = FakeClient(
        [
            response("tool_use", tool_use("t1", "edit_server", {"name": "New"})),
            response("end_turn", text("Renamed.")),
        ]
    )
    agent = AdminAgent(settings(auto_approve=True), client=client)
    ex = RecordingExecutor()
    approve = approver("none")
    await agent.run((1, 42), "rename", ex, approve)
    assert approve.seen == [] and ex.calls == [("edit_server", {"name": "New"})]


async def test_tool_errors_are_returned_to_claude():
    client = FakeClient(
        [
            response("tool_use", tool_use("t1", "get_role_details", {"role_id": "9"})),
            response("end_turn", text("That role doesn't exist.")),
        ]
    )
    agent = AdminAgent(settings(), client=client)
    ex = RecordingExecutor(fail={"get_role_details": "No role with ID 9"})
    await agent.run((1, 42), "what is role 9", ex, approver("all"))
    tr = client.beta.messages.calls[1]["messages"][-1]["content"][0]
    assert tr["is_error"] and "No role" in tr["content"]


async def test_refusal_resets_session():
    client = FakeClient([response("refusal", stop_details=SimpleNamespace(category="cyber"))])
    agent = AdminAgent(settings(), client=client)
    result = await agent.run((1, 42), "x", RecordingExecutor(), approver("all"))
    assert "declined" in result.text and "cyber" in result.text
    assert result.session_reset
    assert agent._session((1, 42)).messages == []


async def test_history_persists_between_requests_and_reset_clears_it():
    client = FakeClient([response("end_turn", text("one")), response("end_turn", text("two"))])
    agent = AdminAgent(settings(), client=client)
    await agent.run((1, 42), "first", RecordingExecutor(), approver("all"))
    await agent.run((1, 42), "second", RecordingExecutor(), approver("all"))
    second_call_messages = client.beta.messages.calls[1]["messages"]
    assert [m["role"] for m in second_call_messages] == ["user", "assistant", "user"]
    agent.reset((1, 42))
    assert agent._session((1, 42)).messages == []


async def test_session_expires_after_ttl():
    now = [0.0]
    client = FakeClient([response("end_turn", text("a")), response("end_turn", text("b"))])
    agent = AdminAgent(settings(session_ttl_seconds=10), client=client, clock=lambda: now[0])
    await agent.run((1, 42), "first", RecordingExecutor(), approver("all"))
    now[0] = 100.0
    await agent.run((1, 42), "second", RecordingExecutor(), approver("all"))
    assert len(client.beta.messages.calls[1]["messages"]) == 1


async def test_iteration_cap():
    client = FakeClient([response("tool_use", tool_use(f"t{i}", "get_server_overview", {})) for i in range(3)])
    agent = AdminAgent(settings(max_iterations=3), client=client)
    result = await agent.run((1, 42), "loop", RecordingExecutor(), approver("all"))
    assert "Stopped after 3 steps" in result.text and result.session_reset


async def test_request_params():
    client = FakeClient([response("end_turn", text("hi"))])
    agent = AdminAgent(settings(), client=client)
    await agent.run((1, 42), "hi", RecordingExecutor(), approver("all"))
    params = client.beta.messages.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["output_config"] == {"effort": "medium"}
    assert "thinking" not in params and "tool_choice" not in params and "temperature" not in params

    client2 = FakeClient([response("end_turn", text("hi"))])
    await AdminAgent(settings(enable_fallbacks=False), client=client2).run(
        (1, 42), "hi", RecordingExecutor(), approver("all")
    )
    assert "fallbacks" not in client2.beta.messages.calls[0]


async def test_concurrent_requests_from_same_user_are_serialized():
    gate = asyncio.Event()
    order = []

    class SlowMessages(FakeMessages):
        async def create(self, **params):
            order.append(params["messages"][-1]["content"])
            if len(order) == 1:
                await gate.wait()
            return response("end_turn", text("ok"))

    client = SimpleNamespace(beta=SimpleNamespace(messages=SlowMessages([])))
    agent = AdminAgent(settings(), client=client)
    t1 = asyncio.create_task(agent.run((1, 42), "first", RecordingExecutor(), approver("all")))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(agent.run((1, 42), "second", RecordingExecutor(), approver("all")))
    await asyncio.sleep(0.01)
    assert order == ["first"]  # second waits for the lock
    gate.set()
    await asyncio.gather(t1, t2)
    assert order == ["first", "second"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in TOOL_SCHEMAS]
    assert len(names) == len(set(names))
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
    for t in TOOL_SCHEMAS:
        schema = t["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert t["description"]


def test_every_tool_has_a_handler():
    guild = SimpleNamespace()
    ex = DiscordToolExecutor(guild, "owner")
    assert set(ex._handlers) == READ_TOOLS | WRITE_TOOLS


# ---------------------------------------------------------------------------
# Permission / parsing helpers
# ---------------------------------------------------------------------------


def test_normalize_permission_names():
    assert normalize_names(["Send Messages", "VIEW_CHANNEL", "send-messages"]) == ["send_messages", "view_channel"]
    with pytest.raises(ToolError, match="Unknown permission"):
        normalize_names(["fly"])


def test_parse_colour_and_id():
    assert parse_colour("#5865F2").value == 0x5865F2
    assert parse_colour(None) is None
    with pytest.raises(ToolError):
        parse_colour("blue")
    assert parse_id("<@&123>", "role") == 123
    with pytest.raises(ToolError):
        parse_id("general", "channel")


def test_load_settings(monkeypatch, tmp_path):
    for k in ("DISCORD_TOKEN", "OWNER_IDS", "GUILD_IDS", "CLAUDE_EFFORT", "AUTO_APPROVE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("# c\nDISCORD_TOKEN=abc\nOWNER_IDS=1, 2\nGUILD_IDS=3   # mine\nAUTO_APPROVE=yes\n")
    s = load_settings(str(env))
    assert s.owner_ids == {1, 2} and s.guild_ids == {3} and s.auto_approve and s.effort == "medium"

    monkeypatch.setenv("OWNER_IDS", "abc")
    with pytest.raises(ConfigError):
        load_settings(str(env))


# ---------------------------------------------------------------------------
# Executor safety checks with fake Discord objects
# ---------------------------------------------------------------------------


@functools.total_ordering
class FakeRole:
    def __init__(self, id_, name, position, managed=False, default=False, perms=None):
        self.id, self.name, self.position, self.managed = id_, name, position, managed
        self._default = default
        self.permissions = perms or discord.Permissions.none()
        self.edits = []
        self.members = []
        self.colour = discord.Colour.default()

    def is_default(self):
        return self._default

    def __eq__(self, other):
        return self.position == other.position

    def __lt__(self, other):
        return self.position < other.position

    __hash__ = object.__hash__

    async def edit(self, **kw):
        self.edits.append(kw)

    async def delete(self, **kw):
        self.edits.append({"deleted": True})


class FakeChannel:
    def __init__(self, id_, name):
        self.id, self.name = id_, name
        self.overwrites = {}
        self.set_calls = []

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.set_calls.append((target, overwrite))
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite


def make_guild():
    everyone = FakeRole(1, "@everyone", 0, default=True)
    member_role = FakeRole(10, "Members", 1)
    bot_role = FakeRole(20, "AdminBot", 5)
    owner_role = FakeRole(30, "Owner", 9)
    integration = FakeRole(40, "SomeBot", 2, managed=True)
    roles = {r.id: r for r in (everyone, member_role, bot_role, owner_role, integration)}
    channel = FakeChannel(100, "general")
    me = SimpleNamespace(id=999, top_role=bot_role)
    guild = SimpleNamespace(
        id=1,
        owner_id=555,
        me=me,
        default_role=everyone,
        get_role=roles.get,
        get_channel={100: channel}.get,
        get_member=lambda _id: None,
    )
    return guild, roles, channel


async def test_cannot_edit_role_above_bot_or_managed():
    guild, roles, _ = make_guild()
    ex = DiscordToolExecutor(guild, "owner")
    with pytest.raises(ToolError, match="at or above"):
        await ex.execute("edit_role", {"role_id": "30", "name": "x"})
    with pytest.raises(ToolError, match="managed"):
        await ex.execute("edit_role", {"role_id": "40", "name": "x"})
    out = json.loads(await ex.execute("edit_role", {"role_id": "10", "grant_permissions": ["send_messages"]}))
    assert out["edited"]["changed"] == ["permissions"]
    assert roles[10].edits[0]["permissions"].send_messages is True


async def test_administrator_grant_blocked_unless_enabled():
    guild, roles, _ = make_guild()
    with pytest.raises(ToolError, match="administrator"):
        await DiscordToolExecutor(guild, "o").execute(
            "edit_role", {"role_id": "10", "grant_permissions": ["administrator"]}
        )
    await DiscordToolExecutor(guild, "o", allow_administrator_grant=True).execute(
        "edit_role", {"role_id": "10", "grant_permissions": ["administrator"]}
    )
    assert roles[10].edits[-1]["permissions"].administrator is True


async def test_cannot_delete_everyone():
    guild, _, _ = make_guild()
    with pytest.raises(ToolError, match="@everyone"):
        await DiscordToolExecutor(guild, "o").execute("delete_role", {"role_id": "1"})


async def test_channel_permissions_merge_and_validate():
    guild, roles, channel = make_guild()
    ex = DiscordToolExecutor(guild, "o")
    await ex.execute(
        "set_channel_permissions",
        {"channel_id": "100", "target_id": "1", "target_type": "role", "deny": ["view_channel"]},
    )
    out = json.loads(
        await ex.execute(
            "set_channel_permissions",
            {"channel_id": "100", "target_id": "1", "target_type": "role", "deny": ["send_messages"]},
        )
    )
    assert out["now_denied"] == ["send_messages", "view_channel"]  # merged, not replaced

    out = json.loads(
        await ex.execute(
            "set_channel_permissions",
            {"channel_id": "100", "target_id": "1", "target_type": "role", "reset": ["send_messages", "view_channel"]},
        )
    )
    assert out["now_denied"] == [] and roles[1] not in channel.overwrites  # empty overwrite removed

    with pytest.raises(ToolError, match="server-wide"):
        await ex.execute(
            "set_channel_permissions",
            {"channel_id": "100", "target_id": "1", "target_type": "role", "allow": ["ban_members"]},
        )
    with pytest.raises(ToolError, match="more than one"):
        await ex.execute(
            "set_channel_permissions",
            {"channel_id": "100", "target_id": "1", "target_type": "role", "allow": ["speak"], "deny": ["speak"]},
        )


async def test_ban_refuses_owner_and_bot():
    guild, _, _ = make_guild()
    ex = DiscordToolExecutor(guild, "o")
    with pytest.raises(ToolError, match="owner"):
        await ex.execute("ban_member", {"user_id": "555"})
    with pytest.raises(ToolError, match="owner or the bot"):
        await ex.execute("ban_member", {"user_id": "999"})


async def test_unknown_channel_is_a_tool_error():
    guild, _, _ = make_guild()
    with pytest.raises(ToolError, match="No channel"):
        await DiscordToolExecutor(guild, "o").execute("delete_channel", {"channel_id": "7"})


def test_describe_uses_names():
    guild, _, _ = make_guild()
    ex = DiscordToolExecutor(guild, "o")
    assert ex.describe("delete_channel", {"channel_id": "100"}) == "🗑️ **Delete** channel #general"
    assert "@Members" in ex.describe("add_role_to_member", {"member_id": "5", "role_id": "10"})


# ---------------------------------------------------------------------------
# Discord formatting helpers
# ---------------------------------------------------------------------------


def test_chunk_text_respects_limit():
    long = ("x" * 50 + "\n") * 100
    chunks = chunk_text(long, limit=200)
    assert all(len(c) <= 200 for c in chunks) and "".join(chunks) == long
    assert chunk_text("y" * 450, limit=200) == ["y" * 200, "y" * 200, "y" * 50]


def test_format_actions_truncates():
    actions = [PendingAction(str(i), "x", {}, "a" * 100) for i in range(50)]
    out = format_actions(actions)
    assert len(out) <= 1800 and "50 total" in out
