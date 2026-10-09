"""Built-in web portal: Dex/OIDC sign-in, the portal pages, and /ui/api for them.

/ui/api serves the same templates, labs and analytics routes as /v1, but the
caller comes from the signed-in session instead of an API key and header.
Enabled with UI_ENABLED=true; /v1 is unchanged either way.
"""

import hashlib
import html
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import _bool, require_tls
from .identity import Caller, IdentityError, verified_email

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "ui_static"
UI_HEADER = "selfservice-ui"

# Every /ui response. The portal is plain same-origin script and CSS, so the
# policy allows no inline script, inline style, eval or framing.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@dataclass(frozen=True)
class UISettings:
    # Public address of the portal as users open it, e.g. https://lab.example.com/portal.
    # Its path is where links and the session cookie point (a route may map it to /ui).
    public_url: str
    # OIDC sign-in (Dex or any provider). Without an issuer the portal runs in
    # development mode as dev_user_email, without sign-in.
    oidc_issuer: str | None
    oidc_client_id: str
    oidc_client_secret: str
    oidc_scopes: str
    session_secret: str
    session_max_age_hours: int
    session_https_only: bool
    dev_user_email: str | None
    title: str
    require_verified_email: bool = True

    @property
    def base_path(self) -> str:
        return urlparse(self.public_url).path.rstrip("/")

    @classmethod
    def from_env(cls) -> "UISettings | None":
        if not _bool(os.environ.get("UI_ENABLED"), False):
            return None
        issuer = (os.environ.get("UI_OIDC_ISSUER") or "").rstrip("/") or None
        dev_user = os.environ.get("UI_DEV_USER_EMAIL") or None
        if issuer is None and dev_user is None:
            raise RuntimeError("UI_ENABLED needs UI_OIDC_ISSUER (+ client id/secret), or UI_DEV_USER_EMAIL for development")
        secret = os.environ.get("UI_SESSION_SECRET", "")
        if issuer and len(secret) < 32:
            raise RuntimeError("UI_SESSION_SECRET must be at least 32 characters")
        if issuer:
            require_tls("UI_OIDC_ISSUER", issuer, _bool(os.environ.get("ALLOW_INSECURE_TRANSPORT"), False))
        return cls(
            public_url=os.environ.get("UI_PUBLIC_URL", "http://localhost:8080/ui").rstrip("/"),
            oidc_issuer=issuer,
            oidc_client_id=os.environ.get("UI_OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.environ.get("UI_OIDC_CLIENT_SECRET", ""),
            oidc_scopes=os.environ.get("UI_OIDC_SCOPES", "openid email profile"),
            session_secret=secret or "development-only-session-secret-not-for-production",
            session_max_age_hours=int(os.environ.get("UI_SESSION_MAX_AGE_HOURS", "8")),
            session_https_only=_bool(os.environ.get("UI_SESSION_HTTPS_ONLY"), issuer is not None),
            dev_user_email=dev_user if issuer is None else None,
            title=os.environ.get("UI_TITLE", "Lab self-service"),
            require_verified_email=_bool(os.environ.get("REQUIRE_VERIFIED_EMAIL"), True),
        )


def mount_ui(app: FastAPI, cfg: UISettings, add_routes: Callable) -> None:
    base = cfg.base_path
    login_href = html.escape(f"{base}/login")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    app.add_middleware(
        SessionMiddleware, secret_key=cfg.session_secret, session_cookie="selfservice_session",
        max_age=cfg.session_max_age_hours * 3600, same_site="lax", https_only=cfg.session_https_only,
        path=base or "/",
    )
    oauth = OAuth()
    if cfg.oidc_issuer:
        oauth.register(
            "oidc", server_metadata_url=f"{cfg.oidc_issuer}/.well-known/openid-configuration",
            client_id=cfg.oidc_client_id, client_secret=cfg.oidc_client_secret,
            client_kwargs={"scope": cfg.oidc_scopes},
        )

    public = urlparse(cfg.public_url)
    public_origin = f"{public.scheme}://{public.netloc}"

    def session_hash(request: Request) -> str | None:
        sid = request.session.get("sid")
        return hashlib.sha256(sid.encode()).hexdigest() if isinstance(sid, str) else None

    async def session_email(request: Request) -> str | None:
        if cfg.dev_user_email:
            return cfg.dev_user_email.lower()
        digest = session_hash(request)
        if digest is None:
            return None
        return await request.app.state.service.db.session_email(digest, datetime.now(UTC))

    async def session_caller(request: Request) -> Caller:
        email = await session_email(request)
        if email is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign-in required")
        return request.app.state.identity.caller(email)

    async def session_reporter(caller: Caller = Depends(session_caller)) -> Caller:
        if not caller.sees_all:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "auditors and admins only")
        return caller

    async def from_portal(x_requested_with: str | None = Header(default=None)) -> None:
        # A custom header cannot be sent cross-site without CORS, which this
        # service does not allow: together with SameSite cookies, this stops
        # other sites from acting with the user's session.
        if x_requested_with != UI_HEADER:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Requested-With header")

    ui_api = APIRouter(prefix="/ui/api", include_in_schema=False)

    @ui_api.get("/me", dependencies=[Depends(from_portal)])
    async def me(caller: Caller = Depends(session_caller)) -> dict:
        return {"email": caller.email, "role": caller.role, "admin": caller.admin, "sees_all": caller.sees_all}

    add_routes(ui_api, session_caller, session_reporter, [Depends(from_portal)])
    app.include_router(ui_api)

    pages = APIRouter(prefix="/ui", include_in_schema=False)

    @pages.get("")
    async def ui_root():
        return RedirectResponse(f"{base}/")

    @pages.get("/")
    async def index(request: Request):
        if await session_email(request) is None:
            return RedirectResponse(f"{base}/login")
        page = (STATIC / "index.html").read_text().replace("%BASE%", html.escape(f"{base}/")).replace("%TITLE%", html.escape(cfg.title))
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @pages.get("/login")
    async def login(request: Request):
        if not cfg.oidc_issuer:
            return RedirectResponse(f"{base}/")
        return await oauth.oidc.authorize_redirect(request, f"{cfg.public_url}/auth/callback")

    @pages.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except OAuthError as error:
            # error comes from the callback's query string (before the state is
            # checked): log it, never echo it into the page.
            log.warning("sign-in failed: %r", error.error)
            return HTMLResponse(f'<p>Sign-in failed. <a href="{login_href}">Try again</a></p>', status_code=401)
        try:
            email = verified_email(token.get("userinfo") or {}, require_verified=cfg.require_verified_email)
        except IdentityError:
            return HTMLResponse("<p>Your account has no verified email address.</p>", status_code=403)
        sid = secrets.token_urlsafe(32)
        created = datetime.now(UTC)
        await request.app.state.service.db.create_session(
            hashlib.sha256(sid.encode()).hexdigest(), email, created, created + timedelta(hours=cfg.session_max_age_hours),
        )
        request.session.clear()
        request.session["sid"] = sid
        return RedirectResponse(f"{base}/")

    @pages.get("/logout")
    async def logout_page():
        # GET changes nothing (a link or image on another site cannot sign users out).
        return HTMLResponse(f'<form method="post" action="{html.escape(base)}/logout"><button type="submit">Sign out</button></form>')

    @pages.post("/logout")
    async def logout(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != public_origin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site sign-out refused")
        if (digest := session_hash(request)) is not None:
            await request.app.state.service.db.delete_session(digest)
        request.session.clear()
        return HTMLResponse(f'<p>Signed out. <a href="{login_href}">Sign in again</a></p>')

    app.include_router(pages)
    app.mount("/ui/static", StaticFiles(directory=STATIC), name="ui-static")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(f"{base}/")
