// Hermetic run history: a services list, a history table per service and a run detail view, all
// over /api/services. The page only calls those routes, so it works unchanged against a server
// that keeps the history for many services.
//
// Everything the API returns is data about a service under test, so it only ever reaches the
// page as text (text nodes), never as markup. Styles are set through the DOM because the
// content security policy forbids inline style attributes.

const app = document.getElementById("app");
const TREND_RUNS = 20;
const RUN_LIMIT = 200; // the most runs the history table lists; the API caps the list to match
const TABS = ["failed", "added", "modified", "dropped", "all"];
const TAB_LABELS = {
  failed: "Failed",
  added: "Added",
  modified: "Modified",
  dropped: "Dropped",
  all: "All",
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined));
  return node;
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(response.status === 404 ? "not found" : `HTTP ${response.status}`);
  }
  return response.json();
}

const serviceLink = (service) => `#/services/${encodeURIComponent(service)}`;
const runLink = (run) =>
  `${serviceLink(run.service)}/runs/${encodeURIComponent(run.run_id)}`;
const ms = (value) => `${Math.round(value)} ms`;
const badge = (label, kind) => el("span", { class: `badge ${kind}` }, label);

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function countResults(run) {
  return run.passed + run.failed + run.errors;
}

function passRate(run) {
  const total = countResults(run);
  return total === 0 ? null : run.passed / total;
}

function changeBadges(run) {
  const parts = [
    [run.added, "+", "added"],
    [run.modified, "~", "modified"],
    [run.dropped, "−", "dropped"],
  ];
  return parts
    .filter(([count]) => count > 0)
    .map(([count, sign, kind]) => badge(`${sign}${count} ${kind}`, kind));
}

// Round down: a run with any failure must not read "100% passed" beside its red bar.
// Integer arithmetic: 29 of 100 is 0.29 * 100 = 28.999..., which would read 28%.
function passedLabel(run) {
  const rate = passRate(run);
  if (rate === null) return "no results";
  const percent = rate === 1 ? 100 : Math.floor((run.passed * 100) / countResults(run));
  return `${percent}% passed`;
}

// ---- services view --------------------------------------------------------------------------

function resultBadge(run) {
  const rate = passRate(run);
  const kind = rate === null ? "skipped" : rate === 1 ? "passed" : "failed";
  return badge(passedLabel(run), kind);
}

function serviceRow(summary, runs) {
  const latest = summary.latest;
  return el(
    "tr",
    {},
    el("td", {}, el("a", { href: serviceLink(summary.service) }, summary.service)),
    el("td", { class: "num" }, String(summary.runs)),
    el("td", {}, el("a", { href: runLink(latest) }, latest.run_id)),
    el("td", {}, formatTime(latest.started_at)),
    el("td", {}, resultBadge(latest)),
    el("td", {}, runs.length > 0 ? trend(runs, true) : null),
  );
}

// `histories` holds the newest runs of each service, in the order of `services`.
function renderServices(services, histories) {
  const title = "Hermetic services";
  const view = el("section", {}, el("h1", {}, "Services"));
  if (services.length === 0) {
    view.append(el("p", { class: "empty" }, "No services yet. `hermetic run` records a run for one."));
    return { title, view };
  }
  const heads = ["Service", "Runs", "Latest run", "Started", "Latest result", "Recent runs"];
  const header = el(
    "tr",
    {},
    heads.map((name, index) => el("th", { class: index === 1 ? "num" : "" }, name)),
  );
  const rows = services.map((summary, index) => serviceRow(summary, histories[index]));
  view.append(el("table", {}, el("thead", {}, header), el("tbody", {}, rows)));
  return { title, view };
}

// ---- history view ---------------------------------------------------------------------------

function trend(runs, compact = false) {
  const recent = runs.slice(0, TREND_RUNS).reverse(); // the API is newest first
  const bars = recent.map((run) => {
    const rate = passRate(run);
    const kind = rate === null ? "none" : rate === 1 ? "ok" : "bad";
    const label = passedLabel(run);
    const bar = el("a", {
      class: `bar ${kind}`,
      href: runLink(run),
      title: `${run.run_id}: ${label}`,
      "aria-label": `${run.run_id}: ${label}`,
    });
    bar.style.height = `${rate === null ? 4 : Math.max(4, rate * 100)}%`;
    return bar;
  });
  return el(
    "div",
    { class: compact ? "trend compact" : "trend", "aria-label": "Pass rate of the most recent runs" },
    bars,
  );
}

