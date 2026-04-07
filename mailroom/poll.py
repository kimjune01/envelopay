"""Poll loop and Lambda entry point generator."""

from __future__ import annotations

import threading
import time

from mailroom import Agent, Context, PROTOCOL_RE, RE_PREFIX
from mailroom.api import RateLimited, api_get

_processing_threads: set[str] = set()
_lock = threading.Lock()


def _last_inbound_index(messages: list[dict], inbox: str) -> int:
    """Return the index of the last message NOT from us, or -1."""
    for i in range(len(messages) - 1, -1, -1):
        sender = messages[i].get("from_", "") or messages[i].get("from", "") or ""
        if inbox not in sender:
            return i
    return -1


def poll_once(agent: Agent, max_threads: int = 50) -> int:
    """Check inbox for unanswered threads. Returns count processed."""
    data = api_get(agent.api_key, f"/inboxes/{agent.inbox}/threads")
    threads = data.get("threads", [])

    # First pass: filter threads using listing metadata (no extra API calls)
    needs_processing = []
    for t in threads[:max_threads]:
        # Skip threads where we've replied after the last inbound
        sent = t.get("sent_timestamp") or ""
        received = t.get("received_timestamp") or ""
        if sent and received and sent >= received:
            continue
        # No sent_timestamp means we've never replied — process it
        needs_processing.append(t)

    processed = 0
    for t in needs_processing:
        tid = t.get("thread_id", "")

        with _lock:
            if tid in _processing_threads:
                continue
            _processing_threads.add(tid)

        try:
            tdata = api_get(agent.api_key, f"/inboxes/{agent.inbox}/threads/{tid}")
            messages = tdata.get("messages", [])
            if not messages:
                continue

            # Find the last inbound message
            last_inbound_idx = _last_inbound_index(messages, agent.inbox)
            if last_inbound_idx < 0:
                continue

            # Skip if any of our replies appear after the last inbound message
            if last_inbound_idx < len(messages) - 1:
                continue

            last = messages[last_inbound_idx]
            from_addr = last.get("from_", "") or last.get("from", "") or ""
            msg_id = last.get("message_id", "") or last.get("id", "")

            raw_subject = (last.get("subject", "") or "").strip()
            subject = RE_PREFIX.sub("", raw_subject).strip()
            text = last.get("text", "") or ""

            match = PROTOCOL_RE.match(subject)
            msg_type = match.group(1).upper() if match else None

            if msg_type in agent.terminal_types:
                continue

            ctx = Context(
                from_addr=from_addr,
                subject=subject,
                text=text,
                message_id=msg_id,
                thread_id=tid,
                _api_key=agent.api_key,
                _inbox=agent.inbox,
            )

            print(f"Processing: {subject} from {from_addr}")

            try:
                if msg_type and msg_type in agent.handlers:
                    agent.handlers[msg_type](ctx)
                elif agent.on_no_match:
                    agent.on_no_match(ctx)
                else:
                    continue
            except RateLimited as e:
                print(f"Rate limited, stopping: {e}")
                break
            except Exception as e:
                print(f"Error processing {msg_id}: {e}")
                continue

            processed += 1
        finally:
            with _lock:
                _processing_threads.discard(tid)

    print(f"Poll complete: {processed} messages processed")
    return processed


def run_forever(agent: Agent, interval: int = 15) -> None:
    """Standalone dev mode. Polls in a loop."""
    print(f"Mailroom agent: {agent.inbox}")
    print(f"Handlers: {', '.join(agent.handlers.keys())}")
    print(f"Polling every {interval}s\n")

    while True:
        try:
            poll_once(agent)
        except KeyboardInterrupt:
            print("\nShutting down.")
            break
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(interval)


def make_lambda_handler(agent: Agent):
    """Return a lambda_handler(event, context) function."""
    def lambda_handler(event, context):
        poll_once(agent)
        return {"statusCode": 200}
    return lambda_handler
