import argparse
import logging
import os
from pathlib import Path

from clangy.config import Config


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

    from clangy.discord_bot import ClangyClient

    ClangyClient(config).run(os.environ[config.token_env], log_handler=None)


if __name__ == "__main__":
    main()
