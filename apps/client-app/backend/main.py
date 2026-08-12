"""Client App backend — architecture.drawio page 2 ("CLIENT WORKSPACE").

This is the one Databricks App deployed into each client's own workspace.
Page 2 defines four modules in its UI: Connectors | LLM Providers | Agents |
Access Control. Of those, the Agents module here is real end to end — the
Generic Agent Interpreter (interpreter.py) genuinely compiles and runs each
agent's own LangGraph flow, calling this workspace's real Foundation Model
serving endpoints via LangChain. Connectors, LLM Providers (as a management
surface), and Access Control are not built yet; their tabs say so honestly
in the UI rather than showing invented data.

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

import store
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


# ---------- static frontend ----------

frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DATABRICKS_APP_PORT", 8200))
    uvicorn.run(app, host="0.0.0.0", port=port)
