import os
import pytest
import respx
from httpx import Response
from fastapi.testclient import TestClient

os.environ.setdefault("GH_OWNER", "forma22-agency")
os.environ.setdefault("EVENT_TYPE", "stackrox_copa")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("RELAY_DEDUP_ENABLED", "false")

import app.main as main  # noqa: E402


@pytest.fixture
def client():
    return TestClient(main.APP)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] in {"ok", "degraded"}


@respx.mock
def test_webhook_success_without_topics(client, monkeypatch):
    # configure module-level flags loaded at import time
    main.RELAY_DEDUP_ENABLED = False
    main.GH_TOKEN = "ghs_dummy"
    main.ALLOWED_TOPICS = []

    body = {
        "alert": {
            "alert": {
                "deployment": {
                    "containers": [
                        {"image": {"name": {"fullName": "ghcr.io/forma22-agency/stackrox-relay-service:1.2.3"}}}
                    ]
                }
            }
        }
    }

    respx.post("https://api.github.com/repos/forma22-agency/stackrox-relay-service/dispatches").mock(return_value=Response(204))

    r = client.post("/webhook", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert j.get("repository") == "forma22-agency/stackrox-relay-service"


@respx.mock
def test_webhook_dedup_in_memory(client):
    main.RELAY_DEDUP_ENABLED = True
    main.REDIS_URL = ""  # ensure in-memory path
    main.GH_TOKEN = "ghs_dummy"
    main.ALLOWED_TOPICS = []

    payload = {
        "alert": {
            "alert": {
                "deployment": {
                    "containers": [
                        {"image": {"name": {"fullName": "registry.local/forma22-agency/stackrox-relay-service:latest"}}}
                    ]
                }
            }
        }
    }

    respx.post("https://api.github.com/repos/forma22-agency/stackrox-relay-service/dispatches").mock(side_effect=[Response(204)])

    r1 = client.post("/webhook", json=payload)
    assert r1.status_code == 200
    assert r1.json().get("deduped") is False

    r2 = client.post("/webhook", json=payload)
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True


@respx.mock
def test_webhook_topics_enforced(client):
    main.RELAY_DEDUP_ENABLED = False
    main.ALLOWED_TOPICS = ["stackrox-copa"]
    main.ALLOWED_TOPICS_MODE = "any"
    main.GH_TOKEN = "ghs_dummy"

    body = {
        "alert": {
            "alert": {
                "deployment": {
                    "containers": [
                        {"image": {"name": {"fullName": "ghcr.io/forma22-agency/stackrox-relay-service:1.2.3"}}}
                    ]
                }
            }
        }
    }

    respx.get("https://api.github.com/repos/forma22-agency/stackrox-relay-service/topics").mock(return_value=Response(200, json={"names": ["stackrox-copa"]}))
    respx.post("https://api.github.com/repos/forma22-agency/stackrox-relay-service/dispatches").mock(return_value=Response(204))

    r = client.post("/webhook", json=body)
    assert r.status_code == 200
    assert r.json()["ok"] is True

