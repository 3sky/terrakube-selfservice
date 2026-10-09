import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.identity import IdentityError, IdentityResolver, verified_email

ISSUER = "https://sso.example.com/dex"
AUDIENCE = "ps-tool"


class Keys:
    """Stands in for PyJWKClient: returns the public key for any token."""

    def __init__(self, public_key):
        self.key = public_key

    def get_signing_key_from_jwt(self, token):
        return self


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def token(private, **claims):
    body = {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "email": "Alice@Example.com",
            "email_verified": True, **claims}
    return jwt.encode(body, private, algorithm="RS256")


def resolver(public):
    return IdentityResolver(("alice@example.com",), issuer=ISSUER, audience=AUDIENCE, signing_keys=Keys(public))


async def test_valid_token_identifies_user_and_ignores_header(keypair):
    private, public = keypair
    caller = await resolver(public).resolve("mallory@example.com", token(private))
    assert caller.email == "alice@example.com" and caller.admin


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.example.com"},
    {"aud": "someone-else"},
    {"exp": int(time.time()) - 10},
    {"email_verified": False},
    {"email_verified": None},
    {"email_verified": "true"},
    {"email": ""},
])
async def test_rejected_tokens(keypair, claims):
    private, public = keypair
    with pytest.raises(IdentityError):
        await resolver(public).resolve(None, token(private, **claims))


async def test_unverified_email_allowed_only_when_configured(keypair):
    private, public = keypair
    lenient = IdentityResolver((), issuer=ISSUER, audience=AUDIENCE, signing_keys=Keys(public),
                               require_verified_email=False)
    assert (await lenient.resolve(None, token(private, email_verified=None))).email == "alice@example.com"
    with pytest.raises(IdentityError):
        await lenient.resolve(None, token(private, email_verified=False))


def test_verified_email():
    assert verified_email({"email": " Bob@Example.com", "email_verified": True}) == "bob@example.com"
    assert verified_email({"mail": "b@x.io"}, "mail", require_verified=False) == "b@x.io"
    for claims in ({"email": "bob@example.com"}, {"email": "bob", "email_verified": True}, {}):
        with pytest.raises(IdentityError):
            verified_email(claims)


async def test_token_signed_by_another_key_is_rejected(keypair):
    _, public = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(IdentityError):
        await resolver(public).resolve(None, token(other))


async def test_token_mode_requires_token(keypair):
    with pytest.raises(IdentityError):
        await resolver(keypair[1]).resolve("alice@example.com", None)


async def test_header_mode():
    caller = await IdentityResolver(()).resolve(" Bob@Example.com ", None)
    assert caller.email == "bob@example.com" and not caller.admin
    with pytest.raises(IdentityError):
        await IdentityResolver(()).resolve(None, None)
