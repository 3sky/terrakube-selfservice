import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .catalog import Catalog
from .config import Settings
from .db import Database
from .identity import Caller, IdentityError, IdentityResolver, discover_jwks_url
from .models import (
    AnalyticsSummary, AnalyticsTimeseries, DestroyRequest, ExtendRequest, Lab, LabAccess, LabCreate,
    LabEventList, LabList, LabStatus, Problem, Template, TemplateList,
)
from .openbao import OpenBaoClient
from .service import LabError, LabService, now
from .terrakube import FileToken, OpenBaoToken, StaticToken, TerrakubeClient, TokenSource

log = logging.getLogger("terrakube_selfservice")

_ERRORS = {
    401: {"model": Problem, "description": "Missing or invalid API key or user identity"},
    403: {"model": Problem, "description": "The user may not do this"},
    404: {"model": Problem, "description": "Not found, or not visible to this user"},
    409: {"model": Problem, "description": "Conflict with the lab's current state"},
    422: {"model": Problem, "description": "Invalid request"},
    501: {"model": Problem, "description": "Not configured on this installation"},
    502: {"model": Problem, "description": "Terrakube or OpenBao request failed"},
}


def errors(*codes: int) -> dict[int, dict]:
    return {c: _ERRORS[c] for c in (401, *codes)}

ActorHeader = Annotated[
    str | None,
    Header(
        alias="X-Actor-Email",
        description="Header mode: the signed-in user, set by the portal backend from its own session. "
        "Ignored in token mode.",
    ),
]
UserTokenHeader = Annotated[
    str | None,
    Header(
        alias="X-User-Token",
        description="Token mode: the signed-in user's OIDC ID token, verified by the service.",
    ),
]


async def _reconcile_forever(service: LabService, interval: int) -> None:
    while True:
        try:
            await service.reconcile()
        except Exception:
            log.exception("reconcile pass failed")
        await asyncio.sleep(interval)


def identity_from_settings(cfg: Settings) -> IdentityResolver:
    if not cfg.user_token_issuer:
        return IdentityResolver(cfg.admin_emails)
    import jwt

    jwks_url = cfg.user_token_jwks_url or discover_jwks_url(cfg.user_token_issuer)
    return IdentityResolver(
        cfg.admin_emails, issuer=cfg.user_token_issuer, audience=cfg.user_token_audience,
        signing_keys=jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=600), email_claim=cfg.user_token_email_claim,
    )


