import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from clangy.config import Config

EX_CONFIG = 78
"""Exit status for a broken setup; the systemd unit doesn't restart on it."""


def main() -> None:
    parser = argparse.ArgumentParser(prog="clangy", description="Clangy v2, the Pulsar Discord support bot")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("command", choices=["run", "check", "sync-knowledge", "timings"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.load(args.config)

    if args.command == "check":
        from clangy.check import main as check

        check(config, args.config)
        return

    if args.command == "timings":
        from clangy.timings import load, summarize

        print(summarize(load(config.data_dir)))
        return

    if args.command == "sync-knowledge":
        from clangy.knowledge import sync

        sync(config)
        return

    from clangy.check import sandbox_problems
    from clangy.discord_bot import ClangyClient

    if config.sandbox.firejail and (problems := asyncio.run(sandbox_problems(config, args.config))):
        # Fail closed: without a working sandbox the agent would run with the bot's own access
        print(f"Refusing to start, the sandbox doesn't work: {', '.join(problems)}. Run 'clangy check'.")
        sys.exit(EX_CONFIG)

    token = config.discord_token or os.environ.get("DISCORD_TOKEN", "")
    if not token:
        print("No Discord token: set discord_token in the config or DISCORD_TOKEN in the environment.")
        sys.exit(EX_CONFIG)
    ClangyClient(config).run(token, log_handler=None)


if __name__ == "__main__":
    main()
