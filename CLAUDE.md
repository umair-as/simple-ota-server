# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small OTA server for RAUC bundles: a FastAPI app (`app/main.py`, single file) behind nginx, with an mTLS device API and an unauthenticated localhost-only dashboard. There is no test suite or linter configured.

## Commands

```bash
docker compose up -d --build        # start / rebuild (app image bakes in app/main.py)
docker compose -f docker-compose.vps.yml up -d --build   # VPS variant (needs SERVER_URL in .env, no mdns sidecar)
docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload   # config/nginx.conf is bind-mounted; reload applies it without a restart
curl http://127.0.0.1:8080/health   # dashboard port; device port is :8443 (mTLS)

./scripts/generate-ca.sh [dir] [--force] [--import <dir>]   # CA is created once; --force invalidates every issued device cert
./scripts/generate-server-cert.sh certs <dns-name> [ip]
./scripts/generate-device-cert.sh <device-id>
./scripts/verify-chain.sh                                    # checks ca -> server/device consistency
```

`static/`, `boards.json`, `data/` are bind-mounted, so changes to them need no rebuild. Changes to `app/main.py` or the Dockerfile do.

## Architecture

Two nginx servers in `config/nginx.conf` front one app on `:8081`:

- **:8443 (mTLS, published to the LAN)** – `ssl_verify_client on` against `certs/ca.crt`. Serves `/bundles/` straight from `/data/bundles` (nginx `alias`, not the app) and proxies only `/api/v1/` and `/health`. nginx sets `X-SSL-Client-Verify`/`-DN`, and the app's `require_mtls()` trusts that header.
- **:8080 (plain HTTP, bound to 127.0.0.1)** – the dashboard and its management routes (`/upload`, `/activate`, `/deactivate`, `/delete`, `/api/bundles`, ...). It must keep clearing `X-SSL-Client-*` headers, otherwise a client could spoof mTLS to the app. Access remote hosts via SSH tunnel.

State is plain files under `DATA_DIR` (`/data`), no database:
- `bundles/<name>.raucb` plus `<name>.raucb.meta.json` sidecar (compatible, board name, upload time)
- `manifests/<compatible>.json` – the active release per RAUC `compatible` string; `manifest.json` is a legacy copy kept in sync for `DEFAULT_COMPATIBLE`. If no manifest exists, the default compatible falls back to the newest bundle (`auto_manifest`).
- A bundle is only deleted when no manifest still references it (`any_manifest_references`).

`boards.json` maps a display name to a `compatible` string. The dashboard uses it for the board picker, and `compatible_to_board_name` reverse-looks-up the name. Supporting another board means adding an entry here; the per-compatible manifest logic already handles it. Devices pick their manifest with `?compatible=`, the `X-RAUC-Compatible` header, or `/api/v1/manifest/<compatible>.json`.

`SERVER_URL` is the externally reachable base URL embedded in manifests (`bundle_url`), so it must match the server cert SAN and be resolvable from devices. The `mdns` sidecar advertises `ota-gw.local` through the host's avahi over D-Bus (best-effort; multicast often doesn't cross consumer routers, `/etc/hosts` is the reliable fallback).

## Conventions that matter

- User-supplied filenames go through `secure_filename()`/`bundle_path()` (only `*.raucb` files that exist); keep new file routes behind them.
- State-changing routes are protected only by the Origin-vs-Host middleware, since the dashboard has no auth. Don't expose `:8080` beyond localhost.
- Never mount `certs/` wholesale into containers; `ca.key` and device keys live there. nginx gets only `ca.crt`, `server.crt`, `server.key`.
- `certs/`, `data/`, `.env` are gitignored. Keep LAN/VPS IPs and local paths out of committed files, and don't reference unreleased boards or projects; this repo is public.
- Git: never commit to `main`; use `feat/…` or `fix/…` branches and PRs, squash-merged.
