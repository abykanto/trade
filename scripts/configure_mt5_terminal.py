#!/usr/bin/env python3
"""Patch MetaTrader 5 common.ini for automated (algo) trading and optional EA sockets."""
from __future__ import annotations

import argparse
import configparser
import io
import os
import re
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    import sys
    sys.path.insert(0, str(_repo_root()))
    from src.core.env import load_project_env
    load_project_env()


def _decode_mt5_ini(raw: bytes) -> str:
    """Decode MT5 common.ini, stripping any leading garbage before [Common]."""
    attempts: list[str] = []
    decoders: list[str] = []
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        decoders.extend(["utf-16", "utf-16-le"])
    else:
        decoders.append("utf-16-le")
    decoders.append("utf-8")

    for enc in decoders:
        try:
            attempts.append(raw.decode(enc))
        except UnicodeDecodeError:
            continue

    for text in attempts:
        idx = text.find("[Common]")
        if idx >= 0:
            if idx > 0:
                text = text[idx:]
            return text.lstrip("\ufeff")
    marker = b"[Common]"
    pos = raw.find(marker)
    if pos >= 0:
        return raw[pos:].decode("ascii", errors="replace")
    raise ValueError("common.ini is unreadable (missing [Common] section)")


def _read_ini(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str  # preserve key case
    if path.is_file():
        text = _decode_mt5_ini(path.read_bytes())
        cfg.read_string(text)
    if "Common" not in cfg:
        cfg["Common"] = {}
    if "Experts" not in cfg:
        cfg["Experts"] = {}
    return cfg


def _write_ini(path: Path, cfg: configparser.ConfigParser) -> None:
    buf = io.StringIO()
    cfg.write(buf, space_around_delimiters=False)
    text = buf.getvalue().replace("\n", "\r\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))


def _apply_login_server(cfg: configparser.ConfigParser) -> None:
    login = os.environ.get("MT5_LOGIN", "").strip()
    server = os.environ.get("MT5_SERVER", "").strip()
    if login:
        cfg["Common"]["Login"] = login
    if server:
        cfg["Common"]["Server"] = server


def _apply_algo_trading(cfg: configparser.ConfigParser) -> None:
    experts = cfg["Experts"]
    experts["AllowLiveTrading"] = "1"
    experts["Enabled"] = "1"
    experts["AllowDllImport"] = "1"
    # Keep algo trading on when account/profile/chart changes.
    experts["Account"] = "0"
    experts["Profile"] = "0"
    experts.setdefault("Chart", "0")
    experts.setdefault("Api", "0")


def _apply_ea_sockets(cfg: configparser.ConfigParser) -> None:
    experts = cfg["Experts"]
    experts["WebRequest"] = "1"
    current = experts.get("WebRequestUrl", "").strip()
    if "127.0.0.1" not in current:
        experts["WebRequestUrl"] = f"{current};127.0.0.1" if current else "127.0.0.1"


def configure(path: Path, *, ea_sockets: bool = False) -> list[str]:
    _load_dotenv()
    cfg = _read_ini(path)
    _apply_login_server(cfg)
    _apply_algo_trading(cfg)
    changes = ["AllowLiveTrading=1", "Enabled=1", "Account=0", "Profile=0"]
    if ea_sockets:
        _apply_ea_sockets(cfg)
        changes.append("WebRequest allowlist 127.0.0.1")
    _write_ini(path, cfg)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ini",
        default=os.environ.get(
            "MT5_COMMON_INI",
            str(
                Path(os.environ.get("WINEPREFIX", str(Path.home() / ".wine")))
                / "drive_c/Program Files/MetaTrader 5/Config/common.ini"
            ),
        ),
        help="Path to MT5 common.ini (default: Wine MT5 install)",
    )
    parser.add_argument(
        "--ea-sockets",
        action="store_true",
        help="Also allow WebRequest / socket access to 127.0.0.1 for the MQL5 EA",
    )
    args = parser.parse_args()
    path = Path(args.ini)
    try:
        changes = configure(path, ea_sockets=args.ea_sockets)
    except Exception as exc:
        print(f"Failed to configure {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Updated {path} (UTF-16-LE): {', '.join(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
