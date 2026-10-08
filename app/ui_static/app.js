// Lab self-service portal. Plain JavaScript, no build step.
// Every call goes to api/... (relative to <base>), answered by the service's /ui/api
// routes with the signed-in user from the session cookie.
"use strict";

const view = document.getElementById("view");
const SETTLED = new Set(["ready", "failed", "destroyed", "destroy_failed"]);
let me = null;
let timer = null;

// ---- helpers ---------------------------------------------------------------

// Build DOM safely: strings become text nodes, never HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function show(...nodes) { view.replaceChildren(...nodes); }

function toast(message, error = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = error ? "error" : "";
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 6000);
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(`api/${path}`, {
    method,
    headers: { "X-Requested-With": "selfservice-ui", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (response.status === 401) { location.href = "login"; throw new Error("sign-in required"); }
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

const money = (value, currency = "USD") =>
  value == null ? "–" : new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: value < 1 ? 3 : 2 }).format(value);
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "–");

function untilText(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return "expired";
  const hours = Math.floor(ms / 3600000), minutes = Math.floor((ms % 3600000) / 60000);
  return hours ? `in ${hours} h ${minutes} min` : `in ${minutes} min`;
}

const badge = (status) => h("span", { class: `badge ${status}` }, status.replace("_", " "));

function pollWhile(condition, refresh) {
  clearTimeout(timer);
  if (condition) timer = setTimeout(refresh, 20000);
}

// ---- catalog and form ------------------------------------------------------

async function catalogView() {
  const { items } = await api("templates");
  show(
    h("h1", {}, "New lab"),
    h("div", { class: "cards" }, items.map((t) =>
      h("div", { class: "card" },
        h("h3", {}, t.name),
        h("p", {}, t.description || ""),
        h("p", {}, `Lifetime ${t.default_ttl_hours} h by default, up to ${t.max_ttl_hours} h`),
        h("a", { class: "button", href: `#/new/${t.id}` }, "Configure")))),
  );
}

function inputField(spec) {
  const common = { name: spec.name, required: spec.required && spec.default == null };
  let control;
  if (spec.type === "boolean") {
    control = h("input", { type: "checkbox", ...common, checked: spec.default === true });
    return h("label", { class: "check" }, control, spec.label, spec.description && h("small", {}, spec.description));
  }
  if (spec.type === "enum") {
    control = h("select", common,
      !spec.required && spec.default == null ? h("option", { value: "" }, "(default)") : null,
      spec.options.map((o) => h("option", { value: o, selected: o === spec.default }, o)));
  } else {
    control = h("input", {
      ...common, type: spec.type === "number" ? "number" : spec.sensitive ? "password" : "text",
      value: spec.default ?? "", min: spec.minimum, max: spec.maximum, pattern: spec.pattern,
    });
  }
  return h("label", {}, spec.label + (common.required ? " *" : ""), control, spec.description && h("small", {}, spec.description));
}

function readInputs(form, template) {
  const inputs = {};
  for (const spec of template.inputs) {
    const el = form.elements[spec.name];
    if (spec.type === "boolean") inputs[spec.name] = el.checked;
    else if (el.value === "") continue;
    else inputs[spec.name] = spec.type === "number" ? Number(el.value) : el.value;
  }
  return inputs;
}

async function newLabView(templateId) {
  const t = await api(`templates/${encodeURIComponent(templateId)}`);
  const estimate = h("div", { class: "estimate" }, "Estimating cost…");
  const ttl = h("input", { type: "number", name: "__ttl", min: 1, max: t.max_ttl_hours, value: t.default_ttl_hours });
  const submit = h("button", { type: "submit" }, "Create lab");
  const form = h("form", { class: "lab-form" },
    t.inputs.map(inputField),
    h("label", {}, `Lifetime (hours, up to ${t.max_ttl_hours})`, ttl),
    estimate,
    h("div", { class: "actions" }, submit, h("a", { class: "button secondary", href: "#/catalog" }, "Cancel")));

  let pending;
  const refreshEstimate = () => {
    clearTimeout(pending);
    pending = setTimeout(async () => {
      try {
        const e = await api(`templates/${t.id}/estimate`, { method: "POST", body: { inputs: readInputs(form, t), ttl_hours: Number(ttl.value) } });
        estimate.replaceChildren(
          "Estimated ", h("b", {}, money(e.total, e.currency)), ` for ${e.ttl_hours} h (${money(e.hourly, e.currency)}/h)`,
          h("div", { class: "note" }, e.items.map((i) => `${i.label} ${money(i.hourly, e.currency)}/h`).join(" · ")));
      } catch (error) {
        estimate.textContent = error.status === 404 ? "No cost estimate for this template." : `Estimate unavailable: ${error.message}`;
      }
    }, 300);
  };
  form.addEventListener("input", refreshEstimate);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const lab = await api("labs", { method: "POST", body: { template_id: t.id, ttl_hours: Number(ttl.value), inputs: readInputs(form, t) } });
      toast(`Lab ${lab.name} is being created`);
      location.hash = `#/labs/${lab.id}`;
    } catch (error) {
      toast(error.message, true);
      submit.disabled = false;
    }
  });
  show(h("h1", {}, `New lab: ${t.name}`), h("p", { class: "muted" }, t.description || ""), form);
  refreshEstimate();
}

