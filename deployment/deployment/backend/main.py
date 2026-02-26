"""
main.py — FastAPI backend for BI Genie (RBAC-aware)
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import psycopg2
from openai import AsyncOpenAI
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUPERSET_URL  = os.getenv("SUPERSET_URL",           "http://superset:8088")
SUPERSET_USER = os.getenv("SUPERSET_ADMIN_USER",    "admin")
SUPERSET_PASS = os.getenv("SUPERSET_ADMIN_PASSWORD","admin")
DB_URL        = os.getenv("DATABASE_URL",           "postgresql://superset:superset@db:5432/superset")
SUPERSET_EXTERNAL_URL = os.getenv("SUPERSET_EXTERNAL_URL", "http://localhost:9088")

LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5@20251001")
_skip_tls = os.getenv("LLM_SKIP_TLS_VERIFY", "false").lower() == "true"
llm = AsyncOpenAI(
    api_key=os.getenv("LITELLM_API_KEY"),
    base_url=os.getenv("LITELLM_URL"),
    http_client=httpx.AsyncClient(verify=not _skip_tls),
)

# ── In-memory state ───────────────────────────────────────────────────────────
datasets: dict = {}   # { table_name: { "id": int, "columns": [str, ...] } }  — admin view (all)
sessions: dict = {}   # { session_id: { history, state, user, ... } }

SESSION_TIMEOUT = 1800  # 30 minutes of inactivity → session expires

# Superset 5.x viz_type mapping (ECharts-based, dist_bar removed in 5.0)
VIZ_MAP = {"bar": "echarts_timeseries_bar", "line": "echarts_timeseries_line",
           "table": "table",  "pie": "pie"}


# ── Superset API helpers ──────────────────────────────────────────────────────

async def superset_session() -> httpx.AsyncClient:
    """Return an authenticated httpx.AsyncClient for Superset API calls (admin)."""
    client = httpx.AsyncClient(base_url=SUPERSET_URL, timeout=30)

    r = await client.post("/api/v1/security/login", json={
        "username": SUPERSET_USER, "password": SUPERSET_PASS,
        "provider": "db", "refresh": True,
    })
    r.raise_for_status()
    client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    r = await client.get("/api/v1/security/csrf_token/")
    r.raise_for_status()
    client.headers.update({
        "X-CSRFToken":    r.json()["result"],
        "Content-Type":   "application/json",
        "Referer":        SUPERSET_URL,
    })
    return client


async def refresh_datasets() -> None:
    """Fetch all Superset datasets (admin view) and update the in-memory cache."""
    global datasets
    try:
        client = await superset_session()
        r = await client.get("/api/v1/dataset/",
                             params={"q": json.dumps({"page_size": 100})})
        r.raise_for_status()
        loaded = {}
        for ds in r.json().get("result", []):
            r2 = await client.get(f"/api/v1/dataset/{ds['id']}")
            if r2.status_code == 200:
                cols = [c["column_name"]
                        for c in r2.json().get("result", {}).get("columns", [])]
                loaded[ds["table_name"]] = {"id": ds["id"], "columns": cols}
        await client.aclose()
        if loaded:
            datasets = loaded
            log.info(f"Refreshed {len(datasets)} datasets: {list(datasets)}")
    except Exception as exc:
        log.warning(f"Dataset refresh failed: {exc}")


async def load_datasets_on_startup() -> None:
    """Retry until Superset is ready, then start periodic refresh."""
    for attempt in range(15):
        await refresh_datasets()
        if datasets:
            return
        log.warning(f"Attempt {attempt + 1}/15: no datasets yet, retrying...")
        await asyncio.sleep(10)
    log.error("Could not load datasets after 15 attempts.")


async def periodic_dataset_refresh() -> None:
    """Refresh datasets every 30 seconds to pick up new uploads."""
    while True:
        await asyncio.sleep(30)
        await refresh_datasets()


# ── RBAC: user context verification ──────────────────────────────────────────

async def verify_user_context(user_context: dict) -> Optional[dict]:
    """Verify the claimed user exists and cross-reference claimed datasets."""
    if not user_context or not user_context.get("user"):
        return None

    claimed_user = user_context["user"]
    claimed_datasets = user_context.get("datasets") or []
    username = claimed_user.get("username")

    if not username:
        return None

    try:
        client = await superset_session()
        r = await client.get("/api/v1/security/users/",
                             params={"q": json.dumps({"filters": [
                                 {"col": "username", "opr": "eq", "value": username}
                             ]})})
        if r.status_code != 200 or not r.json().get("result"):
            log.warning(f"User verification failed for '{username}'")
            await client.aclose()
            return None

        verified_user = r.json()["result"][0]
        user_id = verified_user["id"]
        await client.aclose()

        # Cross-reference: only allow datasets that admin also knows about
        admin_dataset_ids = {info["id"] for info in datasets.values()}
        verified_datasets = {}
        for ds in claimed_datasets:
            if ds["id"] in admin_dataset_ids:
                verified_datasets[ds["table_name"]] = {
                    "id": ds["id"],
                    "columns": ds["columns"],
                }
            else:
                log.warning(f"User '{username}' claimed unknown dataset id={ds['id']}")

        return {
            "id": user_id,
            "username": username,
            "first_name": claimed_user.get("first_name", ""),
            "last_name": claimed_user.get("last_name", ""),
            "datasets": verified_datasets,
        }
    except Exception as exc:
        log.warning(f"User verification error: {exc}")
        return None


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(load_datasets_on_startup())
    refresh_task = asyncio.create_task(periodic_dataset_refresh())
    yield
    refresh_task.cancel()


app = FastAPI(lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "datasets_loaded": len(datasets)}


@app.get("/logo.png")
async def logo():
    return FileResponse("logo.png", media_type="image/png")


@app.get("/chat", response_class=HTMLResponse)
async def chat():
    with open("chat_ui.html") as f:
        return f.read()


@app.get("/history/{session_id}")
async def history(session_id: str):
    """Return previous conversation messages so the UI can restore them after refresh."""
    sess = sessions.get(session_id)
    if not sess:
        return {"messages": []}
    if (time.time() - sess.get("last_active", 0)) > SESSION_TIMEOUT:
        del sessions[session_id]
        return {"messages": []}
    return {"messages": [
        {"role": m["role"], "content": m["content"]}
        for m in sess["history"]
        if not m["content"].startswith("SYSTEM:")
    ]}


@app.post("/reset/{session_id}")
async def reset_session(session_id: str):
    """Clear a chat session so the user can start fresh."""
    sessions.pop(session_id, None)
    return {"status": "ok"}


class MessageRequest(BaseModel):
    session_id: str
    message: str
    user_context: Optional[dict] = None  # { user: {...}, datasets: [...] }


@app.post("/message")
async def message(req: MessageRequest):
    try:
        return await _handle_message(req)
    except Exception as exc:
        log.error(f"Unhandled error in /message: {exc}")
        msg = str(exc)
        if "credit balance" in msg.lower():
            msg = "Insufficient API credits. Please check your LLM provider billing."
        return JSONResponse({"reply": f"Error: {msg}", "state": "new"}, status_code=200)


async def _handle_message(req: MessageRequest):
    sid = req.session_id
    user_msg = req.message.strip()

    # ── Create or retrieve session ────────────────────────────────────────────
    if sid not in sessions:
        verified_user = None
        if req.user_context:
            verified_user = await verify_user_context(req.user_context)
            if verified_user:
                log.info(f"Session {sid}: verified user '{verified_user['username']}' "
                         f"with {len(verified_user['datasets'])} datasets")
            else:
                log.warning(f"Session {sid}: user verification failed, admin fallback")

        sessions[sid] = {"history": [], "state": "new",
                         "proposed_config": None, "dashboard_url": None,
                         "last_active": time.time(),
                         "user": verified_user}
    sess = sessions[sid]
    sess["last_active"] = time.time()

    # Update user context if it wasn't set before but is now available
    if not sess.get("user") and req.user_context:
        verified_user = await verify_user_context(req.user_context)
        if verified_user:
            sess["user"] = verified_user
            log.info(f"Session {sid}: late user context for '{verified_user['username']}'")

    # ── Add user turn to history ──────────────────────────────────────────────
    sess["history"].append({"role": "user", "content": user_msg})

    # ── State: new / proposing → ask Claude for a proposal ───────────────────
    if sess["state"] in ("new", "proposing"):
        reply = await _claude(sess["history"], sess=sess)
        sess["history"].append({"role": "assistant", "content": reply})
        sess["state"] = "waiting_confirm"
        return {"reply": reply, "state": "waiting_confirm"}

    # ── State: waiting_confirm ────────────────────────────────────────────────
    if sess["state"] == "waiting_confirm":
        if _is_yes(user_msg):
            sess["history"].append({"role": "user", "content": (
                "SYSTEM: The user confirmed. The backend will now automatically create the dashboard. "
                "Your ONLY job is to output a single JSON object so the backend knows what to build. "
                "Output ONLY raw JSON — no explanation, no markdown, no code fences. "
                "Use this exact format:\n"
                '{"dashboard_title": "<str>", "charts": ['
                '{"dataset_id": <int>, "metric_column": "<str>", "dimension_column": "<str>", '
                '"chart_type": "<bar|line|table|pie>", "chart_title": "<str>"}'
                "]}\n"
                "Include ALL charts you proposed in the charts array."
            )})
            sess["state"] = "building"
            json_reply = await _claude(sess["history"], max_tokens=1024, sess=sess)
            sess["history"].append({"role": "assistant", "content": json_reply})
            try:
                config = _parse_json(json_reply)
                config = _normalize_config(config)
                user_ds = sess["user"]["datasets"] if sess.get("user") else None
                _validate_config(config, user_datasets=user_ds)
                owner_id = sess["user"]["id"] if sess.get("user") else None
                url = await _build_dashboard(config, user_id=owner_id)
                n = len(config["charts"])
                sess["state"] = "done"
                sess["dashboard_url"] = url
                return {
                    "reply": f"Done! I created {n} chart{'s' if n > 1 else ''} in your dashboard: {url}",
                    "state": "done",
                    "dashboard_url": url,
                }
            except Exception as exc:
                log.error(f"Dashboard build failed: {exc}")
                sess["state"] = "waiting_confirm"
                return {"reply": f"Sorry, the build failed ({exc}). Could you rephrase your request?",
                        "state": "waiting_confirm"}
        else:
            reply = await _claude(sess["history"], sess=sess)
            sess["history"].append({"role": "assistant", "content": reply})
            return {"reply": reply, "state": "waiting_confirm"}

    # ── State: done → start fresh conversation ────────────────────────────────
    if sess["state"] == "done":
        sess["history"] = [{"role": "user", "content": user_msg}]
        sess["state"] = "proposing"
        reply = await _claude(sess["history"], sess=sess)
        sess["history"].append({"role": "assistant", "content": reply})
        sess["state"] = "waiting_confirm"
        return {"reply": reply, "state": "waiting_confirm"}

    return {"reply": "Something went wrong. Please refresh and try again.", "state": "new"}


# ── Claude helpers ────────────────────────────────────────────────────────────

def _system_prompt_for_session(sess: dict) -> str:
    """Build system prompt using the session's user-specific datasets (RBAC)."""
    user = sess.get("user") if sess else None

    if user and user.get("datasets"):
        ds_info = {
            name: {"id": info["id"], "columns": info["columns"]}
            for name, info in user["datasets"].items()
        }
        name = user.get("first_name") or user.get("username", "there")
    else:
        # Fallback: admin view (standalone mode or unverified user)
        ds_info = {
            name: {"id": info["id"], "columns": info["columns"]}
            for name, info in datasets.items()
        }
        name = "there"

    return f"""You are BI Genie — an automated dashboard builder embedded in Apache Superset.
You are connected to a backend that AUTOMATICALLY creates charts and dashboards in Superset via its API.
The user does NOT need to do anything manually — you propose, they confirm, and the system builds it.

You are chatting with {name}.

Available datasets (use the exact dataset_id integer when generating JSON):
{json.dumps(ds_info, indent=2)}

IMPORTANT: Only use datasets listed above. Do NOT reference or propose charts for any dataset not in this list.

WORKFLOW — follow these steps exactly:
1. Understand what the user wants to visualise.
2. Pick the best dataset, numeric metric columns (for SUM), dimension/groupby columns, and chart types.
3. For comprehensive requests, propose MULTIPLE charts (bar, line, table, pie) — up to 6 charts per dashboard.
4. Reply in plain text — short and friendly — describing what you will build. Say "I'll create a dashboard with..." not "you need to create...".
5. Wait for the user to confirm (yes/ok/go ahead).
6. IMPORTANT: Never tell the user to create anything manually. You do everything automatically.
7. Never output JSON during the proposal step — only when explicitly asked by the system.

Available chart types: bar, line, table, pie
For line charts use a date column as the dimension.
Keep replies concise. No markdown. Plain text only."""


