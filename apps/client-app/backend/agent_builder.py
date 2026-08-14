"""Agent Builder — generates an agent's tools and flow graph from a plain-
English task description plus the real, live schema of a connected source.

This is deliberately NOT "the LLM writes arbitrary Python and we exec it
blind." The model only ever proposes a small, structured plan (which table,
which SQL, which parameters, one optional fallback column) as JSON; every
table/column name it names is checked against a real schema introspection
before anything runs, and the actual Python tool code is rendered from ONE
fixed, hand-written template — the model never writes the exec()'d code
directly. That keeps "auto-generated" honest without trusting free-form
LLM code on a live data source.

The graph topology (one Think node bound to every generated Tool node, each
tool looping back to Think) is built deterministically here, not by the
model — that's the exact shape already proven to work in this app's own
hand-built demo agent, so graph validity is guaranteed by construction
rather than depending on the model inventing correct topology.
"""
import json
import re
from typing import Any, Optional

import connectors
from connectors import ConnectorError
from dbx_chat import DatabricksEndpointChat
from langchain_core.messages import HumanMessage, SystemMessage


class AgentBuilderError(Exception):
    """A real problem building the plan — bad model output, missing schema,
    etc. Surfaced verbatim, never silently swallowed into a fake success."""


_SYSTEM_TEMPLATE = """You are designing tools for a data-analysis agent. You are given the REAL schema of a connected data source (catalogs, tables, columns) and a plain-English task. Produce a JSON plan of SQL-backed tools that accomplish the task.

Hard rules:
- Only ever reference table names and column names that literally appear in the schema below. Never invent one.
- Each tool runs exactly ONE SQL statement against exactly one table listed under "table" (fully qualified as catalog.schema.table).
- Use named parameters in the SQL as :param_name, and list every one of them in "args".
- Prefer doing joins, counts, and aggregations inside the SQL itself (GROUP BY, COUNT, JOIN) rather than returning raw rows for the caller to manually tally — SQL is reliable at counting, free-text reasoning over raw rows is not.
- If the task implies "if it's not in the obvious table, check every table" for a concept (e.g. leads, customers), set "fallback_column" to the single column name that identifies that concept elsewhere (e.g. "lead_name"). Leave it null if the task doesn't need that.
- Output ONLY one JSON object. No prose, no markdown code fences, nothing before or after it.

Schema (catalog "{catalog}", schema "{schema}"):
{schema_json}

Task:
{task}

Output exactly this shape:
{{
  "tools": [
    {{
      "name": "snake_case_name",
      "description": "one sentence a model would use to decide when to call this",
      "table": "{catalog}.{schema}.table_name",
      "sql": "SELECT ... FROM {catalog}.{schema}.table_name WHERE col = :param",
      "args": ["param"],
      "fallback_column": null
    }}
  ]
}}"""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AgentBuilderError(f"Model didn't return JSON — got: {text[:300]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AgentBuilderError(f"Model's JSON was malformed: {exc}. Raw: {text[:300]}") from exc


def _render_tool_code(spec: dict[str, Any], catalog: str, schema: str) -> str:
    name = spec["name"]
    args: list[str] = spec.get("args") or []
    sql: str = spec["sql"]
    table: str = spec["table"]
    fallback_column: Optional[str] = spec.get("fallback_column") or None

    arg_sig = ", ".join(f"{a}: str" for a in args)
    params_repr = ", ".join(f'StatementParameterListItem(name={a!r}, value=str({a}))' for a in args)

    fallback_block = ""
    if fallback_column:
        fallback_block = f'''
    if not rows:
        discover_resp = w.statement_execution.execute_statement(
            statement=(
                "SELECT DISTINCT table_name FROM {catalog}.information_schema.columns "
                "WHERE table_schema = '{schema}' AND lower(column_name) = lower('{fallback_column}')"
            ),
            warehouse_id=warehouse_id,
            wait_timeout="30s",
        )
        for row in (discover_resp.result.data_array if discover_resp.result else []):
            candidate_table = f"{catalog}.{schema}.{{row[0]}}"
            fallback_sql = ({sql!r}).replace({table!r}, candidate_table)
            fb_resp = w.statement_execution.execute_statement(
                statement=fallback_sql,
                warehouse_id=warehouse_id,
                parameters=[{params_repr}],
                wait_timeout="30s",
            )
            if fb_resp.result and fb_resp.result.data_array:
                rows = fb_resp.result.data_array
                break
'''

    not_found_msg = f"No matching data found in {table}" + (
        " or any other table with a similarly named column." if fallback_column else "."
    )

    return f'''def {name}({arg_sig}) -> str:
    from databricks.sdk.service.sql import StatementParameterListItem
    warehouse_id = next(w.warehouses.list()).id
    resp = w.statement_execution.execute_statement(
        statement={sql!r},
        warehouse_id=warehouse_id,
        parameters=[{params_repr}],
        wait_timeout="30s",
    )
    failed = bool(resp.status and resp.status.state.value == "FAILED")
    rows = resp.result.data_array if (resp.result and not failed) else []
{fallback_block}
    if not rows:
        return {not_found_msg!r}
    return str(rows)
'''


def build_agent_plan(
    task: str,
    model_endpoint: str,
    catalog: str,
    schema: str,
    obo_token: Optional[str] = None,
) -> dict[str, Any]:
    """Introspects the real schema, asks the model for a tool plan, validates
    every referenced table/column against that real schema, and renders real
    Python tool code from a fixed template. Returns tool configs (not yet
    persisted) plus a deterministically-built graph, and a list of warnings
    for anything that didn't validate — the caller (the API layer) decides
    whether to persist, and the UI shows warnings for human review rather
    than silently dropping or silently trusting them."""
    try:
        tables = connectors.list_tables(catalog, schema)
    except ConnectorError as exc:
        raise AgentBuilderError(f"Could not read the schema for {catalog}.{schema}: {exc}") from exc
    if not tables:
        raise AgentBuilderError(f"{catalog}.{schema} has no tables to build tools from.")

    schema_json = json.dumps(
        {t["name"]: [c["name"] for c in t["columns"]] for t in tables}, indent=2
    )
    known_tables = {f"{catalog}.{schema}.{t['name']}" for t in tables}
    known_columns = {c["name"] for t in tables for c in t["columns"]}

    llm = DatabricksEndpointChat(endpoint=model_endpoint, obo_token=obo_token, temperature=0.1, max_tokens=2048)
    prompt = _SYSTEM_TEMPLATE.format(catalog=catalog, schema=schema, schema_json=schema_json, task=task)
    try:
        response = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=task)])
    except Exception as exc:  # noqa: BLE001 - surface the real model-call failure
        raise AgentBuilderError(f"Model call failed while generating the plan: {exc}") from exc

    plan = _extract_json(response.content or "")
    tool_specs = plan.get("tools") or []
    if not tool_specs:
        raise AgentBuilderError("The model didn't propose any tools for this task.")

    warnings: list[str] = []
    tools: list[dict[str, Any]] = []
    for spec in tool_specs:
        name = spec.get("name")
        table = spec.get("table")
        sql = spec.get("sql")
        args = spec.get("args") or []
        if not name or not table or not sql:
            warnings.append(f"Skipped a malformed tool spec: {spec}")
            continue
        if table not in known_tables:
            warnings.append(f"Tool '{name}' references table '{table}', which isn't in {catalog}.{schema} — skipped.")
            continue
        for arg in args:
            if f":{arg}" not in sql:
                warnings.append(f"Tool '{name}': arg '{arg}' isn't used as :{arg} in its SQL.")
        fallback_column = spec.get("fallback_column")
        if fallback_column and fallback_column not in known_columns:
            warnings.append(f"Tool '{name}': fallback_column '{fallback_column}' doesn't match any known column — dropping the fallback for this tool.")
            spec["fallback_column"] = None

        code = _render_tool_code(spec, catalog, schema)
        tools.append({
            "name": name,
            "description": spec.get("description", ""),
            "code": code,
        })

    if not tools:
        raise AgentBuilderError("Every proposed tool failed validation against the real schema — " + "; ".join(warnings))

    # Deterministic graph: one Think node, entry, bound to every generated
    # tool, each tool looping back to Think — the exact shape already
    # proven live in this app's own hand-built demo agent.
    think_id = "node-think-1"
    graph_nodes = [{"id": think_id, "type": "think"}]
    graph_edges = []
    for i, _tool in enumerate(tools, start=1):
        tool_node_id = f"node-tool-{i}"
        graph_nodes.append({"id": tool_node_id, "type": "tool", "tool_name": _tool["name"]})
        graph_edges.append({"source": think_id, "target": tool_node_id})
        graph_edges.append({"source": tool_node_id, "target": think_id})

    return {
        "tools": tools,
        "graph": {"entry": think_id, "nodes": graph_nodes, "edges": graph_edges},
        "warnings": warnings,
    }
