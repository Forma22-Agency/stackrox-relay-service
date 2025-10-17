# app/main.py

import os, json, time, base64, logging, hashlib
from fastapi import FastAPI, Request, Header, HTTPException
import httpx

APP = FastAPI()

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)
logger = logging.getLogger("stackrox-relay")
if not logger.handlers:
    logging.basicConfig(level=_LEVEL, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger.setLevel(_LEVEL)

GH_OWNER = os.getenv("GH_OWNER")
GH_REPO  = os.getenv("GH_REPO")
GH_TOKEN = os.getenv("GH_TOKEN")
EVENT_TYPE = os.getenv("EVENT_TYPE", "stackrox_copa")
API_VER = os.getenv("GITHUB_API_VERSION", "2022-11-28")
ACS_WEBHOOK_SECRET = os.getenv("ACS_WEBHOOK_SECRET", "")

# --- Deduplication settings ---
RELAY_DEDUP_ENABLED = os.getenv("RELAY_DEDUP_ENABLED", "true").lower() in {"1", "true", "yes"}
try:
    RELAY_DEDUP_TTL_SECONDS = int(os.getenv("RELAY_DEDUP_TTL_SECONDS", "180"))
except Exception:
    RELAY_DEDUP_TTL_SECONDS = 180
REDIS_URL = os.getenv("REDIS_URL", "")

# Multi-repo guard by topics (comma-separated list). If set, only repos
# containing at least one (or all, depending on mode) of these topics will be allowed.
_ALLOWED_TOPICS_RAW = os.getenv("GH_ALLOWED_TOPICS", "")
ALLOWED_TOPICS = [t.strip().lower() for t in _ALLOWED_TOPICS_RAW.split(",") if t.strip()]
ALLOWED_TOPICS_MODE = os.getenv("GH_ALLOWED_TOPICS_MODE", "any").lower()  # "any" or "all"

# --- GitHub App configuration (optional) ---
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_INSTALLATION_ID = os.getenv("GITHUB_APP_INSTALLATION_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")
GITHUB_APP_PRIVATE_KEY_BASE64 = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64")

# Simple in-memory caches for installation discovery and tokens
# Map installation id -> {"token": str, "expires_at": epoch_seconds}
_INSTALLATION_TOKEN_BY_ID: dict[int, dict] = {}
# Map owner (org/user) -> installation id
_CACHED_INSTALLATION_ID_BY_OWNER: dict[str, int] = {}
if GITHUB_APP_INSTALLATION_ID and GH_OWNER:
    try:
        _CACHED_INSTALLATION_ID_BY_OWNER[GH_OWNER] = int(GITHUB_APP_INSTALLATION_ID)
    except Exception:
        pass

# --- Deduplication state ---
_DEDUP_CACHE: dict[str, float] = {}  # key -> expires_at (epoch)
_REDIS_CLIENT = None

async def _get_redis_client():
    global _REDIS_CLIENT
    if not REDIS_URL:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        from redis.asyncio import Redis  # type: ignore
    except Exception as exc:
        logger.warning("redis package not available, falling back to in-memory dedup", extra={"error": str(exc)})
        return None
    try:
        _REDIS_CLIENT = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    except Exception as exc:
        logger.error("failed to init redis client, falling back to in-memory dedup", extra={"error": str(exc)})
        _REDIS_CLIENT = None
    return _REDIS_CLIENT

def _sanitize_for_logging(obj):
    """Best-effort scrubbing of sensitive-looking keys before logging."""
    sensitive_keys = {
        "password",
        "token",
        "authorization",
        "secret",
        "apikey",
        "api_key",
        "privatekey",
        "private_key",
    }
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in sensitive_keys:
                sanitized[k] = "***"
            else:
                sanitized[k] = _sanitize_for_logging(v)
        return sanitized
    if isinstance(obj, list):
        return [_sanitize_for_logging(i) for i in obj]
    return obj

def _build_dedup_key(owner: str, repo: str, image: str, tag: str | None) -> str:
    tag_part = tag or "latest"
    raw = f"{owner}:{repo}:{EVENT_TYPE}:{image}:{tag_part}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"relay:dedup:{digest}"

async def _dedup_should_skip(key: str) -> bool:
    if not RELAY_DEDUP_ENABLED:
        logger.debug("dedup disabled; bypassing", extra={"key": key})
        return False
    now = time.time()
    # Try Redis first
    client = await _get_redis_client()
    if client is not None:
        try:
            logger.debug("dedup check via redis", extra={"key": key, "ttl": RELAY_DEDUP_TTL_SECONDS})
            # NX + EX TTL seconds; True if created, None if exists
            created = await client.set(key, "1", ex=RELAY_DEDUP_TTL_SECONDS, nx=True)
            if not created:
                logger.info("dedup hit (redis)", extra={"key": key})
                return True
            logger.debug("dedup key created (redis)", extra={"key": key, "ttl": RELAY_DEDUP_TTL_SECONDS})
            return False
        except Exception as exc:
            logger.error("redis error, falling back to in-memory dedup", extra={"error": str(exc)})
    # In-memory fallback
    logger.debug("dedup check via memory", extra={"key": key, "ttl": RELAY_DEDUP_TTL_SECONDS})
    # purge expired
    to_delete = [k for k, exp in _DEDUP_CACHE.items() if exp <= now]
    for k in to_delete:
        _DEDUP_CACHE.pop(k, None)
    if key in _DEDUP_CACHE:
        logger.info("dedup hit (memory)", extra={"key": key})
        return True
    _DEDUP_CACHE[key] = now + RELAY_DEDUP_TTL_SECONDS
    logger.debug("dedup key created (memory)", extra={"key": key, "ttl": RELAY_DEDUP_TTL_SECONDS})
    return False

async def _dedup_release_on_failure(key: str):
    if not RELAY_DEDUP_ENABLED:
        return
    client = await _get_redis_client()
    if client is not None:
        try:
            await client.delete(key)
            logger.debug("dedup key removed (redis)", extra={"key": key})
            return
        except Exception as exc:
            logger.warning("failed to remove dedup key in redis", extra={"error": str(exc)})
    _DEDUP_CACHE.pop(key, None)
    logger.debug("dedup key removed (memory)", extra={"key": key})

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

async def _get_installation_id(client: httpx.AsyncClient, owner: str, repo: str) -> int:
    # Cached per owner, as installation is bound to account (org/user)
    if owner in _CACHED_INSTALLATION_ID_BY_OWNER:
        return _CACHED_INSTALLATION_ID_BY_OWNER[owner]

    # Discover installation for the target repository
    jwt_token = _build_app_jwt()
    url = f"https://api.github.com/repos/{owner}/{repo}/installation"
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
    _CACHED_INSTALLATION_ID_BY_OWNER[owner] = inst_id
    return inst_id

async def _get_installation_token(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    # Return cached token for this installation if valid for at least 60 seconds
    installation_id = await _get_installation_id(client, owner, repo)
    cached = _INSTALLATION_TOKEN_BY_ID.get(installation_id)
    if cached and cached.get("token") and cached.get("expires_at", 0) - 60 > time.time():
        return cached["token"]

    jwt_token = _build_app_jwt()
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

    _INSTALLATION_TOKEN_BY_ID[installation_id] = {"token": token, "expires_at": expires_epoch}
    return token

async def _build_github_headers(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    # Prefer GitHub App if configured; fallback to GH_TOKEN
    if _is_github_app_configured():
        inst_token = await _get_installation_token(client, owner, repo)
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

def _derive_owner_repo_from_image(image_ref: str) -> tuple[str, str] | None:
    """Try to derive GitHub owner/repo from ghcr image reference.
    Examples:
      ghcr.io/acme/awesome:1.2.3 -> (acme, awesome)
      ghcr.io/acme/awesome@sha256:... -> (acme, awesome)
    Returns None if cannot derive reliably.
    """
    if not isinstance(image_ref, str):
        return None
    try:
        ref = image_ref.split("@", 1)[0]
        if ref.startswith("ghcr.io/"):
            path = ref[len("ghcr.io/"):]
            parts = path.split(":", 1)[0].split("/")
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
                if owner and repo:
                    return owner, repo
    except Exception:
        return None
    return None

async def _repo_topics_allow(client: httpx.AsyncClient, owner: str, repo: str) -> bool:
    """If ALLOWED_TOPICS is configured, ensure the target repo has required topics.
    Mode any/all controlled by ALLOWED_TOPICS_MODE.
    If ALLOWED_TOPICS empty, always allow.
    """
    if not ALLOWED_TOPICS:
        return True
    headers = await _build_github_headers(client, owner, repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/topics"
    r = await client.get(url, headers=headers)
    if r.status_code != 200:
        # Conservative: deny if we cannot validate
        raise HTTPException(status_code=r.status_code, detail=f"failed to read repo topics: {r.text}")
    data = r.json() or {}
    names = [str(t).lower() for t in data.get("names", [])]
    if ALLOWED_TOPICS_MODE == "all":
        return all(t in names for t in ALLOWED_TOPICS)
    # default: any
    return any(t in names for t in ALLOWED_TOPICS)

# --- Health endpoints ---
@APP.get("/healthz")
async def healthz():
    # In multi-repo mode GH_REPO may be omitted. Consider only creds.
    creds_ok = bool(GH_TOKEN) or _is_github_app_configured()
    ok = creds_ok
    status = "ok" if ok else "degraded"
    logger.debug("healthz check", extra={"status": status})
    return {"status": status}

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
    logger.info("webhook received")

    # Debug logging of the raw payload when log level is DEBUG
    if logger.isEnabledFor(logging.DEBUG):
        try:
            as_text = json.dumps(payload, ensure_ascii=False)
            logger.debug("webhook payload", extra={"payload": as_text})
        except Exception as exc:
            logger.debug("failed to serialize webhook payload for debug", extra={"error": str(exc)})

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
        logger.warning("image not found in payload; cannot proceed")
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
    logger.debug("parsed image and tag", extra={"image": image, "tag": tag})

    body = {
        "event_type": EVENT_TYPE,
        "client_payload": {"image": image, **({"tag": tag} if tag else {})},
    }

    # Choose single target repository by image basename: GH_OWNER/<image_basename>
    def _guess_repo_from_image(ref: str) -> str | None:
        if not isinstance(ref, str) or not ref:
            return None
        # Strip digest and tag
        without_digest = ref.split("@", 1)[0]
        without_tag = without_digest.rsplit(":", 1)[0]
        # Take last path segment
        parts = without_tag.split("/")
        if parts:
            name = parts[-1].strip()
            return name or None
        return None

    repo_name = _guess_repo_from_image(image)
    if not GH_OWNER or not repo_name:
        logger.warning("cannot determine target repository", extra={"gh_owner_set": bool(GH_OWNER), "repo_basename": repo_name})
        raise HTTPException(status_code=400, detail="cannot determine target repository: need GH_OWNER and image basename")

    owner, repo = GH_OWNER, repo_name
    # Deduplication guard: skip duplicates within TTL window
    dedup_key = _build_dedup_key(owner, repo, image, tag)
    logger.debug("dedup candidate", extra={
        "owner": owner,
        "repo": repo,
        "image": image,
        "tag": tag or "latest",
        "event_type": EVENT_TYPE,
        "key": dedup_key,
        "enabled": RELAY_DEDUP_ENABLED,
        "redis": bool(REDIS_URL),
        "ttl": RELAY_DEDUP_TTL_SECONDS,
    })
    if await _dedup_should_skip(dedup_key):
        logger.info("request deduplicated; skipping dispatch", extra={"owner": owner, "repo": repo})
        return {"ok": True, "repository": f"{owner}/{repo}", "deduped": True}
    async with httpx.AsyncClient(timeout=30.0) as client:
        if ALLOWED_TOPICS:
            logger.debug("checking topics policy", extra={"owner": owner, "repo": repo, "mode": ALLOWED_TOPICS_MODE, "required_topics": ALLOWED_TOPICS})
            if not await _repo_topics_allow(client, owner, repo):
                logger.warning("repository denied by topics policy", extra={"owner": owner, "repo": repo})
                raise HTTPException(status_code=403, detail="repository is not allowed by topics policy")
        headers = await _build_github_headers(client, owner, repo)
        url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
        # Log what we send to GitHub (no credentials): URL and JSON body
        try:
            safe_body = json.dumps(body)
        except Exception:
            safe_body = str(body)
        logger.info(
            "dispatching repository_dispatch",
            extra={"owner": owner, "repo": repo, "event_type": EVENT_TYPE}
        )
        logger.debug(
            "dispatch request payload",
            extra={"url": url, "body": safe_body[:2000]}  # truncate to avoid huge logs
        )
        r = await client.post(url, headers=headers, content=safe_body)
        if r.status_code != 204:
            snippet = r.text[:500] if isinstance(r.text, str) else str(r.text)
            logger.error(
                "dispatch failed",
                extra={
                    "owner": owner,
                    "repo": repo,
                    "status": r.status_code,
                    "response": snippet,
                    "request_body": safe_body[:1000],
                }
            )
            # On server errors allow retry by removing dedup key
            if 500 <= r.status_code < 600:
                logger.info("releasing dedup key due to 5xx", extra={"key": dedup_key, "status": r.status_code})
                await _dedup_release_on_failure(dedup_key)
            raise HTTPException(status_code=r.status_code, detail=r.text)
    logger.info("dispatch succeeded", extra={"owner": owner, "repo": repo})
    return {"ok": True, "repository": f"{owner}/{repo}", "deduped": False}