async def _claude(history: list, max_tokens: int = 512, sess: dict = None) -> str:
    system = _system_prompt_for_session(sess) if sess else _system_prompt_for_session({})
    messages = [{"role": "system", "content": system}] + history
    resp = await llm.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        messages=messages,
    )
    return resp.choices[0].message.content


def _is_yes(msg: str) -> bool:
    YES = {"yes", "yep", "ok", "okay", "sure", "correct", "right",
           "looks good", "build it", "go ahead", "do it", "proceed"}
    m = msg.lower().strip()
    return any(w in m for w in YES)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in response")
    return json.loads(text[start:end])


def _normalize_config(config: dict) -> dict:
    """Convert old single-chart format to new multi-chart format if needed."""
    if "charts" in config:
        return config
    return {
        "dashboard_title": config.get("dashboard_title", "Dashboard"),
        "charts": [{
            "dataset_id": config["dataset_id"],
            "metric_column": config["metric_column"],
            "dimension_column": config["dimension_column"],
            "chart_type": config["chart_type"],
            "chart_title": config["chart_title"],
        }],
    }


def _validate_config(config: dict, user_datasets: dict = None) -> None:
    """Validate config. If user_datasets is provided, enforce RBAC."""
    if "dashboard_title" not in config:
        raise ValueError("Missing dashboard_title")
    charts = config.get("charts", [])
    if not charts:
        raise ValueError("No charts in config")
    required = {"dataset_id", "metric_column", "dimension_column",
                "chart_type", "chart_title"}

    # Use user's datasets for RBAC enforcement, or admin's as fallback
    ds_source = user_datasets if user_datasets else datasets
    known_ids = {info["id"] for info in ds_source.values()}

    for i, c in enumerate(charts):
        missing = required - c.keys()
        if missing:
            raise ValueError(f"Chart {i}: missing keys {missing}")
        if c["dataset_id"] not in known_ids:
            raise ValueError(
                f"Chart {i}: dataset_id {c['dataset_id']} is not accessible"
            )


