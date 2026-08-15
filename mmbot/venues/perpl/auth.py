"""Ed25519 request signing for the Perpl API.

REST canonical string (six lines):   chain_id \n METHOD \n target \n ts \n nonce \n sha256(body)hex
Trading-WS canonical (four lines):   chain_id \n "trading-ws-signin" \n ts \n nonce

Signatures and nonces are base64url without padding, per
PerplFoundation/api-docs authentication.md.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_private_key(secret: str) -> Ed25519PrivateKey:
    """Load a 32-byte Ed25519 seed given as hex or base64/base64url."""
    secret = secret.strip()
    raw: bytes | None = None
    candidate = secret.removeprefix("0x")
    try:
        decoded = bytes.fromhex(candidate)
        if len(decoded) == 32:
            raw = decoded
    except ValueError:
        pass
    if raw is None:
        padded = secret.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        try:
            decoded = base64.b64decode(padded)
            if len(decoded) == 32:
                raw = decoded
        except ValueError:
            pass
    if raw is None:
        raise ValueError(
            "PERPL_API_PRIVATE_KEY must be a 32-byte Ed25519 seed in hex or base64"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


@dataclass
class ApiCredentials:
    api_key: str
    private_key: Ed25519PrivateKey

    @classmethod
    def from_env(cls) -> "ApiCredentials":
        api_key = os.environ.get("PERPL_API_KEY", "")
        secret = os.environ.get("PERPL_API_PRIVATE_KEY", "")
        if not api_key or not secret:
            raise RuntimeError(
                "Set PERPL_API_KEY and PERPL_API_PRIVATE_KEY in the environment "
                "(create a key at https://app.perpl.xyz/apikeys)"
            )
        return cls(api_key=api_key, private_key=load_private_key(secret))

    def _sign(self, canonical: str) -> str:
        return _b64url(self.private_key.sign(canonical.encode("utf-8")))

    def rest_headers(
        self, chain_id: int, method: str, target: str, body: bytes = b""
    ) -> dict[str, str]:
        """Signed headers for a REST request. `target` is path + query string."""
        timestamp = str(int(time.time() * 1000))
        nonce = _b64url(os.urandom(16))
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            [str(chain_id), method.upper(), target, timestamp, nonce, body_hash]
        )
        return {
            "X-API-Key": self.api_key,
            "X-API-Timestamp": timestamp,
            "X-API-Nonce": nonce,
            "X-API-Signature": self._sign(canonical),
        }

    def ws_sign_in_frame(self, chain_id: int) -> dict:
        """ApiKeySignIn frame (mt 29) — must be the first frame on /ws/v1/trading."""
        timestamp = str(int(time.time() * 1000))
        nonce = _b64url(os.urandom(16))
        canonical = "\n".join([str(chain_id), "trading-ws-signin", timestamp, nonce])
        return {
            "mt": 29,
            "chain_id": chain_id,
            "api_key": self.api_key,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": self._sign(canonical),
        }
