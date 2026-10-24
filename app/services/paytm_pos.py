"""Paytm Wireless POS (ECR) client: sends a bill's exact amount to the Paytm card machine beside the till and asks
Paytm whether it was paid.

Everything here follows Paytm's published API pages (paytmpayments.com/docs): the Sale API (POST /ecr/payment/request),
the Status Enquiry API (POST /ecr/V2/payment/status) and the Void API (POST /ecr/void). Requests are
{"head": {requestTimeStamp, channelId, checksum}, "body": {...}}; the checksum is Paytm's standard signature of the
body string with the merchant key. Amounts go in paise as a string; merchantTransactionId is 8-32 alphanumerics.

What is NOT verified here and must be checked on the real machine: the production host (Paytm's pages show two
different hosts, so it is read from PAYTM_BASE_URL and never guessed), and whether Void also cancels a request the
customer has not paid yet. Nothing in this file simulates the machine accepting a request.
"""

import base64
import hashlib
import json
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.config import settings

# Staging host from Paytm's Sale / Status / Void pages. Production is deliberately not defaulted.
STAGING_HOST = "https://securegw-stage.paytmpayments.in"
SALE_PATH = "/ecr/payment/request"
STATUS_PATH = "/ecr/V2/payment/status"
VOID_PATH = "/ecr/void"

# Paytm asks for the first status enquiry 10 seconds after the sale request, then every 10 seconds.
ENQUIRY_INTERVAL_SECONDS = 10

# Our states for a request. Only SUCCESS lets a bill be settled.
CREATED = "created"  # saved here, Paytm not asked yet (or the call failed before an answer)
SENT = "sent"  # Paytm accepted it and put it on the machine; waiting for the customer
SUCCESS = "success"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"  # a void was accepted; waiting to see it confirmed
CANCELLED = "cancelled"
UNKNOWN = "unknown"  # no answer we can trust: ask Paytm again before any new request
OPEN_STATES = (CREATED, SENT, CANCEL_REQUESTED, UNKNOWN)

IST = timezone(timedelta(hours=5, minutes=30))
_IV = b"@@@@&&&&####$$$$"  # fixed by Paytm's checksum utility
_ALNUM = string.ascii_letters + string.digits


class PaytmError(Exception):
    """A failure the cashier can be told about; `retryable` says whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def is_configured() -> bool:
    return bool(settings.paytm_merchant_key and settings.paytm_channel_id and base_url())


def base_url() -> str:
    if settings.paytm_base_url:
        return settings.paytm_base_url.rstrip("/")
    return STAGING_HOST if settings.paytm_environment != "production" else ""


# --------------------------------------------------------------------------- checksum

def _aes(key: str) -> Cipher:
    raw = key.encode("utf-8")
    if len(raw) not in (16, 24, 32):
        raise PaytmError("The Paytm merchant key is not a valid length (expected 16 characters)")
    return Cipher(algorithms.AES(raw), modes.CBC(_IV))


def _encrypt(text: str, key: str) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode("utf-8")) + padder.finalize()
    enc = _aes(key).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def _decrypt(token: str, key: str) -> str:
    dec = _aes(key).decryptor()
    data = dec.update(base64.b64decode(token)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode("utf-8")


def generate_signature(body: str, key: str, salt: str | None = None) -> str:
    """Paytm's checksum of a JSON body string: AES(sha256(body|salt) + salt) with the merchant key."""
    salt = salt or "".join(secrets.choice(_ALNUM) for _ in range(4))
    digest = hashlib.sha256(f"{body}|{salt}".encode("utf-8")).hexdigest() + salt
    return _encrypt(digest, key)


def verify_signature(body: str, key: str, checksum: str) -> bool:
    try:
        plain = _decrypt(checksum, key)
    except Exception:  # noqa: BLE001 - a wrong key or a damaged value just fails verification
        return False
    salt = plain[-4:]
    return secrets.compare_digest(hashlib.sha256(f"{body}|{salt}".encode("utf-8")).hexdigest() + salt, plain)


# --------------------------------------------------------------------------- request building

def new_merchant_transaction_id() -> str:
    """Unique per request, alphanumeric, 25 characters. The GPPTM prefix lets the bill sync recognise our references."""
    return "GPPTM" + uuid.uuid4().hex[:20]


