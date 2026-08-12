"""Generic Agent Interpreter — architecture.drawio page 3, "How One Agent Runs".

ONE engine, always on, shared by every agent. It never changes when a
business user creates or edits an agent — it only ever reads that agent's
own config (prompt, tools, model, flow graph) from the Agent Config Store
(store.py) and compiles/runs it fresh with LangGraph. That data/engine split
is exactly why publishing or editing an agent needs no redeploy.

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

MAX_STEPS = 8  # hard stop so a misconfigured loop can't run forever


class AgentError(Exception):
    """Raised for problems that are the agent config's fault, not a bug —
    surfaced verbatim to the caller instead of being swallowed."""


class RunState(TypedDict):
    messages: list[Any]
    trace: list[dict[str, Any]]
    steps: int


def get_llm(model_endpoint: str, obo_token: Optional[str] = None):
    if not model_endpoint:
        raise AgentError("This agent has no model selected — pick a serving endpoint before running it.")
    try:
        return DatabricksEndpointChat(endpoint=model_endpoint, obo_token=obo_token)
    except Exception as exc:  # noqa: BLE001 - surfacing the SDK's own error message is the point
        raise AgentError(f"Could not reach model serving endpoint '{model_endpoint}': {exc}") from exc


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
    namespace: dict[str, Any] = {"w": obo_client or WorkspaceClient()}
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


def run_agent(
    agent: dict[str, Any],
    tools_config: list[dict[str, Any]],
    question: str,
    obo_token: Optional[str] = None,
    caller_email: Optional[str] = None,
) -> dict[str, Any]:
    """Executes one agent's own flow end to end and returns a full trace —
    this is the literal implementation of page 3's Steps 1-3.

    When `obo_token` is available (forwarded from the caller's own browser
    session — see main.py), both the model call and every tool run as that
    employee, so real Unity Catalog grants on THEIR identity are what's
    actually enforced, not the app's own blanket access."""
    # auth_type="pat" is required: without it, Config also picks up this
    # app's own ambient DATABRICKS_CLIENT_ID/SECRET env vars and refuses to
    # proceed with "more than one authorization method configured" — a real
    # error hit building this, not a hypothetical.
    obo_client = (
        WorkspaceClient(config=Config(token=obo_token, auth_type="pat")) if obo_token else None
    )
    llm = get_llm(agent.get("model", ""), obo_token=obo_token)
    tools = [build_tool(t, obo_client) for t in tools_config]
    llm_with_tools = llm.bind_tools(tools) if tools else llm
    tools_by_name = {t.name: t for t in tools}

    graph = StateGraph(RunState)

    def think(state: RunState) -> RunState:
        messages = state["messages"]
        response = llm_with_tools.invoke(messages)
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

    final_answer = ""
    for msg in reversed(final_state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            final_answer = msg.content
            break

    if not obo_token:
        ran_as = "app identity (no OBO token forwarded)"
    elif llm.last_call_was_obo:
        ran_as = f"employee ({caller_email}) — model + tools"
    else:
        # Real, current platform gap, not a bug: Databricks Apps can't
        # declare the "model-serving-inference" scope an OBO'd model call
        # needs, so that leg falls back to the app's own identity while
        # tools (SQL etc.) still run as the employee for real.
        ran_as = f"employee ({caller_email}) for tools; app identity for the model call (Foundation Model OBO invocation isn't supported by Databricks Apps yet)"

    return {
        "id": f"run-{uuid.uuid4().hex[:8]}",
        "agent_id": agent["id"],
        "question": question,
        "answer": final_answer,
        "trace": final_state["trace"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ran_as": ran_as,
    }
