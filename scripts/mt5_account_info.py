#!/usr/bin/env python3
"""Print the MT5 account currently connected via mt5linux."""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.execution.bridge import MT5Bridge


async def main() -> int:
    bridge = MT5Bridge(
        host=os.environ.get("MT5_HOST", "localhost"),
        port=int(os.environ.get("MT5_PORT", "18812")),
    )
    if not await bridge.connect():
        print("Failed to connect to MT5 / mt5linux", file=sys.stderr)
        return 1
    info = await bridge.get_account_info()
    if not info:
        print("No account info", file=sys.stderr)
        await bridge.shutdown()
        return 1
    print(f"login:    {getattr(info, 'login', '')}")
    print(f"server:   {getattr(info, 'server', '')}")
    print(f"balance:  {getattr(info, 'balance', '')}")
    print(f"equity:   {getattr(info, 'equity', '')}")
    print(f"leverage: 1:{getattr(info, 'leverage', '')}")
    print(f"currency: {getattr(info, 'currency', '')}")
    await bridge.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
