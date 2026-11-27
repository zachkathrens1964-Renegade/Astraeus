"""Entry point.

    python -m admin_bot          run the bot
    python -m admin_bot setup    interactive first-run setup (writes .env)
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_settings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "setup":
        from .setup_wizard import main as setup_main

        return setup_main()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print("Run `python -m admin_bot setup` (or the start script) to create your settings.", file=sys.stderr)
        return 2

    from .bot import AdminBot

    bot = AdminBot(settings)
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
