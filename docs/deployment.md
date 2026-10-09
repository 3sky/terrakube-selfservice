# Deployment

How to run terrakube-selfservice next to an existing Terrakube, and what to know when operating it.

## Requirements

| Needs | Why |
|---|---|
| Terrakube (tested with 2.32) and an organization for labs | Runs the labs |
| A Terrakube personal access token whose team has **Manage Workspaces** and **Manage Jobs** in that organization | The service creates workspaces, variables, tags and runs |
| The organization's run templates, by default `Plan and apply` and `Destroy` | Used to apply and destroy labs (names are configurable) |
| A VCS connection in Terrakube, if template repositories are private | Clones the templates |
| PostgreSQL | Labs, history, prices (the schema is created on start) |
| OpenBao or Vault with Kubernetes auth (optional) | Access details, and a place for the Terrakube token |

## Install

The chart is published at `oci://ghcr.io/3sky/charts/terrakube-selfservice`, the image at `ghcr.io/3sky/terrakube-selfservice`, both with the same version.

```bash
helm install selfservice oci://ghcr.io/3sky/charts/terrakube-selfservice --version <version> \
  --namespace terrakube -f values.yaml
```

A minimal `values.yaml`:

```yaml
catalog:                          # your templates; see catalog.md
  currency: USD
  prices: {}
  templates: [...]
apiKeys: ["<random key for the portal>"]
database:
  host: postgres.example.com      # must match the server certificate (sslmode verify-full)
  password: "<password>"
  caSecret:
    name: postgres-ca             # omit to trust public CAs
terrakube:
  apiUrl: https://terrakube-api.example.com
  uiUrl: https://terrakube.example.com
  organization: my-org
  vcsId: "<VCS connection id>"    # for private template repositories
openbao:
  addr: https://openbao.openbao.svc:8200
users:
  adminEmails: [admin@example.com]
```

The chart refuses to install when the catalog is invalid, no Terrakube token source is set, or required values are missing.

## Values

