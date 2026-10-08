"""Built-in web portal: Dex/OIDC sign-in, the portal pages, and /ui/api for them.

/ui/api serves the same templates, labs and analytics routes as /v1, but the
caller comes from the signed-in session instead of an API key and header.
Enabled with UI_ENABLED=true; /v1 is unchanged either way.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .identity import Caller

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "ui_static"
UI_HEADER = "selfservice-ui"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        )


def mount_ui(app: FastAPI, cfg: UISettings, add_routes: Callable) -> None:
    base = cfg.base_path
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

    def session_email(request: Request) -> str | None:
        if cfg.dev_user_email:
            return cfg.dev_user_email.lower()
        user = request.session.get("user")
        return user["email"] if user else None

    async def session_caller(request: Request) -> Caller:
        email = session_email(request)
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
        if session_email(request) is None:
            return RedirectResponse(f"{base}/login")
        html = (STATIC / "index.html").read_text().replace("%BASE%", f"{base}/").replace("%TITLE%", cfg.title)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

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
            log.warning("sign-in failed: %s", error)
            return HTMLResponse(f"<p>Sign-in failed ({error.error}). <a href='{base}/login'>Try again</a></p>",
                                status_code=401)
        claims = token.get("userinfo") or {}
        email = (claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is False:
            return HTMLResponse("<p>Your account has no verified email address.</p>", status_code=403)
        request.session.clear()
        request.session["user"] = {"email": email, "name": claims.get("name") or email}
        return RedirectResponse(f"{base}/")

    @pages.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return HTMLResponse(f"<p>Signed out. <a href='{base}/login'>Sign in again</a></p>")

    app.include_router(pages)
    app.mount("/ui/static", StaticFiles(directory=STATIC), name="ui-static")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(f"{base}/")
