// Page 1: run list + start-new-run form + run detail. Hash-routed within
// one page (#run/<id> for detail, no hash for the list) so back/forward
// and bookmarking work without a server-side router.

const PROVIDER_DEFAULT_MODELS = {
  openai: "gpt-4o-mini",
  nvidia: "meta/llama3-70b-instruct",
  ollama: "llama3",
  ollama_cloud: "deepseek-v4-flash",
};

const app = document.getElementById("app");
let pollTimer = null;

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children || []) {
    if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function badge(state) {
  const s = state || "none";
  return el("span", { class: `badge badge-${s}`, text: s });
}

function numOrNull(v) {
  const t = (v || "").trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isNaN(n) ? null : n;
}

function numOrDefault(v, d) {
  const n = numOrNull(v);
  return n === null ? d : n;
}

function fmtDate(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}

function errorBox(err) {
  const box = el("div", { class: "error-box" }, [
    err.message || String(err),
    err.detail ? el("div", { class: "hint", text: err.detail }) : null,
  ]);
  return box;
}

// ---------------------------------------------------------------- list

async function renderList() {
  app.innerHTML = "";
  app.appendChild(el("h2", { text: "Runs" }));

  const listHost = el("div", {});
  app.appendChild(listHost);
  listHost.appendChild(el("p", { class: "hint", text: "Loading runs…" }));

  try {
    const { runs } = await Api.listRuns();
    listHost.innerHTML = "";
    if (runs.length === 0) {
      listHost.appendChild(el("div", { class: "empty-state", text: "No runs yet. Start one below." }));
    } else {
      listHost.appendChild(renderRunsTable(runs));
    }
  } catch (err) {
    listHost.innerHTML = "";
    listHost.appendChild(errorBox(err));
  }

  app.appendChild(renderStartForm());
}

function renderRunsTable(runs) {
  const table = el("table", {}, [
    el("thead", {}, [
      el("tr", {}, ["Site", "Started", "Type", "Fetched / Discovered", "Pairs", "Status"].map(
        (h) => el("th", { text: h })
      )),
    ]),
  ]);
  const tbody = el("tbody", {});
  for (const r of runs) {
    tbody.appendChild(el("tr", {
      class: "clickable",
      onclick: () => { location.hash = `#run/${encodeURIComponent(r.run_id)}`; },
    }, [
      el("td", { text: r.root_url || r.run_id }),
      el("td", { text: fmtDate(r.started_at) }),
      el("td", { text: r.mode || "—" }),
      el("td", { text: `${r.pages_fetched} / ${r.pages_discovered}` }),
      el("td", { text: String(r.pair_count) }),
      el("td", {}, [badge(r.job_state)]),
    ]));
  }
  table.appendChild(tbody);
  return table;
}

function renderStartForm() {
  const panel = el("div", { class: "panel" });
  panel.appendChild(el("h2", { text: "Start a new run" }));

  const errHost = el("div", {});
  const form = el("form", {});

  const modeRow = el("div", { class: "form-grid" }, [
    labeledField("Mode", el("select", { name: "mode" }, [
      el("option", { value: "map", text: "map (fetch + discover only)" }),
      el("option", { value: "crawl", text: "crawl (real extraction)" }),
    ])),
    labeledField("Root URL", el("input", { name: "root_url", type: "url", placeholder: "https://example.com/docs/", required: "required" })),
    checkboxField("same_site_only", "Same-site only", true,
      "When on, only branches on the root URL's own host are seeded — off-site branches (github.com, etc.) are excluded before the run starts."),
  ]);
  form.appendChild(modeRow);

  const commonRow = el("div", { class: "form-grid" }, [
    labeledField("Max pages", el("input", { name: "max_pages", type: "number", min: "1", placeholder: "unlimited" })),
    labeledField("Max depth", el("input", { name: "max_depth", type: "number", min: "0", placeholder: "unlimited" })),
    labeledField("Crawl workers", el("input", { name: "crawl_workers", type: "number", min: "1", value: "5" })),
  ]);
  form.appendChild(commonRow);

  const crawlFields = el("fieldset", { name: "crawl_fields" }, [
    el("legend", { text: "Crawl-only (extraction)" }),
    el("div", { class: "form-grid" }, [
      labeledField("Intent", el("textarea", { name: "intent", placeholder: "blank = no relevance filtering, every page extracts" })),
      labeledField("Extract workers", el("input", { name: "extract_workers", type: "number", min: "1", value: "2" })),
      labeledField("Extract threshold", el("input", { name: "extract_threshold", type: "number", step: "0.05", min: "0", max: "1", value: "0" })),
      labeledField("Follow threshold", el("input", { name: "follow_threshold", type: "number", step: "0.05", min: "0", max: "1", value: "0" })),
      labeledField("Provider", providerSelect()),
      labeledField("Model", el("input", { name: "model", placeholder: "(provider default)" })),
      checkboxField("build_rag", "Build RAG index (Chroma)", false, null),
    ]),
  ]);
  form.appendChild(crawlFields);

  const modeSelect = form.elements["mode"];
  function syncCrawlFieldsVisibility() {
    crawlFields.style.display = modeSelect.value === "crawl" ? "" : "none";
  }
  modeSelect.addEventListener("change", syncCrawlFieldsVisibility);
  syncCrawlFieldsVisibility();

  const submitBtn = el("button", { type: "submit", text: "Start run" });
  form.appendChild(el("div", { class: "btn-row" }, [submitBtn]));
  form.appendChild(errHost);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errHost.innerHTML = "";
    submitBtn.disabled = true;
    submitBtn.textContent = "Starting…";
    try {
      const runId = await startRunFromForm(form);
      location.hash = `#run/${encodeURIComponent(runId)}`;
    } catch (err) {
      errHost.appendChild(errorBox(err));
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Start run";
    }
  });

  panel.appendChild(form);
  return panel;
}

