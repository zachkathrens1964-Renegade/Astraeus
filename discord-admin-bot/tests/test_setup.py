from __future__ import annotations

import os

import pytest

from admin_bot import setup_wizard as sw
from admin_bot.config import load_settings


def test_invite_url_and_permission_constants():
    import discord

    url = sw.invite_url("123")
    assert "client_id=123" in url and "scope=bot%20applications.commands" in url
    p = discord.Permissions(sw.RECOMMENDED_PERMISSIONS)
    assert not p.administrator and p.manage_roles and p.manage_channels and p.ban_members


def test_owner_ids_user_and_team():
    assert sw.owner_ids_from_application({"owner": {"id": "5", "username": "me"}}) == [("5", "me")]
    team = {"team": {"members": [{"user": {"id": "1", "username": "a"}}, {"user": {"id": "2", "username": "b"}}]}}
    assert sw.owner_ids_from_application(team) == [("1", "a"), ("2", "b")]
    assert sw.owner_ids_from_application({}) == []


def test_members_intent_flag():
    assert sw.has_members_intent({"flags": 1 << 15})
    assert sw.has_members_intent({"flags": 1 << 14})
    assert not sw.has_members_intent({"flags": 0})


def test_render_env_keeps_custom_values_and_defaults(tmp_path):
    path = tmp_path / ".env"
    sw.write_env(str(path), {"DISCORD_TOKEN": "t", "CLAUDE_EFFORT": "low", "EXTRA": "1"})
    values = sw.read_env(str(path))
    assert values["DISCORD_TOKEN"] == "t" and values["CLAUDE_EFFORT"] == "low"
    assert values["AUTO_APPROVE"] == "false" and values["EXTRA"] == "1"


def _scripted(answers):
    it = iter(answers)

    def fake_input(prompt=""):
        return next(it)

    return fake_input


def test_wizard_end_to_end(tmp_path, monkeypatch):
    calls = {"guilds": 0, "app": 0}

    def fake_discord_get(path, token):
        if token != "good-token":
            raise sw.SetupError("bad token")
        if path == "/oauth2/applications/@me":
            calls["app"] += 1
            # First check: intent off. After the user presses Enter: on.
            flags = 0 if calls["app"] == 1 else (1 << 15)
            return {"id": "777", "name": "MyBot", "owner": {"id": "42", "username": "me"}, "flags": flags, "bot_public": False}
        if path == "/users/@me/guilds":
            calls["guilds"] += 1
            return [] if calls["guilds"] == 1 else [{"id": "1001", "name": "A"}, {"id": "1002", "name": "B"}]
        raise AssertionError(path)

    def fake_check(key):
        if key != "sk-good":
            raise sw.SetupError("bad key")

    monkeypatch.setattr(sw, "discord_get", fake_discord_get)
    monkeypatch.setattr(sw, "check_anthropic_key", fake_check)

    answers = [
        "bad-token",   # rejected
        "good-token",
        "",            # press Enter after enabling the intent
        "y",           # owner is me
        "sk-bad",      # rejected
        "sk-good",
        "",            # press Enter: not in any guild yet
        "",            # press Enter again: now in two
        "2",           # pick server B
    ]
    env = tmp_path / ".env"
    assert sw.run_wizard(str(env), input_fn=_scripted(answers)) == 0

    for k in ("DISCORD_TOKEN", "OWNER_IDS", "GUILD_IDS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(str(env))
    assert s.discord_token == "good-token" and s.owner_ids == {42} and s.guild_ids == {1002}
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-good"
    if os.name == "posix":
        assert (env.stat().st_mode & 0o777) == 0o600


def test_main_without_config_points_to_setup(tmp_path, monkeypatch, capsys):
    from admin_bot.__main__ import main

    monkeypatch.chdir(tmp_path)
    for k in ("DISCORD_TOKEN", "OWNER_IDS", "GUILD_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert main([]) == 2
    assert "admin_bot setup" in capsys.readouterr().err


def test_startup_problems_flags_low_role_and_missing_guild():
    from types import SimpleNamespace

    import discord

    from admin_bot.bot import startup_problems
    from admin_bot.config import Settings
    from tests.test_bot import FakeRole

    everyone = FakeRole(1, "@everyone", 0, default=True)
    bot_role = FakeRole(2, "Bot", 1)
    mods = FakeRole(3, "Mods", 5)
    me = SimpleNamespace(top_role=bot_role, guild_permissions=discord.Permissions(manage_channels=True))
    guild = SimpleNamespace(name="S", me=me, roles=[everyone, bot_role, mods], owner_id=42)
    client = SimpleNamespace(get_guild={10: guild}.get, application_id=777, user=None)
    s = Settings(discord_token="x", guild_ids=frozenset({10, 11}), owner_ids=frozenset({42}))
    text = "\n".join(startup_problems(client, s))
    assert "not in server 11" in text and "client_id=777" in text
    assert "Mods" in text and "drag the bot's role higher" in text
    assert "manage_roles" in text
