# Changelog

Image `ghcr.io/3sky/terrakube-selfservice` and chart `oci://ghcr.io/3sky/charts/terrakube-selfservice` share these versions.

## 0.6.1

- Sensitive form inputs (for example a personal Red Hat password) are no longer stored in the service's database: they go to the Terrakube workspace as sensitive variables only, and the lab record keeps `***`.

## 0.6.0

- Roles: `user` (own labs), `auditor` (also views every lab, its history and cost, and the reports) and `admin` (also acts on any lab). Chart `users.auditorEmails` / `AUDITOR_EMAILS`.
- Auditors get `403` when acting on, or reading the access details of, someone else's lab. Reports (`/v1/analytics/*`) are open to auditors as well as admins.
- Portal: "Admin" is now "Reports", shown to auditors and admins; labs of others open read-only, without the Access tab; header logo removed.

## 0.5.1

- Portal look aligned with the Terrakube UI: dark header with a purple active section, breadcrumbs, content on a white panel, Ant Design-style buttons, badges and form fields.
- Lab page laid out like a Terrakube workspace: name, ID with copy, a facts row (status, expiry, cost so far, hourly price) and Overview / Access / History tabs.
- Fixed: access details showed "[object HTMLElement]" instead of the values. They now list each value, with the kubeconfig as a download and passwords hidden until revealed.

## 0.5.0

- Built-in web portal at `/ui` (chart `ui.*`, off by default): OIDC sign-in, catalog with forms and live cost estimates, labs with status, expiry and cost, access details with kubeconfig download, extend, retry, destroy, history, and admin usage and cost views. Plain HTML and JavaScript in `app/ui_static/`.
- `/ui/api/*` serves the same templates, labs and analytics endpoints as `/v1`, with the user from the session; `/v1` is unchanged.
- Chart: `route.rewritePrefix` to publish only the portal (`/portal` → `/ui`).
- Cost estimates no longer fail while required form fields are still empty; invalid values are still rejected.

## 0.4.0

- Lab workspaces go into a Terrakube project, `Self-service` by default (`terrakube.project`), created if missing.
- Workspaces are tagged `lab_owner:<email>` and `expires_at:<UTC time>` (`terrakube.tags`). Extending a lab moves `expires_at`; destroying removes it.

## 0.3.1

- Analytics: labs in `destroy_failed` are no longer counted as active. New `needs_attention` count, overall and per template.

## 0.3.0

- Cost estimates from catalog `prices` and per-template `cost` models: `POST /v1/templates/{id}/estimate`, `estimated_hourly_cost` and `estimated_cost` on labs, `GET /v1/analytics/costs` (admins).
- Each lab stores its hourly price when created; existing labs are priced at startup.

## 0.2.1

- Workspaces are removed like the Terrakube UI does (marked deleted and renamed); Terrakube refuses a plain `DELETE`.
- Destroying a lab whose workspace was deleted outside the service closes it as `destroyed`, with a warning and a `workspace_missing` event, instead of failing forever.

## 0.2.0

**Breaking:** every lab call needs a user (`X-Actor-Email` or `X-User-Token`); analytics are admin-only.

- Ownership: users see and act only on their own labs (others answer `404`); `ADMIN_EMAILS` see all.
- Token mode: users identified by a verified OIDC ID token (`USER_TOKEN_ISSUER`, `USER_TOKEN_AUDIENCE`).
- `GET /v1/labs/{id}/access`: access details the template published in OpenBao/Vault, for the owner or an admin, audited.
- `POST /v1/labs/{id}/retry` for failed labs.
- `name` and `owner_email` are optional when creating: a random `<owner>-<5 chars>` name, owned by the caller.
- Chart: `openbao.*`, `users.*` and `access.*` values; `token.openbao.addr` still works.

## 0.1.0

- Catalog of templates with typed form inputs, labs as Terrakube workspaces, TTL with extend and automatic destroy, usage analytics, audit trail, Helm chart.
