"""Authentication - the least friction that is still safe.

No password.  No email verification loop.  No "create an account".  She is
sent one private link, once; opening it sets a signed, long-lived cookie and
she is permanently signed in on that phone.

For a single-user private tool this is both the lowest friction available and
entirely adequate.  Multi-user auth arrives in phase 2, when there is actually
more than one user.

What still matters, and is done here:

  * the cookie is SIGNED, so it cannot be forged, and it expires
  * every image route is behind it - a public URL raises the stakes over a
    private chat, and these are photographs of a real person
  * the access token is compared in constant time
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE_NAME = "estudio_session"
_SALT = "estudio.session.v1"


@dataclass(frozen=True)
class Session:
    owner: str
    issued_at: float


class Auth:
    def __init__(self, *, secret_key: str, access_token: str, max_age_days: int = 365) -> None:
        # An ephemeral key is generated rather than falling back to a constant.
        # Sessions are lost on restart, which is visible and annoying - and far
        # better than a predictable key that quietly signs forgeable cookies.
        self._ephemeral = not secret_key
        self._secret = secret_key or secrets.token_urlsafe(32)
        self._access_token = access_token
        self._serializer = URLSafeTimedSerializer(self._secret, salt=_SALT)
        self._max_age = max_age_days * 24 * 3600

    @property
    def is_ephemeral(self) -> bool:
        return self._ephemeral

    @property
    def is_open(self) -> bool:
        """No token configured: anyone with the URL is let in.

        Correct for local development, unacceptable once the app is reachable
        from the internet, so the settings screen reports it loudly.
        """
        return not self._access_token

    def verify_token(self, candidate: str) -> bool:
        if self.is_open:
            return True
        return hmac.compare_digest(candidate or "", self._access_token)

    def issue(self, owner: str) -> str:
        return self._serializer.dumps({"owner": owner, "issued_at": time.time()})

    def read(self, cookie: str | None) -> Session | None:
        if not cookie:
            return None
        try:
            payload = self._serializer.loads(cookie, max_age=self._max_age)
        except BadSignature:
            return None
        except Exception:  # noqa: BLE001 - expired or malformed is just absent
            return None
        return Session(
            owner=str(payload.get("owner", "owner")),
            issued_at=float(payload.get("issued_at", 0.0)),
        )
