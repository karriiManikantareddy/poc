"""Client App backend — architecture.drawio page 2 ("CLIENT WORKSPACE").

This is the one Databricks App deployed into each client's own workspace.
Page 2 defines four modules in its UI: Connectors | LLM Providers | Agents |
Access Control. The Agents module is real end to end — the Generic Agent
Interpreter (interpreter.py) genuinely compiles and runs each agent's own
LangGraph flow, calling this workspace's real Foundation Model serving
endpoints via LangChain. The Connectors module (connectors.py) is also
real — genuine Unity Catalog Connection + Lakeflow ingestion Pipeline
objects via databricks-sdk, no sync engine of our own. LLM Providers (as a
management surface) and Access Control are not built yet; their tabs say
so honestly in the UI rather than showing invented data.

Local dev:
    uvicorn main:app --port 8200 --app-dir backend
On Databricks Apps: started via `python backend/main.py` (see app.yaml).
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import connectors
import store
from connectors import ConnectorError
from interpreter import AgentError, run_agent

app = FastAPI(title="Client App — Agents Module POC")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Model endpoints (real, live from this workspace — US-6.1) ----------

@app.get("/api/model-endpoints")
def list_model_endpoints():
    """Resolved live from the workspace every time, never a static list —
    this is what lets the Agents module's model picker be real."""
    try:
        w = WorkspaceClient()
        endpoints = w.serving_endpoints.list()
        return [
            {"name": e.name, "ready": e.state.ready.value if e.state and e.state.ready else "UNKNOWN"}
            for e in endpoints
        ]
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
        raise HTTPException(502, f"Could not list serving endpoints from this workspace: {exc}")


# ---------- Agents (real — architecture.drawio page 3) ----------

@app.get("/api/agents")
def list_agents():
    return store.read("agents")


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    agent = store.get_item("agents", agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@app.post("/api/agents")
def create_agent(agent: dict[str, Any]):
    agent["id"] = f"agent-{uuid.uuid4().hex[:8]}"
    agent.setdefault("status", "draft")
    agent.setdefault("tools", [])
    agent["created_at"] = now()
    return store.upsert_item("agents", agent)


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, agent: dict[str, Any]):
    agent["id"] = agent_id
    return store.upsert_item("agents", agent)


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    if not store.delete_item("agents", agent_id):
        raise HTTPException(404, "Agent not found")
    return {"deleted": agent_id}


class RunRequest(BaseModel):
    question: str


@app.post("/api/agents/{agent_id}/run")
def run(agent_id: str, body: RunRequest):
    agent = store.get_item("agents", agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    tool_ids = agent.get("tools", [])
    all_tools = {t["id"]: t for t in store.read("tools")}
    missing = [tid for tid in tool_ids if tid not in all_tools]
    if missing:
        raise HTTPException(400, f"Agent references tools that no longer exist: {', '.join(missing)}")
    tools_config = [all_tools[tid] for tid in tool_ids]

    try:
        record = run_agent(agent, tools_config, body.question)
    except AgentError as exc:
        raise HTTPException(400, str(exc))
    store.append_item("runs", record)
    return record


@app.get("/api/agents/{agent_id}/runs")
def agent_runs(agent_id: str):
    return [r for r in store.read("runs") if r["agent_id"] == agent_id]


# ---------- Tools / Action Packs (real — exec'd in-process) ----------

@app.get("/api/tools")
def list_tools():
    return store.read("tools")


@app.post("/api/tools")
def create_tool(tool: dict[str, Any]):
    tool["id"] = f"tool-{uuid.uuid4().hex[:8]}"
    return store.upsert_item("tools", tool)


@app.put("/api/tools/{tool_id}")
def update_tool(tool_id: str, tool: dict[str, Any]):
    tool["id"] = tool_id
    return store.upsert_item("tools", tool)


@app.delete("/api/tools/{tool_id}")
def delete_tool(tool_id: str):
    if not store.delete_item("tools", tool_id):
        raise HTTPException(404, "Tool not found")
    return {"deleted": tool_id}


# ---------- Connectors (real — architecture.drawio page 2, Lakeflow Connect) ----------

@app.get("/api/connector-types")
def connector_types():
    """Every real connection type this SDK knows about, not a curated list."""
    try:
        return connectors.list_connection_types()
    except ConnectorError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/connections")
def connections():
    """Live Unity Catalog Connections in this workspace right now — not
    scoped to ones this app created, since Connections are a shared,
    governed UC object other tools may also create or reuse."""
    try:
        return connectors.list_connections()
    except ConnectorError as exc:
        raise HTTPException(502, str(exc))


class CreateConnectionRequest(BaseModel):
    name: str
    connection_type: str
    fields: dict[str, str]
    comment: str = ""


@app.post("/api/connections")
def create_connection(body: CreateConnectionRequest):
    try:
        return connectors.create_host_connection(body.name, body.connection_type, body.fields, body.comment)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/connections/{name}")
def delete_connection(name: str):
    try:
        connectors.delete_connection(name)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc))
    return {"deleted": name}