function historyRow(run) {
  return el(
    "tr",
    {},
    el("td", {}, el("a", { href: runLink(run) }, run.run_id)),
    el("td", {}, formatTime(run.started_at)),
    el("td", {}, run.target),
    el("td", { class: "num" }, String(run.passed)),
    el("td", { class: run.failed > 0 ? "num bad" : "num" }, String(run.failed)),
    el("td", { class: run.errors > 0 ? "num bad" : "num" }, String(run.errors)),
    el("td", { class: "num" }, String(run.skipped)),
    el("td", {}, changeBadges(run)),
  );
}

// The render functions return the page's title with its view, so the caller sets both together,
// and only if the response is still the one being waited for.
function renderHistory(service, runs) {
  const title = `${service} runs · Hermetic`;
  const view = el(
    "section",
    {},
    el("p", {}, el("a", { href: "#/" }, "← All services")),
    el("h1", {}, service),
  );
  if (runs.length === 0) {
    view.append(el("p", { class: "empty" }, "No runs for this service yet. `hermetic run` records one."));
    return { title, view };
  }
  const heads = ["Run", "Started", "Target", "Passed", "Failed", "Errors", "Skipped", "Changes"];
  const header = el(
    "tr",
    {},
    heads.map((name, index) => el("th", { class: index >= 3 && index <= 6 ? "num" : "" }, name)),
  );
  view.append(
    trend(runs),
    el("table", {}, el("thead", {}, header), el("tbody", {}, runs.map(historyRow))),
  );
  if (runs.length >= RUN_LIMIT) {
    view.append(el("p", { class: "empty" }, `Showing the newest ${RUN_LIMIT} runs.`));
  }
  return { title, view };
}

// ---- run detail view ------------------------------------------------------------------------

const pre = (value) => el("pre", {}, value);

function headerLines(headers) {
  return Object.entries(headers)
    .map(([name, value]) => `${name}: ${value}`)
    .join("\n");
}

function copyButton(value) {
  const status = el("span", { class: "copied", role: "status" });
  const button = el(
    "button",
    {
      type: "button",
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(value);
          status.textContent = "Copied";
        } catch {
          status.textContent = "Could not copy: select the command and copy it by hand";
        }
      },
    },
    "Copy curl",
  );
  return el("p", { class: "actions" }, button, status);
}

function exchangeView(exchange) {
  const outcome = exchange.status_code === null ? "no response" : exchange.status_code;
  const view = el(
    "div",
    {},
    el("h4", {}, `${exchange.method} ${exchange.url} → ${outcome} (${ms(exchange.duration_ms)})`),
  );
  const sections = [
    ["Request headers", headerLines(exchange.request_headers)],
    ["Request body", exchange.request_body],
    ["Response headers", headerLines(exchange.response_headers)],
    ["Response body", exchange.response_body],
  ];
  for (const [title, body] of sections) {
    if (body) view.append(el("h3", {}, title), pre(body));
  }
  if (exchange.request_body && !exchange.request_body_complete) {
    view.append(el("p", { class: "empty" }, "The request body is shortened, so it cannot be replayed."));
  }
  return view;
}

function resultRow(result) {
  const summary = el(
    "summary",
    {},
    badge(result.status, result.status),
    el("span", { class: "test-id" }, result.test_id),
    result.operation_id ? el("span", { class: "op" }, result.operation_id) : null,
    el("span", { class: "ms" }, ms(result.duration_ms)),
  );
  const detail = el("div", { class: "detail" });
  if (result.message) detail.append(el("h3", {}, "What happened"), pre(result.message));
  detail.append(...result.exchanges.map(exchangeView));
  if (result.curl) detail.append(el("h3", {}, "Reproduce"), pre(result.curl), copyButton(result.curl));
  return el("details", { class: "test" }, summary, detail);
}

function labelled(title, body) {
  return el("div", {}, el("h4", {}, title), pre(body ?? "(none)"));
}

