import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .catalog import Catalog
from .config import Settings
from .db import Database
from .models import (
    AnalyticsSummary, AnalyticsTimeseries, DestroyRequest, ExtendRequest, Lab, LabCreate, LabEventList,
    LabList, LabStatus, Problem, Template, TemplateList,
)
from .service import LabError, LabService, now
from .terrakube import FileToken, OpenBaoToken, StaticToken, TerrakubeClient, TokenSource

log = logging.getLogger("terrakube_selfservice")

_ERRORS = {
    401: {"model": Problem, "description": "Missing or invalid API key"},
    404: {"model": Problem, "description": "Not found"},
    409: {"model": Problem, "description": "Conflict with the lab's current state"},
    422: {"model": Problem, "description": "Invalid request"},
    502: {"model": Problem, "description": "Terrakube request failed"},
}


def errors(*codes: int) -> dict[int, dict]:
    return {c: _ERRORS[c] for c in (401, *codes)}

ActorHeader = Annotated[
    str | None,
    Header(alias="X-Actor-Email", description="End user acting through the calling portal; recorded in the audit trail."),
]


async def _reconcile_forever(service: LabService, interval: int) -> None:
    while True:
        try:
            await service.reconcile()
        except Exception:
            log.exception("reconcile pass failed")
        await asyncio.sleep(interval)


def build_app(
    settings: Settings | None = None, service: LabService | None = None, run_reconciler: bool = True
) -> FastAPI:
    """Build the app. Tests pass a ready service; production builds it from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal service
        http = None
        if service is None:
            cfg = settings or Settings.from_env()
            http = httpx.AsyncClient(timeout=30)
            tokens: TokenSource
            if cfg.terrakube_token:
                tokens = StaticToken(cfg.terrakube_token)
            elif cfg.terrakube_token_file:
                tokens = FileToken(cfg.terrakube_token_file)
            else:
                tokens = OpenBaoToken(
                    http, cfg.openbao_addr, cfg.openbao_role, cfg.openbao_secret_path, cfg.openbao_secret_key
                )
            db = Database(cfg.database_url)
            await db.open()
            service = LabService(
                cfg, Catalog.load(cfg.catalog_path), db,
                TerrakubeClient(http, cfg.terrakube_api_url, cfg.terrakube_ui_url, cfg.terrakube_organization, tokens),
            )
        app.state.service = service
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
        version="0.1.0",
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

    Service = Annotated[LabService, Depends(current_service)]

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
              summary="Create a lab from a template", responses=errors(409, 422, 502))
    async def create_lab(body: LabCreate, svc: Service, actor: ActorHeader = None) -> Lab:
        """Creates the Terrakube workspace and queues its apply. Poll the lab until `ready` or `failed`."""
        return svc.to_model(await svc.create(body, actor))

    @app.get("/v1/labs", response_model=LabList, dependencies=v1, tags=["labs"], summary="List labs",
             responses=errors(422))
    async def list_labs(
        svc: Service,
        owner_email: str | None = None,
        lab_status: Annotated[LabStatus | None, Query(alias="status")] = None,
        template_id: str | None = None,
        include_destroyed: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> LabList:
        rows = await svc.db.list_labs(
            owner=owner_email, status=lab_status, template_id=template_id,
            include_destroyed=include_destroyed or lab_status == LabStatus.destroyed, limit=limit,
        )
        return LabList(items=[svc.to_model(r) for r in rows])

    @app.get("/v1/labs/{lab_id}", response_model=Lab, dependencies=v1, tags=["labs"], summary="Get a lab",
             responses=errors(404))
    async def get_lab(lab_id: UUID, svc: Service) -> Lab:
        return svc.to_model(await svc.get(lab_id))

    @app.post("/v1/labs/{lab_id}/extend", response_model=Lab, dependencies=v1, tags=["labs"],
              summary="Extend a lab's TTL", responses=errors(404, 409, 422))
    async def extend_lab(lab_id: UUID, body: ExtendRequest, svc: Service, actor: ActorHeader = None) -> Lab:
        return svc.to_model(await svc.extend(lab_id, body.hours, actor))

    @app.post("/v1/labs/{lab_id}/destroy", response_model=Lab, status_code=status.HTTP_202_ACCEPTED,
              dependencies=v1, tags=["labs"], summary="Destroy a lab now", responses=errors(404, 409, 502))
    async def destroy_lab(
        lab_id: UUID, svc: Service, body: DestroyRequest | None = None, actor: ActorHeader = None
    ) -> Lab:
        """Queues a destroy job; the workspace is deleted once it completes. Also retries a `destroy_failed` lab."""
        reason = (body.reason if body and body.reason else None) or "requested"
        return svc.to_model(await svc.destroy(lab_id, actor, reason))

    @app.get("/v1/labs/{lab_id}/events", response_model=LabEventList, dependencies=v1, tags=["labs"],
             summary="Audit trail of a lab", responses=errors(404))
    async def lab_events(lab_id: UUID, svc: Service) -> LabEventList:
        await svc.get(lab_id)
        return LabEventList(items=await svc.db.events(lab_id))

    @app.get("/v1/analytics/summary", response_model=AnalyticsSummary, dependencies=v1, tags=["analytics"],
             summary="Usage summary", responses=errors(422))
    async def analytics_summary(svc: Service, days: Annotated[int, Query(ge=1, le=365)] = 30) -> AnalyticsSummary:
        return AnalyticsSummary(window_days=days, **await svc.db.summary(now() - timedelta(days=days)))

    @app.get("/v1/analytics/timeseries", response_model=AnalyticsTimeseries, dependencies=v1, tags=["analytics"],
             summary="Daily created, destroyed and expired labs", responses=errors(422))
    async def analytics_timeseries(
        svc: Service, days: Annotated[int, Query(ge=1, le=365)] = 30
    ) -> AnalyticsTimeseries:
        return AnalyticsTimeseries(window_days=days, points=await svc.db.timeseries(now() - timedelta(days=days)))

    return app


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return build_app()
