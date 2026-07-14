"""CLI entry point: `gs <service>` or `python -m gs_collector <service>`."""

import argparse
import asyncio
import importlib

SERVICES = {
    "klines": ("gs_collector.klines", "main"),
    "orderbook": ("gs_collector.orderbook", "main"),
    "aggtrades": ("gs_collector.aggtrades", "main"),
    "futures": ("gs_collector.futures", "main"),
    "backfill": ("gs_collector.backfill", "main"),
    "monitor": ("gs_collector.monitor", "main"),
    "quality": ("gs_collector.monitor", "quality_main"),
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="gs", description="Geldscheißer data services")
    parser.add_argument("service", choices=sorted(SERVICES))
    args = parser.parse_args()
    module_name, func_name = SERVICES[args.service]
    func = getattr(importlib.import_module(module_name), func_name)
    try:
        asyncio.run(func())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
