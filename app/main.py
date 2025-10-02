# app/main.py

import os, json, time, base64
from fastapi import FastAPI, Request, Header, HTTPException
import httpx

APP = FastAPI()

GH_OWNER = os.getenv("GH_OWNER")
GH_REPO  = os.getenv("GH_REPO")
GH_TOKEN = os.getenv("GH_TOKEN")
EVENT_TYPE = os.getenv("EVENT_TYPE", "stackrox_copa")
API_VER = os.getenv("GITHUB_API_VERSION", "2022-11-28")
ACS_WEBHOOK_SECRET = os.getenv("ACS_WEBHOOK_SECRET", "")

# --- GitHub App configuration (optional) ---
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_INSTALLATION_ID = os.getenv("GITHUB_APP_INSTALLATION_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")
GITHUB_APP_PRIVATE_KEY_BASE64 = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64")

# Simple in-memory cache for installation token
_INSTALLATION_TOKEN: dict | None = None  # {"token": str, "expires_at": epoch_seconds}
_CACHED_INSTALLATION_ID: int | None = int(GITHUB_APP_INSTALLATION_ID) if GITHUB_APP_INSTALLATION_ID else None

def _is_github_app_configured() -> bool:
    return bool(GITHUB_APP_ID and (GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_BASE64))

def _load_app_private_key_pem() -> str:
    key = None
    if GITHUB_APP_PRIVATE_KEY_BASE64:
        try:
            key = base64.b64decode(GITHUB_APP_PRIVATE_KEY_BASE64).decode("utf-8")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"invalid base64 private key: {exc}")
    elif GITHUB_APP_PRIVATE_KEY:
        # Allow escaped newlines
        key = GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")
    if not key:
        raise HTTPException(status_code=500, detail="GitHub App private key is not configured")
    return key

def _build_app_jwt() -> str:
    try:
        import jwt as pyjwt  # PyJWT
    except Exception:
        raise HTTPException(status_code=500, detail="PyJWT is required for GitHub App authentication")

    if not GITHUB_APP_ID:
        raise HTTPException(status_code=500, detail="GITHUB_APP_ID is not configured")

    private_key_pem = _load_app_private_key_pem()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 9 * 60,
        "iss": GITHUB_APP_ID,
    }
    try:
        token = pyjwt.encode(payload, private_key_pem, algorithm="RS256")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to sign GitHub App JWT: {exc}")
    return token

async def _get_installation_id(client: httpx.AsyncClient) -> int:
    global _CACHED_INSTALLATION_ID
    if _CACHED_INSTALLATION_ID is not None:
        return _CACHED_INSTALLATION_ID

    # Discover installation for the configured repository
    jwt_token = _build_app_jwt()
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/installation"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VER,
    }
    r = await client.get(url, headers=headers)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"failed to get installation id: {r.text}")
    data = r.json()
    inst_id = data.get("id")
    if not isinstance(inst_id, int):
        raise HTTPException(status_code=500, detail="invalid installation id in response")
    _CACHED_INSTALLATION_ID = inst_id
    return inst_id

async def _get_installation_token(client: httpx.AsyncClient) -> str:
    global _INSTALLATION_TOKEN
    # Return cached token if valid for at least 60 seconds
    if _INSTALLATION_TOKEN and _INSTALLATION_TOKEN.get("token") and _INSTALLATION_TOKEN.get("expires_at", 0) - 60 > time.time():
        return _INSTALLATION_TOKEN["token"]

    jwt_token = _build_app_jwt()
    installation_id = await _get_installation_id(client)
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VER,
    }
    r = await client.post(url, headers=headers)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=f"failed to create installation token: {r.text}")
    data = r.json()
    token = data.get("token")
    expires_at_iso = data.get("expires_at")  # e.g., 2024-01-01T00:00:00Z
    if not token or not expires_at_iso:
        raise HTTPException(status_code=500, detail="missing token or expires_at in installation token response")
    # Parse ISO8601 to epoch
    try:
        from datetime import datetime, timezone
        expires_dt = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        expires_epoch = int(expires_dt.timestamp())
    except Exception:
        # Fallback: keep a short TTL if parsing fails
        expires_epoch = int(time.time()) + 8 * 60

    _INSTALLATION_TOKEN = {"token": token, "expires_at": expires_epoch}
    return token

async def _build_github_headers(client: httpx.AsyncClient) -> dict:
    # Prefer GitHub App if configured; fallback to GH_TOKEN
    if _is_github_app_configured():
        inst_token = await _get_installation_token(client)
        return {
            "Authorization": f"token {inst_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VER,
            "Content-Type": "application/json",
        }
    if not GH_TOKEN:
        raise HTTPException(status_code=500, detail="No GitHub credentials configured (GH_TOKEN or GitHub App)")
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VER,
        "Content-Type": "application/json",
    }

# --- Health endpoints ---
@APP.get("/healthz")
async def healthz():
    # Simple check for important variables
    base_ok = all([GH_OWNER, GH_REPO])
    creds_ok = bool(GH_TOKEN) or _is_github_app_configured()
    ok = base_ok and creds_ok
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = await _build_github_headers(client)
        r = await client.post(url, headers=headers, content=json.dumps(body))
    if r.status_code != 204:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return {"ok": True}