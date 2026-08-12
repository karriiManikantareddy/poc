"""Real caller identity + RBAC for the Agents module — architecture.drawio
page 2 ("RBAC / Governance Module": roles mapped to the client's own real
Databricks Groups, not an app-invented role system).

Trust in X-Forwarded-Email / X-Forwarded-Access-Token is not assumed — it
was verified empirically against a live deployment: sending a forged
X-Forwarded-Email from outside the app had zero effect, because Databricks
Apps' own reverse proxy sets these headers itself and overwrites anything
a client sends. They're safe to authorize against.

Local dev (no reverse proxy in front, so no forwarded headers at all)
deliberately falls back to full access rather than failing closed. This
is safe specifically because that branch is unreachable in the real
deployed multi-tenant path — Databricks' proxy always injects these
headers for every request that actually reaches a deployed app, verified
the same way. It only ever fires for a developer running their own copy
locally, who already has the source code and their own CLI credentials.
"""
import os
from typing import NamedTuple, Optional

from databricks.sdk import WorkspaceClient
from fastapi import Request

ADMIN_GROUP = os.environ.get("AGENT_ADMIN_GROUP", "admins")


class Caller(NamedTuple):
    email: Optional[str]
    access_token: Optional[str]

    @property
    def known(self) -> bool:
        return self.email is not None


def get_caller(request: Request) -> Caller:
    return Caller(
        email=request.headers.get("x-forwarded-email"),
        access_token=request.headers.get("x-forwarded-access-token"),
    )


def get_caller_groups(email: str) -> list[str]:
    """Real SCIM lookup, live on every call — no caching, so a group
    change made directly in Databricks takes effect on the very next
    request, same live-fetch principle as the rest of this app. Fails
    safe to no groups (not full access) if the lookup itself errors,
    since that's the conservative direction for an access-control check."""
    try:
        w = WorkspaceClient()
        users = list(w.users.list(filter=f'userName eq "{email}"', attributes="groups"))
    except Exception:  # noqa: BLE001 - fail safe, don't crash the request over a transient SCIM error
        return []
    if not users:
        return []
    return [g.display for g in (users[0].groups or []) if g.display]


def resolve_access(request: Request) -> tuple[list[str], bool]:
    """Returns (groups, is_admin) for the calling request."""
    caller = get_caller(request)
    if not caller.known:
        return [], True  # local dev only — see module docstring
    groups = get_caller_groups(caller.email)
    return groups, ADMIN_GROUP in groups


def can_see(agent: dict, groups: list[str], admin: bool) -> bool:
    """Empty visible_to_groups = visible to everyone — preserves the
    behavior of every agent created before this feature existed."""
    visible_to = agent.get("visible_to_groups") or []
    if not visible_to:
        return True
    return admin or any(g in groups for g in visible_to)
