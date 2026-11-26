"""Discord front end: /claude and /claude-reset slash commands with Confirm/Cancel buttons."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .agent import AdminAgent, PendingAction
from .config import Settings
from .tools import DiscordToolExecutor

log = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 2000


def chunk_text(text: str, limit: int = MAX_DISCORD_MESSAGE) -> list[str]:
    """Split text on line boundaries into Discord-sized pieces."""
    text = text or "Done."
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def format_actions(actions: list[PendingAction], limit: int = 1800) -> str:
    lines = ["**Claude wants to make these changes:**"]
    for i, action in enumerate(actions, 1):
        lines.append(f"{i}. {action.description}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 40].rsplit("\n", 1)[0] + f"\n…and more ({len(actions)} total)"
    return text


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.choice: str = "cancel"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who made the request can confirm.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, choice: str, label: str) -> None:
        self.choice = choice
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        content = (interaction.message.content if interaction.message else "") + f"\n\n**→ {label}**"
        await interaction.response.edit_message(content=content[:MAX_DISCORD_MESSAGE], view=self)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, "confirm", "Confirmed")

    @discord.ui.button(label="Confirm all for this request", style=discord.ButtonStyle.primary)
    async def confirm_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, "confirm_all", "Confirmed (and any further steps of this request)")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, "cancel", "Cancelled")


class AdminBot(discord.Client):
    def __init__(self, settings: Settings, agent: AdminAgent | None = None):
        intents = discord.Intents.default()
        intents.members = True  # privileged: enable "Server Members Intent" in the Developer Portal
        super().__init__(intents=intents)
        self.settings = settings
        self.agent = agent or AdminAgent(settings)
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def is_authorized(self, interaction: discord.Interaction) -> str | None:
        """Return an error message if this interaction may not use the bot, else None."""
        if interaction.guild is None:
            return "Use this inside your server, not in DMs."
        if interaction.guild.id not in self.settings.guild_ids:
            return "This server isn't in the bot's GUILD_IDS allow-list."
        if interaction.user.id not in self.settings.owner_ids:
            return "Only the configured owner(s) can use this bot."
        return None

    def _register_commands(self) -> None:
        guilds = [discord.Object(id=g) for g in self.settings.guild_ids]

        @app_commands.command(name="claude", description="Tell Claude what to change on this server")
        @app_commands.describe(request="e.g. 'make a private #staff channel only Mods can see'")
        @app_commands.default_permissions(manage_guild=True)
        async def claude_cmd(interaction: discord.Interaction, request: str) -> None:
            await self.handle_request(interaction, request)

        @app_commands.command(name="claude-reset", description="Make Claude forget this conversation")
        @app_commands.default_permissions(manage_guild=True)
        async def reset_cmd(interaction: discord.Interaction) -> None:
            error = self.is_authorized(interaction)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            self.agent.reset((interaction.guild.id, interaction.user.id))  # type: ignore[union-attr]
            await interaction.response.send_message("Conversation cleared.", ephemeral=True)

        for cmd in (claude_cmd, reset_cmd):
            self.tree.add_command(cmd, guilds=guilds)

    async def setup_hook(self) -> None:
        for guild_id in self.settings.guild_ids:
            synced = await self.tree.sync(guild=discord.Object(id=guild_id))
            log.info("Synced %d command(s) to guild %s", len(synced), guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        for guild_id in self.settings.guild_ids:
            if self.get_guild(guild_id) is None:
                log.warning("Bot is not in guild %s - invite it with the URL from the README.", guild_id)

    async def _send(self, interaction: discord.Interaction, content: str, **kwargs) -> discord.Message | None:
        """Send an ephemeral followup; fall back to DM if the interaction token expired (15 min)."""
        try:
            return await interaction.followup.send(content, ephemeral=True, wait=True, **kwargs)
        except discord.HTTPException as exc:
            log.warning("Followup failed (%s); falling back to DM", exc)
            try:
                return await interaction.user.send(content, **kwargs)
            except discord.HTTPException:
                log.exception("DM fallback failed")
                return None

    async def handle_request(self, interaction: discord.Interaction, request: str) -> None:
        error = self.is_authorized(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        actor = f"{interaction.user} ({interaction.user.id})"
        executor = DiscordToolExecutor(
            guild, actor, allow_administrator_grant=self.settings.allow_administrator_grant
        )
        approve_all = False

        async def approve(actions: list[PendingAction]) -> set[str]:
            nonlocal approve_all
            if approve_all:
                await self._send(interaction, format_actions(actions) + "\n\n**→ Auto-confirmed (Confirm all)**")
                return {a.tool_use_id for a in actions}
            view = ConfirmView(interaction.user.id, timeout=self.settings.confirm_timeout_seconds)
            message = await self._send(interaction, format_actions(actions), view=view)
            if message is None:
                return set()
            timed_out = await view.wait()
            if timed_out:
                try:
                    await message.edit(content=message.content + "\n\n**→ Timed out, cancelled**", view=None)
                except discord.HTTPException:
                    pass
                return set()
            if view.choice == "confirm_all":
                approve_all = True
            if view.choice in {"confirm", "confirm_all"}:
                return {a.tool_use_id for a in actions}
            return set()

        result = await self.agent.run((guild.id, interaction.user.id), request, executor, approve)
        footer = ""
        if result.actions_run or result.actions_declined:
            footer = f"\n\n-# {result.actions_run} change(s) applied, {result.actions_declined} skipped"
        for chunk in chunk_text(result.text + footer):
            await self._send(interaction, chunk)
