"""Who is calling: the end user behind the portal's request.

The API key proves the request comes from the portal's backend. The user is
then taken from one of:

- `X-User-Token`: the user's OIDC ID token, verified against the issuer's
  signing keys (token mode, enabled by USER_TOKEN_ISSUER). The portal cannot
  act as another user.
- `X-Actor-Email`: an email the portal backend sets from its own session
  (header mode). The service trusts the portal to never take it from the browser.
"""

import asyncio
import ssl
from dataclasses import dataclass
from typing import Protocol

import httpx
import jwt

ACCEPTED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


class IdentityError(Exception):
    pass


def verified_email(claims: dict, email_claim: str = "email", require_verified: bool = True) -> str:
    """The lowercased email from OIDC claims, or IdentityError.

    Roles are granted by email, so an address the provider has not verified
    could claim an admin's. By default `email_verified` must be true; providers
    that only issue verified addresses but omit the claim need require_verified=False.
    """
    email = str(claims.get(email_claim) or "").strip().lower()
    if not email or "@" not in email:
        raise IdentityError(f"token has no {email_claim} claim")
    verified = claims.get("email_verified")
    if verified is False or (require_verified and verified is not True):
        raise IdentityError("email address is not verified")
    return email


ROLES = ("user", "auditor", "admin")


@dataclass(frozen=True)
class Caller:
    """The end user of a request. Roles are cumulative: admin > auditor > user.

    user     create labs; view and act on their own labs (including access details)
    auditor  + view every lab, its history and cost, and the usage and cost reports
    admin    + act on any lab (access details, extend, retry, destroy), create labs for others
    """

    email: str
    role: str = "user"

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @property
    def sees_all(self) -> bool:
        return self.role in ("auditor", "admin")


class SigningKeys(Protocol):
    def get_signing_key_from_jwt(self, token: str): ...


class IdentityResolver:
    def __init__(
        self, admin_emails: tuple[str, ...], *, auditor_emails: tuple[str, ...] = (), issuer: str | None = None,
        audience: str | None = None, signing_keys: SigningKeys | None = None, email_claim: str = "email",
        require_verified_email: bool = True,
    ):
        self.admin_emails = {e.lower() for e in admin_emails}
        self.auditor_emails = {e.lower() for e in auditor_emails}
        self.token_mode = issuer is not None
        self._issuer, self._audience, self._keys, self._email_claim = issuer, audience, signing_keys, email_claim
        self._require_verified = require_verified_email

    async def resolve(self, actor_email: str | None, user_token: str | None) -> Caller:
        email = await self._from_token(user_token) if self.token_mode else (actor_email or "").strip()
        if not email or "@" not in email:
            raise IdentityError("X-User-Token is required" if self.token_mode else "X-Actor-Email is required")
        email = email.lower()
        return self.caller(email)

    def caller(self, email: str) -> Caller:
        email = email.lower()
        role = "admin" if email in self.admin_emails else "auditor" if email in self.auditor_emails else "user"
        return Caller(email=email, role=role)

    async def _from_token(self, token: str | None) -> str:
        if not token:
            raise IdentityError("X-User-Token is required")
        try:
            key = await asyncio.to_thread(self._keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=ACCEPTED_ALGORITHMS, audience=self._audience, issuer=self._issuer,
                options={"require": ["exp", "iss"], "verify_aud": self._audience is not None},
            )
        except Exception as error:
            raise IdentityError(f"invalid user token: {error}") from None
        return verified_email(claims, self._email_claim, self._require_verified)


def discover_jwks_url(issuer: str, verify: ssl.SSLContext | bool = True) -> str:
    """The issuer's jwks_uri from its OpenID discovery document."""
    response = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10, verify=verify)
    response.raise_for_status()
    return response.json()["jwks_uri"]
