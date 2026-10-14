"""Envelope encryption shared with the browser (WebCrypto):
PBKDF2-HMAC-SHA256 (salt + iterations from site/crypto.json) -> AES-256-GCM, plaintext gzipped JSON.
Envelope: {"v":1,"iv":b64,"ct":b64}; the salt/iterations live in crypto.json so one derived key opens both
data.enc (written here) and state.enc (written by the page).
"""
from __future__ import annotations

import base64
import gzip
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def derive_key(passphrase: str, salt_b64: str, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(salt_b64), iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def _np(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o).__name__)


def encrypt_json(obj, key: bytes) -> str:
    iv = os.urandom(12)
    pt = gzip.compress(json.dumps(obj, separators=(",", ":"), allow_nan=False, default=_np).encode("utf-8"), mtime=0)
    ct = AESGCM(key).encrypt(iv, pt, None)
    return json.dumps({"v": 1, "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()})


def decrypt_json(envelope: str, key: bytes):
    env = json.loads(envelope)
    pt = AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None)
    return json.loads(gzip.decompress(pt).decode("utf-8"))