function providerSelect() {
  const sel = el("select", { name: "provider" });
  for (const p of Object.keys(PROVIDER_DEFAULT_MODELS)) {
    sel.appendChild(el("option", { value: p, text: p }));
  }
  return sel;
}

function labeledField(name, inputNode) {
  return el("label", {}, [el("span", { class: "field-name", text: name }), inputNode]);
}

function checkboxField(fieldName, labelText, checked, hint) {
  const input = el("input", { name: fieldName, type: "checkbox" });
  if (checked) input.checked = true;
  const label = el("label", { class: "checkbox-label" }, [input, labelText]);
  if (hint) {
    const wrap = el("div", {}, [label, el("div", { class: "hint", text: hint })]);
    return wrap;
  }
  return label;
}

async function startRunFromForm(form) {
  const mode = form.elements["mode"].value;
  const rootUrl = form.elements["root_url"].value.trim();
  const sameSiteOnly = form.elements["same_site_only"].checked;

  let categories = "all";
  if (sameSiteOnly) {
    const discovered = await Api.discover(rootUrl);
    let rootHost;
    try { rootHost = new URL(rootUrl).hostname; } catch (e) { rootHost = null; }
    const sameHost = discovered.branches.filter((b) => b.host === rootHost).map((b) => b.category);
    categories = sameHost.length > 0 ? sameHost : "all";
  }

  const params = {
    mode,
    root_url: rootUrl,
    categories,
    max_pages: numOrNull(form.elements["max_pages"].value),
    max_depth: numOrNull(form.elements["max_depth"].value),
    crawl_workers: numOrDefault(form.elements["crawl_workers"].value, 5),
  };
  if (mode === "crawl") {
    const provider = form.elements["provider"].value;
    const modelInput = form.elements["model"].value.trim();
    Object.assign(params, {
      extract_workers: numOrDefault(form.elements["extract_workers"].value, 2),
      extract_threshold: numOrDefault(form.elements["extract_threshold"].value, 0),
      follow_threshold: numOrDefault(form.elements["follow_threshold"].value, 0),
      intent: form.elements["intent"].value.trim() || null,
      provider,
      model: modelInput || PROVIDER_DEFAULT_MODELS[provider],
      build_rag: form.elements["build_rag"].checked,
    });
  }

  const result = await Api.startRun(params);
  return result.run_id;
}

