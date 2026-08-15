import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mmbot.venues.perpl.auth import ApiCredentials, load_private_key


SEED = bytes(range(32))


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_load_private_key_hex_and_base64():
    hex_key = load_private_key(SEED.hex())
    b64_key = load_private_key(base64.b64encode(SEED).decode())
    b64url_key = load_private_key(base64.urlsafe_b64encode(SEED).decode().rstrip("="))
    prefixed = load_private_key("0x" + SEED.hex())
    pub = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes_raw()
    for key in (hex_key, b64_key, b64url_key, prefixed):
        assert key.public_key().public_bytes_raw() == pub


def test_rest_headers_signature_verifies():
    creds = ApiCredentials(api_key="k", private_key=load_private_key(SEED.hex()))
    body = b'{"x":1}'
    headers = creds.rest_headers(10143, "POST", "/api/v1/thing?a=b", body)
    canonical = "\n".join(
        [
            "10143",
            "POST",
            "/api/v1/thing?a=b",
            headers["X-API-Timestamp"],
            headers["X-API-Nonce"],
            hashlib.sha256(body).hexdigest(),
        ]
    )
    pub = creds.private_key.public_key()
    pub.verify(_b64url_decode(headers["X-API-Signature"]), canonical.encode())
    assert headers["X-API-Key"] == "k"


def test_ws_sign_in_frame_verifies():
    creds = ApiCredentials(api_key="k", private_key=load_private_key(SEED.hex()))
    frame = creds.ws_sign_in_frame(143)
    assert frame["mt"] == 29 and frame["chain_id"] == 143
    canonical = "\n".join(["143", "trading-ws-signin", frame["timestamp"], frame["nonce"]])
    creds.private_key.public_key().verify(
        _b64url_decode(frame["signature"]), canonical.encode()
    )
    assert len(_b64url_decode(frame["nonce"])) == 16
