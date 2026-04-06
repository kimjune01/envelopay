"""AgentMail HTTP layer. urllib only — httpx hangs in Lambda."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class RateLimited(Exception):
    """Raised when AgentMail returns 429."""
    pass


def _request(api_key: str, method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = e.headers.get("retry-after", "?")
            raise RateLimited(f"429 Too Many Requests (retry-after: {retry})")
        raise


def api_get(api_key: str, path: str) -> dict:
    return _request(api_key, "GET", f"https://api.agentmail.to/v0{path}")


def reply_to(api_key: str, inbox: str, message_id: str, to: str,
             text: str, headers: dict | None = None) -> dict:
    """Reply in-thread via POST /messages/{id}/reply."""
    encoded_id = urllib.parse.quote(message_id, safe="")
    url = f"https://api.agentmail.to/v0/inboxes/{inbox}/messages/{encoded_id}/reply"
    body: dict = {"to": [to], "text": text}
    if headers:
        body["headers"] = headers
    return _request(api_key, "POST", url, body)


def send_new(api_key: str, inbox: str, to: str, subject: str,
             text: str, headers: dict | None = None) -> dict:
    """Send a new message (creates a new thread)."""
    url = f"https://api.agentmail.to/v0/inboxes/{inbox}/messages/send"
    body: dict = {"to": [to], "subject": subject, "text": text}
    if headers:
        body["headers"] = headers
    return _request(api_key, "POST", url, body)
