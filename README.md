# poc — Client Deploy Pipeline

Runs on GitHub Actions, not inside the Control Panel Databricks App itself —
Databricks Apps enforce a 120-second request timeout and a no-root container,
neither of which survive a real 2-4 minute build+deploy. See `architecture.drawio`
in the main project for the full picture.

## Structure

- `apps/client-app/` — the real Client App deployed to each client workspace
  (architecture.drawio page 2: Connectors, LLM Providers, Agents, Access
  Control). Its Agents module is fully real — a generic LangGraph/LangChain
  interpreter that calls this workspace's own Foundation Model serving
  endpoints; Connectors/LLM Providers/Access Control are honest "not built
  yet" placeholders in the UI, not fake data. Its `databricks.yml` has no
  hardcoded workspace host on purpose: the target workspace comes entirely
  from whichever GitHub Environment's secrets are active for a given run.
- `.github/workflows/deploy-client.yml` — the actual deploy job. Triggered via
  `workflow_dispatch` with a `client_environment` input naming which GitHub
  Environment (and therefore which client's credentials) to use.

## Onboarding a new client (no Databricks terminal step required)

1. Client's own admin creates a service principal in their Databricks account,
   scoped to `CAN_MANAGE` on Apps only (see `connection-flow.md` in the main repo),
   and hands over the workspace host + client ID + client secret once.
2. In this repo: **Settings → Environments → New environment**, named after the
   client (e.g. `client-acme`).
3. Add three **environment secrets**: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`,
   `DATABRICKS_CLIENT_SECRET`.
4. Grant that service principal `CAN_MANAGE` on the deployed app itself once it
   exists (`databricks apps update-permissions <app> --json '...'`) — a fresh
   service principal can upload code but can't start/restart the app until this
   is granted. This bit hasn't been made turnkey yet; see Known Gaps below.
5. Deploy via the Control Panel UI, or manually: `gh workflow run deploy-client.yml -f client_environment=client-acme`.

## Known gaps (intentionally not glossed over)

- Step 4 above is still a manual CLI step done once per client, against that
  client's own workspace using the credential they gave us. It has not yet
  been folded into the onboarding UI.
- No automated rollback in this pipeline yet — redeploying a prior tag would
  mean checking out that tag before running the workflow, not yet wired as a
  button anywhere.
- Status feedback to the Control Panel is via polling GitHub's Actions API,
  not a push notification — `workflow_dispatch` doesn't return a run ID, so the
  Control Panel has to look up the most recent run for that workflow shortly
  after dispatching it.
