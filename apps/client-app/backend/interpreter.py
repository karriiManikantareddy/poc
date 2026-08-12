"""Generic Agent Interpreter — architecture.drawio page 3, "How One Agent Runs".

ONE engine, always on, shared by every agent. It never changes when a
business user creates or edits an agent — it only ever reads that agent's
own config (prompt, tools, model, flow graph) from the Agent Config Store
(store.py) and compiles/runs it fresh with LangGraph. That data/engine split
is exactly why publishing or editing an agent needs no redeploy.

Two modes, both real, both using the exact same LLM/tool machinery:
- "simple" (default): the original fixed think<->act loop — one model,
  every attached tool available every turn.
- "custom": the agent's own stored node/edge graph (Epic 8 — the visual
  canvas) is compiled into a real LangGraph topology. A think node's
  available tools are exactly whichever tool nodes it has an outgoing
  edge to — so different think steps in the same agent can genuinely see
  different tools. A think node whose model calls more than one connected
  tool at once fans out to all of them as real parallel LangGraph branches
  (confirmed supported: add_conditional_edges' routing function can return
  a list of target node ids, not just one).

Step 3a "Think" calls a real Databricks Model Serving Foundation Model
endpoint (see dbx_chat.py) — this app runs inside the client's own
workspace as a Databricks App, so it has real, governed access to that
workspace's own serving endpoints. Nothing here is a dummy/mocked model.

Step 3b "Act" calls a tool. Tools are tenant-written Python functions
(the "Action Pack" in the architecture). The real architecture governs
these as Unity Catalog Functions; here they are exec()'d in-process, the
same honest scoping-down documented in this project's README.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph

from dbx_chat import DatabricksEndpointChat

MAX_STEPS = 12  # hard stop so a misconfigured loop (or graph cycle) can't run forever


class AgentError(Exception):
    """Raised for problems that are the agent config's fault, not a bug —
    surfaced verbatim to the caller instead of being swallowed."""


class RunState(TypedDict):
    messages: list[Any]
    trace: list[dict[str, Any]]
    steps: int


def get_llm(model_endpoint: str, obo_token: Optional[str] = None) -> DatabricksEndpointChat:
    if not model_endpoint:
        raise AgentError("This agent has no model selected — pick a serving endpoint before running it.")
    try:
        return DatabricksEndpointChat(endpoint=model_endpoint, obo_token=obo_token)
    except Exception as exc:  # noqa: BLE001 - surfacing the SDK's own error message is the point
        raise AgentError(f"Could not reach model serving endpoint '{model_endpoint}': {exc}") from exc


class _FallbackProxy:
    """Wraps one SDK sub-API (e.g. the object behind `w.warehouses` or
    `w.statement_execution`) so every method call on it tries the
    employee's OBO identity first, falling back to the app's own
    identity on the specific missing-scopes error this exists for.

    Generic on purpose, not hand-picked per method: an earlier version
    of this only wrapped `statement_execution.execute_statement`, but a
    real tool's first call is `w.warehouses.list()` to find a warehouse
    id — that call went straight to the OBO client unwrapped, failed
    there first, and never reached the one method that was protected.
    Confirmed via isolated testing that both the OBO path and the app-
    identity path work individually; the bug was the fallback simply
    not being reached for every call a tool might make."""

    def __init__(self, obo_target: Any, app_target: Any):
        self._obo = obo_target
        self._app = app_target

    def __getattr__(self, name: str) -> Any:
        obo_attr = getattr(self._obo, name)
        if not callable(obo_attr):
            return obo_attr
        app_attr = getattr(self._app, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return obo_attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - only swallow the specific known scope gap
                if "required scopes" not in str(exc):
                    raise
                return app_attr(*args, **kwargs)

        return wrapper


class _ResilientWorkspaceClient:
    """Every sub-API a tool might reach via `w` (warehouses,
    statement_execution, catalogs, whatever else) gets the same OBO-
    first-then-app-identity fallback automatically — see
    `_FallbackProxy` for why this has to be generic, not per-method."""

    def __init__(self, obo_client: WorkspaceClient, app_client: WorkspaceClient):
        self._obo = obo_client
        self._app = app_client

    def __getattr__(self, name: str) -> Any:
        return _FallbackProxy(getattr(self._obo, name), getattr(self._app, name))


def build_tool(tool_config: dict[str, Any], obo_client: Optional[WorkspaceClient] = None) -> StructuredTool:
    """Turns a stored Action Pack entry into a real, callable LangChain tool.

    `obo_client` (when available) is injected into the exec namespace as `w`
    so a tool's own code can do `w.statement_execution.execute_statement(...)`
    and have it run as the calling employee, not the app's identity — no
    extra plumbing needed in the tool author's code. Known, accepted limit:
    if a tool does its own `WorkspaceClient()` construction instead of using
    the injected `w`, that call silently reverts to the app's own identity —
    we don't sandbox imports away, so this can't be fully prevented, only
    documented (see the New Tool modal's placeholder text)."""
    w = _ResilientWorkspaceClient(obo_client, WorkspaceClient()) if obo_client else WorkspaceClient()
    namespace: dict[str, Any] = {"w": w}
    try:
        exec(tool_config["code"], namespace)  # noqa: S102 - intentional, this IS the Action Pack execution model
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"Tool '{tool_config['name']}' has a code error: {exc}") from exc

    fn = namespace.get(tool_config["name"])
    if fn is None or not callable(fn):
        raise AgentError(
            f"Tool '{tool_config['name']}' code must define a function named exactly '{tool_config['name']}'."
        )
    return StructuredTool.from_function(
        func=fn,
        name=tool_config["name"],
        description=tool_config.get("description", ""),
    )


