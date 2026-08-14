"""Agent Builder — generates an agent's tools and flow graph from a plain-
English task description plus the real, live schema of a connected source.

This is deliberately NOT "the LLM writes arbitrary Python and we exec it
blind." The model only ever proposes a small, structured plan (which table,
which base query, which optional filters, one optional fallback column,
one optional dependency on another tool) as JSON; every table/column name
it names is checked against a real schema introspection before anything
runs, and the actual Python tool code — including the WHERE-clause
construction for filters — is rendered from ONE fixed, hand-written
template. The model never writes SQL parameter binding or a raw ":param"
string itself; it only says which column an argument filters on, and the
template builds a correct, safely-conditional WHERE clause from that. That
matters for a real reason found testing this against a live agent: asking
a static ":lead_name IS NULL OR lead_name = :lead_name" pattern to skip
filtering when unset doesn't work, because an omitted argument still binds
as an empty string, not SQL NULL — so "list everyone" would have silently
broken into "list no one." Building the WHERE clause in Python instead of
asking the model to hand-write correct optional-parameter SQL avoids that
whole class of mistake.

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

from databricks.sdk import WorkspaceClient

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
- Each tool's "base_sql" is exactly one SELECT statement (with any JOINs it needs) and must NOT include a WHERE clause, a LIMIT, or any ":param" placeholder — those are added automatically. Put every table this tool touches directly in the FROM/JOIN of base_sql; "table" is just the primary one, used for validation.
- Prefer doing joins, counts, and aggregations inside base_sql itself (GROUP BY, COUNT, JOIN) rather than returning raw rows for the caller to manually tally — SQL is reliable at counting, free-text reasoning over raw rows is not.
- If a join can multiply rows (e.g. joining to a table with many rows per parent, like tickets per employee), use SELECT DISTINCT or aggregate instead of returning every joined row — otherwise the result can be enormous even though the answer is small.
- This agent will be asked many different follow-up questions later, not just the exact task wording above. For EVERY column in base_sql's own SELECT list that identifies a specific named thing (a person's name, a project name, a status, an ID, an email) — not just ones the task literally mentions filtering by — add a matching entry to "filters": {{"arg": "lead_name", "column": "p.lead_name", "required": false}} ("column" may be qualified with the table alias used in base_sql). Go through the SELECT list column by column and ask "would someone plausibly want to filter by this specific value later?" — if yes, it needs a filter, even if the task text never says the word "filter" or mentions that column by name. A tool with no way to filter on a column it selects forces the model to eyeball-search raw text for one matching row later, which is unreliable and has produced wrong answers. Set "required": true only when the tool is meaningless without that value; leave it false when the tool should also work as a "list everything" call.
- If the task implies "if it's not in the obvious table, check every table" for a concept (e.g. leads, customers), set "fallback_column" to the single column name that identifies that concept elsewhere (e.g. "lead_name"). Leave it null if the task doesn't need that.
- When the task asks about people who generally work on / are assigned to / participate in something (as opposed to a specific named role like "the lead" or "the manager" of it), do NOT join through a foreign key column whose name denotes that specific role (e.g. projects.lead_id, projects.manager_id, projects.owner_id, projects.created_by). A column like "lead_id" answers "who holds the lead role for this row," not "who works on this row" — joining a person table to it will silently return ONLY people who hold that role, which is a completely different (and wrong) answer to "list the employees on this project." Instead, look for an activity/assignment/membership-shaped table (tickets, time entries, project members, etc.) that records general participation, and join through that.
- When more than one table could plausibly connect two entities, prefer whichever one connects them in a single join hop over one needing two or more hops through an intermediate table — every extra hop is another foreign key you're guessing the meaning of from its name alone, and a wrong guess produces a query that runs successfully but silently returns nothing. If a table has both a person's ID (e.g. assignee_id) and the other entity's ID (e.g. project_id) directly on the same row, that is almost always the safer, more direct join.
- If a tool's argument value can only be known from ANOTHER tool's result (e.g. "find the lead, then look up that lead's workload"), set "depends_on" to that other tool's exact name. Leave it null if the tool can run on its own with arguments the user directly provides. Don't invent a dependency that isn't really there — most tools should have depends_on: null.
- Never propose two tools whose base_sql and filters are essentially the same thing — every tool must answer a genuinely different question.
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
      "base_sql": "SELECT DISTINCT col1, col2 FROM {catalog}.{schema}.table_name t JOIN {catalog}.{schema}.other_table o ON t.id = o.fk",
      "filters": [
        {{"arg": "some_value", "column": "t.some_column", "required": false}}
      ],
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
    base_sql: str = spec["base_sql"]
    table: str = spec["table"]
    filters: list[dict[str, Any]] = spec.get("filters") or []
    fallback_column: Optional[str] = spec.get("fallback_column") or None

    required_args = [f["arg"] for f in filters if f.get("required")]
    optional_args = [f["arg"] for f in filters if not f.get("required")]
    # "limit" is universal, not something the model proposes per tool — a
    # caller asking "top 10" vs "list everyone" needs to control row count
    # at run time, not just get whatever cap the generator baked in. Capped
    # at 200 regardless of what's requested — the same ceiling that fixed
    # the earlier context-overflow bug, so a bad ask here can't reintroduce it.
    arg_sig = ", ".join([f"{a}: str" for a in required_args] + [f"{a}: str = ''" for a in optional_args] + ["limit: str = '50'"])

    # Every filter is applied "if truthy" regardless of required/optional —
    # required only changes whether the function signature demands a value;
    # this keeps the query-building logic uniform instead of two code paths.
    filter_lines = "\n    ".join(
        f'''if {f["arg"]}:
        conditions.append({f["column"] + " = :" + f["arg"]!r})
        params.append(StatementParameterListItem(name={f["arg"]!r}, value=str({f["arg"]})))'''
        for f in filters
    )

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
            fallback_sql = ({base_sql!r}).replace({table!r}, candidate_table)
            if conditions:
                fallback_sql += " WHERE " + " AND ".join(conditions)
            fallback_sql += f" LIMIT {{limit_n}}"
            fb_resp = w.statement_execution.execute_statement(
                statement=fallback_sql,
                warehouse_id=warehouse_id,
                parameters=params,
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
    try:
        limit_n = min(max(int(limit), 1), 200) if limit else 50
    except (TypeError, ValueError):
        limit_n = 50
    conditions = []
    params = []
    {filter_lines if filter_lines else "pass"}
    sql = {base_sql!r}
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" LIMIT {{limit_n}}"
    resp = w.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=warehouse_id,
        parameters=params,
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


def _test_run_tool(name: str, code: str, has_required_filter: bool, warnings: list[str]) -> None:
    """Actually executes a freshly generated tool once, unfiltered, using
    the app's own identity — not a hypothetical check. Schema/column
    validation can only catch a table or column that doesn't exist; it
    cannot catch a join that's syntactically fine but connects the wrong
    rows (e.g. joining through a foreign key that references a different
    table than the one aliased in the query). That exact failure mode
    showed up for real: a generated tool joined `time_entries` to
    `projects` directly, when `time_entries.project_activity_id` actually
    points at `project_activities`, not `projects` — the query ran
    successfully and silently returned zero rows every time. An empirical
    test call, run once during generation, catches that a static check
    never could. Skipped for tools with a required filter, since calling
    those with no value legitimately returns nothing and would be a false
    alarm, not a real signal."""
    if has_required_filter:
        return
    try:
        namespace: dict[str, Any] = {"w": WorkspaceClient()}
        exec(code, namespace)  # noqa: S102 - same Action Pack execution model as everywhere else
        result = str(namespace[name]())
    except Exception as exc:  # noqa: BLE001 - a test-run failure is itself the finding
        warnings.append(f"Tool '{name}' was test-run and raised an error ({exc}) — review the generated SQL before relying on it.")
        return
    if result.startswith("No matching data found") or result.startswith("Query failed"):
        warnings.append(f"Tool '{name}' was test-run and returned no data ({result}) — the generated join may not correctly connect these tables. Review it (the 'View / edit generated code' button) before relying on it.")


def _validate_and_render_tool(
    spec: dict[str, Any],
    catalog: str,
    schema: str,
    known_tables_lower: set[str],
    known_columns_lower: set[str],
    seen: set[tuple],
    warnings: list[str],
) -> Optional[dict[str, Any]]:
    """One tool spec's worth of validation against the real schema — split
    out from build_agent_plan so that function reads as an overview, not a
    wall of per-tool checks. Returns None (with a warning appended) for
    anything that doesn't validate, rather than raising — one bad tool in a
    batch shouldn't sink an otherwise-good plan."""
    name = spec.get("name")
    table = spec.get("table")
    base_sql = spec.get("base_sql")
    filters = spec.get("filters") or []
    if not name or not table or not base_sql:
        warnings.append(f"Skipped a malformed tool spec: {spec}")
        return None
    if table.lower() not in known_tables_lower:
        warnings.append(f"Tool '{name}' references table '{table}', which isn't in {catalog}.{schema} — skipped.")
        return None
    if re.search(r"\bwhere\b", base_sql, re.IGNORECASE) or ":" in base_sql:
        warnings.append(f"Tool '{name}': base_sql contained a WHERE clause or a :param placeholder — stripped, filtering happens via 'filters' only.")
        base_sql = re.split(r"\bwhere\b", base_sql, maxsplit=1, flags=re.IGNORECASE)[0].rstrip()
        spec["base_sql"] = base_sql

    valid_filters = []
    for f in filters:
        arg, column = f.get("arg"), f.get("column")
        if not arg or not column:
            warnings.append(f"Tool '{name}': skipped a malformed filter {f}.")
            continue
        bare_column = column.split(".")[-1].lower()
        if bare_column not in known_columns_lower:
            warnings.append(f"Tool '{name}': filter column '{column}' doesn't match any known column — dropped.")
            continue
        valid_filters.append(f)
    spec["filters"] = valid_filters

    # Hard safety net, not just a prompt instruction: hit this for real
    # generating an employee/project/lead agent, where the model proposed
    # "list_employees_and_projects" and "limit_employees_and_projects" —
    # the identical query twice under two names, just a different row cap.
    # Now that LIMIT is never model-controlled, a duplicate can only be a
    # genuinely identical base_sql + filter set.
    dedup_key = (
        re.sub(r"\s+", " ", base_sql).strip().lower(),
        tuple(sorted(f["arg"] for f in valid_filters)),
    )
    if dedup_key in seen:
        warnings.append(f"Tool '{name}' runs the same query and filters as an earlier tool — skipped as a duplicate.")
        return None
    seen.add(dedup_key)

    fallback_column = spec.get("fallback_column")
    if fallback_column and fallback_column.lower() not in known_columns_lower:
        warnings.append(f"Tool '{name}': fallback_column '{fallback_column}' doesn't match any known column — dropping the fallback for this tool.")
        spec["fallback_column"] = None

    code = _render_tool_code(spec, catalog, schema)
    has_required_filter = any(f.get("required") for f in valid_filters)
    _test_run_tool(name, code, has_required_filter, warnings)
    description = spec.get("description", "") + " Accepts an optional 'limit' argument (a number as a string, default 50, max 200) to control how many rows come back — e.g. pass limit='10' for a top-10 request, or leave it default for a general listing."
    return {"name": name, "description": description, "code": code}


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
    seen: set[tuple] = set()
    for spec in tool_specs:
        result = _validate_and_render_tool(spec, catalog, schema, known_tables_lower, known_columns_lower, seen, warnings)
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
