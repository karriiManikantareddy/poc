"""Agent Builder — generates an agent's tools and flow graph from a plain-
English task description plus the real, live schema of a connected source.

This is deliberately NOT "the LLM writes arbitrary Python and we exec it
blind." The model only ever proposes a small, structured plan (which table,
which SQL, which parameters, one optional fallback column, one optional
dependency on another tool) as JSON; every table/column name it names is
checked against a real schema introspection before anything runs, and the
actual Python tool code is rendered from ONE fixed, hand-written template —
the model never writes the exec()'d code directly. That keeps
"auto-generated" honest without trusting free-form LLM code on a live
data source.

Two graph topologies, chosen automatically, not by the model:
- Star (default): one Think node bound to every independent Tool node,
  each tool looping back to Think — the shape proven in this app's simple
  demo agents, for tasks whose tools don't depend on each other.
- Chain: when a tool's plan says it depends on another tool's result (e.g.
  "look up the lead, then use that name to find their workload"), the
  tools are wired in dependency order with a fresh Think node between each
  one — think -> tool A -> think -> tool B -> ... -> a final Think node
  for synthesis. That's the exact shape already proven in this app's own
  hand-built "Project Assistant" demo agent, where the model needs to read
  one tool's real result before it can decide the next tool's arguments —
  something a graph literally cannot hardcode, since it depends on the
  actual data returned at run time.
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
- Only ever reference table names and column names that literally appear in the schema below, using the exact spelling shown. Never invent one.
- Each tool runs exactly ONE SQL statement against exactly one table listed under "table" (fully qualified as catalog.schema.table).
- Use named parameters in the SQL as :param_name, and list every one of them in "args". Every :param_name in the SQL must appear in "args", and every entry in "args" must be used as :param_name somewhere in the SQL.
- Prefer doing joins, counts, and aggregations inside the SQL itself (GROUP BY, COUNT, JOIN) rather than returning raw rows for the caller to manually tally — SQL is reliable at counting, free-text reasoning over raw rows is not.
- If a join can multiply rows (e.g. joining to a table with many rows per parent, like tickets per employee), use SELECT DISTINCT or aggregate instead of returning every joined row — otherwise the result can be enormous even though the answer is small.
- Always include a LIMIT (e.g. LIMIT 50) unless the query already aggregates down to a handful of groups. A result that's too large to fit in the caller's context is treated as a failure, not a success.
- Never propose two tools that run essentially the same query with only the row limit or a name changed — that is one tool, not two. Every tool in your output must answer a genuinely different question. If you're tempted to write a second tool just to cap the row count differently, don't — adjust the one tool's LIMIT instead.
- If the task implies "if it's not in the obvious table, check every table" for a concept (e.g. leads, customers), set "fallback_column" to the single column name that identifies that concept elsewhere (e.g. "lead_name"). Leave it null if the task doesn't need that.
- If a tool's argument value can only be known from ANOTHER tool's result (e.g. "find the lead, then look up that lead's workload"), set "depends_on" to that other tool's exact name. Leave it null if the tool can run on its own with arguments the user directly provides. Don't invent a dependency that isn't really there — most tools should have depends_on: null.
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
      "fallback_column": null,
      "depends_on": null
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


_WAREHOUSE_PICK = '''warehouses = list(w.warehouses.list())
    if not warehouses:
        return "No SQL warehouse is available in this workspace."
    running = [wh for wh in warehouses if wh.state and wh.state.value == "RUNNING"]
    warehouse_id = (running[0] if running else warehouses[0]).id'''


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
    if not rows and not failed:
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
    {_WAREHOUSE_PICK}
    resp = w.statement_execution.execute_statement(
        statement={sql!r},
        warehouse_id=warehouse_id,
        parameters=[{params_repr}],
        wait_timeout="30s",
    )
    failed = bool(resp.status and resp.status.state.value == "FAILED")
    rows = resp.result.data_array if (resp.result and not failed) else []
{fallback_block}
    if failed and not rows:
        # A real query error (bad SQL, permission denial not caught by the
        # OBO fallback, etc.) is NOT the same thing as "no data" — surfacing
        # the real message here instead of a generic empty-result string is
        # what lets a demo presenter (or the model itself) tell a broken
        # tool apart from a genuinely empty answer.
        message = resp.status.error.message if resp.status.error else "unknown error"
        return f"Query failed: {{message}}"
    if not rows:
        return {not_found_msg!r}
    return str(rows)
'''


def _validate_and_render_tool(
    spec: dict[str, Any],
    catalog: str,
    schema: str,
    known_tables_lower: set[str],
    known_columns_lower: set[str],
    seen_sql: set[str],
    warnings: list[str],
) -> Optional[dict[str, Any]]:
    """One tool spec's worth of validation against the real schema, plus the
    two hard safety nets (dedup, LIMIT cap) — split out from
    build_agent_plan so that function reads as an overview, not a wall of
    per-tool checks. Returns None (with a warning appended) for anything
    that doesn't validate, rather than raising — one bad tool in a batch
    shouldn't sink an otherwise-good plan."""
    name = spec.get("name")
    table = spec.get("table")
    sql = spec.get("sql")
    args = spec.get("args") or []
    if not name or not table or not sql:
        warnings.append(f"Skipped a malformed tool spec: {spec}")
        return None
    if table.lower() not in known_tables_lower:
        warnings.append(f"Tool '{name}' references table '{table}', which isn't in {catalog}.{schema} — skipped.")
        return None

    # Hard safety net, not just a prompt instruction: hit this for real
    # generating an employee/project/lead agent, where the model proposed
    # "list_employees_and_projects" (LIMIT 200) and
    # "limit_employees_and_projects" (LIMIT 50) — the identical query twice
    # under two names because it read two verbs in the task text as two
    # separate operations. Normalize away the LIMIT clause and
    # whitespace/case before comparing, so this class of near-duplicate
    # can't slip through even when the prompt instruction alone doesn't
    # stop it.
    normalized = re.sub(r"\blimit\s+\d+\b", "", sql, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    if normalized in seen_sql:
        warnings.append(f"Tool '{name}' runs the same query as an earlier tool (just a different row limit) — skipped as a duplicate.")
        return None
    seen_sql.add(normalized)

    # Args and :params in the SQL must match in BOTH directions. An arg
    # declared but never used is just noise; a :param used in the SQL but
    # never declared as an arg silently produces an unbound parameter at
    # execution time — self-heal by recomputing "args" from what the SQL
    # actually references, which is authoritative.
    used_params = sorted(set(re.findall(r":(\w+)", sql)))
    declared = set(args)
    if declared != set(used_params):
        missing = [p for p in used_params if p not in declared]
        unused = [a for a in args if a not in used_params]
        if missing:
            warnings.append(f"Tool '{name}': {':' + ', :'.join(missing)} used in SQL but not declared — added automatically.")
        if unused:
            warnings.append(f"Tool '{name}': arg(s) {', '.join(unused)} declared but never used in the SQL — dropped.")
        spec["args"] = used_params

    fallback_column = spec.get("fallback_column")
    if fallback_column and fallback_column.lower() not in known_columns_lower:
        warnings.append(f"Tool '{name}': fallback_column '{fallback_column}' doesn't match any known column — dropping the fallback for this tool.")
        spec["fallback_column"] = None

    # Hard safety net, not just a prompt instruction: an unbounded join
    # (e.g. one row per ticket rather than per employee) can return a
    # result too large for the model's own context window to accept back —
    # hit this for real testing an employee/project/lead query that
    # returned 233k tokens against a 131k-token model limit. Don't rely on
    # the model remembering to add LIMIT; cap it here too.
    if not re.search(r"\blimit\s+\d+\b", sql, re.IGNORECASE):
        spec["sql"] = sql.rstrip().rstrip(";") + " LIMIT 200"
        warnings.append(f"Tool '{name}': no LIMIT in the generated SQL — capped at 200 rows to avoid an oversized result.")

    code = _render_tool_code(spec, catalog, schema)
    return {"name": name, "description": spec.get("description", ""), "code": code}


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
    # Unity Catalog identifiers are case-insensitive, so validation compares
    # lower-cased — an exact-case-only check would false-positive "unknown
    # table" for a technically valid query that happened to differ in case.
    known_tables_lower = {f"{catalog}.{schema}.{t['name']}".lower() for t in tables}
    known_columns_lower = {c["name"].lower() for t in tables for c in t["columns"]}

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
    depends_on_by_name: dict[str, Optional[str]] = {}
    seen_sql: set[str] = set()
    for spec in tool_specs:
        result = _validate_and_render_tool(spec, catalog, schema, known_tables_lower, known_columns_lower, seen_sql, warnings)
        if result is None:
            continue
        tools.append(result)
        depends_on_by_name[result["name"]] = spec.get("depends_on") or None

    if not tools:
        raise AgentBuilderError("Every proposed tool failed validation against the real schema — " + "; ".join(warnings))

    valid_names = {t["name"] for t in tools}
    for name, dep in list(depends_on_by_name.items()):
        if dep and dep not in valid_names:
            warnings.append(f"Tool '{name}' depends on '{dep}', which doesn't exist among the generated tools — ignoring the dependency.")
            depends_on_by_name[name] = None

    def _grid_pos(idx: int) -> dict[str, int]:
        col, row = idx % 3, idx // 3
        return {"x": 20 + col * 190, "y": 20 + row * 110}

    if any(depends_on_by_name.values()):
        graph = _build_chain_graph(tools, depends_on_by_name, warnings, _grid_pos)
    else:
        graph = _build_star_graph(tools, _grid_pos)

    return {"tools": tools, "graph": graph, "warnings": warnings}


def _build_star_graph(tools: list[dict[str, Any]], grid_pos) -> dict[str, Any]:
    """One shared Think node bound to every independent tool, each tool
    looping back to Think — the model picks whichever tool(s) a given
    question needs, possibly more than one at once (LangGraph fans that out
    as real parallel branches)."""
    think_id = "node-think-1"
    nodes = [{"id": think_id, "type": "think", **grid_pos(0)}]
    edges = []
    for i, tool in enumerate(tools, start=1):
        tool_node_id = f"node-tool-{i}"
        nodes.append({"id": tool_node_id, "type": "tool", "tool_name": tool["name"], **grid_pos(i)})
        edges.append({"source": think_id, "target": tool_node_id})
        edges.append({"source": tool_node_id, "target": think_id})
    return {"entry": think_id, "nodes": nodes, "edges": edges}


def _build_chain_graph(
    tools: list[dict[str, Any]],
    depends_on_by_name: dict[str, Optional[str]],
    warnings: list[str],
    grid_pos,
) -> dict[str, Any]:
    """Sequential think -> tool -> think -> tool -> ... -> think, in
    dependency order, ending on a Think node with no outgoing edges so the
    model gets one final turn to synthesize a combined answer from every
    tool's real result — exactly the shape of this app's own hand-built
    "Project Assistant" demo agent (look up the lead, THEN use that real
    name — which only exists once the first tool has actually run — to
    look up their workload). A graph can't hardcode that argument; a Think
    step reading the prior result and deciding it is the only way."""
    by_name = {t["name"]: t for t in tools}
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(tool: dict[str, Any]) -> None:
        name = tool["name"]
        if name in visited:
            return
        if name in in_progress:
            warnings.append(f"Tool '{name}' is part of a circular dependency chain — ignoring its dependency.")
            visited.add(name)
            ordered.append(tool)
            return
        in_progress.add(name)
        dep_name = depends_on_by_name.get(name)
        if dep_name and dep_name in by_name:
            visit(by_name[dep_name])
        in_progress.discard(name)
        visited.add(name)
        ordered.append(tool)

    for t in tools:
        visit(t)

    nodes = []
    edges = []
    idx = 0
    prev_think = "node-think-1"
    nodes.append({"id": prev_think, "type": "think", **grid_pos(idx)})
    idx += 1
    for i, tool in enumerate(ordered, start=1):
        tool_node_id = f"node-tool-{i}"
        nodes.append({"id": tool_node_id, "type": "tool", "tool_name": tool["name"], **grid_pos(idx)})
        idx += 1
        edges.append({"source": prev_think, "target": tool_node_id})
        next_think = f"node-think-{i + 1}"
        nodes.append({"id": next_think, "type": "think", **grid_pos(idx)})
        idx += 1
        edges.append({"source": tool_node_id, "target": next_think})
        prev_think = next_think

    return {"entry": "node-think-1", "nodes": nodes, "edges": edges}
