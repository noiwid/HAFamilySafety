"""Main FastAPI application for Microsoft Family Safety Auth."""
import logging
import os
import sys

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

# Create FastAPI app
app = FastAPI(
    title="Microsoft Family Safety Auth Service",
    description="Authentication service for Microsoft Family Safety integration",
    version="1.0.0",
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

_API_KEY = os.getenv("API_KEY", "")

# Global instances
storage = SharedStorage(config.share_dir)
browser_manager = None


def _verify_api_key(request: Request):
    """Verify API key for sensitive endpoints."""
    if not _API_KEY:
        return
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if key != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
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


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    _LOGGER.info("Shutting down Microsoft Family Safety Auth Service")
    if browser_manager:
        await browser_manager.cleanup()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main authentication interface."""
    t = get_translations(config.language)
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

        let sessionId = null;
        let statusCheckInterval = null;

        async function startAuth() {{
            const button = document.getElementById('authButton');
            const status = document.getElementById('status');
            button.disabled = true;
            button.innerHTML = '<div class="loader"></div><span>' + T.starting + '</span>';
            try {{
                showStatus(T.auth_starting, "info");
                const response = await fetch('/api/auth/start', {{ method: 'POST' }});
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
                const response = await fetch(`/api/auth/status/${{sessionId}}`);
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
async def start_authentication(_: None = Depends(_verify_api_key)):
    """Start browser authentication flow."""
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    try:
        session_id = await browser_manager.start_auth_session()
        return {"session_id": session_id, "status": "started"}
    except Exception as e:
        _LOGGER.error(f"Failed to start auth: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/auth/status/{session_id}")
async def check_auth_status(session_id: str, _: None = Depends(_verify_api_key)):
    """Check authentication status."""
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return await browser_manager.get_session_status(session_id)


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

    This launches a headless Chromium with saved cookies, navigates to the
    family page (so JS session tokens are established), then calls the
    Microsoft API via fetch() from within the browser context.
    """
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    try:
        result = await browser_manager.browser_fetch(
            "https://account.microsoft.com/family/api/st",
            params={"childId": childId},
        )
        if result is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "NO_RESPONSE",
                    "microsoft_status": "unknown",
                    "message": "browser_fetch returned None unexpectedly",
                },
            )
        # browser_fetch now returns error dicts instead of None
        if isinstance(result, dict) and result.get("__error"):
            error_code = result.get("code", "FETCH_ERROR")
            status = result.get("status", 502)
            text = str(result.get("text", "unknown error"))[:500]
            _LOGGER.warning(
                "Screen time fetch returned error: code=%s status=%s text=%s",
                error_code,
                status,
                text,
            )
            # Forward the status code from browser_fetch (e.g. 503 for BROWSER_BUSY)
            http_status = status if isinstance(status, int) and 400 <= status < 600 else 502
            raise HTTPException(
                status_code=http_status,
                detail={
                    "error": error_code,
                    "microsoft_status": status,
                    "message": text,
                },
            )
        return {"status": "success", "data": result}
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
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    try:
        data = await request.json()
        child_id = data.get("childId")
        day_of_week = data.get("dayOfWeek")
        hours = data.get("hours", 0)
        minutes = data.get("minutes", 0)

        if not child_id or day_of_week is None:
            raise HTTPException(status_code=400, detail="childId and dayOfWeek required")

        body = {
            "childId": str(child_id),
            "dayOfWeek": int(day_of_week),
            "timeSpanDays": 0,
            "timeSpanHours": int(hours),
            "timeSpanMinutes": int(minutes),
        }
        result = await browser_manager.browser_post(
            "https://account.microsoft.com/family/api//st/day-allow",
            body,
        )
        if isinstance(result, dict) and result.get("__error"):
            error_code = result.get("code", "POST_ERROR")
            status = result.get("status", 502)
            http_status = status if isinstance(status, int) and 400 <= status < 600 else 502
            raise HTTPException(
                status_code=http_status,
                detail={
                    "error": error_code,
                    "microsoft_status": status,
                    "message": str(result.get("text", "unknown error"))[:500],
                },
            )
        return {"status": "success", "data": result}
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
    if browser_manager is None:
        raise HTTPException(status_code=503, detail="Service not ready")
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
        result = await browser_manager.browser_post(
            "https://account.microsoft.com/family/api//st/day-allow-int",
            body,
        )
        if isinstance(result, dict) and result.get("__error"):
            error_code = result.get("code", "POST_ERROR")
            status = result.get("status", 502)
            http_status = status if isinstance(status, int) and 400 <= status < 600 else 502
            raise HTTPException(
                status_code=http_status,
                detail={
                    "error": error_code,
                    "microsoft_status": status,
                    "message": str(result.get("text", "unknown error"))[:500],
                },
            )
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.error(f"Set screentime intervals failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(
        app, host=config.host, port=config.port, log_level=config.log_level.lower()
    )
