"""Apple Push Notification service client: token (.p8) authentication over HTTP/2."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .payload import ApnsRequest

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
ENVIRONMENTS = ("production", "sandbox")

#: Apple accepts a provider token for up to an hour and throttles refreshes more often than every 20 minutes.
TOKEN_LIFETIME_SECONDS = 50 * 60
#: A well-formed token no device has. Apple answers ``BadDeviceToken`` for it once the key is accepted.
PROBE_DEVICE_TOKEN = "0" * 64

#: Reasons that mean this device token is finished (the app must hand out a new one).
DEAD_TOKEN_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic", "ExpiredToken"})
#: Reasons that mean the key, team or bundle is wrong for every device.
CREDENTIAL_REASONS = frozenset({
    "InvalidProviderToken", "MissingProviderToken", "ExpiredProviderToken", "TopicDisallowed", "BadTopic",
    "BadCertificate", "BadCertificateEnvironment",
})


class APNsError(Exception):
    """Apple refused the request, or it never got there (``status`` 0)."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"APNs {status}: {reason}")
        self.status = status
        self.reason = reason

    @property
    def dead_token(self) -> bool:
        return self.status == 410 or self.reason in DEAD_TOKEN_REASONS

    @property
    def bad_credentials(self) -> bool:
        return self.reason in CREDENTIAL_REASONS


def load_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """Parse an APNs auth key (.p8 contents). Raises ``ValueError`` when it isn't a P-256 private key."""
    try:
        key = load_pem_private_key(pem.strip().encode(), password=None)
    except (TypeError, ValueError) as err:
        raise ValueError("not_a_key") from err
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("not_a_key")
    return key


@dataclass(frozen=True)
class Credentials:
    """What Apple needs to accept our pushes."""

    key_pem: str
    key_id: str
    team_id: str
    bundle_id: str


class APNsClient:
    """Sends ``ApnsRequest``s for one bundle with one auth key."""

    def __init__(self, credentials: Credentials, http: Any) -> None:
        """``http`` is an ``httpx.AsyncClient`` with HTTP/2 enabled."""
        self.credentials = credentials
        self._key = load_key(credentials.key_pem)
        self._http = http
        self._token: str | None = None
        self._token_issued = 0.0

    def provider_token(self, now: float | None = None) -> str:
        """The signed JWT, reused until it nears Apple's one-hour limit."""
        now = time.time() if now is None else now
        if self._token is None or now - self._token_issued >= TOKEN_LIFETIME_SECONDS:
            self._token = jwt.encode({"iss": self.credentials.team_id, "iat": int(now)}, self._key,
                                     algorithm="ES256", headers={"kid": self.credentials.key_id})
            self._token_issued = now
        return self._token

    async def send(self, device_token: str, request: ApnsRequest, environment: str = "production") -> None:
        """Deliver one push. Raises ``APNsError`` when Apple doesn't answer 200."""
        host = SANDBOX_HOST if environment == "sandbox" else PRODUCTION_HOST
        headers = {**request.headers(self.credentials.bundle_id), "authorization": f"bearer {self.provider_token()}"}
        try:
            response = await self._http.post(f"{host}/3/device/{device_token}", content=request.body(),
                                             headers=headers, timeout=10)
        except Exception as err:  # httpx/httpx2 transport errors share no importable base here
            raise APNsError(0, type(err).__name__) from err
        if response.status_code == 200:
            return
        reason = "Unknown"
        try:
            reason = response.json().get("reason", reason)
        except ValueError:
            pass
        if response.status_code == 403 and reason == "ExpiredProviderToken":
            self._token = None
        raise APNsError(response.status_code, reason)

    async def verify(self) -> None:
        """Check the key, team and bundle with Apple by pushing to a token no device has.

        ``BadDeviceToken`` means Apple accepted our credentials; anything else is raised.
        """
        probe = ApnsRequest(push_type="background", priority=5, payload={"aps": {"content-available": 1}})
        try:
            await self.send(PROBE_DEVICE_TOKEN, probe)
        except APNsError as err:
            if err.reason == "BadDeviceToken":
                return
            raise
