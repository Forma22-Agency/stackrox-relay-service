# StackRox Relay Service

This service is used to relay events from StackRox to GitHub.

## Configuration

The service is configured using the following environment variables:

- `GH_OWNER`: The owner of the repository to send the events to.
- `GH_REPO`: The repository to send the events to.
- `GH_TOKEN`: The token to use to authenticate with GitHub.
- `ACS_WEBHOOK_SECRET`: Shared secret used to authenticate ACS Generic Webhook requests (header `X-ACS-TOKEN`).

## ACS Generic Webhook setup

In ACS → Platform Configuration → Integrations → Notifiers → Generic Webhook:

- Endpoint: `https://relay.example.com/webhook` (or the in-cluster service address if ACS Central runs in the same cluster/VPC)
- Headers: `X-ACS-TOKEN: <value of ACS_WEBHOOK_SECRET>`
- Extra fields: not required — the relay adds `event_type` and `client_payload` automatically.

Attach your policy (e.g., "No Critical CVEs") to this notifier in Enforce on Admission mode. When a deployment is blocked, ACS will send an event to the relay.