"""Localhost-only control server for the crawler -- no auth, not exposed
to the network (see create_app()'s host binding and README.md). Everything
here is thin: it validates a request, shells out to `python main.py ...`
(or, for the two truly long operations, launches one as a tracked
background job) or calls a pure, already-safe read module (crawl/graph.py,
crawl/live_status.py), and returns JSON. No crawl4ai, no async Frontier,
no chromadb client ever runs inside this process -- see server/jobs.py's
module docstring for why.

Every route returns a structured JSON body on error
(`{"error": "..."}`, ideally with `"detail"`), never a raw traceback --
see the error handlers at the bottom.
"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, request, send_from_directory

import config

from crawl.graph import discovery_graph, path_tree
from crawl.live_status import read_live_status
from server.jobs import JobManager, JobError, run_bounded
from server.paths import DATA_DIR, PYTHON, run_dir_for, new_run_id, is_valid_run_id
from server import runs as runs_module

MAIN_PY = os.path.join(os.path.dirname(DATA_DIR), "main.py")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _require_run(run_id: str) -> str:
    try:
        run_dir = run_dir_for(run_id)
    except ValueError as e:
        raise ApiError("invalid run_id", status=400, detail=str(e))
    if not os.path.exists(os.path.join(run_dir, "frontier.db")):
        raise ApiError(f"no such run: {run_id}", status=404)
    return run_dir


def _body() -> dict:
    return request.get_json(silent=True) or {}


def create_app() -> Flask:
    app = Flask(__name__)
    jobs = JobManager()

    # ------------------------------------------------------------------
    # UI (static files -- server/static/, no templating, no server-
    # rendered state; the pages drive everything through the JSON API
    # below via fetch()). /static/<path> is served automatically by
    # Flask's default static handler; these two just serve the two page
    # shells at clean URLs instead of /static/index.html.
    # ------------------------------------------------------------------

    @app.get("/")
    def index_page():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/map")
    def map_page():
        return send_from_directory(app.static_folder, "map.html")

    # ------------------------------------------------------------------
    # health / run history
    # ------------------------------------------------------------------

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/runs")
    def list_runs():
        return jsonify({"runs": runs_module.list_runs(jobs)})

    @app.get("/runs/<run_id>")
    def run_detail(run_id):
        run_dir = _require_run(run_id)
        summary = runs_module.summarize_run(run_id, jobs)
        job = jobs.get_status(run_dir)
        config = jobs.resolved_config(run_dir)
        return jsonify({"run": summary, "job": job, "config": config})

    @app.get("/runs/<run_id>/job")
    def run_job(run_id):
        # Deliberately not _require_run(): frontier.db is created a
        # moment into headless_map/headless_crawl's own startup (once it
        # calls Frontier.open()), not before the subprocess launches --
        # job.json, on the other hand, is written synchronously inside
        # jobs.start() before it returns, so polling this immediately
        # after POST /runs/start must not 404 in that brief window.
        try:
            run_dir = run_dir_for(run_id)
        except ValueError as e:
            raise ApiError("invalid run_id", status=400, detail=str(e))
        job = jobs.get_status(run_dir)
        if job is None:
            raise ApiError("no job has ever been launched for this run", status=404)
        return jsonify({"job": job})

    # ------------------------------------------------------------------
    # discovery + starting/resuming/stopping/pausing runs
    # ------------------------------------------------------------------

    @app.post("/runs/discover")
    def discover():
        body = _body()
        root_url = body.get("root_url")
        if not root_url:
            raise ApiError("root_url is required")
        result = run_bounded([MAIN_PY, "--discover-json", root_url], timeout=60)
        if result.returncode != 0:
            raise ApiError("discovery failed", status=502, detail=result.stderr[-4000:])
        try:
            return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            raise ApiError("discovery produced no parseable output", status=502, detail=result.stdout[-2000:])

    @app.post("/runs/start")
    def start_run():
        body = _body()
        mode = body.get("mode")
        if mode not in ("map", "crawl"):
            raise ApiError("mode must be 'map' or 'crawl'")
        root_url = body.get("root_url")
        if not root_url:
            raise ApiError("root_url is required")

        discovered = run_bounded([MAIN_PY, "--discover-json", root_url], timeout=60)
        if discovered.returncode != 0:
            raise ApiError("discovery failed", status=502, detail=discovered.stderr[-4000:])
        try:
            branches = json.loads(discovered.stdout.strip().splitlines()[-1])["branches"]
        except (json.JSONDecodeError, IndexError, KeyError):
            raise ApiError("discovery produced no parseable output", status=502)

        categories = body.get("categories", "all")
        selected = branches if categories == "all" else [b for b in branches if b["category"] in categories]
        if not selected:
            raise ApiError("no branches matched the requested categories", status=400)

        seeds = [[u, b["category"]] for b in selected for u in b["urls"]]
        host_prefix_pairs = [[b["host"], b["prefix"]] for b in selected]

        run_id = body.get("run_id") or new_run_id(mode, host_prefix_pairs[0][0] if host_prefix_pairs else "site")
        if not is_valid_run_id(run_id):
            raise ApiError("invalid run_id")
        run_dir = run_dir_for(run_id)
        if os.path.exists(os.path.join(run_dir, "frontier.db")):
            raise ApiError(f"run {run_id} already exists -- use /runs/{run_id}/resume instead", status=409)

        cfg = {
            "mode": mode, "run_dir": run_dir, "root_url": root_url,
            "seeds": seeds, "host_prefix_pairs": host_prefix_pairs,
            "max_pages": body.get("max_pages"), "max_depth": body.get("max_depth"),
            "crawl_workers": body.get("crawl_workers", 5),
        }
        if mode == "crawl":
            cfg.update({
                "extract_workers": body.get("extract_workers", 2),
                "extract_threshold": body.get("extract_threshold", 0.0),
                "follow_threshold": body.get("follow_threshold", 0.0),
                "intent": body.get("intent"),
                "provider": body.get("provider", "openai"),
                "model": body.get("model"),
                "build_rag": bool(body.get("build_rag", False)),
                "enable_quality_scoring": bool(body.get("enable_quality_scoring", False)),
                "quality_threshold": body.get("quality_threshold", config.SAMPLE_QUALITY_THRESHOLD),
            })

        job = _launch(jobs, run_dir, mode, cfg)
        return jsonify({"run_id": run_id, "run_dir": run_dir, "job": job}), 202

    @app.post("/runs/<run_id>/resume")
    def resume_run(run_id):
        """Resumes the persisted config as-is by default. A body can
        override fields -- most importantly `mode`: this is how "map
        first, select a subtree, then run a real crawl on it" actually
        works (CLAUDE.md's map-mode workflow) -- the map pass's frontier
        already has the right rows queued (via /select); promoting it to
        a real crawl means re-launching against the SAME run_dir with
        mode=crawl and the crawl-only tunables (provider/model/build_rag/
        thresholds/etc, absent from a map config) filled in from the
        body, not starting a new run and re-discovering from scratch.
        """
        run_dir = _require_run(run_id)
        cfg = jobs.resolved_config(run_dir)
        if cfg is None:
            raise ApiError(f"no run_config.json for {run_id} -- can't resume a run this server didn't start", status=400)
        overrides = _body()
        cfg = dict(cfg)
        cfg["run_dir"] = run_dir
        mode = overrides.get("mode", cfg["mode"])
        cfg["mode"] = mode
        for key in ("max_pages", "max_depth", "crawl_workers"):
            if key in overrides:
                cfg[key] = overrides[key]
        if mode == "crawl":
            for key, default in (
                ("extract_workers", 2), ("extract_threshold", 0.0), ("follow_threshold", 0.0),
                ("intent", None), ("provider", "openai"), ("model", None), ("build_rag", False),
                ("enable_quality_scoring", False), ("quality_threshold", config.SAMPLE_QUALITY_THRESHOLD),
            ):
                cfg[key] = overrides.get(key, cfg.get(key, default))
        job = _launch(jobs, run_dir, mode, cfg)
        return jsonify({"run_id": run_id, "job": job}), 202

    def _launch(jobs: JobManager, run_dir: str, mode: str, cfg: dict) -> dict:
        config_path = os.path.join(run_dir, "run_config.json")
        # written twice deliberately: _launch's cmd needs the path to
        # exist before Popen; JobManager.start() also (re)writes it with
        # started_at filled in -- both writes are the same file, so a
        # resume always launches against the freshest config.
        os.makedirs(run_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        cmd = [PYTHON, "-u", MAIN_PY, "--headless-config", config_path]
        try:
            return jobs.start(run_dir, mode, cmd, config=cfg)
        except JobError as e:
            raise ApiError(str(e), status=409)

    @app.post("/runs/<run_id>/stop")
    def stop_run(run_id):
        run_dir = _require_run(run_id)
        try:
            return jsonify({"job": jobs.stop(run_dir)})
        except JobError as e:
            raise ApiError(str(e), status=404)

    @app.post("/runs/<run_id>/pause")
    def pause_run(run_id):
        run_dir = _require_run(run_id)
        result = run_bounded([MAIN_PY, "--pause", os.path.join(run_dir, "frontier.db")], timeout=15)
        if result.returncode != 0:
            raise ApiError("pause failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"paused": True})

    @app.post("/runs/<run_id>/unpause")
    def unpause_run(run_id):
        run_dir = _require_run(run_id)
        result = run_bounded([MAIN_PY, "--unpause", os.path.join(run_dir, "frontier.db")], timeout=15)
        if result.returncode != 0:
            raise ApiError("unpause failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"paused": False})

    # ------------------------------------------------------------------
    # cancel / select
    # ------------------------------------------------------------------

    @app.post("/runs/<run_id>/cancel")
    def cancel(run_id):
        run_dir = _require_run(run_id)
        body = _body()
        db_path = os.path.join(run_dir, "frontier.db")
        if body.get("prefix"):
            args = ["--cancel-prefix", body["prefix"], "--cancel-db", db_path]
        elif body.get("url"):
            args = ["--cancel", body["url"], "--cancel-db", db_path]
        else:
            raise ApiError("url or prefix is required")
        result = run_bounded([MAIN_PY] + args, timeout=30)
        if result.returncode != 0:
            raise ApiError("cancel failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"output": result.stdout.strip()})

    @app.post("/runs/<run_id>/select")
    def select(run_id):
        run_dir = _require_run(run_id)
        body = _body()
        db_path = os.path.join(run_dir, "frontier.db")
        if body.get("prefix"):
            args = ["--select-prefix", body["prefix"], "--select-db", db_path]
        elif body.get("url"):
            args = ["--select", body["url"], "--select-db", db_path]
        else:
            raise ApiError("url or prefix is required")
        result = run_bounded([MAIN_PY] + args, timeout=30)
        if result.returncode != 0:
            raise ApiError("select failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"output": result.stdout.strip()})

    @app.post("/runs/<run_id>/suggest-selection")
    def suggest_selection(run_id):
        """One LLM call classifying every discovered branch against an
        intent -- proposes only (see crawl/branch_select.py). Same
        bounded-subprocess pattern as /query (also LLM-touching, also one
        call): synchronous, not a tracked background job, since it's one
        request, not a long-running crawl/export. The timeout has to be
        the real chat-completion budget (config.LLM_EXTRACT_TIMEOUT_
        SECONDS, the same one llm_factory.py gives the LLM client itself),
        not /query's 60s -- confirmed the hard way: a real classification
        call against a 70B model took ~5 minutes, so 120s timed out this
        endpoint out from under a call that was still genuinely working."""
        run_dir = _require_run(run_id)
        body = _body()
        intent = body.get("intent")
        if not intent:
            raise ApiError("intent is required")
        db_path = os.path.join(run_dir, "frontier.db")
        args = [
            "--suggest-select", intent,
            "--suggest-select-db", db_path,
            "--suggest-select-provider", body.get("provider", "openai"),
        ]
        if body.get("model"):
            args += ["--suggest-select-model", body["model"]]
        if body.get("root"):
            args += ["--suggest-select-root", body["root"]]
        if body.get("samples"):
            args += ["--suggest-select-samples", str(body["samples"])]
        result = run_bounded([MAIN_PY] + args, timeout=config.LLM_EXTRACT_TIMEOUT_SECONDS + 30)
        if result.returncode != 0:
            raise ApiError("suggest-selection failed", status=502, detail=result.stderr[-4000:])
        try:
            return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            raise ApiError("suggest-selection produced no parseable output", status=502, detail=result.stdout[-2000:])

    # ------------------------------------------------------------------
    # read-only: graph projections, live status, dataset report, query
    # ------------------------------------------------------------------

    @app.get("/runs/<run_id>/graph")
    def graph(run_id):
        run_dir = _require_run(run_id)
        db_path = os.path.join(run_dir, "frontier.db")
        graph_type = request.args.get("type", "discovery")
        root = request.args.get("root")
        max_depth = request.args.get("max_depth", type=int)
        try:
            if graph_type == "discovery":
                projection = discovery_graph(db_path, root_url=root, max_depth=max_depth)
            elif graph_type == "path-tree":
                projection = path_tree(db_path, root_prefix=root, max_depth=max_depth)
            else:
                raise ApiError("type must be 'discovery' or 'path-tree'")
        except ValueError as e:
            raise ApiError(str(e), status=400)
        return jsonify(projection)

    @app.get("/runs/<run_id>/live-status")
    def live_status(run_id):
        run_dir = _require_run(run_id)
        db_path = os.path.join(run_dir, "frontier.db")
        return jsonify(read_live_status(db_path))

    @app.get("/runs/<run_id>/dataset-report")
    def dataset_report(run_id):
        run_dir = _require_run(run_id)
        canonical_path = os.path.join(run_dir, "canonical.jsonl")
        if not os.path.exists(canonical_path):
            raise ApiError("no canonical.jsonl for this run yet", status=404)
        args = [
            "--dataset-report", canonical_path,
            "--dataset-report-frontier-db", os.path.join(run_dir, "frontier.db"),
        ]
        result = run_bounded([MAIN_PY] + args, timeout=60)
        if result.returncode != 0:
            raise ApiError("dataset report failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"report_text": result.stdout})

    @app.post("/runs/<run_id>/query")
    def query(run_id):
        run_dir = _require_run(run_id)
        body = _body()
        question = body.get("question")
        if not question:
            raise ApiError("question is required")
        chroma_dir = os.path.join(run_dir, "chroma_db")
        if not os.path.isdir(chroma_dir):
            raise ApiError("this run has no RAG index (build_rag was off)", status=404)
        result = run_bounded(
            [MAIN_PY, "--query", question, "--query-chroma-dir", chroma_dir], timeout=60,
        )
        if result.returncode != 0:
            raise ApiError("query failed", status=502, detail=result.stderr[-2000:])
        return jsonify({"report_text": result.stdout})

    # ------------------------------------------------------------------
    # export (a job -- can run long over a large canonical.jsonl)
    # ------------------------------------------------------------------

    @app.post("/runs/<run_id>/export")
    def export(run_id):
        run_dir = _require_run(run_id)
        body = _body()
        canonical_path = os.path.join(run_dir, "canonical.jsonl")
        if not os.path.exists(canonical_path):
            raise ApiError("no canonical.jsonl for this run yet", status=404)
        schema = body.get("schema")
        if not schema:
            raise ApiError("schema is required")
        out_dir = body.get("out") or os.path.join(run_dir, "export", schema)

        args = [
            "-m", "export.export", canonical_path, "--out", out_dir, "--schema", schema,
            "--framework", body.get("framework", "plain-jsonl"),
        ]
        if body.get("min_answer_length") is not None:
            args += ["--min-answer-length", str(body["min_answer_length"])]
        if body.get("split_by"):
            args += ["--split-by", body["split_by"]]
        if body.get("intent"):
            args += ["--intent", body["intent"]]
        if body.get("semantic_dedup"):
            args.append("--semantic-dedup")
        if body.get("semantic_dedup_threshold") is not None:
            args += ["--semantic-dedup-threshold", str(body["semantic_dedup_threshold"])]
        if body.get("semantic_dedup_report"):
            args.append("--semantic-dedup-report")

        export_job_dir = os.path.join(run_dir, "export_jobs", schema + "-" + body.get("framework", "plain-jsonl"))
        cmd = [PYTHON, "-u"] + args
        try:
            job = jobs.start(export_job_dir, "export", cmd, config={"canonical_path": canonical_path, "out": out_dir, **body})
        except JobError as e:
            raise ApiError(str(e), status=409)
        return jsonify({"job": job, "out": out_dir, "job_dir": os.path.relpath(export_job_dir, run_dir)}), 202

    @app.get("/runs/<run_id>/export/<path:job_key>/job")
    def export_job_status(run_id, job_key):
        run_dir = _require_run(run_id)
        export_job_dir = os.path.join(run_dir, "export_jobs", job_key)
        job = jobs.get_status(export_job_dir)
        if job is None:
            raise ApiError("no such export job", status=404)
        return jsonify({"job": job})

    # ------------------------------------------------------------------
    # errors -- structured JSON always, never a raw traceback
    # ------------------------------------------------------------------

    @app.errorhandler(ApiError)
    def handle_api_error(e: ApiError):
        body = {"error": e.message}
        if e.detail:
            body["detail"] = e.detail
        return jsonify(body), e.status

    @app.errorhandler(404)
    def handle_404(e):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(Exception)
    def handle_unexpected(e: Exception):
        app.logger.exception("unhandled error")
        return jsonify({"error": "internal error", "detail": str(e)}), 500

    return app
