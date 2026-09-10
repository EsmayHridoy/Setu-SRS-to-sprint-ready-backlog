"""Encryption for repository access tokens.

Tokens are written encrypted and never returned by the API. Only the last four
characters are exposed, so an admin can recognise which token is stored without
being able to read it back.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_DEV_KEY_WARNING = (
    "SETU_SECRET_KEY is not set. Falling back to a development key. "
    "Set a real key before storing any production token."
)


def _fernet() -> Fernet:
    raw = get_settings().secret_key
    if not raw:
        raw = "setu-development-key-do-not-use-in-production"
    # Accept either a proper Fernet key or any passphrase.
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def dev_key_in_use() -> bool:
    return not get_settings().secret_key
