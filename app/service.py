import logging
import re
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from .catalog import Catalog, public_inputs, resolve_inputs
from .config import Settings
from .db import RECONCILER_LOCK_ID, Database, NameTaken
from .models import CostLine, CostReport, Estimate, EstimateRequest, Lab, LabCreate, LabStatus, OwnerCost
from .identity import Caller
from .openbao import OpenBaoClient
from .terrakube import JOB_FAILED, JOB_SUCCEEDED, Terrakube, WorkspaceGone

log = logging.getLogger(__name__)

TTL_POLICY = "ttl-policy"
NAME_ATTEMPTS = 5
_ALPHABET = string.ascii_lowercase + string.digits


def random_name(owner_email: str) -> str:
    """`<owner>-<5 random chars>`, e.g. jwolynko-k3x9p; always matches NAME_PATTERN."""
    slug = re.sub(r"[^a-z0-9]+", "-", owner_email.split("@")[0].lower()).strip("-")[:20].rstrip("-")
    if not slug or not slug[0].isalpha():
        slug = f"lab-{slug}".rstrip("-")[:20].rstrip("-")
    return f"{slug}-{''.join(secrets.choice(_ALPHABET) for _ in range(5))}"
LIVE = {LabStatus.pending, LabStatus.provisioning, LabStatus.ready, LabStatus.failed, LabStatus.destroy_failed}


class LabError(Exception):
    """Client error: status is the HTTP status to return."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def now() -> datetime:
    return datetime.now(UTC)


def tag_time(moment: datetime) -> str:
    """Compact UTC minute for tags, e.g. 2026-09-30T12:00Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