# ── Dashboard builder ─────────────────────────────────────────────────────────

def _chart_params(viz: str, metric_col: str, dim_col: str) -> dict:
    metric = {
        "expressionType": "SIMPLE",
        "column": {"column_name": metric_col},
        "aggregate": "SUM",
        "label": metric_col,
        "hasCustomLabel": False,
    }
    base = {"viz_type": viz, "time_range": "No filter"}
    if viz in ("echarts_timeseries_line", "echarts_timeseries_bar"):
        return {**base, "x_axis": dim_col, "metrics": [metric],
                "groupby": [], "row_limit": 10000,
                "order_desc": True, "truncate_metric": True}
    if viz == "pie":
        return {**base, "metric": metric, "groupby": [dim_col], "row_limit": 25}
    # table
    return {**base, "metrics": [metric], "groupby": [dim_col],
            "row_limit": 100, "order_desc": True}


def _build_position_json(chart_ids: list[int], chart_titles: list[str]) -> dict:
    """Build Superset position JSON for N charts in a grid layout (2 per row)."""
    n = len(chart_ids)
    position = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
    }

    row_ids = []
    chart_entries = []
    i = 0
    row_num = 0
    while i < n:
        row_num += 1
        row_id = f"ROW-{row_num}"
        row_ids.append(row_id)
        children = []

        if i + 1 < n:
            for j in range(2):
                ck = f"CHART-{i + j + 1}"
                children.append(ck)
                chart_entries.append((ck, chart_ids[i + j], chart_titles[i + j], 6, row_id))
            i += 2
        else:
            ck = f"CHART-{i + 1}"
            children.append(ck)
            chart_entries.append((ck, chart_ids[i], chart_titles[i], 12, row_id))
            i += 1

        position[row_id] = {
            "children": children, "id": row_id, "type": "ROW",
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        }

    position["GRID_ID"] = {
        "children": row_ids, "id": "GRID_ID", "type": "GRID",
        "parents": ["ROOT_ID"],
    }

    for ck, cid, title, width, row_id in chart_entries:
        position[ck] = {
            "children": [], "id": ck, "type": "CHART",
            "meta": {"chartId": cid, "height": 50,
                     "sliceName": title, "width": width},
            "parents": ["ROOT_ID", "GRID_ID", row_id],
        }

    return position


