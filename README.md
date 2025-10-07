StackRox Relay Service — Helm chart and GitHub Pages publishing

Overview
- A lightweight HTTP relay that receives StackRox (ACS) Generic Webhook events and forwards them to GitHub via the `repository_dispatch` API.
- Main flow:
  1) StackRox sends POST to the relay
  2) The relay verifies a shared secret (optional but recommended)
  3) The relay authenticates to GitHub (PAT or GitHub App)
  4) The relay triggers `repository_dispatch` with `event_type` and `client_payload`

Endpoints
- POST `/webhook`: accepts StackRox webhook payloads, validates header `X-ACS-TOKEN` when configured
- GET `/healthz`: returns `ok` when configuration is valid, otherwise `degraded`

Configuration (env)
- `GH_OWNER`: GitHub org/user that owns the target repo
- `GH_REPO`: target repository name
- `GITHUB_API_VERSION` (optional): default `2022-11-28`
- `EVENT_TYPE` (optional): default `stackrox_copa`
- `ACS_WEBHOOK_SECRET` (optional but recommended): shared secret, expected in `X-ACS-TOKEN`

Authentication options (choose one)
- Personal Access Token (PAT) or GHA token: `GH_TOKEN`
- GitHub App (recommended for org-wide, auditable access):
  - `GITHUB_APP_ID`
  - Private key: one of `GITHUB_APP_PRIVATE_KEY` (literal with `\n`) or `GITHUB_APP_PRIVATE_KEY_BASE64`
  - Optional: `GITHUB_APP_INSTALLATION_ID` (auto-discovered when omitted)

Helm repository (GitHub Pages)
- Helm repo URL: `https://forma22-agency.github.io/stackrox-relay-service/`
- Branch: `gh-pages` (index at `index.yaml`)
- CI release: on push to `main` touching `helm-chart/**`, workflow `.github/workflows/release_helm_chart.yaml` uses `helm/chart-releaser-action@v1.7.0` to package charts and update `gh-pages`. Ensure `Settings → Pages` serves from the `gh-pages` branch.

Quick start (install)
```bash
helm repo add aws-cd https://forma22-agency.github.io/stackrox-relay-service
helm repo update
helm install stackrox-relay aws-cd/stackrox-relay-service \
  --version 0.0.1 \
  --namespace stackrox-relay --create-namespace
```

values.yaml (key parameters)
- `deployment.replicas`: number of pods (default 1)
- `deployment.image.repository`: container image repo (default `ghcr.io/Forma22-Agency/stackrox-relay-service`)
- `deployment.image.tag`: image tag/sha
- `deployment.resources`: CPU/memory requests/limits (default 100m/100Mi)
- `deployment.containerSecurityContext`: non-root, read-only FS, no privileges, drop ALL caps
- `deployment.readinessProbe` / `deployment.livenessProbe`: HTTP `/healthz` on port 8080
- `service`: type/port mapping (default `ClusterIP`, port 80 → 8080)
- `configmap`: plain-text app configuration
  - `GH_OWNER`, `GH_REPO`, `GITHUB_API_VERSION`, `EVENT_TYPE`, `ACS_WEBHOOK_SECRET`, `GH_TOKEN`
- `externalSecrets`: manage secrets via External Secrets Operator
  - `enabled`: `true`/`false`
  - `data.refreshInterval`: e.g. `1h`
  - `data.kind`: e.g. `ClusterSecretStore`
  - `data.name`: name of the SecretStore (e.g. `gcp-clustersecretstore`)
  - `data.creationPolicy`: e.g. `Owner`
  - `token.enabled`: set to `true` to populate `GH_TOKEN` from a provider secret
    - `parameters.name`: Kubernetes Secret name to create
    - `parameters.remoteSecretKey`: remote provider secret key
  - `app.enabled`: set to `true` to use a GitHub App
    - `parameters.githubAppID`, `parameters.githubAppInstallationID`, `parameters.remoteSecretKey`

Install examples
1) Plain config with PAT (DEV/testing)
```bash
helm install stackrox-relay stackrox-relay-service/stackrox-relay-service \
  --namespace stackrox-relay --create-namespace \
  --set configmap.GH_OWNER="<org-or-user>" \
  --set configmap.GH_REPO="<repo>" \
  --set configmap.ACS_WEBHOOK_SECRET="<random-secret>" \
  --set configmap.GH_TOKEN="<pat-token>"
```

2) External Secrets (recommended)
```bash
helm install stackrox-relay stackrox-relay-service/stackrox-relay-service \
  --namespace stackrox-relay --create-namespace \
  --set configmap.GH_OWNER="<org-or-user>" \
  --set configmap.GH_REPO="<repo>" \
  --set configmap.ACS_WEBHOOK_SECRET="<random-secret>" \
  --set externalSecrets.enabled=true \
  --set externalSecrets.data.kind=ClusterSecretStore \
  --set externalSecrets.data.name=gcp-clustersecretstore \
  --set externalSecrets.token.enabled=true \
  --set externalSecrets.token.parameters.name=stackrox-relay-gh-token \
  --set externalSecrets.token.parameters.remoteSecretKey=stackrox-relay-gh-token
```

StackRox Generic Webhook (notifier) setup
- Endpoint: `https://<your-domain>/webhook` (or in-cluster address if co-located)
- Headers: `X-ACS-TOKEN: <value of ACS_WEBHOOK_SECRET>`
- Extra fields: not required — the relay adds `event_type` and `client_payload` automatically

Health check
- `GET /healthz` returns `ok` when `GH_OWNER`, `GH_REPO` and either `GH_TOKEN` or a valid GitHub App configuration are present; otherwise `degraded`.

Upgrade / Uninstall
```bash
# upgrade
helm upgrade stackrox-relay stackrox-relay-service/stackrox-relay-service -n stackrox-relay

# uninstall
helm uninstall stackrox-relay -n stackrox-relay
```

Release a new chart version
1) Bump `version` in `helm-chart/stackrox-relay-service/Chart.yaml`
2) Commit and push changes in `helm-chart/**` to `main`
3) Wait for the workflow to complete — a GitHub Release is created and `gh-pages/index.yaml` is updated

Troubleshooting
- 401/403 from GitHub: check PAT scopes or GitHub App permissions/installation
- `healthz` is `degraded`: verify env/config and secrets are present
- No `repository_dispatch` events: ensure `GH_OWNER/GH_REPO` exists and the token/app has access
- External Secrets unresolved: verify the `ClusterSecretStore` name (`gcp-clustersecretstore`) and the `remoteSecretKey`

Security notes
- The chart config enforces non-root, read-only root filesystem, and drops Linux capabilities by default.
Helm chart → publishing in GitHub Pages