// -------------------------------------------------------------- detail

async function renderDetail(runId) {
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back-link", href: "#", text: "← all runs" }));
  app.appendChild(el("h2", { text: runId }));

  const body = el("div", {});
  app.appendChild(body);
  body.appendChild(el("p", { class: "hint", text: "Loading…" }));

  let sawRunExist = false;
  let notFoundRetries = 0;
  const MAX_NOT_FOUND_RETRIES = 10; // ~10s -- generous over main.py's observed multi-second import startup

  async function refresh() {
    let detail;
    try {
      detail = await Api.runDetail(runId);
      sawRunExist = true;
    } catch (err) {
      // A run just started via the form navigates straight here, but
      // frontier.db isn't created until the subprocess's own
      // Frontier.open() call, several seconds into main.py's own import
      // startup -- a 404 in that window is expected, not a real failure
      // (found by actually clicking "Start run" and landing here, not by
      // review). Retry silently for a bit before treating it as real --
      // capped, so a genuinely bad run_id still ends up a real error.
      if (err.status === 404 && !sawRunExist && notFoundRetries < MAX_NOT_FOUND_RETRIES) {
        notFoundRetries++;
        body.innerHTML = "";
        body.appendChild(el("p", { class: "hint", text: "Starting…" }));
        setTimeout(refresh, 1000);
        return;
      }
      body.innerHTML = "";
      body.appendChild(errorBox(err));
      stopPolling();
      return;
    }
    body.innerHTML = "";
    body.appendChild(buildDetailView(runId, detail, refresh));

    const jobState = detail.job && detail.job.state;
    if (jobState === "running" && !pollTimer) {
      pollTimer = setInterval(refresh, 2000);
    } else if (jobState !== "running" && pollTimer) {
      stopPolling();
    }
  }

  await refresh();
}

function buildDetailView(runId, detail, refresh) {
  const frag = document.createDocumentFragment();
  const run = detail.run || {};
  const job = detail.job;
  const cfg = detail.config;

  const header = el("div", { class: "panel" }, [
    el("div", {}, [
      el("strong", { text: run.root_url || "(unknown root)" }),
      " ",
      badge(job ? job.state : null),
      el("span", { class: "hint", text: `  mode=${run.mode || "?"}  started ${fmtDate(run.started_at)}` }),
    ]),
    el("div", { class: "btn-row" }, controlButtons(runId, job, refresh)),
  ]);
  frag.appendChild(header);

  frag.appendChild(liveStatusPanel(run));

  frag.appendChild(datasetReportPanel(runId));
  frag.appendChild(exportPanel(runId));
  frag.appendChild(ragQueryPanel(runId, run));

  frag.appendChild(el("div", { class: "panel" }, [
    el("a", { href: `/map?run=${encodeURIComponent(runId)}`, text: "View on map →" }),
  ]));

  return frag;
}

function controlButtons(runId, job, refresh) {
  const state = job ? job.state : null;
  const buttons = [];
  const wrap = async (fn) => {
    try { await fn(); await refresh(); } catch (err) { alert((err.message || err) + (err.detail ? "\n" + err.detail : "")); }
  };

  if (state === "running") {
    buttons.push(el("button", { class: "secondary", text: "Pause", onclick: () => wrap(() => Api.pauseRun(runId)) }));
    buttons.push(el("button", { class: "secondary", text: "Unpause", onclick: () => wrap(() => Api.unpauseRun(runId)) }));
    buttons.push(el("button", { class: "danger", text: "Stop", onclick: () => wrap(() => Api.stopRun(runId)) }));
  } else {
    buttons.push(el("button", {
      text: "Resume (continue as-is)",
      onclick: () => wrap(() => Api.resumeRun(runId)),
    }));
    if (state === "done" || state === "stopped" || state === "exited") {
      buttons.push(el("button", {
        class: "secondary",
        text: "Promote to real crawl…",
        onclick: () => promoteToCrawl(runId, wrap),
      }));
    }
  }
  return buttons;
}