@app.get("/api/catalogs")
def catalogs():
    try:
        return connectors.list_catalogs()
    except ConnectorError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/catalogs/{catalog}/schemas")
def schemas(catalog: str):
    try:
        return connectors.list_schemas(catalog)
    except ConnectorError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/connectors")
def list_connectors():
    """Ingestion pipelines created through this tab. Scoped to our own
    pointer records (connector_links) since a workspace may have many
    Lakeflow pipelines unrelated to this app; status is always re-fetched
    live, never cached, so a pipeline deleted outside this app shows up
    honestly as gone instead of silently staying in a stale 'connected' state."""
    links = store.read("connector_links")
    result = []
    for link in links:
        try:
            status = connectors.get_pipeline_status(link["pipeline_id"])
        except ConnectorError:
            status = {"state": "NOT_FOUND", "last_update_state": None}
        result.append({**link, **status})
    return result


class CreateConnectorRequest(BaseModel):
    label: str
    connection_name: str
    destination_catalog: str
    destination_schema: str
    sync_mode: str  # "schema" | "tables"
    source_schema: str
    tables: list[str] = []
    schedule_cron: str = ""


@app.post("/api/connectors")
def create_connector(body: CreateConnectorRequest):
    try:
        result = connectors.create_ingestion_pipeline(
            name=f"connector-{body.label}",
            connection_name=body.connection_name,
            destination_catalog=body.destination_catalog,
            destination_schema=body.destination_schema,
            sync_mode=body.sync_mode,
            source_schema=body.source_schema,
            tables=body.tables,
            schedule_cron=body.schedule_cron or None,
        )
    except ConnectorError as exc:
        raise HTTPException(400, str(exc))

    link = {
        "id": f"connector-{uuid.uuid4().hex[:8]}",
        "label": body.label,
        "connection_name": body.connection_name,
        "pipeline_id": result["pipeline_id"],
        "destination_catalog": body.destination_catalog,
        "destination_schema": body.destination_schema,
        "created_at": now(),
    }
    store.append_item("connector_links", link)
    return link


@app.post("/api/connectors/{connector_id}/run")
def run_connector(connector_id: str):
    link = store.get_item("connector_links", connector_id)
    if not link:
        raise HTTPException(404, "Connector not found")
    try:
        return connectors.start_pipeline_update(link["pipeline_id"])
    except ConnectorError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/connectors/{connector_id}")
def delete_connector(connector_id: str):
    link = store.get_item("connector_links", connector_id)
    if not link:
        raise HTTPException(404, "Connector not found")
    try:
        connectors.delete_pipeline(link["pipeline_id"])
    except ConnectorError as exc:
        raise HTTPException(400, str(exc))
    store.delete_item("connector_links", connector_id)
    return {"deleted": connector_id}


# ---------- static frontend ----------

frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DATABRICKS_APP_PORT", 8200))
    uvicorn.run(app, host="0.0.0.0", port=port)
