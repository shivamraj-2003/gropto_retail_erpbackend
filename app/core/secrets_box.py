"""Encrypts small secrets (a store's Paytm merchant key) before they are saved in the database.

The key is derived from SECRETS_KEY, or from JWT_SECRET when SECRETS_KEY is not set. Set SECRETS_KEY once and never change
it: a value saved under one key cannot be read under another (the store's key would have to be entered again).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    secret = settings.secrets_key or settings.jwt_secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    """The original text, or None when there is nothing saved or it cannot be read with the current key."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
