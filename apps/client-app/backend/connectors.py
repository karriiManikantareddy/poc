"""Connectors module — architecture.drawio page 2 ("Connectors Module", Tech:
Lakeflow Connect). Real Unity Catalog Connection + Lakeflow ingestion Pipeline
objects via databricks-sdk — no sync engine of our own to build or maintain.

Two kinds of source, confirmed against current Databricks docs (not assumed):
- Key/host/password sources (databases) — the whole thing (Connection +
  Pipeline) can be created here, no browser hop.
- OAuth SaaS sources (Salesforce, HubSpot, Workday, ServiceNow, Slack,
  Zendesk, Jira, etc.) — Databricks does not expose an API to complete the
  OAuth consent handshake; that one step has to happen in the workspace's
  own Catalog Explorer. Everything else (which tables, destination, schedule)
  still happens here, once the Connection exists.

Databricks does not publish a discoverable options schema per connection
type, so this only offers a real, verified inline form for the well-documented
key/host/password types. Every other real type from the SDK is still shown
live (not hardcoded) but routed to the "finish in Databricks" flow rather
than a guessed form.
"""
from typing import Any, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import DatabricksError
from databricks.sdk.service.catalog import ConnectionType
from databricks.sdk.service.pipelines import (
    CronTrigger,
    IngestionConfig,
    IngestionPipelineDefinition,
    ManualTrigger,
    PipelineTrigger,
    SchemaSpec,
    TableSpec,
)

# Connection types with a well-documented, verified options schema — plain
# host/port/user/password, no OAuth consent screen needed. Everything else
# in ConnectionType is real and shown to the user, just routed to the
# "finish in Databricks" flow instead of a guessed form.
HOST_BASED_TYPES: dict[str, list[str]] = {
    "POSTGRESQL": ["host", "port", "user", "password"],
    "MYSQL": ["host", "port", "user", "password"],
    "SQLSERVER": ["host", "port", "user", "password", "database"],
}


class ConnectorError(Exception):
    """A real Databricks-side failure (permissions, conflict, validation) —
    surfaced verbatim, never swallowed or faked into a success."""


def _client() -> WorkspaceClient:
    return WorkspaceClient()


def list_connection_types() -> list[dict[str, Any]]:
    """Every real connection type this SDK/workspace knows about, not a
    curated or invented subset. `form` tells the UI whether we can render
    a real inline form (host-based) or must hand off to Databricks (OAuth
    and everything else undocumented)."""
    return [
        {
            "type": t.value,
            "form": "host" if t.value in HOST_BASED_TYPES else "external",
            "fields": HOST_BASED_TYPES.get(t.value, []),
        }
        for t in ConnectionType
        if t.value != "UNKNOWN_CONNECTION_TYPE"
    ]


def list_connections() -> list[dict[str, Any]]:
    w = _client()
    try:
        return [
            {
                "name": c.name,
                "connection_type": c.connection_type.value if c.connection_type else None,
                "owner": c.owner,
                "comment": c.comment,
                "created_at": c.created_at,
            }
            for c in w.connections.list()
        ]
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc


def create_host_connection(name: str, connection_type: str, fields: dict[str, str], comment: str = "") -> dict[str, Any]:
    if connection_type not in HOST_BASED_TYPES:
        raise ConnectorError(
            f"'{connection_type}' has no verified options schema in this app — create it in Databricks "
            "(Catalog > gear icon > Connections > Create connection), then refresh the list here."
        )
    required = HOST_BASED_TYPES[connection_type]
    missing = [f for f in required if not fields.get(f)]
    if missing:
        raise ConnectorError(f"Missing required field(s) for {connection_type}: {', '.join(missing)}")

    # Verified against a real workspace: passing password/user directly in
    # `options` works and Databricks redacts them automatically on read-back
    # (confirmed via connections.get() — only non-sensitive keys echo back).
    # The alternative `secrets` param expects a UC Secret reference and is
    # gated behind a preview flag (`enableUcSecretsInConnections`) that's
    # off by default, so it isn't a reliable path across client workspaces.
    w = _client()
    try:
        info = w.connections.create(
            name=name,
            connection_type=ConnectionType(connection_type),
            options=fields,
            comment=comment or None,
        )
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc
    return {"name": info.name, "connection_type": info.connection_type.value if info.connection_type else None}


def delete_connection(name: str) -> None:
    w = _client()
    try:
        w.connections.delete(name)
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc


def list_catalogs() -> list[str]:
    w = _client()
    try:
        return [c.name for c in w.catalogs.list()]
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc


def list_schemas(catalog: str) -> list[str]:
    w = _client()
    try:
        return [s.name for s in w.schemas.list(catalog_name=catalog)]
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc


def create_ingestion_pipeline(
    name: str,
    connection_name: str,
    destination_catalog: str,
    destination_schema: str,
    sync_mode: str,  # "schema" | "tables"
    source_schema: str,
    tables: Optional[list[str]],
    schedule_cron: Optional[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    if sync_mode == "schema":
        objects = [IngestionConfig(schema=SchemaSpec(
            source_schema=source_schema,
            destination_catalog=destination_catalog,
            destination_schema=destination_schema,
        ))]
    elif sync_mode == "tables":
        if not tables:
            raise ConnectorError("Pick at least one table, or choose 'entire schema' instead.")
        objects = [
            IngestionConfig(table=TableSpec(
                source_table=t,
                source_schema=source_schema,
                destination_catalog=destination_catalog,
                destination_schema=destination_schema,
            ))
            for t in tables
        ]
    else:
        raise ConnectorError(f"Unknown sync mode '{sync_mode}'")

    trigger = (
        PipelineTrigger(cron=CronTrigger(quartz_cron_schedule=schedule_cron, timezone_id="UTC"))
        if schedule_cron
        else PipelineTrigger(manual=ManualTrigger())
    )

    w = _client()
    try:
        response = w.pipelines.create(
            name=name,
            ingestion_definition=IngestionPipelineDefinition(
                connection_name=connection_name,
                objects=objects,
            ),
            catalog=destination_catalog,
            schema=destination_schema,
            trigger=trigger,
            continuous=False,
            serverless=True,
            dry_run=dry_run,
        )
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc
    return {"pipeline_id": response.pipeline_id}


def get_pipeline_status(pipeline_id: str) -> dict[str, Any]:
    w = _client()
    try:
        info = w.pipelines.get(pipeline_id)
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc
    latest = info.latest_updates[0] if info.latest_updates else None
    return {
        "pipeline_id": pipeline_id,
        "name": info.name,
        "state": info.state.value if info.state else None,
        "last_update_state": latest.state.value if latest and latest.state else None,
        "cause": info.cause,
    }


def start_pipeline_update(pipeline_id: str) -> dict[str, Any]:
    w = _client()
    try:
        response = w.pipelines.start_update(pipeline_id)
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc
    return {"update_id": response.update_id}


def delete_pipeline(pipeline_id: str) -> None:
    w = _client()
    try:
        w.pipelines.delete(pipeline_id)
    except DatabricksError as exc:
        raise ConnectorError(str(exc)) from exc
