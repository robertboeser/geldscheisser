"""CLI entry point: `gs-archive <command>` or `python -m gs_archive <command>`."""

import argparse
import asyncio
import importlib

COMMANDS = {
    "sync": ("gs_archive.sync", "main"),            # long-running: sync on a fixed interval
    "sync-once": ("gs_archive.sync", "sync_once_main"),  # one-shot, for cron/manual runs
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="gs-archive", description="Geldscheißer archive service")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    module_name, func_name = COMMANDS[args.command]
    func = getattr(importlib.import_module(module_name), func_name)
    try:
        asyncio.run(func())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
