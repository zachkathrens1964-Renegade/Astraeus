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

## Setup (about 10 minutes)

You need two things only you can create, then the setup wizard does the rest.

### 1. Create the Discord bot (2 min)
1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it.
2. **Bot** tab:
   - Click **Reset Token**, then copy the token.
   - Turn **Server Members Intent** ON and click **Save**.
   - Turn **Public Bot** OFF.

### 2. Get a Claude API key (1 min)
<https://console.anthropic.com> → **API Keys** → **Create Key**, then copy it. API usage is billed separately
from a Claude.ai subscription.

### 3. Download and start
Get the `discord-admin-bot` folder from this repo (**Code → Download ZIP** on GitHub, then unzip it). Then:

| Your computer | Do this |
|---|---|
| **Windows** | Install [Python 3.11+](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**. Then double-click **`start-windows.bat`**. |
| **Mac / Linux / Raspberry Pi** | In a terminal, inside the folder: `./start.sh` |

The first run opens the **setup wizard**:
1. Paste the bot token. It checks the token, and that the Members intent is on.
2. Confirm your user ID. It reads this from the app's owner, so you don't need Developer Mode.
3. Paste the API key. It checks the key.
4. **Open the invite link it prints and click Authorize.** Then press Enter, and it finds your server.

Settings are saved to `.env`. Later runs go straight to starting the bot. To redo setup, run
`python -m admin_bot setup` or delete `.env`.

### 4. Move the bot's role up
**Server Settings → Roles** → drag the bot's role **above** every role it should manage. The bot can't
touch roles above its own. The bot's log warns you if it's too low.

When the log says `Ready. Type /claude in your server.`, you're done.

### Keeping it running
The bot only works while that window or terminal is open. For 24/7:
- **Windows:** leave the window open, or add `start-windows.bat` to Task Scheduler with the "At log on" trigger.
- **Any machine with Docker:** run setup once (`./start.sh` or `python -m admin_bot setup`), then
  `docker compose up -d`. It restarts by itself after a crash or reboot.

### About the invite permissions
The invite link asks for every permission **except Administrator**. Discord only lets a bot hand out
permissions it has itself, so this is what "manage my roles" needs. If you want a narrower bot, replace
the number in the link with `1099780131894` (channels, roles and moderation only). Claude will then get
"missing permission" errors when it tries to grant anything outside that set.

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