class LabService:
    def __init__(
        self, settings: Settings, catalog: Catalog, db: Database, terrakube: Terrakube,
        access_store: OpenBaoClient | None = None,
    ):
        self.settings, self.catalog, self.db, self.terrakube = settings, catalog, db, terrakube
        self.access_store = access_store

    def to_model(self, row: dict[str, Any]) -> Lab:
        template = self.catalog.get(row["template_id"])
        hourly = row.get("hourly_cost")
        cost = None
        if hourly is not None:
            end = row["destroyed_at"] or now()
            hours = max((end - row["created_at"]).total_seconds() / 3600, 0) if row["ready_at"] else 0
            cost = round(hours * hourly, 2)
        return Lab.model_validate({
            **row, "inputs": public_inputs(template, row["inputs"]),
            "estimated_hourly_cost": hourly, "estimated_cost": cost,
        })

    # --- cost --------------------------------------------------------------

    def estimate(self, template_id: str, request: EstimateRequest) -> Estimate:
        template = self.catalog.get(template_id)
        if template is None:
            raise LabError(404, "template not found")
        values, errors = resolve_inputs(template, request.inputs)
        # Prices never depend on free-text fields, so a form that is still being
        # filled in (a required field empty) gets an estimate; invalid values do not.
        errors = [e for e in errors if not e.endswith(": required")]
        if errors:
            raise LabError(422, "; ".join(errors))
        priced = self.catalog.estimate(template, values)
        if priced is None:
            raise LabError(404, f"no cost model for template {template_id}")
        hourly, items = priced
        ttl = request.ttl_hours or template.default_ttl_hours
        return Estimate(currency=self.catalog.currency, hourly=round(hourly, 4), ttl_hours=ttl,
                        total=round(hourly * ttl, 2), items=items)

    async def backfill_prices(self) -> int:
        """Price labs created before their template had a cost model (or before 0.3.0)."""
        priced = 0
        for row in await self.db.unpriced_labs():
            template = self.catalog.get(row["template_id"])
            result = self.catalog.estimate(template, row["inputs"]) if template else None
            if result is not None:
                await self.db.set_price(row["id"], result[0], self.catalog.currency)
                priced += 1
        return priced

    async def cost_report(self, days: int) -> CostReport:
        lines = await self.db.cost_lines(now() - timedelta(days=days))
        owners: dict[str, OwnerCost] = {}
        templates: dict[str, CostLine] = {}
        for line in lines:
            item = CostLine(**{k: line[k] for k in ("template_id", "labs", "lab_hours", "estimated_cost")})
            owner = owners.setdefault(line["owner_email"], OwnerCost(
                owner_email=line["owner_email"], labs=0, lab_hours=0, estimated_cost=0, templates=[]))
            owner.templates.append(item)
            owner.labs += item.labs
            owner.lab_hours = round(owner.lab_hours + item.lab_hours, 2)
            owner.estimated_cost = round(owner.estimated_cost + item.estimated_cost, 2)
            total = templates.setdefault(item.template_id, CostLine(
                template_id=item.template_id, labs=0, lab_hours=0, estimated_cost=0))
            total.labs += item.labs
            total.lab_hours = round(total.lab_hours + item.lab_hours, 2)
            total.estimated_cost = round(total.estimated_cost + item.estimated_cost, 2)
        by_cost = lambda x: (-x.estimated_cost, -x.lab_hours)  # noqa: E731
        return CostReport(
            window_days=days,
            currency=self.catalog.currency,
            labs=sum(o.labs for o in owners.values()),
            lab_hours=round(sum(o.lab_hours for o in owners.values()), 2),
            estimated_cost=round(sum(o.estimated_cost for o in owners.values()), 2),
            unpriced_labs=sum(line["unpriced"] for line in lines),
            owners=sorted(owners.values(), key=by_cost),
            templates=sorted(templates.values(), key=by_cost),
            method=(
                "List prices from the catalog times lab-hours. A lab's hours run from creation to destruction "
                "(or now) within the window; only labs that reached ready are charged, at the hourly price "
                "snapshotted when they were created. Excludes taxes, transfer overage and resources a failed "
                "run left behind."
            ),
        )

    async def get(self, lab_id: UUID) -> dict[str, Any]:
        row = await self.db.get_lab(lab_id)
        if row is None:
            raise LabError(404, "lab not found")
        return row

    async def get_owned(self, lab_id: UUID, caller: Caller) -> dict[str, Any]:
        """The lab if the caller owns it or is an admin; otherwise 404, so other users' labs stay invisible."""
        row = await self.get(lab_id)
        if not caller.admin and row["owner_email"].lower() != caller.email:
            raise LabError(404, "lab not found")
        return row

    # --- create ------------------------------------------------------------

    async def create(self, request: LabCreate, caller: Caller) -> dict[str, Any]:
        owner = (request.owner_email or caller.email).lower()
        if owner != caller.email and not caller.admin:
            raise LabError(403, "only admins can create labs for another owner")
        request = request.model_copy(update={"owner_email": owner})
        actor = caller.email
        template = self.catalog.get(request.template_id)
        if template is None:
            raise LabError(422, f"unknown template {request.template_id!r}")
        ttl = request.ttl_hours or template.default_ttl_hours
        if ttl > template.max_ttl_hours:
            raise LabError(422, f"ttl_hours must be <= {template.max_ttl_hours} for {template.id}")
        values, errors = resolve_inputs(template, request.inputs)
        if errors:
            raise LabError(422, "; ".join(errors))

        lab_id = uuid.uuid4()
        created = now()
        priced = self.catalog.estimate(template, values)
        name = await self._insert(
            request, lab_id, template.id, values, created + timedelta(hours=ttl), actor,
            hourly_cost=priced[0] if priced else None,
        )

        workspace_id: str | None = None
        project_id = await self._project_id()
        try:
            workspace_id = await self.terrakube.create_workspace(
                name=f"lab-{name}",
                description=f"{template.name} for {request.owner_email} (lab {lab_id})",
                repository=template.source.repository,
                branch=template.source.branch,
                folder=template.source.folder,
                iac_type=template.source.iac_type,
                iac_version=template.source.iac_version,
                vcs_id=self.settings.terrakube_vcs_id if template.source.use_vcs_connection else None,
                project_id=project_id,
            )
            await self.db.update_lab(
                lab_id, workspace_id=workspace_id,
                workspace_url=await self.terrakube.workspace_url(workspace_id),
            )
            sensitive = {i.name for i in template.inputs if i.sensitive}
            for key, value in {**template.variables, **values}.items():
                await self.terrakube.add_variable(
                    workspace_id, key=key, value=value, category="TERRAFORM",
                    sensitive=key in sensitive, description="lab input",
                )
            metadata = {
                "TF_VAR_lab_id": str(lab_id),
                "TF_VAR_lab_name": name,
                "TF_VAR_lab_owner": request.owner_email,
                "TF_VAR_lab_expires_at": (created + timedelta(hours=ttl)).isoformat(),
            }
            for key, value in {**template.env, **metadata}.items():
                await self.terrakube.add_variable(
                    workspace_id, key=key, value=value, category="ENV", sensitive=False, description="lab metadata",
                )
            await self._tag(workspace_id, {"lab_owner": request.owner_email,
                                           "expires_at": tag_time(created + timedelta(hours=ttl))})
            job_id = await self.terrakube.start_job(workspace_id, self.settings.terrakube_apply_template)
        except Exception as error:
            log.exception("provisioning lab %s failed", lab_id)
            await self.db.update_lab(
                lab_id, status=LabStatus.failed, status_detail=f"provisioning request failed: {error}"[:500],
                event=("provision_failed", TTL_POLICY, {"stage": "request", "error": str(error)[:500]}),
            )
            raise LabError(502, f"Terrakube request failed; lab {lab_id} marked failed") from error

        return await self.db.update_lab(
            lab_id, status=LabStatus.provisioning, job_id=job_id, status_detail="apply job queued",
            event=("provision_started", actor, {"workspace_id": workspace_id, "job_id": job_id}),
        )

    async def _insert(
        self, request: LabCreate, lab_id: UUID, template_id: str, values: dict[str, str],
        expires_at: datetime, actor: str | None, hourly_cost: float | None = None,
    ) -> str:
        """Record the lab; a generated name is retried on the rare collision."""
        attempts = 1 if request.name else NAME_ATTEMPTS
        for _ in range(attempts):
            name = request.name or random_name(request.owner_email)
            try:
                await self.db.insert_lab(
                    lab_id=lab_id, name=name, template_id=template_id, owner_email=request.owner_email,
                    inputs=values, expires_at=expires_at, actor=actor,
                    hourly_cost=hourly_cost, currency=self.catalog.currency if hourly_cost is not None else None,
                )
                return name
            except NameTaken:
                continue
        if request.name:
            raise LabError(409, f"a lab named {request.name!r} already exists")
        raise LabError(409, "could not generate a free lab name; retry or pass one")

    async def _project_id(self) -> str | None:
        if not self.settings.terrakube_project:
            return None
        try:
            return await self.terrakube.project_id(self.settings.terrakube_project)
        except Exception:
            log.warning("project %r unavailable; creating the workspace without it",
                        self.settings.terrakube_project, exc_info=True)
            return None

    async def _tag(self, workspace_id: str, tags: dict[str, str]) -> None:
        """Best effort: tags help admins in the Terrakube UI but never fail a lab."""
        if not self.settings.terrakube_tags:
            return
        try:
            await self.terrakube.set_workspace_tags(workspace_id, tags)
        except Exception:
            log.warning("tagging workspace %s failed", workspace_id, exc_info=True)

    # --- lifecycle ---------------------------------------------------------

    async def extend(self, lab_id: UUID, hours: int, caller: Caller) -> dict[str, Any]:
        row = await self.get_owned(lab_id, caller)
        actor = caller.email
        if row["status"] not in {LabStatus.pending, LabStatus.provisioning, LabStatus.ready, LabStatus.failed}:
            raise LabError(409, f"cannot extend a lab in status {row['status']}")
        template = self.catalog.get(row["template_id"])
        max_hours = template.max_ttl_hours if template else 0
        limit = row["created_at"] + timedelta(hours=max_hours)
        target = min(max(row["expires_at"], now()) + timedelta(hours=hours), limit)
        if target <= row["expires_at"]:
            raise LabError(409, f"lab already at its maximum lifetime ({max_hours}h, until {limit.isoformat()})")
        updated = await self.db.update_lab(
            lab_id, expect_status={row["status"]}, expires_at=target, extension_count=row["extension_count"] + 1,
            event=("extended", actor, {"from": row["expires_at"].isoformat(), "to": target.isoformat()}),
        )
        if updated is None:
            raise LabError(409, "lab changed while extending; retry")
        if row["workspace_id"]:
            await self._tag(row["workspace_id"], {"expires_at": tag_time(target)})
        return updated

    async def destroy(self, lab_id: UUID, caller: Caller, reason: str) -> dict[str, Any]:
        row = await self.get_owned(lab_id, caller)
        if row["status"] in {LabStatus.destroying, LabStatus.destroyed}:
            raise LabError(409, f"lab is already {row['status']}")
        return await self._start_destroy(row, caller.email, reason)

    async def retry(self, lab_id: UUID, caller: Caller) -> dict[str, Any]:
        """Run the apply again for a failed lab, e.g. after fixing its template or a permission."""
        row = await self.get_owned(lab_id, caller)
        if row["status"] != LabStatus.failed:
            raise LabError(409, f"only failed labs can be retried; this one is {row['status']}")
        if not row["workspace_id"]:
            raise LabError(409, "nothing was created in Terrakube; destroy this lab and create a new one")
        if row["expires_at"] <= now():
            raise LabError(409, "lab has expired; it is being destroyed")
        try:
            job_id = await self.terrakube.start_job(row["workspace_id"], self.settings.terrakube_apply_template)
        except Exception as error:
            log.exception("retry of lab %s failed to start", lab_id)
            raise LabError(502, f"Terrakube request failed: {error}") from error
        updated = await self.db.update_lab(
            lab_id, expect_status={LabStatus.failed}, status=LabStatus.provisioning, job_id=job_id,
            status_detail="apply job queued (retry)",
            event=("retry_requested", caller.email, {"job_id": job_id, "previous_job_id": row["job_id"]}),
        )
        if updated is None:
            raise LabError(409, "lab changed while retrying; reload it")
        return updated

    async def access(self, lab_id: UUID, caller: Caller) -> tuple[dict[str, Any], dict[str, str]]:
        """The access details the template published for this lab, for its owner or an admin."""
        row = await self.get_owned(lab_id, caller)
        if self.access_store is None:
            raise LabError(501, "access details are not configured (OPENBAO_ADDR)")
        if row["status"] != LabStatus.ready:
            raise LabError(409, f"access details are available once the lab is ready; it is {row['status']}")
        path = self.settings.access_secret_path.format(name=row["name"])
        try:
            values = await self.access_store.read(path)
        except Exception as error:
            log.exception("reading access details of lab %s failed", lab_id)
            raise LabError(502, "could not read access details") from error
        if not values:
            raise LabError(404, "this lab published no access details")
        await self.db.add_event(lab_id, "access_viewed", caller.email, {"keys": sorted(values)})
        return row, {k: str(v) for k, v in values.items()}

    async def _start_destroy(self, row: dict[str, Any], actor: str | None, reason: str) -> dict[str, Any]:
        lab_id = row["id"]
        requested = {"destroy_requested_at": now(), "destroy_reason": reason}
        if not row["workspace_id"]:
            # Nothing was created in Terrakube.
            return await self.db.update_lab(
                lab_id, expect_status={row["status"]}, status=LabStatus.destroyed, destroyed_at=now(),
                status_detail="no workspace to destroy", event=("destroyed", actor, {"reason": reason}), **requested,
            ) or await self.get(lab_id)
        try:
            job_id = await self.terrakube.start_job(row["workspace_id"], self.settings.terrakube_destroy_template)
        except WorkspaceGone:
            # Deleted in Terrakube by hand: nothing left to run a destroy in.
            # Close the lab but say plainly that its resources were not checked.
            log.warning("workspace of lab %s no longer exists in Terrakube", lab_id)
            return await self.db.update_lab(
                lab_id, status=LabStatus.destroyed, destroyed_at=now(),
                status_detail="workspace was deleted in Terrakube before a destroy ran; "
                "its resources were not destroyed by the service, check the cloud account",
                event=("workspace_missing", actor, {"workspace_id": row["workspace_id"], "reason": reason}),
                **requested,
            ) or await self.get(lab_id)
        except Exception as error:
            log.exception("destroy of lab %s failed to start", lab_id)
            await self.db.update_lab(
                lab_id, status=LabStatus.destroy_failed, status_detail=f"destroy request failed: {error}"[:500],
                event=("destroy_failed", actor, {"stage": "request", "error": str(error)[:500]}), **requested,
            )
            raise LabError(502, f"Terrakube request failed; lab {lab_id} marked destroy_failed") from error
        return await self.db.update_lab(
            lab_id, status=LabStatus.destroying, job_id=job_id, status_detail="destroy job queued",
            event=("destroy_requested", actor, {"job_id": job_id, "reason": reason}), **requested,
        )

    # --- reconciler --------------------------------------------------------

    async def reconcile(self) -> None:
        """One pass: follow Terrakube jobs, enforce TTL. Skipped if another replica holds the lock."""
        async with self.db.pool.connection() as conn:
            if not await self.db.try_reconciler_lock(conn):
                return
            try:
                await self._follow_jobs()
                await self._fail_stuck_pending()
                await self._expire()
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (RECONCILER_LOCK_ID,))

    async def _follow_jobs(self) -> None:
        for row in await self.db.labs_in_status({LabStatus.provisioning, LabStatus.destroying}):
            if not row["job_id"]:
                continue
            try:
                status = await self.terrakube.job_status(row["job_id"])
            except Exception:
                log.exception("reading job %s of lab %s failed", row["job_id"], row["id"])
                continue
            if row["status"] == LabStatus.provisioning:
                await self._on_apply_job(row, status)
            else:
                await self._on_destroy_job(row, status)

    async def _on_apply_job(self, row: dict[str, Any], status: str) -> None:
        guard = {"expect_status": {LabStatus.provisioning}, "expect_job": row["job_id"]}
        if status in JOB_SUCCEEDED:
            await self.db.update_lab(
                row["id"], **guard, status=LabStatus.ready, ready_at=now(), status_detail=None,
                event=("provisioned", TTL_POLICY, {"job_id": row["job_id"], "job_status": status}),
            )
        elif status in JOB_FAILED:
            await self.db.update_lab(
                row["id"], **guard, status=LabStatus.failed, status_detail=f"apply job {status}",
                event=("provision_failed", TTL_POLICY, {"job_id": row["job_id"], "job_status": status}),
            )
        elif row["status_detail"] != f"apply job {status}":
            await self.db.update_lab(row["id"], **guard, status_detail=f"apply job {status}")

    async def _on_destroy_job(self, row: dict[str, Any], status: str) -> None:
        guard = {"expect_status": {LabStatus.destroying}, "expect_job": row["job_id"]}
        if status in JOB_SUCCEEDED:
            detail = None
            if self.settings.terrakube_tags:
                try:
                    await self.terrakube.release_workspace_tags(row["workspace_id"], ["expires_at"])
                except Exception:
                    log.warning("releasing tags of lab %s failed", row["id"], exc_info=True)
            if self.settings.delete_workspace_after_destroy:
                try:
                    await self.terrakube.delete_workspace(row["workspace_id"])
                except Exception as error:
                    log.exception("deleting workspace of lab %s failed", row["id"])
                    detail = f"resources destroyed; workspace delete failed: {error}"[:500]
            await self.db.update_lab(
                row["id"], **guard, status=LabStatus.destroyed, destroyed_at=now(), status_detail=detail,
                event=("destroyed", TTL_POLICY, {"job_id": row["job_id"], "reason": row["destroy_reason"]}),
            )
        elif status in JOB_FAILED:
            await self.db.update_lab(
                row["id"], **guard, status=LabStatus.destroy_failed, status_detail=f"destroy job {status}",
                event=("destroy_failed", TTL_POLICY, {"job_id": row["job_id"], "job_status": status}),
            )
        elif row["status_detail"] != f"destroy job {status}":
            await self.db.update_lab(row["id"], **guard, status_detail=f"destroy job {status}")

    async def _fail_stuck_pending(self) -> None:
        cutoff = now() - timedelta(minutes=self.settings.pending_timeout_minutes)
        for row in await self.db.labs_in_status({LabStatus.pending}):
            if row["created_at"] < cutoff:
                await self.db.update_lab(
                    row["id"], expect_status={LabStatus.pending}, status=LabStatus.failed,
                    status_detail="provisioning did not start (service restarted mid-request?)",
                    event=("provision_failed", TTL_POLICY, {"stage": "pending-timeout"}),
                )

    async def _expire(self) -> None:
        for row in await self.db.expired_labs(now()):
            # Record the policy decision, then destroy. Provisioning labs are
            # destroyed too; Terrakube queues the destroy after the apply.
            await self.db.update_lab(
                row["id"], expect_status={row["status"]},
                event=("expired", TTL_POLICY, {"expires_at": row["expires_at"].isoformat()}),
                destroy_reason="expired",
            )
            try:
                await self._start_destroy({**row, "destroy_reason": "expired"}, TTL_POLICY, "expired")
            except LabError:
                pass  # recorded as destroy_failed
