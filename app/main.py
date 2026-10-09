import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .catalog import Catalog
from .config import Settings
from .db import Database
from .identity import Caller, IdentityError, IdentityResolver, discover_jwks_url
from .models import (
    AnalyticsSummary, AnalyticsTimeseries, CostReport, DestroyRequest, Estimate, EstimateRequest, ExtendRequest,
    Lab, LabAccess, LabCreate, LabEventList, LabList, LabStatus, Problem, Template, TemplateList,
)
from .openbao import OpenBaoClient
from .service import LabError, LabService, now
from .terrakube import FileToken, OpenBaoToken, StaticToken, TerrakubeClient, TokenSource
from .ui import UISettings, mount_ui

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


API_DESCRIPTION = """
Self-service environments (**labs**) on Terrakube. A portal lists templates, the user fills in a form, and the
service creates a Terrakube workspace, applies it, hands over its access details, and destroys it when its
lifetime ends.

**Authentication.** Every `/v1` call carries the portal's API key (`Authorization: Bearer <key>`) and the end
user: a verified OIDC ID token in `X-User-Token` (token mode) or `X-Actor-Email` set by the portal backend from its
own session (header mode). Call the service from the portal backend only, never from a browser.

**Roles.** `user`: own labs only (another user's lab answers `404`). `auditor`: also reads every lab and the
analytics, but cannot act on other people's labs or read their access details (`403`). `admin`: everything.

**Lifecycle.** `pending` → `provisioning` → `ready` or `failed` → `destroying` → `destroyed` or `destroy_failed`.
Poll a lab every 15-30 s while it is `pending` or `provisioning`.

**Lifetimes.** Labs expire at `expires_at` and are destroyed automatically; `extend` adds hours up to the
template's `max_ttl_hours` from creation.

Integration guide: https://github.com/3sky/terrakube-selfservice/blob/main/docs/portal-integration.md
""".strip()

OPENAPI_TAGS = [
    {"name": "templates", "description": "The catalog: templates, their form inputs, and cost estimates."},
    {"name": "labs", "description": "Create labs and follow, extend, retry, destroy and access them. "
                                    "Users see their own labs, auditors and admins see all; only owners and admins act on a lab."},
    {"name": "analytics", "description": "Usage and estimated cost reports. Auditors and admins."},
]


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
        return IdentityResolver(cfg.admin_emails, auditor_emails=cfg.auditor_emails)
    import jwt

    jwks_url = cfg.user_token_jwks_url or discover_jwks_url(cfg.user_token_issuer)
    return IdentityResolver(
        cfg.admin_emails, auditor_emails=cfg.auditor_emails, issuer=cfg.user_token_issuer,
        audience=cfg.user_token_audience,
        signing_keys=jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=600), email_claim=cfg.user_token_email_claim,
        require_verified_email=cfg.require_verified_email,
    )


