#!/usr/bin/env python3
"""Extract trading signals from Telegram messages and emit curl payloads."""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.ai import create_signal_ai_provider
from src.ai.text_signals import ProcessStatus, payload_to_curl, process_signal_message

SIGNALS_URL = os.environ.get("SIGNALS_URL", "http://localhost:8001/signals")

DEMO_MESSAGES = [
    """
    Pair: XAUUSD

    Type: BUY

    Entry: 4315

    TP1: 4318
    TP2: 4320
    TP3: 4322
    TP4: 4324
    TP5: 4326
    TP6: 4336

    Stop Loss: 4305
    """,
    """
    🔥 GOLD VIP SIGNAL 🔥

    Pair: XAUUSD

    Type: SELL

    Entry: 4290

    TP1: 4285
    TP2: 4280
    TP3: 4275
    TP4: 4270
    TP5: 4265

    Stop Loss: 4300

    Trade safely.
    """,
    """
    Good Morning Traders

    Have a great day.
    """,
    """
    BTC update coming soon
    """,
]


def print_result(result, url: str) -> None:
    print("\n" + "=" * 80)
    print("NEW MESSAGE")
    print("=" * 80)
    print(result.message)

    if result.status == ProcessStatus.SKIPPED:
        print(f"\nSKIPPED ({result.reason})")
        return

    if result.status == ProcessStatus.REJECTED:
        print(f"\nREJECTED: {result.reason}")
        return

    if result.status == ProcessStatus.FAILED:
        print(f"\nFAILED: {result.reason}")
        return

    print(f"\nSUCCESS [{result.signal.method}]")
    print("\nPAYLOAD:")
    print(json.dumps(result.payload, indent=2))
    print("\nCURL COMMAND:")
    print(payload_to_curl(result.payload, url))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract trading signals from Telegram-style messages."
    )
    parser.add_argument(
        "messages",
        nargs="*",
        help="Messages to process. Runs demo messages if omitted.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read messages from stdin (blank line separates messages).",
    )
    parser.add_argument(
        "--signals-url",
        default=SIGNALS_URL,
        help=f"Target signals API URL (default: {SIGNALS_URL}).",
    )
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="Never call AI; only accept regex-parsed signals.",
    )
    parser.add_argument(
        "--ai-provider",
        default=os.environ.get("SIGNAL_AI_PROVIDER", "cerebras"),
        help="AI provider plugin (default: cerebras). Use 'none' to disable.",
    )
    return parser.parse_args()


def read_stdin_messages() -> list[str]:
    raw = sys.stdin.read()
    chunks = [chunk.strip() for chunk in raw.split("\n\n") if chunk.strip()]
    return chunks or ([raw.strip()] if raw.strip() else [])


def main() -> int:
    args = parse_args()

    if args.stdin:
        messages = read_stdin_messages()
    elif args.messages:
        messages = args.messages
    else:
        messages = DEMO_MESSAGES

    if not messages:
        print("No messages to process.", file=sys.stderr)
        return 1

    ai_provider = None
    if not args.deterministic_only:
        ai_provider = create_signal_ai_provider(args.ai_provider)
        if ai_provider is None:
            print(
                "AI provider unavailable (set CEREBRAS_API_KEY / CEREBRAS_API_KEYS or use "
                "--deterministic-only); using regex parsing only.",
                file=sys.stderr,
            )

    for message in messages:
        result = process_signal_message(message, ai_provider=ai_provider)
        print_result(result, args.signals_url)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
