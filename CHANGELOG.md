# Changelog

Image `ghcr.io/3sky/terrakube-selfservice` and chart `oci://ghcr.io/3sky/charts/terrakube-selfservice` share these versions.

## Unreleased

Security fixes from [SECURITY-REVIEW.md](SECURITY-REVIEW.md). Upgrades may need values changes, see **Breaking**.

- Portal: the OIDC callback no longer echoes the provider's `error` parameter into the page (reflected XSS). Every `/ui` response carries a strict Content-Security-Policy (no inline script or style, no framing), `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy`.
- Access details: a secret written before the lab was created (left behind by an earlier lab with the same name) is refused instead of handed to the new lab's owner.
- Number inputs reject `NaN` and `Infinity`, which skipped `minimum`/`maximum` and broke cost reports.
- Portal sessions are stored server-side (`ui_sessions` table, created on startup): signing out revokes the session, so a copied cookie stops working. Sign-out is a `POST` refused from other origins; `GET /ui/logout` only shows the button.
- Chart `tls.secretName`: the pod serves HTTPS, so the API key and user tokens are encrypted up to the pod.
- Dependencies: FastAPI 0.141.1 with Starlette 1.7.0 (Starlette 0.48 had six advisories, one in `StaticFiles` range requests), Authlib 1.8.0 and PyJWT 2.15.1 (both had published advisories), pytest 9.1.1. The image installs only hash-checked wheels from `requirements.lock` and copies the app instead of building it; the base image and CI actions are pinned by digest/SHA, with Dependabot proposing updates.
- Lab creation times and report windows use the service's clock only (they mixed it with the database's).
- CI security pipeline (`security.yml`, required before publishing): Semgrep (community rules plus tested project rules for this project's past bugs), CodeQL, pip-audit, dependency review, gitleaks, zizmor, actionlint, hadolint, Checkov on the rendered chart, Grype on the image. Findings go to code scanning.
- Releases: the image is scanned before it is pushed; image and chart are signed with cosign (keyless) and get SLSA provenance attestations, the image an SPDX SBOM attestation. See README "Verifying releases".
- Chart: `image.digest` pins the image; every resource sets its namespace; the service account token is only mounted when OpenBao is used; optional `networkPolicy` (ingress from listed peers, optional egress rules).

**Breaking**

- `terrakube.apiUrl` / `TERRAKUBE_API_URL` is required and must be `https://`, as must `openbao.addr`, the OIDC issuers and `users.token.jwksUrl`. Loopback addresses are exempt; `allowInsecureTransport: true` (`ALLOW_INSECURE_TRANSPORT`) allows plain HTTP for development. Rotate the Terrakube token if it was ever sent over HTTP.
- `database.sslmode` defaults to `verify-full` and must be `verify-full` or `verify-ca`. Mount the server's CA with `database.caSecret`, or rely on public CAs (`sslrootcert=system`). With `existingSecret`, the service checks `DATABASE_URL` at startup.
- Token mode needs `users.token.audience` / `USER_TOKEN_AUDIENCE`; without it, an ID token issued to any client of the issuer was accepted.
- OIDC identities (token mode and the portal) need `email_verified: true`; an absent claim was accepted. For a provider that only issues verified addresses but omits the claim, set `users.requireVerifiedEmail: false` (`REQUIRE_VERIFIED_EMAIL=false`).
- Portal users are signed out once on upgrade (sessions move server-side).

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