def to_paise(amount_rupees: Decimal | float | str) -> str:
    paise = (Decimal(str(amount_rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(int(paise))


def now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def build_request(body: dict, *, key: str, channel_id: str, now: str | None = None) -> str:
    """The exact text to POST. The body text that is signed is the same text that is sent."""
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    head = {"requestTimeStamp": now or now_ist(), "channelId": channel_id, "checksum": generate_signature(body_text, key)}
    return '{"head":' + json.dumps(head, separators=(",", ":")) + ',"body":' + body_text + "}"


def sale_body(*, mid: str, tid: str, merchant_txn_id: str, amount_rupees: Decimal | float, reference: str | None, when: str | None = None) -> dict:
    body = {
        "paytmMid": mid,
        "paytmTid": tid,
        "transactionDateTime": when or now_ist(),
        "merchantTransactionId": merchant_txn_id,
        "transactionAmount": to_paise(amount_rupees),
    }
    if reference:
        body["merchantReferenceNo"] = reference[:32]
    return body


def lookup_body(*, mid: str, tid: str, merchant_txn_id: str, when: str, void: bool = False) -> dict:
    body = {"paytmMid": mid, "paytmTid": tid, "transactionDateTime": when, "merchantTransactionId": merchant_txn_id}
    if void:
        body["event"] = "VOID"  # asks for the status of the void, not of the sale
    return body


# --------------------------------------------------------------------------- answers

# Paytm result codes (Sale, Status Enquiry and Void pages)
_STILL_OPEN_CODES = {"0010", "0030", "0009"}  # on the machine / accepted
_REFUSED_CODES = {
    "0330": "Paytm rejected the request signature - check the merchant key",
    "0007": "The Paytm machine is not active",
    "0333": "The machine already has a payment waiting. Finish or cancel it first",
    "0002": "Paytm did not accept the request details (check the MID and TID)",
    "0022": "This Paytm merchant is blocked",
    "0902": "This Paytm merchant is blocked",
    "9001": "Paytm POS is not switched on for this merchant (merchant configuration error)",
    "0233": "Paytm says this payment request already exists",
    "0029": "Paytm could not find this merchant or machine",
    "0012": "Paytm had an internal error. Try again",
}


def result_of(response: dict) -> dict:
    info = (response.get("body") or {}).get("resultInfo") or {}
    return {
        "status": str(info.get("resultStatus") or ""),
        "code": str(info.get("resultCodeId") or info.get("resultCode") or ""),
        "message": str(info.get("resultMsg") or ""),
    }


def read_sale_answer(response: dict) -> tuple[str, str]:
    """(state, message) from the answer to the Sale request: SENT when the machine has it, FAILED when Paytm refused."""
    r = result_of(response)
    if r["status"] == "A" or r["code"] == "0009":
        return SENT, "Sent to the Paytm machine"
    if r["code"] == "0012":  # their error: nothing reached the machine, safe to retry only after a status check
        return UNKNOWN, _REFUSED_CODES["0012"]
    return FAILED, _REFUSED_CODES.get(r["code"]) or r["message"] or "Paytm did not accept the request"


def read_status_answer(response: dict, *, void: bool = False) -> tuple[str, str]:
    """(state, message) from the Status Enquiry answer. Only resultStatus S / code 0000 counts as paid."""
    r = result_of(response)
    if void:
        if r["status"] == "S" or r["code"] == "0000":
            return CANCELLED, "Cancelled"
        if r["code"] in ("0090", "0180"):
            return FAILED, r["message"] or "The cancel did not go through"
        return CANCEL_REQUESTED, "Waiting for the cancel to finish"
    if r["status"] == "S" or r["code"] == "0000":
        return SUCCESS, "Paid"
    # These come back marked FAIL but say nothing about the payment itself, so they must never be read as "not paid"
    if r["code"] == "0404":
        return UNKNOWN, "Paytm has no record of this request yet"
    if r["code"] in ("0330", "0012"):
        return UNKNOWN, _REFUSED_CODES[r["code"]]
    if r["status"] == "F" or r["code"] == "0011":
        return FAILED, r["message"] or "The payment failed"
    if r["status"] in ("A", "U") or r["code"] in _STILL_OPEN_CODES:
        return SENT, "Waiting for the customer to pay on the machine"
    return UNKNOWN, r["message"] or "Paytm gave an answer that could not be read"


def paid_details(response: dict) -> dict:
    """The bank-side references Paytm returns once paid, for the record and the receipt."""
    body = response.get("body") or {}
    keys = ("acquirementId", "retrievalReferenceNo", "authCode", "payMethod", "cardScheme", "issuerMaskCardNo", "bankResponseCode", "transactionAmount")
    return {k: body[k] for k in keys if body.get(k) not in (None, "")}


def amount_matches(response: dict, amount_rupees: Decimal | float) -> bool:
    """A paid answer must carry the amount we asked for; a different amount is never accepted as paid."""
    paid = (response.get("body") or {}).get("transactionAmount")
    return paid is not None and str(paid) == to_paise(amount_rupees)


# --------------------------------------------------------------------------- calls

async def _post(path: str, text: str) -> dict:
    url = base_url()
    if not url:
        raise PaytmError("Paytm production host is not set (PAYTM_BASE_URL)")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url + path, content=text.encode("utf-8"), headers={"Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise PaytmError("Could not reach Paytm", retryable=True) from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise PaytmError(f"Paytm sent an unreadable answer (HTTP {resp.status_code})", retryable=True) from exc
    if resp.status_code >= 500:
        raise PaytmError(f"Paytm is not available right now (HTTP {resp.status_code})", retryable=True)
    return data


def _signed(body: dict) -> str:
    return build_request(body, key=settings.paytm_merchant_key, channel_id=settings.paytm_channel_id)


async def send_sale(body: dict) -> dict:
    return await _post(SALE_PATH, _signed(body))


async def enquire(body: dict) -> dict:
    return await _post(STATUS_PATH, _signed(body))


async def send_void(body: dict) -> dict:
    return await _post(VOID_PATH, _signed(body))
