# Portal integration requirements

What a portal (for example an internal PS tool) must do to offer terrakube-selfservice labs to its users. The API contract is [`openapi.yaml`](../openapi.yaml); this document covers what the contract cannot: where calls come from, how users are identified, and how secrets are handled.

## 1. Call the service from the portal backend only

```
browser ──(portal session)──► portal backend ──(API key + user identity)──► terrakube-selfservice
```

- **The browser never calls the service.** The API key and every response pass through the portal backend.
- **Keep the API key server-side**, in the portal's secret store, never in frontend code, HTML or browser storage.
- **Rotation without downtime:** the service accepts several keys (`apiKeys` in the chart). Add the new key, switch the portal, then remove the old one.
- **Network:** the service is cluster-internal by default. Expose it (chart `route.*`) only as far as the portal backend needs; it is still protected by the key.

## 2. Identify the user on every call

Every lab endpoint acts on behalf of one user. The service limits each user to their own labs, and returns `404` for other users' labs so their existence is not revealed. Admins (chart `users.adminEmails`) see all labs and analytics.

The portal must use one of two modes:

| Mode | Header | When | The portal must |
|---|---|---|---|
| **Token (recommended)** | `X-User-Token: <OIDC ID token>` | The portal signs users in with an OIDC provider the service can verify (chart `users.token.issuer`, `users.token.audience`). | Forward the signed-in user's ID token, unchanged. The service verifies its signature, issuer, audience and expiry, and uses its `email` claim. A forged or foreign token is rejected, so a portal bug cannot act as another user. |
| **Header** | `X-Actor-Email: <email>` | The portal cannot forward a token. | Set the header **only from the portal's own authenticated session**. Never copy it from a request parameter, form field, cookie or anything the browser sends. The service trusts it completely. |

A request without a valid identity gets `401`. In token mode, `X-Actor-Email` is ignored.

**Minimum for header mode:** a portal user must not be able to change which email the backend sends. If that cannot be guaranteed, use token mode.

## 3. Screens and the calls behind them

### Catalog and form

`GET /v1/templates` returns every template with its `inputs`. Build the form from them. Do not hard-code templates, because the catalog is deployment configuration and changes without a portal release.

| `inputs[].type` | Widget | Send as |
|---|---|---|
| `string` | text field; validate `pattern` if present | string |
| `number` | number field; enforce `minimum` / `maximum` | number |
| `boolean` | checkbox or toggle | `true` / `false` |
| `enum` | select from `options` | one of `options` (string) |

- Pre-fill `default`, and mark `required` inputs. Show `label` and `description`.
- `sensitive` inputs: use a password field. The service never returns their values.
- Offer a lifetime between 1 hour and `max_ttl_hours`, defaulting to `default_ttl_hours`.
- The service validates everything again. Show its `422` messages next to the form.
- **Show the cost** before submitting: call `POST /v1/templates/{id}/estimate` with the current inputs and TTL whenever they change (debounced), and show `hourly` and `total` with `currency`. `404` means the template has no cost model; hide the estimate.

### Create

```http
POST /v1/labs
{"template_id": "lke-lab", "ttl_hours": 2, "inputs": {"node_pool_instance_count": 2}}
```

- `owner_email` defaults to the calling user. Only admins may set another owner.
- `name` is optional. Omit it to get a random `<owner>-<5 chars>` name; if a user picks one, handle `409` (taken).
- The response is `202` with the lab in `provisioning`. Go to the lab page.

### My labs and the lab page

- `GET /v1/labs` lists the caller's labs (admins: everyone's; filter with `owner_email`). `?include_destroyed=true` adds history.
- `GET /v1/labs/{id}` while `pending` or `provisioning`: **poll every 15 to 30 seconds**, and stop when the status is final for now (`ready`, `failed`, `destroyed`, `destroy_failed`). Provisioning takes minutes (LKE about 5 to 10).
- Show `expires_at` in the user's time zone, `status_detail`, and `workspace_url` for users who can open Terrakube.
- Show `estimated_cost` (so far) and `estimated_hourly_cost` with `currency`, labelled as an estimate. Extending a lab adds hours at the same hourly price.

| Status | Show | Offer |
|---|---|---|
| `pending`, `provisioning` | progress, `status_detail` | Destroy |
| `ready` | expiry countdown | Show access details, Extend, Destroy |
| `failed` | `status_detail` | Retry, Destroy |
| `destroying` | progress | none |
| `destroy_failed` | "cleanup needs attention" | Destroy (retries cleanup); tell an admin |
| `destroyed` | when and why (`destroy_reason`) | none |

### Access details (secrets)

`GET /v1/labs/{id}/access` returns what the template published for a `ready` lab (kubeconfig, passwords, URLs, SSH commands) as `values`.

- **Fetch only when the user asks**, for example a "Show access details" button, not when loading the lab page.
- **Do not cache, store or log** the response. The service sends `Cache-Control: no-store`; keep it on the way to the browser and exclude this route from request/response logging and APM payload capture.
- Offer multi-line values (kubeconfig) as a download (`<lab name>.kubeconfig`), and short values (passwords) behind a reveal and copy button.
- Every read is recorded in the lab's audit trail as `access_viewed`, with the user.
- Expected errors: `409` not ready yet, `404` no details published (some templates publish none) or not visible to this user, `501` not configured on this installation.

### Extend, retry, destroy

- **Extend:** `POST /v1/labs/{id}/extend {"hours": 4}`. The service caps the lifetime at `max_ttl_hours` from creation; `409` means the cap is reached.
- **Retry:** `POST /v1/labs/{id}/retry` re-runs the apply of a `failed` lab in its existing workspace. `409` explains when that is not possible (nothing was created, or the lab expired).
- **Destroy:** `POST /v1/labs/{id}/destroy {"reason": "done"}`. Ask for confirmation first. The lab moves to `destroying`, then `destroyed`.
- **History:** `GET /v1/labs/{id}/events` for an activity list on the lab page.

### Admin views

`GET /v1/analytics/summary?days=30` and `/timeseries` (admins only, `403` otherwise) for a usage dashboard: labs per template and owner, expiries, failures, lifetimes, time to ready.

`GET /v1/analytics/costs?days=30` (admins only) for a cost view: totals, then each owner with lab count, lab-hours and estimated cost split by template, and per-template totals. Show `method` next to the numbers, so readers know they are list-price estimates.

## 4. Errors

Every error body is `{"detail": "..."}` (a list of field errors for `422`).

| Code | Meaning | Portal behaviour |
|---|---|---|
| 401 | Bad API key, or missing/invalid user identity | Alert operators (key) or re-authenticate the user (token) |
| 403 | Not allowed for this user (admin-only action) | Hide the action for non-admins |
| 404 | Not found, or another user's lab | Treat both the same: "lab not found" |
| 409 | Not possible in the lab's current state | Show `detail`, reload the lab |
| 422 | Invalid input | Show next to the form fields |
| 501 | Feature not configured | Hide the feature |
| 502 | Terrakube or OpenBao call failed | Show "try again later"; the lab records what happened |

## 5. Checklist

- [ ] Calls go from the portal backend only; the API key never reaches a browser.
- [ ] Token mode, or header mode with the email taken from the portal session only.
- [ ] Forms are built from `GET /v1/templates`, not hard-coded.
- [ ] Polling stops at final statuses and backs off on errors.
- [ ] Access details are fetched on demand, never cached, stored or logged.
- [ ] Destroy asks for confirmation.
- [ ] Admin-only views are hidden from other users.