function changeRow(change) {
  const row = el(
    "li",
    { class: "change" },
    el("div", {}, badge(change.kind, change.kind), el("span", { class: "test-id" }, change.test_id)),
    el("p", { class: "reason" }, change.reason),
  );
  if (change.kind === "modified" && (change.before || change.after)) {
    row.append(
      el("div", { class: "diff" }, labelled("Before", change.before), labelled("After", change.after)),
    );
  }
  return row;
}

function panelFor(tab, items) {
  if (items.length === 0) return el("p", { class: "empty" }, "Nothing here.");
  if (tab === "failed" || tab === "all") return el("div", {}, items.map(resultRow));
  return el("ul", { class: "changes" }, items.map(changeRow));
}

function renderRun(run) {
  const failing = run.results.filter((r) => r.status === "failed" || r.status === "error");
  const items = {
    failed: failing,
    added: run.changes.filter((c) => c.kind === "added"),
    modified: run.changes.filter((c) => c.kind === "modified"),
    dropped: run.changes.filter((c) => c.kind === "dropped"),
    all: run.results,
  };
  const facts = [
    ["Target", run.target],
    ["Started", formatTime(run.started_at)],
    ["Finished", formatTime(run.finished_at)],
    ["Spec hash", run.spec_hash ?? "none"],
    ["Prompt version", run.prompt_version ?? "none"],
    ["Hermetic", run.tool_version],
  ];
  const meta = el(
    "dl",
    { class: "meta" },
    facts.flatMap(([name, value]) => [el("dt", {}, name), el("dd", {}, value)]),
  );

  const tablist = el("div", { role: "tablist", class: "tabs" });
  const panel = el("div", { role: "tabpanel", class: "panel" });
  const select = (tab) => {
    for (const button of tablist.children) {
      const selected = button.getAttribute("data-tab") === tab;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    panel.replaceChildren(panelFor(tab, items[tab]));
  };
  for (const tab of TABS) {
    tablist.append(
      el(
        "button",
        { role: "tab", type: "button", "data-tab": tab, onclick: () => select(tab) },
        `${TAB_LABELS[tab]} (${items[tab].length})`,
      ),
    );
  }
  select(items.failed.length > 0 ? "failed" : "all");

  const view = el(
    "section",
    {},
    el("p", {}, el("a", { href: serviceLink(run.service) }, `← ${run.service} runs`)),
    el("h1", {}, run.run_id),
    meta,
    tablist,
    panel,
  );
  return { title: `Run ${run.run_id} · Hermetic`, view };
}

// ---- routing --------------------------------------------------------------------------------

let navigation = 0;

// Hash segments arrive percent-encoded; decode one, and encode it again for the API path.
const decoded = (segment) => decodeURIComponent(segment);
const apiSegment = (segment) => encodeURIComponent(decoded(segment));

async function load(hash) {
  // `#/runs/<service>/<id>` is the older form of a run's address; old links keep working.
  const run = /^#\/services\/([^/]+)\/runs\/([^/]+)$/.exec(hash) ?? /^#\/runs\/([^/]+)\/([^/]+)$/.exec(hash);
  if (run) {
    return renderRun(await getJson(`/api/services/${apiSegment(run[1])}/runs/${apiSegment(run[2])}`));
  }
  const service = /^#\/services\/([^/]+)$/.exec(hash);
  if (service) {
    const runs = await getJson(`/api/services/${apiSegment(service[1])}/runs?limit=${RUN_LIMIT}`);
    return renderHistory(decoded(service[1]), runs);
  }
  const services = await getJson("/api/services");
  // One request per service for its trend; a service whose runs cannot be read still lists.
  const histories = await Promise.all(
    services.map((summary) =>
      getJson(`/api/services/${encodeURIComponent(summary.service)}/runs?limit=${TREND_RUNS}`).catch(
        () => [],
      ),
    ),
  );
  return renderServices(services, histories);
}

async function route() {
  const current = ++navigation;
  app.replaceChildren("Loading…");
  let page;
  try {
    page = await load(location.hash);
  } catch (error) {
    page = {
      title: "Not available · Hermetic",
      view: el("p", { class: "error" }, `Could not load this page: ${error.message}`),
    };
  }
  if (current === navigation) {
    document.title = page.title;
    app.replaceChildren(page.view);
  }
}

addEventListener("hashchange", route);
route();
