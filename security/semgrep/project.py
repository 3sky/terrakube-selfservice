# Test cases for project.yml (semgrep --test security/semgrep).
import html
import os

from authlib.integrations.starlette_client import OAuthError
from fastapi.responses import HTMLResponse


async def callback_vulnerable(request):
    try:
        await oauth.oidc.authorize_access_token(request)
    except OAuthError as error:
        # ruleid: html-response-from-untrusted-input
        return HTMLResponse(f"<p>Sign-in failed ({error.error})</p>", status_code=401)


async def callback_fixed(request):
    try:
        await oauth.oidc.authorize_access_token(request)
    except OAuthError as error:
        log.warning("sign-in failed: %r", error.error)
        # ok: html-response-from-untrusted-input
        return HTMLResponse("<p>Sign-in failed.</p>", status_code=401)


async def echo(request):
    name = request.query_params.get("name")
    # ruleid: html-response-from-untrusted-input
    return HTMLResponse(f"<p>Hello {name}</p>")


async def echo_escaped(request):
    name = request.query_params.get("name")
    # ok: html-response-from-untrusted-input
    return HTMLResponse(f"<p>Hello {html.escape(name)}</p>")


# ruleid: plaintext-http-default-url
API = os.environ.get("TERRAKUBE_API_URL", "http://terrakube-api-service.terrakube.svc.cluster.local:8080")
# ok: plaintext-http-default-url
PUBLIC = os.environ.get("UI_PUBLIC_URL", "http://localhost:8080/ui")
# ok: plaintext-http-default-url
SECURE = os.environ.get("OPENBAO_ADDR", "https://openbao.example.com")


def fail_open(claims):
    # ruleid: email-verified-fail-open
    if claims.get("email_verified") is False:
        raise ValueError("not verified")


def fail_closed(claims):
    # ok: email-verified-fail-open
    if claims.get("email_verified") is not True:
        raise ValueError("not verified")
