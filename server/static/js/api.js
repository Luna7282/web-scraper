// Thin fetch wrapper over the control server's JSON API. Every function
// here maps 1:1 to a server/app.py route -- no logic beyond building the
// request and unwrapping the response, so the rest of the UI never talks
// to fetch() directly. Errors from the server are always structured JSON
// ({error, detail}); ApiError carries that through so callers can show a
// real message instead of "something went wrong".

class ApiError extends Error {
  constructor(status, body) {
    super(body && body.error ? body.error : `HTTP ${status}`);
    this.status = status;
    this.detail = body && body.detail;
  }
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let json = null;
  try {
    json = await res.json();
  } catch (e) {
    // No body / not JSON -- fall through, res.ok check below still applies.
  }
  if (!res.ok) throw new ApiError(res.status, json);
  return json;
}

const Api = {
  health: () => request("GET", "/health"),

  listRuns: () => request("GET", "/runs"),
  runDetail: (runId) => request("GET", `/runs/${encodeURIComponent(runId)}`),
  runJob: (runId) => request("GET", `/runs/${encodeURIComponent(runId)}/job`),

  discover: (rootUrl) => request("POST", "/runs/discover", { root_url: rootUrl }),
  startRun: (params) => request("POST", "/runs/start", params),
  resumeRun: (runId, overrides) => request("POST", `/runs/${encodeURIComponent(runId)}/resume`, overrides || {}),
  stopRun: (runId) => request("POST", `/runs/${encodeURIComponent(runId)}/stop`),
  pauseRun: (runId) => request("POST", `/runs/${encodeURIComponent(runId)}/pause`),
  unpauseRun: (runId) => request("POST", `/runs/${encodeURIComponent(runId)}/unpause`),

  cancel: (runId, target) => request("POST", `/runs/${encodeURIComponent(runId)}/cancel`, target),
  select: (runId, target) => request("POST", `/runs/${encodeURIComponent(runId)}/select`, target),

  graph: (runId, { type = "discovery", root, maxDepth } = {}) => {
    const qs = new URLSearchParams({ type });
    if (root) qs.set("root", root);
    if (maxDepth !== undefined && maxDepth !== null) qs.set("max_depth", maxDepth);
    return request("GET", `/runs/${encodeURIComponent(runId)}/graph?${qs}`);
  },

  liveStatus: (runId) => request("GET", `/runs/${encodeURIComponent(runId)}/live-status`),
  suggestSelection: (runId, params) => request("POST", `/runs/${encodeURIComponent(runId)}/suggest-selection`, params),
  datasetReport: (runId) => request("GET", `/runs/${encodeURIComponent(runId)}/dataset-report`),
  query: (runId, question) => request("POST", `/runs/${encodeURIComponent(runId)}/query`, { question }),

  startExport: (runId, params) => request("POST", `/runs/${encodeURIComponent(runId)}/export`, params),
  exportJob: (runId, jobDir) => request("GET", `/runs/${encodeURIComponent(runId)}/export/${jobDir}/job`),
};