// ---- labs ------------------------------------------------------------------

async function labsView() {
  const history = new URLSearchParams(location.hash.split("?")[1]).has("history");
  const { items } = await api(`labs${history ? "?include_destroyed=true" : ""}`);
  const rows = items.map((l) =>
    h("tr", { class: "click", onclick: () => (location.hash = `#/labs/${l.id}`) },
      h("td", {}, h("b", {}, l.name), h("div", { class: "note" }, l.template_id)),
      me.admin ? h("td", {}, l.owner_email) : null,
      h("td", {}, badge(l.status)),
      h("td", {}, l.status === "destroyed" ? when(l.destroyed_at) : untilText(l.expires_at)),
      h("td", {}, money(l.estimated_cost, l.currency || "USD"))));
  show(
    h("h1", {}, me.admin ? "Labs (all owners)" : "My labs"),
    h("div", { class: "actions" },
      h("a", { class: "button", href: "#/catalog" }, "New lab"),
      h("a", { class: "button secondary", href: history ? "#/labs" : "#/labs?history" }, history ? "Hide destroyed" : "Show destroyed")),
    items.length
      ? h("table", {}, h("tr", {}, h("th", {}, "Lab"), me.admin ? h("th", {}, "Owner") : null, h("th", {}, "Status"), h("th", {}, history ? "Expires / destroyed" : "Expires"), h("th", {}, "Cost so far")), rows)
      : h("p", { class: "muted" }, "No labs yet."),
  );
  pollWhile(items.some((l) => !SETTLED.has(l.status)), () => route());
}

async function act(path, body, message) {
  try {
    await api(path, { method: "POST", body });
    toast(message);
  } catch (error) {
    toast(error.message, true);
  }
  route();
}