def build_app(
    settings: Settings | None = None, service: LabService | None = None, run_reconciler: bool = True,
    identity: IdentityResolver | None = None, ui: UISettings | None = None, ui_from_env: bool = False,
) -> FastAPI:
    """Build the app. Tests pass a ready service; production builds it from the environment."""
    ui_settings = ui or (UISettings.from_env() if ui_from_env else None)

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
        try:
            if priced := await service.backfill_prices():
                log.info("priced %d existing labs from the catalog", priced)
        except Exception:
            log.exception("pricing existing labs failed")
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
        version="0.6.1",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
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

    async def reports_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
        if not caller.sees_all:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "auditors and admins only")
        return caller

    Service = Annotated[LabService, Depends(current_service)]

    @app.exception_handler(LabError)
    async def lab_error(_: Request, error: LabError) -> JSONResponse:
        return JSONResponse({"detail": error.detail}, status_code=error.status)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def add_routes(router: APIRouter, user_dep, reports_dep, deps: list) -> None:
        """Templates, labs and analytics, for one way of identifying the caller (API or UI)."""
        User = Annotated[Caller, Depends(user_dep)]  # noqa: N806
        Reporter = Annotated[Caller, Depends(reports_dep)]  # noqa: N806

        @router.get("/templates", response_model=TemplateList, dependencies=deps, tags=["templates"],
                 summary="List lab templates", responses=errors())
        async def list_templates(svc: Service) -> TemplateList:
            return TemplateList(items=[Template.model_validate(t.model_dump()) for t in svc.catalog.all()])

        @router.get("/templates/{template_id}", response_model=Template, dependencies=deps, tags=["templates"],
                 summary="Get a template and its form inputs", responses=errors(404))
        async def get_template(template_id: str, svc: Service) -> Template:
            template = svc.catalog.get(template_id)
            if template is None:
                raise LabError(404, "template not found")
            return Template.model_validate(template.model_dump())

        @router.post("/templates/{template_id}/estimate", response_model=Estimate, dependencies=deps, tags=["templates"],
                  summary="Estimate a lab's cost before creating it", responses=errors(404, 422))
        async def estimate_template(template_id: str, body: EstimateRequest, svc: Service) -> Estimate:
            """Hourly and total list-price estimate for these form inputs; 404 when the template has no cost model."""
            return svc.estimate(template_id, body)

        @router.post("/labs", response_model=Lab, status_code=status.HTTP_202_ACCEPTED, dependencies=deps, tags=["labs"],
                  summary="Create a lab from a template", responses=errors(403, 409, 422, 502))
        async def create_lab(body: LabCreate, svc: Service, caller: User) -> Lab:
            """Creates the Terrakube workspace and queues its apply. Poll the lab until `ready` or `failed`."""
            return svc.to_model(await svc.create(body, caller))

        @router.get("/labs", response_model=LabList, dependencies=deps, tags=["labs"], summary="List labs",
                 responses=errors(422))
        async def list_labs(
            svc: Service,
            caller: User,
            owner_email: Annotated[str | None, Query(description="Auditors and admins; users always see their own labs.")] = None,
            lab_status: Annotated[LabStatus | None, Query(alias="status")] = None,
            template_id: str | None = None,
            include_destroyed: bool = False,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> LabList:
            owner = (owner_email.lower() if owner_email else None) if caller.sees_all else caller.email
            rows = await svc.db.list_labs(
                owner=owner, status=lab_status, template_id=template_id,
                include_destroyed=include_destroyed or lab_status == LabStatus.destroyed, limit=limit,
            )
            return LabList(items=[svc.to_model(r) for r in rows])

        @router.get("/labs/{lab_id}", response_model=Lab, dependencies=deps, tags=["labs"], summary="Get a lab",
                 responses=errors(404))
        async def get_lab(lab_id: UUID, svc: Service, caller: User) -> Lab:
            return svc.to_model(await svc.get_visible(lab_id, caller))

        @router.get("/labs/{lab_id}/access", response_model=LabAccess, dependencies=deps, tags=["labs"],
                 summary="Access details of a ready lab", responses=errors(403, 404, 409, 501, 502))
        async def lab_access(lab_id: UUID, svc: Service, caller: User, response: Response) -> LabAccess:
            """Kubeconfig, passwords, URLs published by the template. Owner or admin only (auditors get 403); every read is audited.

            The response is marked `Cache-Control: no-store`: show it to the user, never cache, log or store it.
            """
            row, values = await svc.access(lab_id, caller)
            response.headers["Cache-Control"] = "no-store"
            return LabAccess(lab_id=row["id"], name=row["name"], values=values)

        @router.post("/labs/{lab_id}/extend", response_model=Lab, dependencies=deps, tags=["labs"],
                  summary="Extend a lab's TTL", responses=errors(403, 404, 409, 422))
        async def extend_lab(lab_id: UUID, body: ExtendRequest, svc: Service, caller: User) -> Lab:
            return svc.to_model(await svc.extend(lab_id, body.hours, caller))

        @router.post("/labs/{lab_id}/retry", response_model=Lab, status_code=status.HTTP_202_ACCEPTED,
                  dependencies=deps, tags=["labs"], summary="Retry a failed lab", responses=errors(403, 404, 409, 502))
        async def retry_lab(lab_id: UUID, svc: Service, caller: User) -> Lab:
            """Runs the apply again in the lab's existing workspace; the lab goes back to `provisioning`."""
            return svc.to_model(await svc.retry(lab_id, caller))

        @router.post("/labs/{lab_id}/destroy", response_model=Lab, status_code=status.HTTP_202_ACCEPTED,
                  dependencies=deps, tags=["labs"], summary="Destroy a lab now", responses=errors(403, 404, 409, 502))
        async def destroy_lab(lab_id: UUID, svc: Service, caller: User, body: DestroyRequest | None = None) -> Lab:
            """Queues a destroy job; the workspace is deleted once it completes. Also retries a `destroy_failed` lab."""
            reason = (body.reason if body and body.reason else None) or "requested"
            return svc.to_model(await svc.destroy(lab_id, caller, reason))

        @router.get("/labs/{lab_id}/events", response_model=LabEventList, dependencies=deps, tags=["labs"],
                 summary="Audit trail of a lab", responses=errors(404))
        async def lab_events(lab_id: UUID, svc: Service, caller: User) -> LabEventList:
            await svc.get_visible(lab_id, caller)
            return LabEventList(items=await svc.db.events(lab_id))

        @router.get("/analytics/summary", response_model=AnalyticsSummary, dependencies=deps, tags=["analytics"],
                 summary="Usage summary (auditors, admins)", responses=errors(403, 422))
        async def analytics_summary(
            svc: Service, _: Reporter, days: Annotated[int, Query(ge=1, le=365)] = 30
        ) -> AnalyticsSummary:
            return AnalyticsSummary(window_days=days, **await svc.db.summary(now() - timedelta(days=days)))

        @router.get("/analytics/costs", response_model=CostReport, dependencies=deps, tags=["analytics"],
                 summary="Estimated cost per owner and template (auditors, admins)", responses=errors(403, 422))
        async def analytics_costs(svc: Service, _: Reporter, days: Annotated[int, Query(ge=1, le=365)] = 30) -> CostReport:
            """Lab-hours and estimated cost in the window, per owner (split by template) and per template."""
            return await svc.cost_report(days)

        @router.get("/analytics/timeseries", response_model=AnalyticsTimeseries, dependencies=deps, tags=["analytics"],
                 summary="Daily created, destroyed and expired labs (auditors, admins)", responses=errors(403, 422))
        async def analytics_timeseries(
            svc: Service, _: Reporter, days: Annotated[int, Query(ge=1, le=365)] = 30
        ) -> AnalyticsTimeseries:
            moment = now()
            return AnalyticsTimeseries(window_days=days, points=await svc.db.timeseries(moment - timedelta(days=days), moment))

    api = APIRouter(prefix="/v1")
    add_routes(api, current_caller, reports_caller, [Depends(authenticated)])
    app.include_router(api)

    if ui_settings is not None:
        mount_ui(app, ui_settings, add_routes)

    return app


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return build_app(ui_from_env=True)