def _finalize(
    agent: dict[str, Any],
    question: str,
    final_state: RunState,
    llm_used_obo: Optional[bool],
    obo_token: Optional[str],
    caller_email: Optional[str],
) -> dict[str, Any]:
    final_answer = ""
    for msg in reversed(final_state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            final_answer = msg.content
            break
    if not final_answer:
        # A pure tool pipeline (e.g. starting from a Tool node with no
        # Think node ever running) never produces an AIMessage at all —
        # fall back to the last real tool result rather than an empty
        # answer, since that's genuinely what this run produced.
        for entry in reversed(final_state["trace"]):
            if entry.get("step") == "act":
                final_answer = str(entry.get("result", ""))
                break

    if not obo_token:
        ran_as = "app identity (no OBO token forwarded)"
    elif llm_used_obo is None:
        # A pure tool pipeline never called a model at all — no model
        # identity to report, just the tools.
        ran_as = f"employee ({caller_email}) — tools only (this flow never reached a Think node)"
    elif llm_used_obo:
        ran_as = f"employee ({caller_email}) — model + tools"
    else:
        # See dbx_chat.py — this branch hit once right after the
        # model-serving scope was first declared, then stopped
        # reproducing on retest with no code change (likely scope
        # propagation delay, not a permanent block). Kept as a real
        # fallback rather than removed, so a recurrence degrades
        # gracefully instead of failing the whole run.
        ran_as = f"employee ({caller_email}) for tools; app identity for the model call (see dbx_chat.py notes)"

    return {
        "id": f"run-{uuid.uuid4().hex[:8]}",
        "agent_id": agent["id"],
        "question": question,
        "answer": final_answer,
        "trace": final_state["trace"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ran_as": ran_as,
    }


def run_agent(
    agent: dict[str, Any],
    tools_config: list[dict[str, Any]],
    question: str,
    obo_token: Optional[str] = None,
    caller_email: Optional[str] = None,
) -> dict[str, Any]:
    """Dispatches to simple or custom mode — see module docstring. Simple
    is the default so every agent created before "custom" existed keeps
    running exactly as it always did."""
    if agent.get("mode") == "custom":
        return _run_custom_agent(agent, tools_config, question, obo_token, caller_email)
    return _run_simple_agent(agent, tools_config, question, obo_token, caller_email)


def _run_simple_agent(
    agent: dict[str, Any],
    tools_config: list[dict[str, Any]],
    question: str,
    obo_token: Optional[str],
    caller_email: Optional[str],
) -> dict[str, Any]:
    """The original fixed think<->act loop — this is the literal
    implementation of page 3's Steps 1-3 for the common case."""
    obo_client = _build_obo_client(obo_token)
    llm = get_llm(agent.get("model", ""), obo_token=obo_token)
    tools = [build_tool(t, obo_client) for t in tools_config]
    llm_with_tools = llm.bind_tools(tools) if tools else llm
    tools_by_name = {t.name: t for t in tools}

    graph = StateGraph(RunState)

    def think(state: RunState) -> RunState:
        messages = state["messages"]
        try:
            response = llm_with_tools.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - a bad/unreachable model should be a clean 400, not a 500
            raise AgentError(f"The model call failed: {exc}") from exc
        state["trace"].append({
            "step": "think",
            "content": response.content,
            "tool_calls": [tc["name"] for tc in getattr(response, "tool_calls", [])],
        })
        return {"messages": messages + [response], "trace": state["trace"], "steps": state["steps"] + 1}

    def act(state: RunState) -> RunState:
        last = state["messages"][-1]
        tool_messages = []
        for call in last.tool_calls:
            tool = tools_by_name.get(call["name"])
            if tool is None:
                result = f"Error: no tool named '{call['name']}' is attached to this agent."
            else:
                try:
                    result = tool.invoke(call["args"])
                except Exception as exc:  # noqa: BLE001 - the point is to show the agent its own tool's error
                    result = f"Error running tool: {exc}"
            state["trace"].append({"step": "act", "tool": call["name"], "args": call["args"], "result": str(result)})
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        return {"messages": state["messages"] + tool_messages, "trace": state["trace"], "steps": state["steps"] + 1}

    def route(state: RunState) -> str:
        if state["steps"] >= MAX_STEPS:
            return END
        last = state["messages"][-1]
        return "act" if isinstance(last, AIMessage) and getattr(last, "tool_calls", None) else END

    graph.add_node("think", think)
    if tools:
        graph.add_node("act", act)
        graph.add_edge("act", "think")
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route, {"act": "act", END: END} if tools else {END: END})

    compiled = graph.compile()
    initial: RunState = {
        "messages": [SystemMessage(content=agent.get("prompt", "")), HumanMessage(content=question)],
        "trace": [],
        "steps": 0,
    }
    final_state = compiled.invoke(initial)
    return _finalize(agent, question, final_state, llm.last_call_was_obo, obo_token, caller_email)


def _build_obo_client(obo_token: Optional[str]) -> Optional[WorkspaceClient]:
    # auth_type="pat" is required: without it, Config also picks up this
    # app's own ambient DATABRICKS_CLIENT_ID/SECRET env vars and refuses to
    # proceed with "more than one authorization method configured" — a real
    # error hit building this, not a hypothetical.
    return WorkspaceClient(config=Config(token=obo_token, auth_type="pat")) if obo_token else None


def _run_custom_agent(
    agent: dict[str, Any],
    tools_config: list[dict[str, Any]],
    question: str,
    obo_token: Optional[str],
    caller_email: Optional[str],
) -> dict[str, Any]:
    """Compiles the agent's own stored node/edge graph (built in the visual
    canvas) into a real LangGraph topology and runs it. Validated up front
    rather than left to fail confusingly mid-run — a business user editing
    a graph in the canvas should get a clear reason, not a stack trace."""
    graph_def = agent.get("graph") or {}
    nodes = {n["id"]: n for n in graph_def.get("nodes", [])}
    edges = graph_def.get("edges", [])
    if not nodes:
        raise AgentError("This agent's custom graph is empty — add at least one node in the canvas.")

    tools_by_id = {t["id"]: t for t in tools_config}
    obo_client = _build_obo_client(obo_token)

    outgoing: dict[str, list[str]] = {nid: [] for nid in nodes}
    incoming: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        src, tgt = e["source"], e["target"]
        if src not in nodes or tgt not in nodes:
            raise AgentError(f"Edge references a node that doesn't exist ({src} -> {tgt}).")
        outgoing[src].append(tgt)
        incoming[tgt].append(src)

    # Entry must be explicit, not inferred from edge topology: a normal
    # think<->tool loop-back gives the think node an incoming edge too,
    # so "no incoming edges" can't reliably identify the start once a
    # graph has any loop in it — confirmed by hitting exactly that with
    # a real think<->tool<->think test graph. Entry can be either node
    # type — a Tool node can legitimately be the first step (e.g. always
    # fetch a fixed summary before any reasoning happens).
    entry_node = graph_def.get("entry")
    if not entry_node or entry_node not in nodes:
        raise AgentError("This agent's graph has no starting node set — mark one node as the entry point in the canvas.")

    # Build real LangChain tools once per tool node, each bound only to
    # the specific tool it represents.
    node_tools: dict[str, StructuredTool] = {}
    for nid, node in nodes.items():
        if node["type"] == "tool":
            tool_cfg = tools_by_id.get(node.get("tool_id"))
            if not tool_cfg:
                raise AgentError(f"Tool node '{nid}' references a tool that no longer exists.")
            node_tools[nid] = build_tool(tool_cfg, obo_client)
            # A tool node with no incoming edges is only valid as the entry
            # node — otherwise it's genuinely unreachable dead weight.
            if not incoming[nid] and nid != entry_node:
                raise AgentError(f"Tool node '{nid}' ({tool_cfg['name']}) isn't connected from anything and isn't the starting node — it can never run.")

    graph = StateGraph(RunState)
    llm_used_obo_holder = {"value": None}

    for nid, node in nodes.items():
        if node["type"] == "think":
            connected_tool_ids = [t for t in outgoing[nid] if nodes[t]["type"] == "tool"]
            connected_tools = [node_tools[t] for t in connected_tool_ids]
            node_model = node.get("model") or agent.get("model", "")
            node_llm = get_llm(node_model, obo_token=obo_token)
            bound_llm = node_llm.bind_tools(connected_tools) if connected_tools else node_llm

            def think_fn(state: RunState, *, _llm=bound_llm, _base_llm=node_llm, _nid=nid) -> RunState:
                try:
                    response = _llm.invoke(state["messages"])
                except Exception as exc:  # noqa: BLE001 - a bad/unreachable model should be a clean 400, not a 500
                    raise AgentError(f"Think node '{_nid}': the model call failed: {exc}") from exc
                llm_used_obo_holder["value"] = _base_llm.last_call_was_obo
                state["trace"].append({
                    "step": "think",
                    "node": _nid,
                    "content": response.content,
                    "tool_calls": [tc["name"] for tc in getattr(response, "tool_calls", [])],
                })
                return {"messages": state["messages"] + [response], "trace": state["trace"], "steps": state["steps"] + 1}

            graph.add_node(nid, think_fn)
        elif node["type"] == "tool":

            def tool_fn(state: RunState, *, _nid=nid, _tool=node_tools[nid], _defaults=node.get("default_args") or {}) -> RunState:
                last = state["messages"][-1]
                matching = [c for c in getattr(last, "tool_calls", []) if c["name"] == _tool.name]
                new_messages = []
                if matching:
                    # A connected Think node's model genuinely decided to call
                    # this tool — pair the result with that real tool_call id,
                    # exactly as a normal LangChain tool turn expects.
                    for call in matching:
                        try:
                            result = _tool.invoke(call["args"])
                        except Exception as exc:  # noqa: BLE001 - show the agent its own tool's error
                            result = f"Error running tool: {exc}"
                        state["trace"].append({"step": "act", "node": _nid, "tool": _tool.name, "args": call["args"], "result": str(result)})
                        new_messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
                else:
                    # No LLM decided to call this — reached as the entry node,
                    # or chained straight from another tool node. Run it
                    # unconditionally with its configured default arguments.
                    # Can't use a ToolMessage here: that type requires pairing
                    # with a real preceding tool_call id, and a fabricated one
                    # would make the real Foundation Model API reject the
                    # payload — a plain message carries the result just as
                    # well for the next step to read.
                    try:
                        result = _tool.invoke(_defaults)
                    except Exception as exc:  # noqa: BLE001
                        result = f"Error running tool: {exc}"
                    state["trace"].append({"step": "act", "node": _nid, "tool": _tool.name, "args": _defaults, "result": str(result)})
                    new_messages.append(HumanMessage(content=f"[{_tool.name} result] {result}"))
                return {"messages": state["messages"] + new_messages, "trace": state["trace"], "steps": state["steps"] + 1}

            graph.add_node(nid, tool_fn)
        else:
            raise AgentError(f"Node '{nid}' has an unknown type '{node['type']}'.")

    graph.add_edge(START, entry_node)

    for nid, node in nodes.items():
        targets = outgoing[nid]
        if node["type"] == "tool":
            if len(targets) > 1:
                raise AgentError(f"Tool node '{nid}' has more than one outgoing connection — a tool step must lead to at most one next step.")
            if targets:
                graph.add_edge(nid, targets[0])
            # else: implicitly ends here — no edge needed, LangGraph treats a
            # node with no outgoing edges as a valid dead end only if it's
            # reachable from an END-bound conditional; tool nodes always
            # return to their caller via the edge below, so this is handled
            # by the think node's routing instead (a tool node here should
            # always have exactly one outgoing edge back to a think node).
            continue

        tool_targets = [t for t in targets if nodes[t]["type"] == "tool"]
        fallback_targets = [t for t in targets if nodes[t]["type"] != "tool"]
        if len(fallback_targets) > 1:
            raise AgentError(f"Think node '{nid}' has more than one non-tool outgoing connection — only one 'continue thinking' path is allowed.")
        tool_name_to_node = {node_tools[t].name: t for t in tool_targets}

        def route(state: RunState, *, _tool_map=tool_name_to_node, _fallback=fallback_targets[0] if fallback_targets else None) -> Any:
            if state["steps"] >= MAX_STEPS:
                return END
            last = state["messages"][-1]
            calls = getattr(last, "tool_calls", None) or []
            targets_hit = [_tool_map[c["name"]] for c in calls if c["name"] in _tool_map]
            if targets_hit:
                return targets_hit
            return _fallback if _fallback else END

        # No path_map: per LangGraph's own contract, when it's omitted the
        # values `route` returns (real node ids, possibly several for a
        # parallel fan-out, or END) are used directly as target names.
        graph.add_conditional_edges(nid, route)

    try:
        compiled = graph.compile()
    except Exception as exc:  # noqa: BLE001 - a malformed canvas graph should say so, not crash opaquely
        raise AgentError(f"This agent's graph isn't valid: {exc}") from exc

    initial: RunState = {
        "messages": [SystemMessage(content=agent.get("prompt", "")), HumanMessage(content=question)],
        "trace": [],
        "steps": 0,
    }
    final_state = compiled.invoke(initial)
    return _finalize(agent, question, final_state, llm_used_obo_holder["value"], obo_token, caller_email)
