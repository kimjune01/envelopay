"""AgentMail webhook handler for the envelopay demo agent.

Receives incoming emails via webhook, verifies payment proofs,
does work, and replies. Deploy as a Lambda behind API Gateway,
or run locally with ngrok for testing.

Usage (local):
    pip install agentmail flask
    export AGENTMAIL_API_KEY="am_..."
    export SOLANA_PRIVATE_KEY="..."  # axiomatic's private key
    python webhook_handler.py

Usage (Lambda):
    Set AGENTMAIL_API_KEY and SOLANA_PRIVATE_KEY as env vars.
    Point API Gateway to the lambda_handler function.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request

from agentmail import AgentMail

INBOX = "axiomatic@agentmail.to"
WALLET = "BhNb354sCVmuUSm1DvYANtEJwCtDMs4ARHo3FchPBYtY"
MAINNET_RPC = "https://api.mainnet-beta.solana.com"
MAX_LAMPORTS = 100_000  # 0.0001 SOL — about one penny
KNOWN_TYPES = {"WHICH", "METHODS", "PAY", "ORDER", "FULFILL", "INVOICE", "OFFER", "ACCEPT", "OOPS"}
PROTOCOL_RE = re.compile(r"^([A-Z]+)(\s*\|.*)?$")
RE_PREFIX = re.compile(r"^(Re:\s*)+", re.IGNORECASE)


def process_email(payload: dict) -> None:
    """Process an incoming email from the webhook."""
    client = AgentMail()

    message = payload.get("message", {})
    from_addr = message.get("from_", "")
    subject = message.get("subject", "")
    inbox_id = message.get("inbox_id", INBOX)
    thread_id = message.get("thread_id", "")
    msg_id = message.get("message_id", "")

    # Verify DKIM on the raw email
    dkim_ok = _verify_dkim_via_raw(client, inbox_id, msg_id)
    if not dkim_ok:
        print(f"DKIM verification failed for {msg_id} from {from_addr}")
        # Continue processing but log the failure

    # Skip our own sent messages
    if INBOX in from_addr:
        return

    # Protocol mismatch — subject looks like a type keyword but isn't one we know
    match = PROTOCOL_RE.match(RE_PREFIX.sub("", subject.strip()).strip())
    if match and match.group(1) not in KNOWN_TYPES:
        types = " | ".join(sorted(KNOWN_TYPES))
        _oops(client, inbox_id, thread_id,
              f"Unknown type: {match.group(1)}",
              {"code": "unknown_type",
               "sent": match.group(1),
               "supported": sorted(KNOWN_TYPES),
               "spec": "https://june.kim/certified-mail"})
        return

    # WHICH — reply with METHODS
    if subject.strip().upper() == "WHICH":
        terms = {
            "v": "0.1.0",
            "type": "methods",
            "note": "Solana, SOL",
            "rails": [
                {"chain": "solana", "token": "SOL", "wallet": WALLET},
            ],
        }
        note = terms["note"]
        _reply(client, inbox_id, thread_id,
               subject=f"METHODS | {note}",
               text=json.dumps(terms, indent=2),
               headers={"X-Envelopay-Type": "METHODS"})
        return

    # INVOICE — send money to the requester's wallet
    if subject.strip().upper() == "INVOICE":
        invoice = _parse_json_from_text(message.get("text", "") or "")
        wallet = invoice.get("wallet", "") if invoice else ""
        if wallet and len(wallet) >= 32 and len(wallet) <= 44:
            # Send whatever we can, up to the cap
            refund = _refund(wallet, min(_get_balance(), MAX_LAMPORTS))
            if refund:
                result = {
                    "v": "0.1.0",
                    "type": "fulfill",
                    "note": f"Refund sent: {refund.get('amount', 0)} lamports",
                    "agent": INBOX,
                    "refund": refund,
                }
                note = result["note"]
                _reply(client, inbox_id, thread_id,
                       subject=f"FULFILL | {note}",
                       text=json.dumps(result, indent=2),
                       headers={"X-Envelopay-Type": "FULFILL"})
            else:
                _oops(client, inbox_id, thread_id,
                      "Insufficient funds",
                      {"code": "insufficient_funds"})
        else:
            _oops(client, inbox_id, thread_id,
                  "Missing wallet address",
                  {"code": "missing_wallet", "expected": '{"wallet": "your_address"}'})
        return

    # ORDER (or any unhandled message) — reply with INVOICE (worker sets the price)
    text = message.get("text", "") or ""
    body_json = _parse_json_from_text(text)
    task_desc = body_json.get("task", {}).get("description", "") if isinstance(body_json.get("task"), dict) else body_json.get("note", subject)
    order_id = body_json.get("id", "")

    invoice = {
        "v": "0.1.0",
        "type": "invoice",
        "note": f"Invoice for: {task_desc}",
        "amount": "50000",
        "token": "SOL",
        "chain": "solana",
        "wallet": WALLET,
    }
    if order_id:
        invoice["order_ref"] = order_id

    _reply(client, inbox_id, thread_id,
           subject=f"INVOICE | {invoice['note']}",
           text=json.dumps(invoice, indent=2),
           headers={"X-Envelopay-Type": "INVOICE"})


def _extract_task_and_payment(client: AgentMail, inbox_id: str, message: dict) -> tuple[dict, dict]:
    """Extract task and payment from an envelopay email."""
    task = {}
    payment = {}

    # Check for envelopay.json attachment
    attachments = message.get("attachments", []) or []
    for att in attachments:
        if att.get("filename", "") == "envelopay.json" or att.get("content_type", "") == "application/json":
            try:
                # Fetch attachment content
                msg_id = message.get("message_id", "")
                att_id = att.get("attachment_id", "")
                if msg_id and att_id:
                    content = client.inboxes.messages.attachments.get(
                        inbox_id=inbox_id, message_id=msg_id, attachment_id=att_id
                    )
                    data = json.loads(content) if isinstance(content, str) else content
                    task = data.get("task", data)
                    payment = data.get("proof", data.get("payment", {}))
            except Exception:
                pass

    # Fallback: try parsing the text body as JSON
    if not task:
        text = message.get("text", "") or ""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    task = data.get("task", data)
                    payment = data.get("proof", data.get("payment", {}))
                except json.JSONDecodeError:
                    pass

    return task, payment


def _parse_json_from_text(text: str) -> dict:
    """Try to parse JSON from a text body."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    # Try the whole body as JSON
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return {}


