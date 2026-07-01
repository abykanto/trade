"""Identify orders placed by this trade manager on MT5."""

from __future__ import annotations

BOT_ORDER_COMMENT_PREFIX = "TradeIdeaBot"


def is_bot_order_comment(comment: str | None) -> bool:
    return (comment or "").strip().startswith(BOT_ORDER_COMMENT_PREFIX)


def is_bot_placed_order(*, magic: int, bot_magic: int, comment: str | None) -> bool:
    return magic == bot_magic and is_bot_order_comment(comment)
