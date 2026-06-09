#!/usr/bin/env bash
# ==============================================================================
# Standalone entrypoint for the Microsoft Family Safety Auth service.
#
# This is the non Home Assistant variant of rootfs/usr/local/bin/run.sh: it does
# not use bashio or the Supervisor API. All configuration comes from environment
# variables (see docker-compose.yml), with sensible defaults. It starts the
# virtual display, the VNC and noVNC interfaces, and the FastAPI service.
# ==============================================================================
set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ------------------------------------------------------------------------------
# Configuration (environment variables, matching app/config.py)
# ------------------------------------------------------------------------------
export LOG_LEVEL="${LOG_LEVEL:-info}"
export AUTH_TIMEOUT="${AUTH_TIMEOUT:-300}"
export SESSION_DURATION="${SESSION_DURATION:-86400}"
export LANGUAGE="${LANGUAGE:-en-US}"
export TIMEZONE="${TIMEZONE:-Europe/Paris}"
VNC_PASSWORD="${VNC_PASSWORD:-familysafety}"

log "Starting Microsoft Family Safety Auth Service (standalone)"
log "  Log Level:        ${LOG_LEVEL}"
log "  Auth Timeout:     ${AUTH_TIMEOUT}s"
log "  Session Duration: ${SESSION_DURATION}s"
log "  Language:         ${LANGUAGE}"
log "  Timezone:         ${TIMEZONE}"

# ------------------------------------------------------------------------------
# Shared storage (cookies + encryption key live here; mount a volume on it)
# ------------------------------------------------------------------------------
mkdir -p /share/familysafety
chmod 700 /share/familysafety
log "Shared storage ready at /share/familysafety"

# ------------------------------------------------------------------------------
# D-Bus (non critical, silences Chromium warnings)
# ------------------------------------------------------------------------------
if [ ! -S /run/dbus/system_bus_socket ]; then
    mkdir -p /run/dbus
    dbus-daemon --system --fork 2>/dev/null || log "D-Bus not available (non-critical)"
fi

# ------------------------------------------------------------------------------
# Virtual display + window manager
# ------------------------------------------------------------------------------
log "Starting virtual display (Xvfb)..."
Xvfb :99 -screen 0 1280x1024x16 -ac -nolisten tcp &
export DISPLAY=:99
sleep 2
fluxbox &

# ------------------------------------------------------------------------------
# VNC + noVNC (browser based access to the auth session on port 6081)
# ------------------------------------------------------------------------------
log "Starting VNC server (localhost only)..."
x11vnc -display :99 -forever -shared -rfbport 5900 -localhost -passwd "${VNC_PASSWORD}" &
VNC_PID=$!
sleep 1
if ! kill -0 "${VNC_PID}" 2>/dev/null; then
    log "WARNING: x11vnc failed to start, noVNC will not be available"
fi

log "Starting noVNC on port 6081..."
websockify --web=/usr/share/novnc 6081 localhost:5900 &
NOVNC_PID=$!
sleep 1
if ! kill -0 "${NOVNC_PID}" 2>/dev/null; then
    log "WARNING: websockify/noVNC failed to start on port 6081"
fi

# ------------------------------------------------------------------------------
# FastAPI application (the HTTP API on port 8098 the integration talks to)
# ------------------------------------------------------------------------------
log "Starting FastAPI application on port 8098..."
cd /app
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8098 \
    --log-level "${LOG_LEVEL}" \
    --no-access-log \
    --workers 1
