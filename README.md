# Terrakube Self-Service

A small API that turns [Terrakube](https://terrakube.io) into a self-service environment catalog. A portal (your internal tool, Backstage, a form) lists templates, shows their form, and creates a *lab*. The service creates a Terrakube workspace, applies it, follows the run, and destroys the lab when its time-to-live expires. It also keeps an audit trail and usage analytics.

```
portal ──HTTP──► terrakube-selfservice ──JSON:API──► Terrakube ──► workspace per lab
                      │
                      └── PostgreSQL (labs, events, analytics)
```

- **Templates with forms**: each template is a Terraform/OpenTofu module in Git, plus typed inputs (string, number, boolean, enum; required, defaults, patterns, ranges) that portals render as a form.
- **TTL policy**: default and maximum lifetime per template; labs can be extended up to the maximum and are destroyed automatically, then their workspace is deleted.
- **Analytics**: labs per template and owner, expiries, failures, lifetimes, time to ready, daily series.
- **Audit trail**: every state change with the acting user.

The API contract is [`openapi.yaml`](openapi.yaml).

## API

| Endpoint | Purpose |
|---|---|
| `GET /v1/templates`, `GET /v1/templates/{id}` | Templates and their form inputs |
| `POST /v1/labs` | Create a lab: `template_id`, `name`, `owner_email`, optional `ttl_hours`, `inputs` |
| `GET /v1/labs`, `GET /v1/labs/{id}` | Labs and their status |
| `POST /v1/labs/{id}/extend` | Extend the TTL (capped at the template's `max_ttl_hours` from creation) |
| `POST /v1/labs/{id}/destroy` | Destroy now; also retries a `destroy_failed` lab |
| `GET /v1/labs/{id}/events` | Audit trail |
| `GET /v1/analytics/summary`, `GET /v1/analytics/timeseries` | Usage analytics (`?days=30`) |

Requests carry `Authorization: Bearer <api key>`. Send `X-Actor-Email` with the end user's address to record who acted.

Lab status: `pending` → `provisioning` → `ready` or `failed` → `destroying` → `destroyed` or `destroy_failed`. A `failed` lab still expires and is destroyed, which cleans up partial resources. A `destroy_failed` lab waits for a person to inspect the Terrakube run and retry.

## Catalog

The catalog is deployment configuration, not part of this repository: each installation defines its own templates. Pass it to the Helm chart as the `catalog` value, or mount a file and point `CATALOG_PATH` at it. See [`examples/catalog.yaml`](examples/catalog.yaml).

```yaml
templates:
  - id: aws-lab
    name: AWS lab
    default_ttl_hours: 72
    max_ttl_hours: 336
    source:
      repository: https://github.com/your-org/lab-templates
      folder: /aws-lab
    inputs:
      - name: aws_region
        label: AWS region
        type: enum
        options: [eu-central-1, us-east-1]
        default: eu-central-1
```

- `inputs` become Terraform variables; `env` and `variables` add fixed ENV and Terraform variables to every lab.
- Every lab also gets `TF_VAR_lab_id`, `TF_VAR_lab_name`, `TF_VAR_lab_owner` and `TF_VAR_lab_expires_at`. Declare them in the module to tag resources.
- Validate a catalog before deploying: `python -m app.catalog check catalog.yaml` (also available in the image). [`catalog.schema.json`](catalog.schema.json) gives editor completion, and the Helm chart rejects an invalid `catalog` value at install time.
- The service validates the catalog at startup and refuses to start on errors, so a bad change never replaces a running version.

## Deploy

Requirements: Terrakube, PostgreSQL, and a Terrakube personal access token whose team has *Manage Workspaces* and *Manage Jobs* in the target organization.

```bash
helm install selfservice oci://ghcr.io/3sky/charts/terrakube-selfservice \
  --namespace terrakube \
  --set terrakube.uiUrl=https://terrakube.example.com \
  --set terrakube.organization=my-org \
  --set token.existingSecret.name=terrakube-selfservice-token \
  --set 'apiKeys[0]=<random key for the portal>' \
  --set database.host=postgres.example.com --set database.password=<password> \
  -f my-catalog-values.yaml   # catalog: { templates: [...] }
```

The Terrakube token can come from:

1. `token.existingSecret`: a Kubernetes Secret mounted as a file. It is re-read when Terrakube rejects the old token, so rotation needs no restart.
2. `token.openbao`: OpenBao or Vault kv-v2, read with the pod's Kubernetes service account (`serviceAccount.name`). Create a Kubernetes auth role bound to that service account with read access to `token.openbao.secretPath`.
3. `token.value`: stored in the chart's Secret. Use for development only.

`existingSecret` (with `API_KEYS` and `DATABASE_URL`) and `existingCatalogConfigMap` replace the chart-managed Secret and ConfigMap. `route.*` publishes the API through a Gateway API `HTTPRoute`; otherwise it is cluster-internal.

Environment variables (for running without the chart): `DATABASE_URL`, `API_KEYS` (comma-separated), `CATALOG_PATH`, `TERRAKUBE_API_URL`, `TERRAKUBE_UI_URL`, `TERRAKUBE_ORGANIZATION`, `TERRAKUBE_VCS_ID`, `TERRAKUBE_APPLY_TEMPLATE` (default `Plan and apply`), `TERRAKUBE_DESTROY_TEMPLATE` (default `Destroy`), one of `TERRAKUBE_TOKEN` / `TERRAKUBE_TOKEN_FILE` / `OPENBAO_ADDR` (+ `OPENBAO_ROLE`, `OPENBAO_SECRET_PATH`, `OPENBAO_SECRET_KEY`), `RECONCILE_INTERVAL_SECONDS` (default 30), `DELETE_WORKSPACE_AFTER_DESTROY` (default true).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
docker run -d --name tss-db -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:17-alpine
TEST_DATABASE_URL=postgresql://postgres:pw@localhost:5432/postgres PYTHONPATH=.:tests pytest tests
python -m scripts.export_specs   # regenerate openapi.yaml and the catalog/chart schemas
```

Tests fail when the committed specs are stale. Releases: push a `vX.Y.Z` tag to publish the image `ghcr.io/<owner>/terrakube-selfservice:X.Y.Z` and the chart `oci://ghcr.io/<owner>/charts/terrakube-selfservice:X.Y.Z`. Pushes to `main` publish an image tagged with the commit SHA.

## License

[MIT](LICENSE)
