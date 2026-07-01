#!/usr/bin/env python3
"""Verify MT5 algo trading is enabled via mt5linux; toggle toolbar if still off."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    sys.path.insert(0, str(_repo_root()))
    from src.core.env import load_project_env
    load_project_env()


def _trade_allowed_sync(host: str, port: int) -> bool | None:
    try:
        from mt5linux import MetaTrader5
    except ImportError:
        print("mt5linux not installed; skipping algo-trading verification", file=sys.stderr)
        return None

    mt5 = MetaTrader5(host=host, port=port)
    if not mt5.initialize():
        return None
    try:
        info = mt5.terminal_info()
        if info is None:
            return None
        return bool(getattr(info, "trade_allowed", False))
    finally:
        mt5.shutdown()


async def _wait_trade_allowed(host: str, port: int, timeout_sec: float) -> bool | None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        allowed = await asyncio.to_thread(_trade_allowed_sync, host, port)
        if allowed is True:
            return True
        if allowed is False:
            return False
        await asyncio.sleep(1.0)
    return None


def _toggle_mt5_toolbar() -> bool:
    if not shutil.which("xdotool"):
        return False
    try:
        window = subprocess.check_output(
            ["xdotool", "search", "--name", "MetaTrader 5"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    if not window:
        return False
    wid = window[0]
    subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
    # Default MT5 shortcut for automated trading (can be customized in terminal).
    subprocess.run(["xdotool", "key", "--window", wid, "ctrl+e"], check=False)
    time.sleep(1.0)
    return True


async def main_async() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("MT5_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MT5_PORT", "18812")))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--toggle", action="store_true", help="Try xdotool Ctrl+E if still disabled")
    args = parser.parse_args()

    allowed = await _wait_trade_allowed(args.host, args.port, args.timeout)
    if allowed is True:
        print("MT5 algo trading is enabled (trade_allowed=True)")
        return 0
    if allowed is False:
        print("MT5 algo trading is OFF (trade_allowed=False)", file=sys.stderr)
        if args.toggle and _toggle_mt5_toolbar():
            allowed = await _wait_trade_allowed(args.host, args.port, 10.0)
            if allowed is True:
                print("MT5 algo trading enabled after toolbar toggle")
                return 0
        print(
            "Enable Algo Trading in MT5 (green toolbar button) or re-run start_all.sh "
            "after stopping the terminal so common.ini is applied.",
            file=sys.stderr,
        )
        return 1
    print("Could not verify MT5 algo trading (terminal or mt5linux not ready)", file=sys.stderr)
    return 2


def main() -> int:
    _load_dotenv()
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
