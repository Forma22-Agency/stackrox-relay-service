# StackRox Relay Service

This service is used to relay events from StackRox to GitHub.

## Configuration

The service is configured using environment variables.

Core:

- `GH_OWNER`: GitHub org or user that owns the repository.
- `GH_REPO`: Target repository name.
- `GITHUB_API_VERSION` (optional): GitHub API version to use. Default: `2022-11-28`.
- `EVENT_TYPE` (optional): Event name for `repository_dispatch`. Default: `stackrox_copa`.
- `ACS_WEBHOOK_SECRET` (optional but recommended): Shared secret for inbound webhook; must be sent as `X-ACS-TOKEN` header.

Authentication options (choose one):

- `GH_TOKEN`: A GitHub token (PAT or GitHub Actions-provided) used directly for the API call.
  - Tip: in GitHub Actions, `${{ secrets.GITHUB_TOKEN }}` works if the repo permissions allow repository_dispatch.

Or configure a GitHub App (recommended for org-wide, auditable access):

- `GITHUB_APP_ID`: Your GitHub App ID.
- `GITHUB_APP_PRIVATE_KEY`: PEM private key (with literal `\n` newlines) or
- `GITHUB_APP_PRIVATE_KEY_BASE64`: Base64-encoded PEM private key.
- `GITHUB_APP_INSTALLATION_ID` (optional): Installation ID. If omitted, the service auto-discovers the installation for `GH_OWNER/GH_REPO`.

Health:

- `GET /healthz` returns `ok` if `GH_OWNER` and `GH_REPO` are set and either `GH_TOKEN` or a valid GitHub App configuration is present; otherwise `degraded`.

## StackRox Generic Webhook setup

In StackRox → Platform Configuration → Integrations → Notifiers → Generic Webhook:

- Endpoint: `https://relay.example.com/webhook` (or the in-cluster service address if StackRox Central runs in the same cluster/VPC)
- Headers: `X-ACS-TOKEN: <value of ACS_WEBHOOK_SECRET>`
- Extra fields: not required — the relay adds `event_type` and `client_payload` automatically.