def build_app(
    settings: Settings | None = None, service: LabService | None = None, run_reconciler: bool = True,
    identity: IdentityResolver | None = None,
) -> FastAPI:
    """Build the app. Tests pass a ready service; production builds it from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal service, identity
        http = None
        if service is None:
            cfg = settings or Settings.from_env()
            http = httpx.AsyncClient(timeout=30)
            bao = OpenBaoClient(http, cfg.openbao_addr, cfg.openbao_role) if cfg.openbao_addr else None
            tokens: TokenSource
            if cfg.terrakube_token:
                tokens = StaticToken(cfg.terrakube_token)
            elif cfg.terrakube_token_file:
                tokens = FileToken(cfg.terrakube_token_file)
            else:
                tokens = OpenBaoToken(bao, cfg.openbao_secret_path, cfg.openbao_secret_key)
            db = Database(cfg.database_url)
            await db.open()
            service = LabService(
                cfg, Catalog.load(cfg.catalog_path), db,
                TerrakubeClient(http, cfg.terrakube_api_url, cfg.terrakube_ui_url, cfg.terrakube_organization, tokens),
                access_store=bao,
            )
        app.state.service = service
        app.state.identity = identity or identity_from_settings(service.settings)
        task = (
            asyncio.create_task(_reconcile_forever(service, service.settings.reconcile_interval_seconds))
            if run_reconciler
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if http is not None:
                await service.db.close()
                await http.aclose()

    app = FastAPI(
        title="Terrakube Self-Service",
        version="0.2.1",
        description=(
            "Self-service environments (labs) on Terrakube. Pick a template, submit its inputs, and the service "
            "creates a Terrakube workspace, applies it, and destroys it when its TTL expires."
        ),
        lifespan=lifespan,
        generate_unique_id_function=lambda route: route.name,
    )
    bearer = HTTPBearer(auto_error=False, description="API key issued to the calling portal.")

    def current_service(request: Request) -> LabService:
        return request.app.state.service

    def authenticated(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> None:
        keys = request.app.state.service.settings.api_keys
        supplied = credentials.credentials if credentials else ""
        if not any(hmac.compare_digest(supplied.encode(), k.encode()) for k in keys):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key", {"WWW-Authenticate": "Bearer"})

    async def current_caller(request: Request, actor: ActorHeader = None, user_token: UserTokenHeader = None) -> Caller:
        try:
            return await request.app.state.identity.resolve(actor, user_token)
        except IdentityError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from None

    async def admin_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
        if not caller.admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")
        return caller

    Service = Annotated[LabService, Depends(current_service)]
    User = Annotated[Caller, Depends(current_caller)]
    Admin = Annotated[Caller, Depends(admin_caller)]

    @app.exception_handler(LabError)
    async def lab_error(_: Request, error: LabError) -> JSONResponse:
        return JSONResponse({"detail": error.detail}, status_code=error.status)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    v1 = [Depends(authenticated)]

    @app.get("/v1/templates", response_model=TemplateList, dependencies=v1, tags=["templates"],
             summary="List lab templates", responses=errors())
    async def list_templates(svc: Service) -> TemplateList:
        return TemplateList(items=[Template.model_validate(t.model_dump()) for t in svc.catalog.all()])

    @app.get("/v1/templates/{template_id}", response_model=Template, dependencies=v1, tags=["templates"],
             summary="Get a template and its form inputs", responses=errors(404))
    async def get_template(template_id: str, svc: Service) -> Template:
        template = svc.catalog.get(template_id)
        if template is None:
            raise LabError(404, "template not found")
        return Template.model_validate(template.model_dump())

    @app.post("/v1/labs", response_model=Lab, status_code=status.HTTP_202_ACCEPTED, dependencies=v1, tags=["labs"],
              summary="Create a lab from a template", responses=errors(403, 409, 422, 502))
    async def create_lab(body: LabCreate, svc: Service, caller: User) -> Lab:
        """Creates the Terrakube workspace and queues its apply. Poll the lab until `ready` or `failed`."""
        return svc.to_model(await svc.create(body, caller))

    @app.get("/v1/labs", response_model=LabList, dependencies=v1, tags=["labs"], summary="List labs",
             responses=errors(422))
    async def list_labs(
        svc: Service,
        caller: User,
        owner_email: Annotated[str | None, Query(description="Admins only; other users always see their own labs.")] = None,
        lab_status: Annotated[LabStatus | None, Query(alias="status")] = None,
        template_id: str | None = None,
        include_destroyed: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> LabList:
        owner = (owner_email.lower() if owner_email else None) if caller.admin else caller.email
        rows = await svc.db.list_labs(
            owner=owner, status=lab_status, template_id=template_id,
            include_destroyed=include_destroyed or lab_status == LabStatus.destroyed, limit=limit,
        )
        return LabList(items=[svc.to_model(r) for r in rows])

    @app.get("/v1/labs/{lab_id}", response_model=Lab, dependencies=v1, tags=["labs"], summary="Get a lab",
             responses=errors(404))
    async def get_lab(lab_id: UUID, svc: Service, caller: User) -> Lab:
        return svc.to_model(await svc.get_owned(lab_id, caller))

    @app.get("/v1/labs/{lab_id}/access", response_model=LabAccess, dependencies=v1, tags=["labs"],
             summary="Access details of a ready lab", responses=errors(404, 409, 501, 502))
    async def lab_access(lab_id: UUID, svc: Service, caller: User, response: Response) -> LabAccess:
        """Kubeconfig, passwords, URLs published by the template. Owner or admin only; every read is audited.

        The response is marked `Cache-Control: no-store`: show it to the user, never cache, log or store it.
        """
        row, values = await svc.access(lab_id, caller)
        response.headers["Cache-Control"] = "no-store"
        return LabAccess(lab_id=row["id"], name=row["name"], values=values)

    @app.post("/v1/labs/{lab_id}/extend", response_model=Lab, dependencies=v1, tags=["labs"],
              summary="Extend a lab's TTL", responses=errors(404, 409, 422))
    async def extend_lab(lab_id: UUID, body: ExtendRequest, svc: Service, caller: User) -> Lab:
        return svc.to_model(await svc.extend(lab_id, body.hours, caller))

    @app.post("/v1/labs/{lab_id}/retry", response_model=Lab, status_code=status.HTTP_202_ACCEPTED,
              dependencies=v1, tags=["labs"], summary="Retry a failed lab", responses=errors(404, 409, 502))
    async def retry_lab(lab_id: UUID, svc: Service, caller: User) -> Lab:
        """Runs the apply again in the lab's existing workspace; the lab goes back to `provisioning`."""
        return svc.to_model(await svc.retry(lab_id, caller))

    @app.post("/v1/labs/{lab_id}/destroy", response_model=Lab, status_code=status.HTTP_202_ACCEPTED,
              dependencies=v1, tags=["labs"], summary="Destroy a lab now", responses=errors(404, 409, 502))
    async def destroy_lab(lab_id: UUID, svc: Service, caller: User, body: DestroyRequest | None = None) -> Lab:
        """Queues a destroy job; the workspace is deleted once it completes. Also retries a `destroy_failed` lab."""
        reason = (body.reason if body and body.reason else None) or "requested"
        return svc.to_model(await svc.destroy(lab_id, caller, reason))

    @app.get("/v1/labs/{lab_id}/events", response_model=LabEventList, dependencies=v1, tags=["labs"],
             summary="Audit trail of a lab", responses=errors(404))
    async def lab_events(lab_id: UUID, svc: Service, caller: User) -> LabEventList:
        await svc.get_owned(lab_id, caller)
        return LabEventList(items=await svc.db.events(lab_id))

    @app.get("/v1/analytics/summary", response_model=AnalyticsSummary, dependencies=v1, tags=["analytics"],
             summary="Usage summary (admins)", responses=errors(403, 422))
    async def analytics_summary(
        svc: Service, _: Admin, days: Annotated[int, Query(ge=1, le=365)] = 30
    ) -> AnalyticsSummary:
        return AnalyticsSummary(window_days=days, **await svc.db.summary(now() - timedelta(days=days)))

    @app.get("/v1/analytics/timeseries", response_model=AnalyticsTimeseries, dependencies=v1, tags=["analytics"],
             summary="Daily created, destroyed and expired labs (admins)", responses=errors(403, 422))
    async def analytics_timeseries(
        svc: Service, _: Admin, days: Annotated[int, Query(ge=1, le=365)] = 30
    ) -> AnalyticsTimeseries:
        return AnalyticsTimeseries(window_days=days, points=await svc.db.timeseries(now() - timedelta(days=days)))

    return app


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return build_app()
