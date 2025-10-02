# StackRox Relay Service

This service is used to relay events from StackRox to GitHub.

## Configuration

The service is configured using the following environment variables:

- `GH_OWNER`: The owner of the repository to send the events to.
- `GH_REPO`: The repository to send the events to.
- `GH_TOKEN`: The token to use to authenticate with GitHub.
- `GITHUB_API_VERSION`: The version of the GitHub API to use.
- `EVENT_TYPE`: The type of event to send to GitHub.
- `STACKROX_WEBHOOK_SECRET`: The secret to use to authenticate with StackRox.


## StackRox Generic Webhook setup

In StackRox → Platform Configuration → Integrations → Notifiers → Generic Webhook:

- Endpoint: `https://relay.example.com/webhook` (or the in-cluster service address if StackRox Central runs in the same cluster/VPC)
- Headers: `X-ACS-TOKEN: <value of STACKROX_WEBHOOK_SECRET>`
- Extra fields: not required — the relay adds `event_type` and `client_payload` automatically.