function promoteToCrawl(runId, wrap) {
  const provider = prompt("Provider (openai/nvidia/ollama/ollama_cloud)", "openai");
  if (!provider) return;
  // Deliberately no max_pages prompt here, AND explicitly cleared
  // (null, not omitted) rather than left alone. max_pages counts
  // CUMULATIVE done+skipped_extract+mapped rows since this frontier.db
  // was created, not "new pages this pass" -- setting a small cap after
  // a map phase has already produced N mapped rows makes claim() refuse
  // everything immediately (N < max_pages is false from the first
  // claim), silently doing nothing. Worse: the server's /resume only
  // overrides a config key when the request actually includes it, so a
  // stale max_pages from an EARLIER promotion attempt otherwise survives
  // silently into this one too (found by hitting exactly that -- a
  // second promotion after a first one that had set max_pages:1 still
  // did nothing, because omitting the field isn't the same as clearing
  // it). The real scope boundary for "promote a selection to a real
  // crawl" is the map→select→cancel-the-rest sequence itself (only the
  // selected rows are left 'queued' — see the Map page), not max_pages.
  wrap(() => Api.resumeRun(runId, { mode: "crawl", provider, max_pages: null, max_depth: null }));
}

function liveStatusPanel(run) {
  const panel = el("div", { class: "panel" });
  panel.appendChild(el("h3", { text: "Status" }));

  const counts = run.counts_by_status || {};
  const chips = el("div", { class: "status-counts" });
  const keys = Object.keys(counts);
  if (keys.length === 0) {
    chips.appendChild(el("span", { class: "hint", text: "No pages yet." }));
  } else {
    for (const k of keys.sort()) {
      chips.appendChild(el("span", { class: "status-chip" }, [k, el("b", { text: String(counts[k]) })]));
    }
  }
  panel.appendChild(chips);

  const inFlightHost = el("div", {});
  panel.appendChild(inFlightHost);

  Api.liveStatus(run.run_id).then((status) => {
    inFlightHost.innerHTML = "";
    if (status.in_flight && status.in_flight.length > 0) {
      inFlightHost.appendChild(el("h3", { text: `In flight (${status.in_flight.length})` }));
      const ul = el("ul", { class: "in-flight-list" });
      for (const f of status.in_flight) {
        ul.appendChild(el("li", { text: `${f.running_seconds}s  ${f.url}` }));
      }
      inFlightHost.appendChild(ul);
    }
  }).catch(() => {});

  return panel;
}

function datasetReportPanel(runId) {
  const panel = el("div", { class: "panel" });
  panel.appendChild(el("h3", { text: "Dataset report" }));
  const out = el("pre", { class: "report", style: "display:none" });
  const btn = el("button", {
    class: "secondary",
    text: "Load report",
    onclick: async () => {
      btn.disabled = true;
      btn.textContent = "Loading…";
      try {
        const { report_text } = await Api.datasetReport(runId);
        out.textContent = report_text;
        out.style.display = "";
      } catch (err) {
        out.textContent = (err.message || err) + (err.detail ? "\n" + err.detail : "");
        out.style.display = "";
      } finally {
        btn.disabled = false;
        btn.textContent = "Reload report";
      }
    },
  });
  panel.appendChild(el("div", { class: "btn-row" }, [btn]));
  panel.appendChild(out);
  return panel;
}

const EXPORT_SCHEMAS = [
  "conversational", "alpaca", "prompt_completion", "embedding_pairs",
  "rag_eval", "openai_finetune", "vertex", "raw_text", "triplets",
];
const EXPORT_FRAMEWORKS = ["plain-jsonl", "mlx", "huggingface", "llama-factory", "axolotl"];