def _verify_dkim_via_raw(client: AgentMail, inbox_id: str, message_id: str) -> bool:
    """Fetch raw .eml from AgentMail and verify DKIM signature."""
    if not message_id:
        return False
    try:
        import dkim
        raw_resp = client.inboxes.messages.get_raw(inbox_id=inbox_id, message_id=message_id)
        req = urllib.request.Request(raw_resp.download_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_bytes = resp.read()
        return dkim.verify(raw_bytes)
    except ImportError:
        print("dkimpy not installed — skipping DKIM verification")
        return False
    except Exception as e:
        print(f"DKIM verification error: {e}")
        return False


def _get_balance() -> int:
    """Get axiomatic's SOL balance in lamports."""
    try:
        result = _rpc("getBalance", [WALLET])
        return result.get("result", {}).get("value", 0)
    except Exception:
        return 0


def _rpc(method: str, params: list) -> dict:
    """Make a Solana RPC call."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(
        MAINNET_RPC, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _verify_payment(payment: dict) -> dict | None:
    """Verify a payment proof on Solana. Returns settlement dict with sender info."""
    tx_hash = payment.get("tx", "")
    if not tx_hash:
        return None

    try:
        data = _rpc("getTransaction", [
            tx_hash,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ])
        result = data.get("result")
        if not result:
            return None
        if result.get("meta", {}).get("err") is not None:
            return None

        # Extract sender and lamports from the transaction
        sender = ""
        lamports = 0
        for ix in result.get("transaction", {}).get("message", {}).get("instructions", []):
            parsed = ix.get("parsed", {})
            if isinstance(parsed, dict) and parsed.get("type") == "transfer":
                info = parsed.get("info", {})
                sender = info.get("source", "")
                lamports = info.get("lamports", 0)
                break

        return {
            "tx": tx_hash,
            "verified": True,
            "block": result.get("slot", 0),
            "sender": sender,
            "lamports": lamports,
        }
    except Exception:
        return None


def _refund(sender_pubkey: str, lamports: int) -> dict | None:
    """Send SOL back to the sender. Returns refund tx info or None."""
    private_key = os.environ.get("SOLANA_PRIVATE_KEY", "")
    if not private_key or not sender_pubkey or lamports <= 0:
        return None

    try:
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash

        kp = Keypair.from_base58_string(private_key)
        to = Pubkey.from_string(sender_pubkey)

        # Reserve 5000 lamports for tx fee
        refund_amount = lamports - 5000
        if refund_amount <= 0:
            return None

        bh_result = _rpc("getLatestBlockhash", [{"commitment": "finalized"}])
        blockhash = bh_result["result"]["value"]["blockhash"]

        ix = transfer(TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=to,
            lamports=refund_amount,
        ))
        msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(blockhash))
        tx = Transaction.new_unsigned(msg)
        tx.sign([kp], Hash.from_string(blockhash))

        encoded = base64.b64encode(bytes(tx)).decode()
        result = _rpc("sendTransaction", [encoded, {"encoding": "base64"}])

        if "error" in result:
            print(f"Refund failed: {result['error']}")
            return None

        return {
            "tx": result["result"],
            "amount": refund_amount,
            "to": sender_pubkey,
        }
    except Exception as e:
        print(f"Refund error: {e}")
        return None


def _do_work(task: dict, settlement: dict) -> dict:
    """Do the work. Whatever it is."""
    task_type = task.get("task", task.get("description", "unknown")) if isinstance(task, dict) else str(task)

    result = {
        "v": "0.1.0",
        "type": "fulfill",
        "note": f"Completed: {task_type}",
        "result": f"Completed: {task_type}",
        "agent": INBOX,
        "wallet": WALLET,
        "deliverable": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "settlement": settlement,
    }

    # Refund the sender (minus tx fee), capped
    refund = _refund(settlement.get("sender", ""), min(settlement.get("lamports", 0), MAX_LAMPORTS))
    if refund:
        result["refund"] = refund

    return result


def _oops(client: AgentMail, inbox_id: str, thread_id: str,
          note: str, error: dict | None = None) -> None:
    """Send an OOPS reply — something went wrong."""
    body = {"v": "0.1.0", "type": "oops", "note": note}
    if error:
        body["error"] = error
    _reply(client, inbox_id, thread_id,
           subject=f"OOPS | {note}",
           text=json.dumps(body, indent=2),
           headers={"X-Envelopay-Type": "OOPS"})


def _reply(client: AgentMail, inbox_id: str, thread_id: str,
           subject: str, text: str, headers: dict | None = None) -> None:
    """Send a reply via AgentMail."""
    try:
        client.inboxes.threads.reply(
            inbox_id=inbox_id,
            thread_id=thread_id,
            text=text,
            subject=subject,
            headers=headers or {},
        )
    except Exception as e:
        print(f"Reply failed: {e}")


# --- Lambda handler ---

def lambda_handler(event, context):
    """AWS Lambda entry point."""
    body = json.loads(event.get("body", "{}"))
    if body.get("event_type") == "message.received":
        process_email(body)
    return {"statusCode": 200}


# --- Local Flask server ---

if __name__ == "__main__":
    from flask import Flask, request, Response
    app = Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook():
        payload = request.json
        if payload.get("event_type") == "message.received":
            process_email(payload)
        return Response(status=200)

    print(f"Envelopay agent listening on http://localhost:3000/webhook")
    print(f"Inbox: {INBOX}")
    print(f"Wallet: {WALLET}")
    app.run(port=3000)
