# poc — Client Deploy Pipeline

Runs on GitHub Actions, not inside the Control Panel Databricks App itself —
Databricks Apps enforce a 120-second request timeout and a no-root container,
neither of which survive a real 2-4 minute build+deploy. See `architecture.drawio`
in the main project for the full picture.

**Status: proven end to end.** This pipeline has been run for real against a
separate client workspace — dispatched from GitHub Actions, deployed as a real
Databricks App, and the deployed Agents module verified to actually call that
workspace's own Foundation Model serving endpoint.

## Structure

- `apps/client-app/` — the real Client App deployed to each client workspace
  (architecture.drawio page 2: Connectors, LLM Providers, Agents, Access
  Control).
  - **Agents** is fully real: a generic LangGraph interpreter reads each
    agent's own config (prompt, model, tools) and runs it, calling that
    workspace's own Foundation Model serving endpoints via a direct,
    OpenAI-compatible HTTP call (`backend/dbx_chat.py`) — no dummy model.
    Tools are real Python, executed for real.
  - **Connectors**, **LLM Providers** (as a management surface), and
    **Access Control** are honest "not built yet" placeholders in the UI —
    no invented sample data standing in for them.
  - Its `databricks.yml` has no hardcoded workspace host on purpose: the
    target workspace comes entirely from whichever GitHub Environment's
    secrets are active for a given run, so one bundle deploys the same app
    into any number of client workspaces.
- `.github/workflows/deploy-client.yml` — the actual deploy job. Triggered via
  `workflow_dispatch` with a `client_environment` input naming which GitHub
  Environment (and therefore which client's credentials) to use. Pure Python
  app, no JS build step.

## Onboarding a new client (no Databricks terminal step required)

1. Client's own admin creates a service principal in their Databricks account,
   scoped to `CAN_MANAGE` on Apps only (see `connection-flow.md` in the main
   repo), and hands over the workspace host + client ID + client secret once.
2. In this repo: **Settings → Environments → New environment**, named after the
   client (e.g. `client-acme`).
3. Add three **environment secrets**: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`,
   `DATABRICKS_CLIENT_SECRET`.
4. Deploy via the Control Panel UI, or manually:
   `gh workflow run deploy-client.yml -f client_environment=client-acme`.

That's it — as long as the service principal is the one deploying the app for
the first time (true for every real new client), it's automatically its own
app's creator and gets full manage/start permissions with no extra grant step.
(An earlier version of this doc listed a manual `CAN_MANAGE` grant as a
required step. That was an artifact of testing with two different identities
deploying the same app name — confirmed by re-testing with the service
principal as the sole creator, which needed no extra step at all.)

## Known gaps (intentionally not glossed over)

- No automated rollback in this pipeline yet — redeploying a prior tag would
  mean checking out that tag before running the workflow, not yet wired as a
  button anywhere.
- Status feedback to the Control Panel is via polling GitHub's Actions API,
  not a push notification — `workflow_dispatch` doesn't return a run ID, so the
  Control Panel has to look up the most recent run for that workflow shortly
  after dispatching it.
- Foundation Model calls in the deployed app run as the app's own service
  principal, not the logged-in employee — real, but not yet OBO-scoped per
  user (see the commented-out `user_api_scopes` in `apps/client-app/databricks.yml`).