async def _build_dashboard(config: dict, user_id: int = None) -> str:
    """Create chart(s) + dashboard in Superset, link them, return URL."""
    charts_cfg = config["charts"]
    dash_title = config["dashboard_title"]

    client = await superset_session()

    owners = [user_id] if user_id else []

    # 1. Create all charts ────────────────────────────────────────────────────
    chart_ids = []
    chart_titles = []
    for c in charts_cfg:
        viz = VIZ_MAP.get(c["chart_type"], "echarts_timeseries_bar")
        payload = {
            "slice_name":      c["chart_title"],
            "viz_type":        viz,
            "datasource_id":   c["dataset_id"],
            "datasource_type": "table",
            "params":          json.dumps(_chart_params(viz, c["metric_column"], c["dimension_column"])),
        }
        if owners:
            payload["owners"] = owners
        r = await client.post("/api/v1/chart/", json=payload)
        r.raise_for_status()
        chart_ids.append(r.json()["id"])
        chart_titles.append(c["chart_title"])
        log.info(f"Created chart id={chart_ids[-1]} ({c['chart_title']}) owner={user_id or 'admin'}")

    # 2. Create dashboard with position layout ────────────────────────────────
    position = _build_position_json(chart_ids, chart_titles)
    dash_payload = {
        "dashboard_title": dash_title,
        "published":       True,
        "position_json":   json.dumps(position),
    }
    if owners:
        dash_payload["owners"] = owners
    r = await client.post("/api/v1/dashboard/", json=dash_payload)
    r.raise_for_status()
    dash_id = r.json()["id"]
    log.info(f"Created dashboard id={dash_id} ({dash_title}) owner={user_id or 'admin'}")

    await client.aclose()

    # 3. Link all charts → dashboard via Postgres ─────────────────────────────
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    for cid in chart_ids:
        cur.execute(
            "INSERT INTO dashboard_slices (dashboard_id, slice_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (dash_id, cid),
        )
    cur.close()
    conn.close()
    log.info(f"Linked {len(chart_ids)} charts → dashboard {dash_id}")

    return f"{SUPERSET_EXTERNAL_URL}/superset/dashboard/{dash_id}/"