function exportPanel(runId) {
  const panel = el("div", { class: "panel" });
  panel.appendChild(el("h3", { text: "Export" }));

  const schemaSel = el("select", { name: "schema" }, EXPORT_SCHEMAS.map((s) => el("option", { value: s, text: s })));
  const frameworkSel = el("select", { name: "framework" }, EXPORT_FRAMEWORKS.map((f) => el("option", { value: f, text: f })));
  const splitSel = el("select", { name: "split_by" }, [
    el("option", { value: "section", text: "section" }),
    el("option", { value: "source_url", text: "source_url" }),
  ]);
  const semDedup = el("input", { type: "checkbox", name: "semantic_dedup" });
  const semDedupReport = el("input", { type: "checkbox", name: "semantic_dedup_report" });

  const form = el("div", { class: "form-grid" }, [
    labeledField("Schema", schemaSel),
    labeledField("Framework", frameworkSel),
    labeledField("Split by", splitSel),
    el("label", { class: "checkbox-label" }, [semDedup, "Semantic dedup"]),
    el("label", { class: "checkbox-label" }, [semDedupReport, "Semantic dedup report only"]),
  ]);
  panel.appendChild(form);

  const status = el("div", { class: "hint" });
  const startBtn = el("button", { text: "Export" });
  panel.appendChild(el("div", { class: "btn-row" }, [startBtn]));
  panel.appendChild(status);
  const resultOut = el("pre", { class: "report", style: "display:none" });
  panel.appendChild(resultOut);

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    status.textContent = "Starting export…";
    resultOut.style.display = "none";
    try {
      const params = {
        schema: schemaSel.value,
        framework: frameworkSel.value,
        split_by: splitSel.value,
        semantic_dedup: semDedup.checked,
        semantic_dedup_report: semDedupReport.checked,
      };
      const { job, job_dir, out } = await Api.startExport(runId, params);
      status.textContent = `Running (writing to ${out})…`;
      const jobDirEncoded = job_dir.split(/[\\/]/).map(encodeURIComponent).join("/");
      const poll = setInterval(async () => {
        try {
          const { job: j } = await Api.exportJob(runId, jobDirEncoded);
          if (j.state === "running") return;
          clearInterval(poll);
          startBtn.disabled = false;
          if (j.state === "done") {
            status.textContent = `Done — wrote to ${out}`;
            resultOut.textContent = JSON.stringify(j.result, null, 2);
            resultOut.style.display = "";
          } else {
            status.textContent = `Export ${j.state}`;
          }
        } catch (err) {
          clearInterval(poll);
          startBtn.disabled = false;
          status.textContent = "";
          resultOut.textContent = err.message || String(err);
          resultOut.style.display = "";
        }
      }, 1500);
    } catch (err) {
      startBtn.disabled = false;
      status.textContent = "";
      resultOut.textContent = (err.message || err) + (err.detail ? "\n" + err.detail : "");
      resultOut.style.display = "";
    }
  });

  return panel;
}

function ragQueryPanel(runId, run) {
  const panel = el("div", { class: "panel" });
  panel.appendChild(el("h3", { text: "RAG query" }));
  if (!run.has_rag) {
    panel.appendChild(el("p", { class: "hint", text: "This run has no RAG index (build_rag was off)." }));
    return panel;
  }
  const input = el("input", { type: "text", placeholder: "Ask a question about this crawl", style: "width:100%" });
  const out = el("pre", { class: "report", style: "display:none" });
  const btn = el("button", {
    text: "Ask",
    onclick: async () => {
      const q = input.value.trim();
      if (!q) return;
      btn.disabled = true;
      btn.textContent = "Asking…";
      try {
        const { report_text } = await Api.query(runId, q);
        out.textContent = report_text;
        out.style.display = "";
      } catch (err) {
        out.textContent = (err.message || err) + (err.detail ? "\n" + err.detail : "");
        out.style.display = "";
      } finally {
        btn.disabled = false;
        btn.textContent = "Ask";
      }
    },
  });
  panel.appendChild(el("div", { class: "btn-row" }, [input, btn]));
  panel.appendChild(out);
  return panel;
}

// --------------------------------------------------------------- router

function route() {
  stopPolling();
  const hash = location.hash;
  const m = hash.match(/^#run\/(.+)$/);
  if (m) {
    renderDetail(decodeURIComponent(m[1]));
  } else {
    renderList();
  }
}

window.addEventListener("hashchange", route);
route();
