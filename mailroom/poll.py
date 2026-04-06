"""Poll loop and Lambda entry point generator."""

from __future__ import annotations

import time

from mailroom import Agent, Context, PROTOCOL_RE, RE_PREFIX
from mailroom.api import RateLimited, api_get


def poll_once(agent: Agent, max_threads: int = 50) -> int:
    """Check inbox for unanswered threads. Returns count processed."""
    data = api_get(agent.api_key, f"/inboxes/{agent.inbox}/threads")
    threads = data.get("threads", [])

    processed = 0
    checked = 0
    for t in threads:
        tid = t.get("thread_id", "")
        checked += 1
        if checked > max_threads:
            print(f"Hit {max_threads}-thread cap, stopping early")
            break

        tdata = api_get(agent.api_key, f"/inboxes/{agent.inbox}/threads/{tid}")
        messages = tdata.get("messages", [])
        if not messages:
            continue

        last = messages[-1]

        # Skip if the last message is from us (already responded)
        last_from = last.get("from_", "") or last.get("from", "") or ""
        if agent.inbox in last_from:
            continue
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
