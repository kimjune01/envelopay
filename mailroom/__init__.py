"""Mailroom — shared agent framework for Envelopay email agents.

Define an agent with an inbox and handler map. The framework owns
polling, protocol parsing, Re: stripping, reply threading, rate
limit backoff, and Lambda entry points.

Usage:
    from mailroom import Agent, Context

    agent = Agent(inbox="blader@agentmail.to")

    @agent.on("WHICH")
    def handle_which(ctx: Context):
        ctx.reply("Here are my blades...")

    # Lambda
    from mailroom.poll import make_lambda_handler
    lambda_handler = make_lambda_handler(agent)

    # Local dev
    from mailroom.poll import run_forever
    run_forever(agent)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable

from mailroom.api import reply_to, send_new

logger = logging.getLogger(__name__)

PROTOCOL_RE = re.compile(
    r"^(WHICH|METHODS|PAY|ORDER|FULFILL|INVOICE|OFFER|ACCEPT|OOPS)(\s*\|.*)?$",
    re.IGNORECASE,
)
RE_PREFIX = re.compile(r"^(Re:\s*)+", re.IGNORECASE)
DEFAULT_TERMINAL = {"METHODS", "FULFILL", "OOPS", "PAY", "ACCEPT"}


@dataclass
class Context:
    """Passed to every handler. Call ctx.reply() to respond in-thread."""

    from_addr: str
    subject: str       # Re:-stripped
    text: str
    message_id: str
    thread_id: str
    _api_key: str = field(repr=False)
    _inbox: str = field(repr=False)

    def reply(self, text: str, subject: str | None = None,
              headers: dict | None = None) -> None:
        full_text = f"{subject}\n\n{text}" if subject else text
        if self.message_id:
            reply_to(self._api_key, self._inbox, self.message_id,
                     self.from_addr, full_text, headers)
        else:
            logger.warning("No message_id — sending as new thread: %s", subject)
            send_new(self._api_key, self._inbox, self.from_addr,
                     subject or "", full_text, headers)


Handler = Callable[[Context], None]


class Agent:
    """An email agent defined by inbox + handler map."""

    def __init__(self, inbox: str, api_key: str | None = None,
                 handlers: dict[str, Handler] | None = None,
                 on_no_match: Handler | None = None,
                 terminal_types: set[str] | None = None):
        self.inbox = inbox
        self.api_key = api_key or os.environ.get("AGENTMAIL_API_KEY", "")
        self.handlers: dict[str, Handler] = handlers or {}
        self.on_no_match = on_no_match
        self.terminal_types = terminal_types or DEFAULT_TERMINAL

    def on(self, msg_type: str) -> Callable[[Handler], Handler]:
        """Decorator to register a handler: @agent.on("WHICH")"""
        def decorator(fn: Handler) -> Handler:
            self.handlers[msg_type.upper()] = fn
            return fn
        return decorator
