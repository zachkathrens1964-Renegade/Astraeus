"""Entry point: python -m admin_bot"""

from __future__ import annotations

import logging
import sys

from .bot import AdminBot
from .config import ConfigError, load_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    bot = AdminBot(settings)
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
