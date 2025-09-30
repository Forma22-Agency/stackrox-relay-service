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

# Lightweight check endpoint for the webhook path (GET/HEAD)
@APP.get("/webhook")
async def webhook_probe():
    return {"status": "ok", "hint": "POST JSON payload to this endpoint"}

# --- Webhook (POST only) ---
@APP.post("/webhook")
async def webhook(req: Request, x_acs_token: str | None = Header(None)):
    if ACS_WEBHOOK_SECRET and x_acs_token != ACS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid token")

    payload = await req.json()

    # Helper to get a nested path safely
    def get_path(obj, path):
        cur = obj
        try:
            for key in path:
                if isinstance(key, int):
                    cur = cur[key]
                else:
                    if not isinstance(cur, dict):
                        return None
                    cur = cur.get(key)
                if cur is None:
                    return None
            return cur
        except Exception:
            return None

    # Try the path from your payload structure first
    image = get_path(payload, ["alert", "alert", "deployment", "containers", 0, "image", "name", "fullName"])

    # Fallbacks: common shapes or recursive search for first 'fullName' string
    if not image:
        candidates = [
            ["alert", "deployment", "containers", 0, "image", "name", "fullName"],
            ["deployment", "containers", 0, "image", "name", "fullName"],
            ["image", "name", "fullName"],
        ]
        for p in candidates:
            val = get_path(payload, p)
            if isinstance(val, str) and val:
                image = val
                break

    if not image:
        # Last resort: recursive search for any key named 'fullName'
        def find_fullname(obj):
            if isinstance(obj, dict):
                if isinstance(obj.get("fullName"), str):
                    return obj.get("fullName")
                for v in obj.values():
                    found = find_fullname(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = find_fullname(item)
                    if found:
                        return found
            return None
        image = find_fullname(payload)

    if not image:
        raise HTTPException(status_code=400, detail="cannot determine image from webhook payload")

    # Extract tag explicitly (if present in payload) or derive from image string
    tag = get_path(payload, ["alert", "alert", "deployment", "containers", 0, "image", "name", "tag"]) or \
          get_path(payload, ["alert", "deployment", "containers", 0, "image", "name", "tag"]) or \
          get_path(payload, ["image", "name", "tag"]) or None

    if not tag:
        # Parse tag from full image reference if available
        def parse_tag(ref: str) -> str | None:
            if not isinstance(ref, str):
                return None
            name_part = ref.split("@", 1)[0]
            last_colon = name_part.rfind(":")
            last_slash = name_part.rfind("/")
            if last_colon > last_slash:
                return name_part[last_colon + 1 :]
            return None

        tag = parse_tag(image)

    body = {
        "event_type": EVENT_TYPE,
        "client_payload": {"image": image, **({"tag": tag} if tag else {})},
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