"""Main FastAPI application for Microsoft Family Safety Auth."""
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.auth.browser import BrowserAuthManager
from app.storage.file_storage import SharedStorage
from app.config import get_config
from app.translations import get_translations

# Configure logging
config = get_config()
logging.basicConfig(
    level=config.log_level.upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)

_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize services on startup, clean up on shutdown."""
    global browser_manager
    _LOGGER.info("Starting Microsoft Family Safety Auth Service v1.0.0")
    _LOGGER.info(
        f"Configuration: log_level={config.log_level}, auth_timeout={config.auth_timeout}s"
    )

    try:
        browser_manager = BrowserAuthManager(
            auth_timeout=config.auth_timeout,
            language=config.language,
            timezone=config.timezone,
            storage=storage,
        )
        await browser_manager.initialize()
        _LOGGER.info("Service started successfully")
    except Exception as e:
        _LOGGER.error(f"Failed to start service: {e}")
        raise

    yield

    _LOGGER.info("Shutting down Microsoft Family Safety Auth Service")
    if browser_manager:
        await browser_manager.cleanup()


# Create FastAPI app
app = FastAPI(
    title="Microsoft Family Safety Auth Service",
    description="Authentication service for Microsoft Family Safety integration",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8123",
        "http://homeassistant.local:8123",
        "http://supervisor:8123",
        "http://homeassistant:8123",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Global instances
storage = SharedStorage(config.share_dir)
browser_manager = None

# Path where the auto-generated API key is persisted (shared with the
# Home Assistant integration through the /share/familysafety directory).
_API_KEY_FILE = Path(config.share_dir) / ".api_key"


def _resolve_api_key() -> str:
    """Return the API key, generating and persisting one if needed.

    Resolution order:
    1. ``API_KEY`` environment variable (set by the user, e.g. standalone).
    2. The persisted ``.api_key`` file in the shared directory.
    3. A freshly generated key, written to that file (mode 0600).

    Because a key is always present, the high-harm endpoints below are
    always authenticated — closing the LAN cookie-theft hole even when the
    user never configured anything.
    """
    env_key = os.getenv("API_KEY", "").strip()
    if env_key:
        # Keep the file in sync so the integration (shared volume) can read it
        try:
            if not _API_KEY_FILE.exists() or _API_KEY_FILE.read_text().strip() != env_key:
                _API_KEY_FILE.write_text(env_key)
                os.chmod(_API_KEY_FILE, 0o600)
        except Exception as err:  # noqa: BLE001 - best effort, env key still works
            _LOGGER.warning("Could not persist API key file: %s", err)
        return env_key

    try:
        if _API_KEY_FILE.exists():
            existing = _API_KEY_FILE.read_text().strip()
            if existing:
                return existing
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not read API key file: %s", err)

    key = secrets.token_urlsafe(32)
    try:
        _API_KEY_FILE.write_text(key)
        os.chmod(_API_KEY_FILE, 0o600)
        _LOGGER.info("Generated a new API key at %s", _API_KEY_FILE)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Could not persist generated API key: %s", err)
    return key


_API_KEY = _resolve_api_key()

# Lightweight, per-process token guarding the browser-driven auth endpoints
# (start/status). It is injected into the served web UI so the parent's
# browser can use it, but it never grants access to cookies or screen-time
# data — so even if a LAN client scrapes it from the page, the worst it can
# do is start/poll an auth session (it still cannot log in without the
# parent's Microsoft credentials, nor read cookies or change limits).
_UI_TOKEN = os.getenv("UI_TOKEN", "").strip() or secrets.token_urlsafe(16)


def _verify_api_key(request: Request):
    """Authenticate high-harm endpoints (cookies, screen time) via API key."""
    key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(key, _API_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


def _verify_ui_token(request: Request):
    """Authenticate browser-driven auth endpoints via the UI token."""
    token = request.headers.get("X-UI-Token", "")
    if not secrets.compare_digest(token, _UI_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid or missing UI token")


def _require_browser_manager() -> BrowserAuthManager:
    """Return the browser manager or fail with 503 if not ready."""
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return browser_manager


def _unwrap_browser_result(result, default_error_code: str) -> dict:
    """Raise an HTTPException if a browser call returned an error dict."""
    if result is None:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "NO_RESPONSE",
                "microsoft_status": "unknown",
                "message": "browser call returned None unexpectedly",
            },
        )
    if isinstance(result, dict) and result.get("__error"):
        error_code = result.get("code", default_error_code)
        status = result.get("status", 502)
        text = str(result.get("text", result.get("message", "unknown error")))[:500]
        _LOGGER.warning(
            "Browser call returned error: code=%s status=%s text=%s",
            error_code, status, text,
        )
        # Forward the status code from the browser call (e.g. 503 for BROWSER_BUSY)
        http_status = status if isinstance(status, int) and 400 <= status < 600 else 502
        raise HTTPException(
            status_code=http_status,
            detail={
                "error": error_code,
                "microsoft_status": status,
                "message": text,
            },
        )
    return result


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main authentication interface."""
    t = get_translations(config.language)
    ui_token = _UI_TOKEN
    html_content = f"""
<!DOCTYPE html>
<html lang="{t['html_lang']}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{t['title']}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0078d4 0%, #005a9e 100%);
            min-height: 100vh; display: flex; align-items: center;
            justify-content: center; padding: 20px;
        }}
        .container {{
            background: white; border-radius: 16px; padding: 40px;
            max-width: 500px; width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }}
        h1 {{ color: #333; margin-bottom: 10px; font-size: 28px;
             display: flex; align-items: center; gap: 10px; }}
        .subtitle {{ color: #666; margin-bottom: 30px; font-size: 14px; line-height: 1.5; }}
        .status {{ padding: 15px; border-radius: 8px; margin-bottom: 20px; display: none; }}
        .status.success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
        .status.error {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        .status.info {{ background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }}
        button {{
            width: 100%; padding: 16px; background: #0078d4; color: white;
            border: none; border-radius: 8px; font-size: 16px; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
            display: flex; align-items: center; justify-content: center; gap: 10px;
        }}
        button:hover:not(:disabled) {{
            background: #005a9e; transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 120, 212, 0.4);
        }}
        button:disabled {{ background: #ccc; cursor: not-allowed; transform: none; }}
        .instructions {{
            background: #f8f9fa; border-radius: 8px; padding: 20px; margin-top: 20px;
        }}
        .instructions h3 {{ color: #333; margin-bottom: 10px; font-size: 16px; }}
        .instructions ol {{ margin-left: 20px; color: #666; font-size: 14px; line-height: 1.8; }}
        .instructions li {{ margin-bottom: 8px; }}
        .loader {{
            border: 3px solid #f3f3f3; border-top: 3px solid #0078d4;
            border-radius: 50%; width: 20px; height: 20px;
            animation: spin 1s linear infinite;
        }}
        @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
        .info-box {{
            background: #e7f3ff; border-left: 4px solid #0078d4; padding: 15px;
            margin-top: 20px; border-radius: 4px; font-size: 14px; color: #005a9e;
        }}
        .novnc-link {{
            display: inline-block; margin-top: 10px; padding: 8px 16px;
            background: #0078d4; color: white; text-decoration: none;
            border-radius: 6px; font-weight: 500; font-size: 14px; transition: background 0.2s;
        }}
        .novnc-link:hover {{ background: #005a9e; }}
        .novnc-hint {{ margin-top: 8px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Microsoft Family Safety</h1>
        <p class="subtitle">{t['subtitle']}</p>
        <div id="status" class="status"></div>
        <button id="authButton" onclick="startAuth()">{t['start_auth']}</button>
        <button id="reAuthButton" onclick="startAuth()" style="display:none; margin-top:10px; background:#6c757d;">{t['reauth_button']}</button>
        <div class="instructions">
            <h3>{t['instructions_title']}</h3>
            <ol>
                <li>{t['instruction_1']}</li>
                <li>{t['instruction_2']}</li>
                <li>{t['instruction_3']}</li>
                <li>{t['instruction_4']}</li>
                <li>{t['instruction_5']}</li>
                <li>{t['instruction_6']}</li>
                <li>{t['instruction_7']}</li>
            </ol>
        </div>
        <div class="info-box">
            <strong>Note:</strong> {t['info_note']}<br>
            <a id="novnc-link" class="novnc-link" href="#" target="_blank">{t['novnc_link_text']}</a>
            <div class="novnc-hint">{t['novnc_password_hint']}</div>
        </div>
    </div>
    <script>
        const T = {{
            starting: "{t['starting']}", waiting: "{t['waiting']}",
            auth_starting: "{t['auth_starting']}", browser_open: "{t['browser_open']}",
            start_failed: "{t['start_failed']}", retry: "{t['retry']}",
            auth_success: "{t['auth_success']}", auth_completed: "{t['auth_completed']}",
            auth_timeout: "{t['auth_timeout']}", retry_auth: "{t['retry_auth']}",
            auth_error: "{t['auth_error']}", unknown_error: "{t['unknown_error']}",
            cookies_exist: "{t['cookies_exist']}", cookies_valid: "{t['cookies_valid']}",
            cookies_expired: "{t['cookies_expired']}", reauth_button: "{t['reauth_button']}",
            start_error: "{t['start_error']}"
        }};
        const novncUrl = window.location.protocol + '//' + window.location.hostname + ':6081/vnc.html?autoconnect=true&password=familysafety';
        document.getElementById('novnc-link').href = novncUrl;

        // Token guarding the auth start/status endpoints (browser-driven).
        const UI_TOKEN = "{ui_token}";

        let sessionId = null;
        let statusCheckInterval = null;

        async function startAuth() {{
            const button = document.getElementById('authButton');
            const status = document.getElementById('status');
            button.disabled = true;
            button.innerHTML = '<div class="loader"></div><span>' + T.starting + '</span>';
            try {{
                showStatus(T.auth_starting, "info");
                const response = await fetch('/api/auth/start', {{
                    method: 'POST',
                    headers: {{ 'X-UI-Token': UI_TOKEN }}
                }});
                if (!response.ok) throw new Error(T.start_error);
                const data = await response.json();
                sessionId = data.session_id;
                showStatus(T.browser_open, "info");
                button.innerHTML = '<div class="loader"></div><span>' + T.waiting + '</span>';
                statusCheckInterval = setInterval(checkAuthStatus, 2000);
            }} catch (error) {{
                showStatus(T.start_failed + error.message, "error");
                button.disabled = false;
                button.innerHTML = T.retry;
            }}
        }}

        async function checkAuthStatus() {{
            if (!sessionId) return;
            try {{
                const response = await fetch(`/api/auth/status/${{sessionId}}`, {{
                    headers: {{ 'X-UI-Token': UI_TOKEN }}
                }});
                const data = await response.json();
                if (data.status === 'completed') {{
                    clearInterval(statusCheckInterval);
                    showStatus(T.auth_success.replace('{{count}}', data.cookie_count), 'success');
                    const button = document.getElementById('authButton');
                    button.innerHTML = T.auth_completed;
                    button.style.background = '#28a745';
                }} else if (data.status === 'timeout') {{
                    clearInterval(statusCheckInterval);
                    showStatus(T.auth_timeout, "error");
                    const button = document.getElementById('authButton');
                    button.disabled = false;
                    button.innerHTML = T.retry_auth;
                }} else if (data.status === 'error') {{
                    clearInterval(statusCheckInterval);
                    showStatus(T.auth_error + (data.error || T.unknown_error), "error");
                    const button = document.getElementById('authButton');
                    button.disabled = false;
                    button.innerHTML = T.retry_auth;
                }}
            }} catch (error) {{ console.error('Status check failed:', error); }}
        }}

        function showStatus(message, type) {{
            const status = document.getElementById('status');
            status.textContent = message;
            status.className = `status ${{type}}`;
            status.style.display = 'block';
        }}

        window.addEventListener('load', async () => {{
            try {{
                const response = await fetch('/api/cookies/check');
                const data = await response.json();
                if (data.exists && !data.expired) {{
                    // Valid cookies — hide main auth, show optional re-auth
                    const msg = T.cookies_valid
                        .replace('{{count}}', data.count || '?')
                        .replace('{{age}}', Math.round(data.age_hours || 0));
                    showStatus(msg, "success");
                    document.getElementById('authButton').style.display = 'none';
                    document.getElementById('reAuthButton').style.display = 'flex';
                }} else if (data.exists && data.expired) {{
                    // Expired cookies — prompt re-auth
                    showStatus(T.cookies_expired, "error");
                }}
            }} catch (error) {{}}
        }});
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "familysafety-auth", "version": "1.0.0"}


@app.post("/api/auth/start")
async def start_authentication(_: None = Depends(_verify_ui_token)):
    """Start browser authentication flow (browser-driven, UI-token guarded)."""
    manager = _require_browser_manager()
    try:
        session_id = await manager.start_auth_session()
        return {"session_id": session_id, "status": "started"}
    except Exception as e:
        _LOGGER.error(f"Failed to start auth: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/auth/status/{session_id}")
async def check_auth_status(session_id: str, _: None = Depends(_verify_ui_token)):
    """Check authentication status (browser-driven, UI-token guarded)."""
    return await _require_browser_manager().get_session_status(session_id)


@app.get("/api/cookies/check")
async def check_cookies():
    """Check if cookies exist and return their validity info."""
    info = await storage.get_cookie_info()
    if info.get("exists") and info.get("age_hours") is not None:
        max_hours = config.session_duration / 3600
        info["expired"] = info["age_hours"] >= max_hours
    return info


@app.get("/api/cookies")
async def get_cookies(_: None = Depends(_verify_api_key)):
    """Retrieve stored cookies (for integration)."""
    try:
        cookies = await storage.load_cookies()
        return {"cookies": cookies, "status": "success", "count": len(cookies)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No cookies found")
    except Exception as e:
        _LOGGER.error(f"Failed to load cookies: {e}")
        raise HTTPException(status_code=500, detail="Failed to load cookies")


@app.delete("/api/cookies")
async def delete_cookies(_: None = Depends(_verify_api_key)):
    """Delete stored cookies."""
    try:
        await storage.clear_cookies()
        return {"status": "success", "message": "Cookies deleted"}
    except Exception as e:
        _LOGGER.error(f"Failed to delete cookies: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete cookies")


@app.get("/api/screentime")
async def get_screentime(childId: str, _: None = Depends(_verify_api_key)):
    """Fetch screen time policy through an authenticated browser session.

    Navigates to the family page with saved cookies (so JS session tokens
    are established), then calls the Microsoft API via fetch() from within
    the browser context.
    """
    manager = _require_browser_manager()
    try:
        result = await manager.browser_fetch(
            "https://account.microsoft.com/family/api/st",
            params={"childId": childId},
        )
        return {"status": "success", "data": _unwrap_browser_result(result, "FETCH_ERROR")}
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.error(f"Screen time fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/screentime/set-allowance")
async def set_screentime_allowance(request: Request, _: None = Depends(_verify_api_key)):
    """Set daily screen time allowance via browser session.

    Expects JSON body: {childId, dayOfWeek, hours, minutes}
    """
    manager = _require_browser_manager()
    try:
        data = await request.json()
        child_id = data.get("childId")
        day_of_week = data.get("dayOfWeek")

        if not child_id or day_of_week is None:
            raise HTTPException(status_code=400, detail="childId and dayOfWeek required")

        body = {
            "childId": str(child_id),
            "dayOfWeek": int(day_of_week),
            "timeSpanDays": 0,
            "timeSpanHours": int(data.get("hours", 0)),
            "timeSpanMinutes": int(data.get("minutes", 0)),
        }
        result = await manager.browser_post(
            "https://account.microsoft.com/family/api//st/day-allow",
            body,
        )
        return {"status": "success", "data": _unwrap_browser_result(result, "POST_ERROR")}
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.error(f"Set screentime allowance failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/screentime/set-intervals")
async def set_screentime_intervals(request: Request, _: None = Depends(_verify_api_key)):
    """Set allowed time intervals via browser session.

    Expects JSON body: {childId, dayOfWeek, allowedIntervals: [48 booleans]}
    """
    manager = _require_browser_manager()
    try:
        data = await request.json()
        child_id = data.get("childId")
        day_of_week = data.get("dayOfWeek")
        allowed_intervals = data.get("allowedIntervals")

        if not child_id or day_of_week is None or not allowed_intervals:
            raise HTTPException(
                status_code=400,
                detail="childId, dayOfWeek, and allowedIntervals required",
            )

        body = {
            "childId": str(child_id),
            "dayOfWeek": int(day_of_week),
            "allowedIntervals": allowed_intervals,
        }
        result = await manager.browser_post(
            "https://account.microsoft.com/family/api//st/day-allow-int",
            body,
        )
        return {"status": "success", "data": _unwrap_browser_result(result, "POST_ERROR")}
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.error(f"Set screentime intervals failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(
        app, host=config.host, port=config.port, log_level=config.log_level.lower()
    )
