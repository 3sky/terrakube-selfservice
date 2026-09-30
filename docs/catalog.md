# Catalog reference

The catalog lists the templates an installation offers. It is **deployment configuration**: it lives with the deployment (for example as the Helm chart's `catalog` value), not in this repository, and a change rolls the service. [`examples/catalog.yaml`](../examples/catalog.yaml) is a complete example, and [`catalog.schema.json`](../catalog.schema.json) gives editors completion and validation (`# yaml-language-server: $schema=...`).

## Shape

```yaml
currency: USD                 # of the prices below
prices:                       # per hour, used by cost models
  g6-standard-2: 0.036
  nodebalancer: 0.015
templates:
  - id: lke-lab                         # stable id; used by the API and in reports
    name: LKE cluster                   # shown in the portal
    description: Kubernetes cluster with ingress.
    default_ttl_hours: 8                # lifetime when the user does not choose
    max_ttl_hours: 72                   # longest lifetime, including extensions
    source:
      repository: https://github.com/your-org/lab-templates
      branch: main
      folder: /lke-lab                  # root module inside the repository
      iac_type: tofu                    # tofu or terraform
      iac_version: 1.12.6
      use_vcs_connection: true          # clone through Terrakube's VCS connection (private repos)
    variables:                          # fixed Terraform variables for every lab
      install_addons: "true"
    env:                                # fixed environment variables for every lab
      AWS_DEFAULT_REGION: eu-central-1
    inputs:                             # the form
      - name: node_pool_instance_count
        label: Node count
        type: number
        minimum: 1
        maximum: 10
        default: 3
    cost:                               # billable parts, for estimates
      - label: Worker nodes
        price: g6-standard-2
        quantity_from: node_pool_instance_count
      - label: NodeBalancer
        price: nodebalancer
```

## Inputs (the form)

Each input becomes a Terraform variable of the same `name` in the lab's workspace. Declare it in the template's `variables.tf`.

| Field | Meaning |
|---|---|
| `name` | Terraform variable name (`^[a-z][a-z0-9_]*$`) |
| `label`, `description` | Shown in the portal |
| `type` | `string`, `number`, `boolean` or `enum` |
| `required` | Must have a value: from the user, or from `default` |
| `default` | Pre-filled value; also used when the user leaves it out |
| `options` | Allowed values for `enum` |
| `pattern` | Regular expression a `string` must fully match |
| `minimum`, `maximum` | Range for `number` |
| `sensitive` | Hidden in API responses and in the Terrakube UI |

The service validates every request against these rules (`422` with a message per field) before it touches Terrakube. Values reach Terraform as strings; Terraform converts `"3"` and `"true"` to the declared type.

Inputs are scalar. For a list, use a comma-separated `string` input and split it in the module:

```hcl
variable "authorized_keys_csv" { default = "" }
locals { authorized_keys = compact([for k in split(",", var.authorized_keys_csv) : trimspace(k)]) }
```

Never put secrets in inputs. Templates read them at run time (for example from OpenBao), so they are neither typed into a form nor stored in the lab record.

## Variables every lab receives

The service sets these environment variables in each lab's workspace. Terraform ignores `TF_VAR_*` for variables a module does not declare, so declare the ones you use:

| Variable | Example | Use |
|---|---|---|
| `lab_id` | `6d138dae-e5c7-...` | Unique id, for resource names or state lookups |
| `lab_name` | `jwolynko-k3x9p` | Short, human-readable; also the workspace name `lab-<name>` |
| `lab_owner` | `alice@example.com` | Cloud tags, notifications |
| `lab_expires_at` | `2026-09-30T10:50:32+00:00` | Cloud tags, so cleanup tools can find expired resources |

```hcl
variable "lab_name"       { default = "" }
variable "lab_owner"      { default = "" }
variable "lab_expires_at" { default = "" }
```

Templates run without them too (locally or in CI), because every declaration has a default.

## Handing access to the owner

If the owner needs credentials (kubeconfig, passwords, URLs), the template writes them to OpenBao or Vault kv-v2 at the service's access path (default `secret/data/labs/<lab_name>`), and deletes them on destroy. The owner then gets them through `GET /v1/labs/{id}/access`. A template that publishes nothing is fine; that endpoint then answers `404`.

A template writes it with the credentials of whatever runs Terraform, for example a Terrakube executor role allowed to write `secret/data/labs/*`. See [deployment](deployment.md#openbao-or-vault) for the policies.

## Lifetimes

- A lab is created with `ttl_hours` from the request, or `default_ttl_hours`.
- `max_ttl_hours` caps the whole lifetime from creation, including every extension.
- When a lab expires, the service runs the Terrakube destroy template and removes the workspace. A lab that failed to provision still expires, which cleans up anything it created.

## Cost models

Estimates multiply list prices by hours; they are meant for "roughly what does this cost", not billing.

- `currency` and `prices` sit at the top of the catalog, once for all templates. Prices are per hour.
- Each template's `cost` is a list of billable parts:

| Field | Meaning |
|---|---|
| `label` | Shown in estimates |
| `price` | A fixed key in `prices` (for example `nodebalancer`) |
| `price_from` | An input whose value is a key in `prices` (for example `node_pool_instance_type`); a list tries each input in turn and uses the first one with a value |
| `quantity` | Fixed multiplier (for example 100 for 100 GB of storage priced per GB) |
| `quantity_from` | A `number` input to multiply by (for example `node_pool_instance_count`) |
| `when` | A `boolean` input; the part only counts when it is true |

Set exactly one of `price` and `price_from`. The catalog check fails when a price key is missing, including for any option of an `enum` input used in `price_from`, so the form can never offer an unpriced choice.

How numbers are produced:

- `POST /v1/templates/{id}/estimate`: the hourly price for the given inputs, times the lifetime.
- Each lab stores its hourly price **when it is created**. Later price changes do not rewrite history; labs created before a template had a cost model are priced from the catalog when the service starts.
- A lab's cost runs from creation to destruction (or now), but **only for labs that reached `ready`**: the service cannot know what a failed run created.
- Not included: discounts, taxes, data transfer, and anything a failed run left behind.

Keep `prices` current with your provider's list prices. For example, Linode publishes them at `https://api.linode.com/v4/linode/types` (also `/lke/types`, `/nodebalancers/types`, `/volumes/types`).

## Checking a catalog

```bash
python -m app.catalog check catalog.yaml            # from a checkout
docker run --rm -v $PWD/catalog.yaml:/c.yaml --entrypoint python \
  ghcr.io/3sky/terrakube-selfservice:<version> -m app.catalog check /c.yaml
```

The Helm chart also rejects an invalid `catalog` value at install time, and the service refuses to start with an invalid catalog, so a broken change never replaces a running version.
