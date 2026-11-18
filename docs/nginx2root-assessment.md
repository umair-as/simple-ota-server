# nginx2root assessment (2026-11-18)

Pinned image: `nginx:1.30.7-alpine@sha256:bef15473…8598`, the multi-arch index digest.
Verified with `skopeo inspect --raw` (index sha256) and `nginx -v` = 1.30.7. `RepoDigests[0]`
after a pull shows the per-arch manifest digest (amd64: `sha256:4d1d4ce0…91a4`). That's expected, not a mismatch.

## Batch: 10 CVEs (source: nginx.org/en/security_advisories.html)

1.30.7 fixes 9. CVE-2026-120993 is fixed only in 1.31.9, but its range (1.31.4-1.31.8) never included 1.30.x.

| CVE | Area | Applies here? |
|---|---|---|
| CVE-2026-123007 | HTTP/2 flow control, memory corruption (major) | **Yes**: `http2 on` on :8443 |
| CVE-2026-123633 | HTTP/2 use-after-free | **Yes**: `http2 on` on :8443 |
| CVE-2026-118899 | infinite loop reading request body over TLS 1.3 (major) | **Yes**: :8443 offers TLS 1.3 |
| CVE-2026-123634 | resolver use-after-free | No: no `resolver`, upstream resolved at startup |
| CVE-2026-125283 | `alias` + `try_files` overflow | No: `alias` without `try_files` |
| CVE-2026-121810 | HTTP/3 use-after-free | No: no QUIC listener |
| CVE-2026-113425 | mail proxy use-after-free | No: no `mail {}` |
| CVE-2026-120967 | stream SNI bypass with client certs | No: no `stream {}` |
| CVE-2026-123640 | stream `ssl_preread` | No: no `stream {}` |
| CVE-2026-120993 | PROXY protocol v2 NULL deref | No: no `proxy_protocol`, and 1.30.x was never affected |

## Exposure

- **:8443 (LAN, or internet on the VPS): mTLS does not shield the HTTP/2 layer.** nginx checks the
  client cert per request, after the TLS handshake and after HTTP/2 frames are parsed. Observed:
  `curl --http2` with no client cert gets ALPN `h2` and a response of `HTTP/2 400 No required SSL
  certificate was sent`. So CVE-2026-123007 and CVE-2026-123633 were reachable without a
  certificate. CVE-2026-118899 needs a request body to be read, which happens in the proxied
  handlers after the cert check, so it most likely needs a valid device cert (not tested).
- **:8080 (dashboard):** bound to 127.0.0.1, plain HTTP/1.1, so none of the HTTP/2 or TLS CVEs apply.

## Status

Fixed by PR #10 (pin to 1.30.7). PR #13 adds container hardening (cap_drop, read_only,
no-new-privileges) as defence in depth.
