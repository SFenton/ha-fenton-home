"""The APNs client: provider tokens, request shape and error handling, with a fake HTTP/2 client."""

from __future__ import annotations

import asyncio

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fenton_home.apns import (
    PROBE_DEVICE_TOKEN,
    SANDBOX_HOST,
    TOKEN_LIFETIME_SECONDS,
    APNsClient,
    APNsError,
    Credentials,
    load_key,
)
from fenton_home.payload import ApnsRequest


def pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


KEY = ec.generate_private_key(ec.SECP256R1())
CREDS = Credentials(key_pem=pem(KEY), key_id="ABC123DEFG", team_id="TEAM123456", bundle_id="com.sfenton.home")


class Response:
    def __init__(self, status: int, body: dict | None = None, bad_json: bool = False) -> None:
        self.status_code = status
        self._body = body or {}
        self._bad_json = bad_json

    def json(self) -> dict:
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class FakeHTTP:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, bytes, dict]] = []

    async def post(self, url, content, headers, timeout):
        self.calls.append((url, content, headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(coro):
    return asyncio.run(coro)


def test_load_key_refuses_other_keys() -> None:
    assert load_key(pem(KEY)).curve.name == "secp256r1"
    with pytest.raises(ValueError):
        load_key("not a key")
    with pytest.raises(ValueError):
        load_key(pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
    with pytest.raises(ValueError):
        load_key(pem(ec.generate_private_key(ec.SECP384R1())))


def test_provider_token_is_signed_and_cached() -> None:
    client = APNsClient(CREDS, FakeHTTP())
    token = client.provider_token(now=1_000)
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "ABC123DEFG", "typ": "JWT"}
    claims = jwt.decode(token, KEY.public_key(), algorithms=["ES256"])
    assert claims == {"iss": "TEAM123456", "iat": 1_000}
    assert client.provider_token(now=1_000 + TOKEN_LIFETIME_SECONDS - 1) == token
    assert client.provider_token(now=1_000 + TOKEN_LIFETIME_SECONDS) != token


def test_send_posts_to_the_environment_with_headers() -> None:
    http = FakeHTTP(Response(200))
    client = APNsClient(CREDS, http)
    request = ApnsRequest(push_type="alert", priority=10, payload={"aps": {"alert": {"body": "hi"}}}, collapse_id="t")
    run(client.send("ab" * 32, request, "sandbox"))
    url, body, headers = http.calls[0]
    assert url == f"{SANDBOX_HOST}/3/device/{'ab' * 32}"
    assert body == b'{"aps":{"alert":{"body":"hi"}}}'
    assert headers["apns-topic"] == "com.sfenton.home" and headers["apns-collapse-id"] == "t"
    assert headers["authorization"].startswith("bearer ")


def test_errors_carry_apples_reason() -> None:
    client = APNsClient(CREDS, FakeHTTP(Response(410, {"reason": "Unregistered"}), Response(500, bad_json=True),
                                        OSError("down")))
    request = ApnsRequest(push_type="alert", priority=10, payload={})
    with pytest.raises(APNsError) as gone:
        run(client.send("t", request))
    assert gone.value.dead_token and gone.value.status == 410
    with pytest.raises(APNsError) as unknown:
        run(client.send("t", request))
    assert unknown.value.reason == "Unknown" and not unknown.value.dead_token
    with pytest.raises(APNsError) as offline:
        run(client.send("t", request))
    assert offline.value.status == 0 and offline.value.reason == "OSError"


def test_expired_provider_token_is_reissued_next_time() -> None:
    http = FakeHTTP(Response(403, {"reason": "ExpiredProviderToken"}), Response(200))
    client = APNsClient(CREDS, http)
    request = ApnsRequest(push_type="alert", priority=10, payload={})
    with pytest.raises(APNsError) as err:
        run(client.send("t", request))
    assert err.value.bad_credentials
    assert client._token is None
    run(client.send("t", request))
    assert client._token is not None


def test_verify_accepts_bad_device_token_only() -> None:
    http = FakeHTTP(Response(400, {"reason": "BadDeviceToken"}))
    run(APNsClient(CREDS, http).verify())
    assert http.calls[0][0].endswith(PROBE_DEVICE_TOKEN)
    with pytest.raises(APNsError) as err:
        run(APNsClient(CREDS, FakeHTTP(Response(403, {"reason": "InvalidProviderToken"}))).verify())
    assert err.value.bad_credentials
