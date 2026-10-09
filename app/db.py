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

-- 0.3.0: hourly cost estimate, snapshotted when the lab is created.
ALTER TABLE labs ADD COLUMN IF NOT EXISTS hourly_cost double precision;
ALTER TABLE labs ADD COLUMN IF NOT EXISTS currency text;

-- Portal sign-ins. The cookie holds a random id; only its SHA-256 is stored,
-- and signing out deletes the row, so a copied cookie stops working.
CREATE TABLE IF NOT EXISTS ui_sessions (
    id         text PRIMARY KEY,
    email      text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ui_sessions_expires_at ON ui_sessions (expires_at);
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
        inputs: dict[str, str], created_at: datetime, expires_at: datetime, actor: str | None,
        hourly_cost: float | None = None, currency: str | None = None,
    ) -> dict[str, Any]:
        """created_at comes from the service's clock, like every other lab timestamp."""
        from psycopg.errors import UniqueViolation

        try:
            async with self.pool.connection() as conn, conn.transaction():
                row = await (await conn.execute(
                    """
                    INSERT INTO labs (id, name, template_id, owner_email, inputs, status, created_at, expires_at,
                                      hourly_cost, currency)
                    VALUES (%(id)s, %(name)s, %(template_id)s, %(owner)s, %(inputs)s, 'pending', %(created_at)s,
                            %(expires_at)s, %(hourly_cost)s, %(currency)s)
                    RETURNING *
                    """,
                    {"id": lab_id, "name": name, "template_id": template_id, "owner": owner_email,
                     "inputs": Jsonb(inputs), "created_at": created_at, "expires_at": expires_at, "hourly_cost": hourly_cost,
                     "currency": currency},
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

    async def add_event(self, lab_id: UUID, kind: str, actor: str | None, details: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO lab_events (lab_id, type, actor, details) VALUES (%s, %s, %s, %s)",
                (lab_id, kind, actor, Jsonb(details)),
            )

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
              count(*) FILTER (WHERE l.status NOT IN ('destroyed', 'destroy_failed')) AS active,
              count(*) FILTER (WHERE l.status = 'destroy_failed')                    AS needs_attention,
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
              count(*) FILTER (WHERE status NOT IN ('destroyed', 'destroy_failed')) AS active
            FROM labs
            WHERE created_at >= %(since)s OR status <> 'destroyed'
            GROUP BY owner_email
            ORDER BY created DESC, active DESC, owner_email
            LIMIT 10
            """,
            params,
        )
        # Active: holds (or is creating) resources on purpose. destroy_failed labs
        # are counted separately: their cleanup needs a person.
        active = sum(n for s, n in by_status.items() if s not in ("destroyed", "destroy_failed"))
        return {
            "active_labs": active, "needs_attention": by_status.get("destroy_failed", 0), "by_status": by_status,
            **counts, "templates": templates, "top_owners": owners,
        }

    async def timeseries(self, since: datetime, now: datetime) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT d::date AS day,
              count(e.*) FILTER (WHERE e.type = 'created')   AS created,
              count(e.*) FILTER (WHERE e.type = 'destroyed') AS destroyed,
              count(e.*) FILTER (WHERE e.type = 'expired')   AS expired
            FROM generate_series(date_trunc('day', %(since)s::timestamptz), date_trunc('day', %(now)s::timestamptz), interval '1 day') d
            LEFT JOIN lab_events e ON date_trunc('day', e.at) = d
            GROUP BY d ORDER BY d
            """,
            {"since": since, "now": now},
        )

    async def unpriced_labs(self) -> list[dict[str, Any]]:
        return await self._all("SELECT id, template_id, inputs FROM labs WHERE hourly_cost IS NULL")

    async def set_price(self, lab_id: UUID, hourly_cost: float, currency: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE labs SET hourly_cost = %s, currency = %s WHERE id = %s AND hourly_cost IS NULL",
                (hourly_cost, currency, lab_id),
            )

    async def cost_lines(self, since: datetime, now: datetime) -> list[dict[str, Any]]:
        """Per owner and template: labs, hours inside the window, and estimated cost.

        Hours run from creation to destruction (or now), clipped to the window.
        Only labs that reached `ready` are charged; see the report's `method`.
        """
        return await self._all(
            """
            WITH clipped AS (
              SELECT owner_email, template_id, hourly_cost, ready_at IS NOT NULL AS charged,
                GREATEST(0, EXTRACT(EPOCH FROM
                  LEAST(COALESCE(destroyed_at, %(now)s), %(now)s) - GREATEST(created_at, %(since)s)
                ) / 3600) AS hours
              FROM labs
              WHERE COALESCE(destroyed_at, %(now)s) >= %(since)s
            )
            SELECT owner_email, template_id,
              count(*)::int                                                                  AS labs,
              round(sum(hours)::numeric, 2)::float8                                          AS lab_hours,
              round(sum(CASE WHEN charged THEN hours * COALESCE(hourly_cost, 0) ELSE 0 END)::numeric, 2)::float8
                                                                                             AS estimated_cost,
              count(*) FILTER (WHERE charged AND hourly_cost IS NULL)::int                   AS unpriced
            FROM clipped
            GROUP BY owner_email, template_id
            ORDER BY estimated_cost DESC, lab_hours DESC, owner_email, template_id
            """,
            {"since": since, "now": now},
        )

    # --- portal sessions -----------------------------------------------------

    async def create_session(self, session_hash: str, email: str, created_at: datetime, expires_at: datetime) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM ui_sessions WHERE expires_at <= %s", (created_at,))
            await conn.execute(
                "INSERT INTO ui_sessions (id, email, created_at, expires_at) VALUES (%s, %s, %s, %s)",
                (session_hash, email, created_at, expires_at),
            )

    async def session_email(self, session_hash: str, now: datetime) -> str | None:
        row = await self._one(
            "SELECT email FROM ui_sessions WHERE id = %(id)s AND expires_at > %(now)s", {"id": session_hash, "now": now}
        )
        return row["email"] if row else None

    async def delete_session(self, session_hash: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM ui_sessions WHERE id = %s", (session_hash,))

    async def try_reconciler_lock(self, conn) -> bool:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (RECONCILER_LOCK_ID,))).fetchone()
        return bool(row["locked"])