function accessBlock(lab) {
  const box = h("div", {});
  const button = h("button", { class: "secondary", onclick: async () => {
    button.disabled = true;
    try {
      const { values } = await api(`labs/${lab.id}/access`);
      box.replaceChildren(h("h2", {}, "Access details"), h("dl", { class: "facts" }, Object.entries(values).map(([key, value]) => {
        const multiline = value.includes("\n");
        const secret = /pass|token|secret|key/i.test(key);
        let dd;
        if (multiline) {
          dd = h("dd", {},
            h("button", { class: "secondary", onclick: () => download(`${lab.name}.${key === "kubeconfig" ? "kubeconfig" : "txt"}`, value) }, `Download ${key}`),
            " ", h("button", { class: "secondary", onclick: () => copy(value) }, "Copy"));
        } else if (secret) {
          const code = h("code", {}, "••••••••");
          dd = h("dd", { class: "secret" }, code,
            h("button", { class: "secondary", onclick: () => (code.textContent = code.textContent.startsWith("•") ? value : "••••••••") }, "Show"),
            h("button", { class: "secondary", onclick: () => copy(value) }, "Copy"));
        } else {
          dd = h("dd", {}, value);
        }
        return [h("dt", {}, key), dd];
      })), h("p", { class: "note" }, "Shown on request only and recorded in the lab's history. Do not share."));
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  } }, "Show access details");
  box.append(button);
  return box;
}

function download(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  h("a", { href: url, download: filename }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function copy(text) {
  await navigator.clipboard.writeText(text);
  toast("Copied");
}

async function labView(id) {
  const [lab, { items: events }] = await Promise.all([api(`labs/${id}`), api(`labs/${id}/events`)]);
  const currency = lab.currency || "USD";
  const hours = h("select", {}, [1, 2, 4, 8, 24].map((n) => h("option", { value: n }, `+${n} h`)));
  const live = !["destroying", "destroyed"].includes(lab.status);
  show(
    h("h1", {}, lab.name, " ", badge(lab.status)),
    h("dl", { class: "facts" },
      h("dt", {}, "Template"), h("dd", {}, lab.template_id),
      h("dt", {}, "Owner"), h("dd", {}, lab.owner_email),
      lab.status_detail && [h("dt", {}, "Detail"), h("dd", {}, lab.status_detail)],
      h("dt", {}, "Created"), h("dd", {}, when(lab.created_at)),
      h("dt", {}, "Ready"), h("dd", {}, when(lab.ready_at)),
      lab.status === "destroyed"
        ? [h("dt", {}, "Destroyed"), h("dd", {}, `${when(lab.destroyed_at)} (${lab.destroy_reason || "–"})`)]
        : [h("dt", {}, "Expires"), h("dd", {}, `${when(lab.expires_at)} (${untilText(lab.expires_at)})`)],
      h("dt", {}, "Estimated cost"), h("dd", {}, `${money(lab.estimated_cost, currency)} so far · ${money(lab.estimated_hourly_cost, currency)}/h`),
      h("dt", {}, "Inputs"), h("dd", {}, Object.entries(lab.inputs).map(([k, v]) => `${k}=${v}`).join(", ") || "–"),
      lab.workspace_url && [h("dt", {}, "Terrakube"), h("dd", {}, h("a", { href: lab.workspace_url, target: "_blank", rel: "noopener" }, "Workspace and run logs"))]),
    h("div", { class: "actions" },
      lab.status === "ready" ? accessBlock(lab) : null,
      ["ready", "provisioning", "pending", "failed"].includes(lab.status)
        ? [hours, h("button", { class: "secondary", onclick: () => act(`labs/${id}/extend`, { hours: Number(hours.value) }, "Lifetime extended") }, "Extend")]
        : null,
      lab.status === "failed" ? h("button", { class: "secondary", onclick: () => act(`labs/${id}/retry`, null, "Retrying") }, "Retry") : null,
      live ? h("button", { class: "danger", onclick: () => confirm(`Destroy ${lab.name}? This cannot be undone.`) && act(`labs/${id}/destroy`, { reason: "requested in portal" }, "Destroying") }, "Destroy") : null),
    h("h2", {}, "History"),
    h("ul", { class: "events" }, events.slice().reverse().map((e) => h("li", {}, h("time", {}, when(e.at)), e.type.replaceAll("_", " "), e.actor ? ` · ${e.actor}` : ""))),
    h("p", {}, h("a", { href: "#/labs" }, "← All labs")),
  );
  pollWhile(!SETTLED.has(lab.status), () => route());
}

// ---- admin -----------------------------------------------------------------

async function adminView() {
  const days = Number(new URLSearchParams(location.hash.split("?")[1]).get("days") || 30);
  const [summary, costs] = await Promise.all([api(`analytics/summary?days=${days}`), api(`analytics/costs?days=${days}`)]);
  const stat = (value, label) => h("div", { class: "stat" }, h("b", {}, value), h("span", {}, label));
  show(
    h("h1", {}, "Admin"),
    h("div", { class: "actions" }, [7, 30, 90].map((d) => h("a", { class: `button ${d === days ? "" : "secondary"}`, href: `#/admin?days=${d}` }, `${d} days`))),
    h("div", { class: "stats" },
      stat(summary.active_labs, "active labs"),
      stat(summary.needs_attention, "need attention"),
      stat(summary.created, "created"),
      stat(summary.expired, "expired"),
      stat(summary.provision_failures, "failed to provision"),
      stat(money(costs.estimated_cost, costs.currency), `estimated cost, ${Math.round(costs.lab_hours)} lab-hours`)),
    h("h2", {}, "Cost per owner"),
    h("table", {},
      h("tr", {}, h("th", {}, "Owner"), h("th", {}, "Template"), h("th", {}, "Labs"), h("th", {}, "Lab-hours"), h("th", {}, "Estimated cost")),
      costs.owners.flatMap((o) => [
        h("tr", {}, h("td", {}, h("b", {}, o.owner_email)), h("td", {}, "all"), h("td", {}, o.labs), h("td", {}, o.lab_hours), h("td", {}, h("b", {}, money(o.estimated_cost, costs.currency)))),
        ...o.templates.map((t) => h("tr", {}, h("td", {}), h("td", {}, t.template_id), h("td", {}, t.labs), h("td", {}, t.lab_hours), h("td", {}, money(t.estimated_cost, costs.currency)))),
      ])),
    h("h2", {}, "Per template"),
    h("table", {},
      h("tr", {}, h("th", {}, "Template"), h("th", {}, "Created"), h("th", {}, "Active"), h("th", {}, "Failed"), h("th", {}, "Avg. lifetime"), h("th", {}, "Avg. time to ready")),
      summary.templates.map((t) => h("tr", {}, h("td", {}, t.template_id), h("td", {}, t.created), h("td", {}, t.active), h("td", {}, t.failed),
        h("td", {}, t.avg_lifetime_hours == null ? "–" : `${t.avg_lifetime_hours} h`), h("td", {}, t.avg_provision_minutes == null ? "–" : `${t.avg_provision_minutes} min`)))),
    h("p", { class: "note" }, costs.method),
  );
}

// ---- routing ---------------------------------------------------------------

async function route() {
  clearTimeout(timer);
  const [path] = location.hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean);
  for (const a of document.querySelectorAll("[data-nav]")) a.classList.toggle("active", a.dataset.nav === (parts[0] === "new" ? "catalog" : parts[0] || "catalog"));
  try {
    if (parts[0] === "new" && parts[1]) await newLabView(parts[1]);
    else if (parts[0] === "labs" && parts[1]) await labView(parts[1]);
    else if (parts[0] === "labs") await labsView();
    else if (parts[0] === "admin" && me.admin) await adminView();
    else await catalogView();
  } catch (error) {
    show(h("h1", {}, "Something went wrong"), h("p", {}, error.message), h("p", {}, h("a", { href: "#/labs" }, "My labs")));
  }
}

(async () => {
  me = await api("me");
  document.getElementById("user-email").textContent = me.email;
  document.getElementById("nav-admin").hidden = !me.admin;
  window.addEventListener("hashchange", route);
  route();
})();
