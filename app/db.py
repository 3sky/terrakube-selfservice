from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS labs (
    id                   uuid PRIMARY KEY,
    name                 text NOT NULL,
    template_id          text NOT NULL,
    owner_email          text NOT NULL,
    inputs               jsonb NOT NULL DEFAULT '{}',
    status               text NOT NULL,
    status_detail        text,
    workspace_id         text,
    workspace_url        text,
    job_id               text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    ready_at             timestamptz,
    expires_at           timestamptz NOT NULL,
    destroy_requested_at timestamptz,
    destroyed_at         timestamptz,
    destroy_reason       text,
    extension_count      integer NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS labs_active_name ON labs (name) WHERE status <> 'destroyed';
CREATE INDEX IF NOT EXISTS labs_status ON labs (status);
CREATE INDEX IF NOT EXISTS labs_created_at ON labs (created_at);

CREATE TABLE IF NOT EXISTS lab_events (
    id      bigserial PRIMARY KEY,
    lab_id  uuid NOT NULL REFERENCES labs (id),
    type    text NOT NULL,
    actor   text,
    at      timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS lab_events_lab ON lab_events (lab_id, at);
CREATE INDEX IF NOT EXISTS lab_events_type_at ON lab_events (type, at);
"""

# Only one replica runs the reconciler at a time.
RECONCILER_LOCK_ID = 7_401_952


class NameTaken(Exception):
    pass


class Database:
    def __init__(self, url: str):
        self.pool = AsyncConnectionPool(url, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})

    async def open(self) -> None:
        await self.pool.open(wait=True)
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def _all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params or {})).fetchall()

    # --- labs --------------------------------------------------------------

    async def insert_lab(
        self, *, lab_id: UUID, name: str, template_id: str, owner_email: str,
        inputs: dict[str, str], expires_at: datetime, actor: str | None,
    ) -> dict[str, Any]:
        from psycopg.errors import UniqueViolation

        try:
            async with self.pool.connection() as conn, conn.transaction():
                row = await (await conn.execute(
                    """
                    INSERT INTO labs (id, name, template_id, owner_email, inputs, status, expires_at)
                    VALUES (%(id)s, %(name)s, %(template_id)s, %(owner)s, %(inputs)s, 'pending', %(expires_at)s)
                    RETURNING *
                    """,
                    {"id": lab_id, "name": name, "template_id": template_id, "owner": owner_email,
                     "inputs": Jsonb(inputs), "expires_at": expires_at},
                )).fetchone()
                await conn.execute(
                    "INSERT INTO lab_events (lab_id, type, actor, details) VALUES (%s, 'created', %s, %s)",
                    (lab_id, actor or owner_email, Jsonb({"template_id": template_id, "expires_at": expires_at.isoformat()})),
                )
                return row
        except UniqueViolation as error:
            raise NameTaken(name) from error

    async def get_lab(self, lab_id: UUID) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM labs WHERE id = %(id)s", {"id": lab_id})

    async def list_labs(
        self, *, owner: str | None, status: str | None, template_id: str | None,
        include_destroyed: bool, limit: int,
    ) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT * FROM labs
            WHERE (%(owner)s::text IS NULL OR owner_email = %(owner)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
              AND (%(template)s::text IS NULL OR template_id = %(template)s)
              AND (%(all)s OR status <> 'destroyed')
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {"owner": owner, "status": status, "template": template_id, "all": include_destroyed, "limit": limit},
        )

    async def update_lab(
        self, lab_id: UUID, *, expect_status: set[str] | None = None, expect_job: str | None = None,
        event: tuple[str, str | None, dict[str, Any]] | None = None, **fields: Any,
    ) -> dict[str, Any] | None:
        """Update a lab if it is still in the expected state; optionally record an event.

        Returns the updated row, or None when the lab changed in the meantime.
        """
        assignments = ", ".join(f"{k} = %({k})s" for k in fields)
        where = ["id = %(_id)s"]
        params: dict[str, Any] = {**fields, "_id": lab_id}
        if expect_status is not None:
            where.append("status = ANY(%(_status)s)")
            params["_status"] = list(expect_status)
        if expect_job is not None:
            where.append("job_id = %(_job)s")
            params["_job"] = expect_job
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                f"UPDATE labs SET {assignments} WHERE {' AND '.join(where)} RETURNING *", params
            )).fetchone()
            if row is not None and event is not None:
                kind, actor, details = event
                await conn.execute(
                    "INSERT INTO lab_events (lab_id, type, actor, details) VALUES (%s, %s, %s, %s)",
                    (lab_id, kind, actor, Jsonb(details)),
                )
            return row

    async def labs_in_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM labs WHERE status = ANY(%(s)s)", {"s": list(statuses)})

    async def expired_labs(self, now: datetime) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM labs WHERE status IN ('ready', 'failed', 'provisioning') AND expires_at <= %(now)s",
            {"now": now},
        )

    async def events(self, lab_id: UUID) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT type, actor, at, details FROM lab_events WHERE lab_id = %(id)s ORDER BY at, id", {"id": lab_id}
        )

    # --- analytics ---------------------------------------------------------

    async def summary(self, since: datetime) -> dict[str, Any]:
        params = {"since": since}
        by_status = {r["status"]: r["n"] for r in await self._all(
            "SELECT status, count(*) AS n FROM labs WHERE status <> 'destroyed' OR destroyed_at >= %(since)s GROUP BY status",
            params,
        )}
        counts = await self._one(
            """
            SELECT
              count(*) FILTER (WHERE type = 'created')          AS created,
              count(*) FILTER (WHERE type = 'destroyed')        AS destroyed,
              count(*) FILTER (WHERE type = 'expired')          AS expired,
              count(*) FILTER (WHERE type = 'provision_failed') AS provision_failures
            FROM lab_events WHERE at >= %(since)s
            """,
            params,
        )
        templates = await self._all(
            """
            SELECT l.template_id,
              count(*) FILTER (WHERE l.created_at >= %(since)s)                       AS created,
              count(*) FILTER (WHERE l.status <> 'destroyed')                        AS active,
              count(*) FILTER (WHERE l.destroyed_at >= %(since)s)                    AS destroyed,
              count(*) FILTER (WHERE l.destroy_reason = 'expired' AND l.destroy_requested_at >= %(since)s) AS expired,
              count(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM lab_events e WHERE e.lab_id = l.id AND e.type = 'provision_failed' AND e.at >= %(since)s
              ))                                                                      AS failed,
              round((avg(extract(epoch FROM l.destroyed_at - l.created_at) / 3600)
                FILTER (WHERE l.destroyed_at >= %(since)s))::numeric, 2)::float8       AS avg_lifetime_hours,
              round((avg(extract(epoch FROM l.ready_at - l.created_at) / 60)
                FILTER (WHERE l.ready_at >= %(since)s))::numeric, 2)::float8           AS avg_provision_minutes
            FROM labs l
            WHERE l.created_at >= %(since)s OR l.status <> 'destroyed' OR l.destroyed_at >= %(since)s
            GROUP BY l.template_id
            ORDER BY created DESC, l.template_id
            """,
            params,
        )
        owners = await self._all(
            """
            SELECT owner_email,
              count(*) FILTER (WHERE created_at >= %(since)s) AS created,
              count(*) FILTER (WHERE status <> 'destroyed')  AS active
            FROM labs
            WHERE created_at >= %(since)s OR status <> 'destroyed'
            GROUP BY owner_email
            ORDER BY created DESC, active DESC, owner_email
            LIMIT 10
            """,
            params,
        )
        active = sum(n for s, n in by_status.items() if s != "destroyed")
        return {"active_labs": active, "by_status": by_status, **counts, "templates": templates, "top_owners": owners}

    async def timeseries(self, since: datetime) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT d::date AS day,
              count(e.*) FILTER (WHERE e.type = 'created')   AS created,
              count(e.*) FILTER (WHERE e.type = 'destroyed') AS destroyed,
              count(e.*) FILTER (WHERE e.type = 'expired')   AS expired
            FROM generate_series(date_trunc('day', %(since)s::timestamptz), date_trunc('day', now()), interval '1 day') d
            LEFT JOIN lab_events e ON date_trunc('day', e.at) = d
            GROUP BY d ORDER BY d
            """,
            {"since": since},
        )

    async def try_reconciler_lock(self, conn) -> bool:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (RECONCILER_LOCK_ID,))).fetchone()
        return bool(row["locked"])