| Value | Default | Purpose |
|---|---|---|
| `catalog` | none | The templates ([catalog reference](catalog.md)); validated at install time |
| `existingCatalogConfigMap` | `""` | Use a ConfigMap with a `catalog.yaml` key instead |
| `apiKeys` | none | Bearer keys for portals; several allow rotation |
| `database.*` | | PostgreSQL connection (`host`, `port`, `name`, `user`, `password`) |
| `database.sslmode` | `verify-full` | `verify-full` or `verify-ca`; weaker modes need `allowInsecureTransport` |
| `database.caSecret.name`, `.key` | `""`, `ca.crt` | Secret with the database server's CA; without it, public CAs (`sslrootcert=system`) |
| `existingSecret` | `""` | A Secret with `API_KEYS` and `DATABASE_URL` instead of the two above |
| `terrakube.apiUrl` | required | Terrakube API base URL, `https://` |
| `terrakube.uiUrl` | required | Used for `workspace_url` links |
| `terrakube.organization` | required | Organization for lab workspaces |
| `terrakube.vcsId` | `""` | VCS connection for private template repositories |
| `terrakube.applyTemplate`, `destroyTemplate` | `Plan and apply`, `Destroy` | Terrakube run templates |
| `terrakube.project` | `Self-service` | Project for lab workspaces, created if missing; `""` for none |
| `terrakube.tags` | `true` | Tag workspaces with `lab_owner:` and `expires_at:` |
| `token.*` | | Where the Terrakube token comes from ([below](#the-terrakube-token)) |
| `openbao.addr`, `openbao.role` | `""`, `terrakube-selfservice` | OpenBao/Vault (`https://`) with Kubernetes auth; enables the access endpoint |
| `access.secretPath` | `secret/data/labs/{name}` | Where templates publish access details |
| `users.adminEmails` | `[]` | Admins: act on any lab, plus everything auditors can |
| `users.auditorEmails` | `[]` | Auditors: view every lab and the reports, act only on their own labs |
| `users.token.*` | | Token mode ([below](#users)) |
| `users.requireVerifiedEmail` | `true` | OIDC identities (token mode, portal) need `email_verified: true`; `false` only for providers that omit the claim |
| `serviceAccount.name` | `terrakube-selfservice` | Service account bound in OpenBao/Vault |
| `allowInsecureTransport` | `false` | Development only: allow `http://` Terrakube, OpenBao and OIDC URLs and unverified database TLS |
| `reconcileIntervalSeconds` | `30` | How often labs are checked and expired |
| `deleteWorkspaceAfterDestroy` | `true` | Remove the workspace after a successful destroy |
| `ui.enabled`, `ui.publicUrl`, `ui.title` | `false` | The [web portal](#web-portal) and the address users open |
| `ui.oidc.issuer`, `clientId`, `clientSecret`, `scopes` | | Portal sign-in |
| `ui.sessionSecret`, `ui.sessionMaxAgeHours` | , `8` | Session cookie signing key (32+ characters) and lifetime |
| `image.digest` | `""` | `sha256:...`: deploy exactly this (cosign-verified) image; overrides `image.tag` ([verifying releases](../README.md#verifying-releases)) |
| `networkPolicy.enabled`, `ingressFrom`, `egress` | `false`, `[]`, `[]` | NetworkPolicy: only `ingressFrom` peers (gateway, portal backend) reach the pod; `egress` rules optional |
| `tls.secretName` | `""` | A `kubernetes.io/tls` Secret: the pod serves HTTPS on 8080 (probes follow); with `route.*`, add a BackendTLSPolicy |
| `route.*` | disabled | Publish through a Gateway API `HTTPRoute`: `pathPrefix` is rewritten to `rewritePrefix` (`/` for the API, `/ui` for the portal only); otherwise cluster-internal |

## The Terrakube token

Set exactly one of:

- `token.existingSecret.name` / `.key`: a Kubernetes Secret mounted as a file. Re-read when Terrakube rejects the old token, so rotation needs no restart.
- OpenBao/Vault kv-v2 at `token.openbao.secretPath`, key `token.openbao.secretKey`, read through `openbao.addr` (used when neither of the others is set). Re-read after a rejection too.
- `token.value`: stored in the chart's Secret. Development only.

Personal access tokens expire (Terrakube asks for a lifetime in days); plan the rotation.

## Users

Every lab request identifies an end user. Pick the mode that matches the portal ([details](portal-integration.md#2-identify-the-user-on-every-call)):

- **Token mode (recommended):** set `users.token.issuer` and `users.token.audience` (both required) to the OIDC provider and client the portal signs users in with. The service verifies the ID token in `X-User-Token` against the issuer's signing keys (from its discovery document, or `users.token.jwksUrl`) and uses the `email` claim (`users.token.emailClaim`).
- **Header mode (default):** the service trusts `X-Actor-Email` from the portal backend. Only safe when the portal sets it from its own session.

Roles, cumulative: everyone who signs in is a **user** (own labs only); **auditors** (`users.auditorEmails`) also see every lab, its history and cost, and the reports; **admins** (`users.adminEmails`) also act on any lab and read its access details. See the [role table](../README.md#roles).

## OpenBao or Vault

The service logs in with its Kubernetes service account. Its role needs:

```hcl
path "secret/data/terrakube-selfservice" { capabilities = ["read"] }   # token source 3 only
path "secret/data/labs/*"                { capabilities = ["read"] }   # access details
```

```bash
bao write auth/kubernetes/role/terrakube-selfservice \
  bound_service_account_names=terrakube-selfservice \
  bound_service_account_namespaces=terrakube \
  token_policies=terrakube-selfservice token_ttl=15m
```

Templates publish access details with their own credentials, typically the Terrakube executor's role:

```hcl
path "secret/data/labs/*"     { capabilities = ["create", "update", "read"] }
path "secret/metadata/labs/*" { capabilities = ["read", "list", "delete"] }
```

## Web portal

The built-in portal is off by default. To enable it, register an OIDC client (for example a Dex static client) with the redirect URI `<publicUrl>/auth/callback`, then:

```yaml
ui:
  enabled: true
  publicUrl: https://lab.example.com/portal
  oidc:
    issuer: https://lab.example.com/dex
    clientId: selfservice-portal
    clientSecret: "<client secret>"
  sessionSecret: "<at least 32 random characters>"
route:
  enabled: true
  hostnames: [lab.example.com]
  pathPrefix: /portal
  rewritePrefix: /ui          # publishes only the portal; /v1 stays cluster-internal
  parentRefs: [{name: public, namespace: gateway}]
```

- Users sign in with the provider; the portal needs their `email` claim. Admins are `users.adminEmails`, as for the API.
- Sessions are signed cookies (`SameSite=Lax`, `Secure`, scoped to the portal path) holding a random id that is checked against the `ui_sessions` table; they expire after `ui.sessionMaxAgeHours`, and signing out (a `POST`, refused from other origins) deletes the session server-side, so a copied cookie stops working. The portal's API calls also need an `X-Requested-With` header, so other sites cannot act with a user's session.
- Every `/ui` response carries a strict Content-Security-Policy and refuses framing.
- The portal uses no API key: it calls the service in-process. `/v1` keeps working unchanged for other tools.

## What it does in Terrakube

- Each lab is a workspace `lab-<name>` in the configured organization and project, with the form inputs as Terraform variables and `TF_VAR_lab_*` as environment variables.
- Tags `lab_owner:<email>` and `expires_at:<UTC time>`: extending a lab moves `expires_at`, and destroying it deletes that tag once no workspace uses it. Terrakube before 2.34 has no tag values, hence `name:value`.
- After a successful destroy the workspace is removed the way the Terrakube UI does it: marked deleted and renamed `<name>_DEL_<4 chars>`, which keeps its run history.
- Project, tag and cleanup failures are logged and never fail a lab.

## Operating

**Health and logs.** `GET /healthz` for probes. The service logs every Terrakube or OpenBao failure with the lab id; the lab's `status_detail` and `GET /v1/labs/{id}/events` say what happened.

**Things to know:**

| Situation | What happens | What to do |
|---|---|---|
| A workspace is deleted in the Terrakube UI before the lab is destroyed | Destroying the lab closes it as `destroyed` with "resources were not destroyed by the service" and a `workspace_missing` event | Check the cloud account for leftovers. Destroy labs through the service, not the Terrakube UI |
| A destroy run fails | Lab is `destroy_failed`; counted as `needs_attention` in analytics | Fix the cause (see the Terrakube run), then `POST /v1/labs/{id}/destroy` |
| An apply fails after creating resources | Lab is `failed`; it still expires and is destroyed | `POST /v1/labs/{id}/retry` after fixing the cause |
| Template repositories cannot be cloned ("not authorized") | Every run fails at "Failed to prepare work dir" | Check the Terrakube VCS connection. GitHub App user tokens expire after 8 hours unless token expiration is turned off for the app; a GitHub App connection avoids this |
| The service restarts mid-request | A lab stuck in `pending` is marked `failed` after 10 minutes | Retry or destroy it |
| Prices change | New labs use the new prices; existing labs keep theirs | Update `catalog.prices` |

**Several replicas.** Safe: the background loop takes a PostgreSQL advisory lock, so only one replica moves labs at a time. One replica is enough for most installations.

## Environment variables

For running the image without the chart:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | PostgreSQL URL; `sslmode=verify-full` or `verify-ca` unless the host is loopback |
| `API_KEYS` | required | Comma-separated portal keys |
| `CATALOG_PATH` | `/etc/terrakube-selfservice/catalog.yaml` | Catalog file |
| `TERRAKUBE_API_URL` | required | Terrakube API, `https://` |
| `TERRAKUBE_UI_URL` | required | Terrakube UI, for links |
| `TERRAKUBE_ORGANIZATION` | required | Organization for labs |
| `TERRAKUBE_VCS_ID` | | VCS connection for private templates |
| `TERRAKUBE_APPLY_TEMPLATE`, `TERRAKUBE_DESTROY_TEMPLATE` | `Plan and apply`, `Destroy` | Run templates |
| `TERRAKUBE_PROJECT` | `Self-service` | Project; empty for none |
| `TERRAKUBE_TAGS` | `true` | Workspace tags |
| `TERRAKUBE_TOKEN`, `TERRAKUBE_TOKEN_FILE` | | Token as a value or a file |
| `OPENBAO_ADDR`, `OPENBAO_ROLE` | , `terrakube-selfservice` | OpenBao/Vault; enables access details and the token source |
| `OPENBAO_SECRET_PATH`, `OPENBAO_SECRET_KEY` | `secret/data/terrakube-selfservice`, `terrakube_token` | Token in OpenBao |
| `ACCESS_SECRET_PATH` | `secret/data/labs/{name}` | Access details path |
| `ADMIN_EMAILS`, `AUDITOR_EMAILS` | | Comma-separated admins and auditors |
| `USER_TOKEN_ISSUER`, `USER_TOKEN_AUDIENCE`, `USER_TOKEN_JWKS_URL`, `USER_TOKEN_EMAIL_CLAIM` | , , discovery, `email` | Token mode; the audience is required with an issuer |
| `REQUIRE_VERIFIED_EMAIL` | `true` | Reject OIDC identities without `email_verified: true` |
| `UVICORN_SSL_CERTFILE`, `UVICORN_SSL_KEYFILE` | | Serve HTTPS (the chart's `tls.secretName` sets them) |
| `ALLOW_INSECURE_TRANSPORT` | `false` | Development only: allow `http://` (Terrakube, OpenBao, OIDC issuers, JWKS) and unverified database TLS; loopback is always allowed |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Loop interval |
| `PENDING_TIMEOUT_MINUTES` | `10` | When a stuck `pending` lab is marked failed |
| `DELETE_WORKSPACE_AFTER_DESTROY` | `true` | Remove workspaces after destroy |
| `UI_ENABLED`, `UI_PUBLIC_URL`, `UI_TITLE` | `false`, `http://localhost:8080/ui`, `Lab self-service` | Web portal |
| `UI_OIDC_ISSUER`, `UI_OIDC_CLIENT_ID`, `UI_OIDC_CLIENT_SECRET`, `UI_OIDC_SCOPES` | , , , `openid email profile` | Portal sign-in |
| `UI_SESSION_SECRET`, `UI_SESSION_MAX_AGE_HOURS`, `UI_SESSION_HTTPS_ONLY` | , `8`, `true` with OIDC | Session cookie |
| `UI_DEV_USER_EMAIL` | | Development only: no sign-in, everyone acts as this user (ignored when an issuer is set) |
