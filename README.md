# StackRox Relay Service

This service is used to relay events from StackRox to GitHub.

## Configuration

The service is configured using environment variables.

Core:

- `GH_OWNER`: GitHub org or user that owns the repository.
- `GITHUB_API_VERSION` (optional): GitHub API version to use. Default: `2022-11-28`.
- `EVENT_TYPE` (optional): Event name for `repository_dispatch`. Default: `stackrox_copa`.
- `ACS_WEBHOOK_SECRET` (optional but recommended): Shared secret for inbound webhook; must be sent as `X-ACS-TOKEN` header.

Multi-repo (by repository topics):

- `GH_ALLOWED_TOPICS` (optional): Comma-separated list of topics. If set, the relay will only dispatch to repositories that have these topics. If empty, any repository is allowed (subject to credentials).
- `GH_ALLOWED_TOPICS_MODE` (optional): `any` (default) — at least one topic must be present; `all` — all topics must be present.

Repository selection logic:

The relay triggers a workflow in a single repository derived from the container image name.

- We compute the image basename: the last path segment of the image reference with tag/digest stripped.
  - Examples:
    - `registry.example.com/team/mysuperapp:1.2.3` → basename = `mysuperapp`
    - `ghcr.io/org/service-backend@sha256:...` → basename = `service-backend`
- Target repository becomes `GH_OWNER/<basename>`.
- `GH_OWNER` must be set. `GH_REPO` is not used in this mode.
- If basename cannot be derived or `GH_OWNER` is missing, the request is rejected with 400.

Authentication options (choose one):

- `GH_TOKEN`: A GitHub token (PAT or GitHub Actions-provided) used directly for the API call.
  - Tip: in GitHub Actions, `${{ secrets.GITHUB_TOKEN }}` works if the repo permissions allow repository_dispatch.

Or configure a GitHub App (recommended for org-wide, auditable access):

- `GITHUB_APP_ID`: Your GitHub App ID.
- `GITHUB_APP_PRIVATE_KEY`: PEM private key (with literal `\n` newlines) or
- `GITHUB_APP_PRIVATE_KEY_BASE64`: Base64-encoded PEM private key.
- `GITHUB_APP_INSTALLATION_ID` (optional): Installation ID. If omitted, the service auto-discovers the installation for the target repository. Tokens are cached per installation.

Health:

- `GET /healthz` returns `ok` if either `GH_TOKEN` or a valid GitHub App configuration is present; otherwise `degraded`. In image-basename selection mode `GH_REPO` can be omitted.

Topics policy (optional):

- `GH_ALLOWED_TOPICS` — comma-separated list of GitHub topics. If empty, the check is disabled.
- `GH_ALLOWED_TOPICS_MODE` — `any` (default, at least one topic must match) or `all` (all topics must match).
- Before dispatching, the service reads the target repository topics via the GitHub API and applies the policy.
  - If the repository does not satisfy the policy, the service returns 403 and does not dispatch.
  - If the GitHub API call to read topics fails, the error is propagated (the request fails).

Responses and errors:

- 204 from GitHub → considered success; the service returns `{ ok: true, repository: "<owner>/<repo>" }`.
- 400 — could not determine the target repository (missing `GH_OWNER` or cannot extract image basename).
- 403 — repository does not satisfy the topics policy.
- 401/403/404 from GitHub Dispatch — returned as-is (e.g., the token/app lacks permissions for the repository).

## StackRox Generic Webhook setup

In StackRox → Platform Configuration → Integrations → Notifiers → Generic Webhook:

- Endpoint: `https://relay.example.com/webhook` (or the in-cluster service address if StackRox Central runs in the same cluster/VPC)
- Headers: `X-ACS-TOKEN: <value of ACS_WEBHOOK_SECRET>`
- Extra fields: not required — the relay adds `event_type` and `client_payload` automatically.