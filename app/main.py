# app/main.py

import os, json
from fastapi import FastAPI, Request, Header, HTTPException
import httpx

APP = FastAPI()

GH_OWNER = os.getenv("GH_OWNER")
GH_REPO  = os.getenv("GH_REPO")
GH_TOKEN = os.getenv("GH_TOKEN")
EVENT_TYPE = os.getenv("EVENT_TYPE", "stackrox_copa")
API_VER = os.getenv("GITHUB_API_VERSION", "2022-11-28")
ACS_WEBHOOK_SECRET = os.getenv("ACS_WEBHOOK_SECRET", "")

# --- Health endpoints ---
@APP.get("/healthz")
async def healthz():
    # простая проверка наличия важных переменных
    ok = all([GH_OWNER, GH_REPO, GH_TOKEN])
    return {"status": "ok" if ok else "degraded"}

@APP.get("/")
async def root():
    return {"service": "gh-dispatch-relay", "status": "ok"}

# --- Webhook (POST only) ---
@APP.post("/webhook")
async def webhook(req: Request, x_acs_token: str | None = Header(None)):
    if ACS_WEBHOOK_SECRET and x_acs_token != ACS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid token")

    alert_payload = await req.json()
    body = {
        "event_type": EVENT_TYPE,
        "client_payload": {"alert": alert_payload},
    }

    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VER,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=headers, content=json.dumps(body))
    if r.status_code != 204:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return {"ok": True}