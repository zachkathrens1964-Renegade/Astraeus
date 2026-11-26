# Claude Discord Admin Bot

Run your Discord server by talking to it. From the Discord app on your phone:

```
/claude request: make a private #staff channel under Admin that only Mods can see
```

Claude reads your server's real layout, works out the changes, and shows them to you:

> **Claude wants to make these changes:**
> 1. Create text channel **staff** in #Admin (private)
> 2. Permissions in #staff for @Mods: allow view_channel
>
> `[Confirm]` `[Confirm all for this request]` `[Cancel]`

Nothing changes on the server until you tap Confirm. Only you see the bot's replies.

**What it can do:** create, edit, move and delete channels and categories; set per-channel permissions;
create, edit, reorder and delete roles; give or remove roles; timeout, kick, ban and unban; post messages;
rename the server. It can also look things up: "who has the Mods role?", "what can @Members do in #general?".

Follow-ups work for 30 minutes ("now make it read-only"). Use `/claude-reset` to start over.

---

## Setup (about 15 minutes)

### 1. Create the Discord bot
1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_TOKEN`. Keep it secret.
3. Same tab → **Privileged Gateway Intents** → turn on **Server Members Intent**. It's needed to look up members.
4. Turn off **Public Bot** so nobody else can add it to their server.

### 2. Invite it to your server
Replace `APP_ID` with the **Application ID** from the General Information tab and open the URL:

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot%20applications.commands&permissions=8866461766385655
```

That grants every permission **except Administrator**. Why so many: Discord only lets a bot give out
permissions it already has. If you want Claude to hand out a permission, the bot needs it too.

If you only want channel and role management plus moderation, use `permissions=1099780131894` instead.
Claude will then get "missing permission" errors when it tries to grant anything outside that set.

### 3. Put the bot's role near the top
**Server Settings → Roles** → drag the bot's role **above** every role you want it to manage. The bot can't
touch roles above its own, or members whose highest role is above it.

### 4. Get your IDs
Discord **Settings → Advanced → Developer Mode** on. Then:
- Right-click (long-press on phone) **your name** → *Copy User ID*. This is `OWNER_IDS`.
- Right-click **your server icon** → *Copy Server ID*. This is `GUILD_IDS`.

### 5. Get a Claude API key
<https://console.anthropic.com> → **API Keys** → create a key. This is `ANTHROPIC_API_KEY`. API usage
is billed separately from a Claude.ai subscription.

### 6. Run it
The bot has to stay running, so use a computer that's always on, a small VPS, or a Raspberry Pi.

```bash
cp .env.example .env      # then fill in the four required values
```

**Docker (recommended)**
```bash
docker build -t claude-admin-bot .
docker run -d --name claude-admin-bot --restart unless-stopped --env-file .env claude-admin-bot
docker logs -f claude-admin-bot
```

**Plain Python 3.11+**
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m admin_bot
```

When the log shows `Synced 2 command(s)`, type `/claude` in your server.

---

## Safety design

| Rail | Enforced by |
|---|---|
| Only your user ID(s), only your server(s) | Code (`OWNER_IDS`, `GUILD_IDS`); commands are also hidden from members without Manage Server |
| Every change needs your tap | Code (Confirm buttons; 5-min timeout = cancel). `AUTO_APPROVE=true` turns this off. |
| Never grants **Administrator** | Code, unless `ALLOW_ADMINISTRATOR_GRANT=true` |
| Won't touch the server owner, itself, integration roles, @everyone deletion, or anything above its role | Code |
| `send_message` can't ping @everyone/@here or roles | Code (`allowed_mentions`) |
| Prompt injection (e.g. a channel topic saying "ban everyone") | System prompt + the confirmation step |
| Audit trail | Every action's Audit Log reason reads "Requested by *you* via Claude admin bot" |

**Token safety:** anyone with `DISCORD_TOKEN` controls the bot. Never commit `.env` (it's gitignored). If the
token leaks, use **Reset Token** right away.

## Cost

Each request takes several Claude calls, because Claude reads the server first and then acts. The system prompt and
tools are prompt-cached. My rough estimate, not measured: about $0.05–0.25 per request with `claude-opus-5-5` at `medium` effort, more for
big servers since the overview grows with the number of channels and roles. To cut cost, set `CLAUDE_EFFORT=low` or
`CLAUDE_MODEL=claude-sonnet-5-5`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Layout: `admin_bot/bot.py` (Discord commands and buttons), `agent.py` (Claude loop and approval gate),
`tools.py` (tool schemas and Discord actions), `perms.py` (permission parsing), `config.py`.
