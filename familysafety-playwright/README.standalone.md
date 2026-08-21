# Standalone Docker deployment

> **This service is a legacy fallback.** The Microsoft Family Safety integration
> now authenticates **natively** inside Home Assistant and calls Microsoft's
> private web API directly — no browser automation, no add-on and no container
> are needed in the normal case. See the
> [main README](../README.md#legacy-add-on-mode).
>
> Use this deployment only if you already run it and prefer not to migrate, if
> your Home Assistant instance is not reachable over HTTPS and you do not want
> to enable the local-HTTP authentication option, or if native authentication
> fails on your account.
>
> Note that migrating to native authentication changes entity IDs — see
> [Breaking changes](../README.md#breaking-changes).

Run the Microsoft Family Safety Auth service on a plain Docker host (for example
a Debian server) instead of as a Home Assistant add-on. This is useful when disk
space is tight on the Home Assistant device (Green / Yellow) and you prefer to
offload the browser based auth service to a separate machine.

The Home Assistant integration talks to this service over HTTP. The only change
on the Home Assistant side is setting the integration's **Legacy auth add-on
URL** option to point at this container.

## Requirements

- A Docker host reachable from Home Assistant on the network
- Ports `8098` (HTTP API) and `6081` (noVNC web UI) available

## Quick start (prebuilt image)

A multi-arch image (amd64 and arm64) is published to GHCR on each release.

```bash
# In this folder (familysafety-playwright/)
docker compose up -d
```

The compose file pulls `ghcr.io/noiwid/hafamilysafety-auth:latest` by default.
To pin a version, replace `latest` with a release tag, e.g.
`ghcr.io/noiwid/hafamilysafety-auth:1.1.0`.

## Build locally instead

If you prefer to build from source, edit `docker-compose.yml`: comment out the
`image:` line and uncomment the `build:` block, then:

```bash
docker compose up -d --build
```

Or build the image directly:

```bash
docker build -f Dockerfile.standalone -t familysafety-auth:local .
```

## First-time authentication

1. Open the noVNC web UI at `http://YOUR_SERVER_IP:6081`.
2. Enter the VNC password (default `familysafety`, configurable via
   `VNC_PASSWORD`).
3. Sign in to your Microsoft **parent** account. The service captures and stores
   the session cookies automatically.

Cookies and the encryption key are stored in the `familysafety_data` named
volume, so they persist across restarts and image upgrades.

## Connect Home Assistant

In Home Assistant, go to **Settings > Devices & Services > Microsoft Family
Safety > Configure** and set **Legacy auth add-on URL** to:

```
http://YOUR_SERVER_IP:8098
```

The integration will load cookies and read/write screen time through this
container exactly as it does with the add-on.

An integration entry uses this container only when it has no natively captured
web session. Setting this URL during initial setup makes the config flow skip
the native Family web-session phase and create a legacy-mode entry.

## Migrating from the Home Assistant add-on

If you already run the Family Safety auth add-on and want to move it to a
standalone container, follow these steps:

1. **Stop** the auth add-on in Home Assistant.
2. **Delete** the existing Microsoft Family Safety integration entry
   (Settings > Devices & Services). Changing `auth_url` on an existing entry is
   not enough — remove it and re-add it.
3. **Start the standalone container** and complete the
   [first-time authentication](#first-time-authentication) over noVNC.
4. **Re-add** the integration. The config flow will ask you to authenticate
   again (this is expected even though the container already holds the
   cookies), then set the **auth URL** to `http://YOUR_SERVER_IP:8098`.
5. **Clean up** on the Home Assistant host: uninstall the add-on and delete the
   now-unused `/share/familysafety` folder to reclaim disk space
   (~2.6 GB: image + browser profile).

Thanks to @laurentlbm for testing this path and reporting the exact steps in
issue #25.

## Migrating away from this container (to native authentication)

Preferred for new setups. Native authentication removes this container entirely.

1. **Delete** the existing Microsoft Family Safety integration entry
   (Settings > Devices & Services). An entry stays in legacy mode as long as it
   has no natively captured web session, so it must be recreated.
2. **Re-add** the integration, leaving **Legacy auth add-on URL** and **Legacy
   add-on API key** empty, and complete the browser sign-in. Home Assistant must
   be reachable over HTTPS (or you must enable the local-HTTP testing option).
3. **Fix your entity IDs** — they change. See
   [Breaking changes](../README.md#breaking-changes).
4. **Stop and remove** this container and its `familysafety_data` volume once
   everything works.

## Configuration

All settings are environment variables (see `docker-compose.yml`):

| Variable           | Default          | Description                                   |
|--------------------|------------------|-----------------------------------------------|
| `LOG_LEVEL`        | `info`           | `trace`, `debug`, `info`, `warning`, `error`  |
| `AUTH_TIMEOUT`     | `300`            | Seconds to wait for sign-in (60-600)          |
| `SESSION_DURATION` | `86400`          | Session validity in seconds (3600-604800)     |
| `LANGUAGE`         | `en-US`          | Browser locale, e.g. `fr-FR`                  |
| `TIMEZONE`         | `Europe/Paris`   | Browser timezone                              |
| `VNC_PASSWORD`     | `familysafety`   | Password for the noVNC interface              |

## Health and logs

```bash
docker compose ps          # health status (healthcheck hits /api/health)
docker compose logs -f     # follow service logs
```

## Notes

- This standalone image does not use the Home Assistant Supervisor, so language
  and timezone are not auto-detected. Set `LANGUAGE` and `TIMEZONE` explicitly.
- The service keeps a single browser instance with a lock, so requests are
  queued. This is expected and matches the add-on behaviour.
- The noVNC interface is only needed for the initial sign-in and occasional
  re-authentication. You can keep port `6081` closed to the wider network and
  open it only when needed.
