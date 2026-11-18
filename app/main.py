#!/usr/bin/env python3
"""
RAUC Simple Server - OTA update server with mTLS and web dashboard.
"""
import os
import json
import hashlib
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

app = FastAPI(title="RAUC Simple Server", version="1.0.0")

# Configuration
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
BUNDLES_DIR = DATA_DIR / 'bundles'
SERVER_URL = os.environ.get('SERVER_URL', 'https://localhost:8443')
DEFAULT_COMPATIBLE = os.environ.get(
    'DEFAULT_COMPATIBLE',
    os.environ.get('COMPATIBLE', 'default'),
)
MANIFESTS_DIR = DATA_DIR / 'manifests'
LEGACY_MANIFEST_FILE = DATA_DIR / 'manifest.json'
BOARDS_FILE = Path('/app/boards.json')
UPLOAD_TMP_DIR = DATA_DIR / '.tmp'

# Ensure directories exist
BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
# In-flight uploads live outside bundles/ (which nginx serves to devices) but on
# the same filesystem, so the final os.replace() into bundles/ stays atomic.
UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
# Sweep temp files left by interrupted uploads, including the .upload-*.part
# files that older versions staged inside bundles/. Only files older than a day,
# so a second process can never pull an in-flight upload out from under another.
STALE_UPLOAD_AGE = 24 * 3600
for stale in [*UPLOAD_TMP_DIR.glob('upload-*.part'), *BUNDLES_DIR.glob('.upload-*.part')]:
    try:
        if time.time() - stale.stat().st_mtime > STALE_UPLOAD_AGE:
            stale.unlink()
    except FileNotFoundError:
        pass
MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

# Static files
STATIC_DIR = Path('/app/static')
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


@app.middleware('http')
async def reject_cross_origin_writes(request: Request, call_next):
    """CSRF guard: the dashboard has no auth, so refuse state-changing requests
    whose Origin does not match the Host. Requests without Origin (curl, CI/CD)
    are unaffected."""
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin is not None:
            origin_host = urlsplit(origin).hostname
            request_host = urlsplit('//' + request.headers.get('host', '')).hostname
            if not origin_host or origin_host != request_host:
                return JSONResponse({'detail': 'Cross-origin request rejected'}, status_code=403)
    return await call_next(request)


# =============================================================================
# Helpers
# =============================================================================

def require_mtls(request: Request) -> None:
    """Verify request came through mTLS."""
    if request.headers.get('X-SSL-Client-Verify') != 'SUCCESS':
        raise HTTPException(status_code=403, detail='mTLS client certificate required')


def normalize_compatible(compatible: Optional[str]) -> str:
    """Normalize compatible string into a safe, stable identifier."""
    if not compatible:
        return DEFAULT_COMPATIBLE
    cleaned = ''.join(c if c.isalnum() or c in '._-' else '-' for c in compatible.strip())
    return cleaned or DEFAULT_COMPATIBLE


def manifest_path_for(compatible: str) -> Path:
    """Return manifest path for the requested compatible."""
    return MANIFESTS_DIR / f'{normalize_compatible(compatible)}.json'


def manifest_bundle_url(filename: str) -> str:
    """Build externally reachable bundle URL."""
    return f'{SERVER_URL}/bundles/{quote(filename)}'


def get_requested_compatible(request: Request) -> str:
    """Resolve compatible from query/header and fallback to default."""
    query_compatible = request.query_params.get('compatible')
    header_compatible = request.headers.get('X-RAUC-Compatible')
    return normalize_compatible(query_compatible or header_compatible or DEFAULT_COMPATIBLE)


def auto_manifest(compatible: str) -> dict:
    """Auto-detect latest bundle for compatible if no manifest exists."""
    bundles = sorted(BUNDLES_DIR.glob('*.raucb'), key=lambda p: p.stat().st_mtime, reverse=True)
    if bundles:
        bundle = bundles[0]
        return {
            'bundle_url': manifest_bundle_url(bundle.name),
            'compatible': compatible,
            'filename': bundle.name,
            'size': bundle.stat().st_size,
            'sha256': hash_file(bundle),
            'released_at': datetime.fromtimestamp(bundle.stat().st_mtime).isoformat(),
        }
    return {'bundle_url': '', 'compatible': compatible, 'filename': ''}


def get_manifest(compatible: Optional[str] = None) -> dict:
    """Get manifest by compatible from file, legacy file, or auto state."""
    resolved_compatible = normalize_compatible(compatible)
    manifest_file = manifest_path_for(resolved_compatible)
    if manifest_file.exists():
        return json.loads(manifest_file.read_text())

    if resolved_compatible == DEFAULT_COMPATIBLE and LEGACY_MANIFEST_FILE.exists():
        legacy_manifest = json.loads(LEGACY_MANIFEST_FILE.read_text())
        legacy_manifest['compatible'] = resolved_compatible
        return legacy_manifest

    if resolved_compatible == DEFAULT_COMPATIBLE:
        return auto_manifest(resolved_compatible)

    return {'bundle_url': '', 'compatible': resolved_compatible, 'filename': ''}


def save_manifest(manifest: dict, compatible: str) -> None:
    """Save compatible-specific manifest to file."""
    resolved_compatible = normalize_compatible(compatible)
    manifest['compatible'] = resolved_compatible
    manifest_path_for(resolved_compatible).write_text(json.dumps(manifest, indent=2))
    if resolved_compatible == DEFAULT_COMPATIBLE:
        LEGACY_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))


def secure_filename(filename: str) -> str:
    """Sanitize filename for safe filesystem storage."""
    filename = filename.replace('/', '').replace('\\', '').replace('\x00', '')
    return ''.join(c for c in filename if c.isalnum() or c in '._-')


def bundle_path(filename: str) -> Path:
    """Resolve a user-supplied name to an existing bundle file, or raise 404."""
    safe = secure_filename(filename)
    path = BUNDLES_DIR / safe
    if not safe.endswith('.raucb') or not path.is_file():
        raise HTTPException(status_code=404, detail='Bundle not found')
    return path


def hash_file(filepath: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calculate SHA256 hash using streaming."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()


def read_manifest_file(path: Path) -> Optional[dict]:
    """Read manifest JSON file safely."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def refresh_manifests_for(filename: str, size: int, sha256: str) -> None:
    """A re-upload replaced the bytes behind filename; keep every manifest that
    points at it in step, or devices would verify against the old sha256/size."""
    for mf in MANIFESTS_DIR.glob('*.json'):
        parsed = read_manifest_file(mf)
        if parsed and parsed.get('filename') == filename:
            parsed['size'] = size
            parsed['sha256'] = sha256
            save_manifest(parsed, compatible=parsed.get('compatible') or mf.stem)


def any_manifest_references(filename: str) -> bool:
    """Return true if any manifest references the bundle filename."""
    for mf in MANIFESTS_DIR.glob('*.json'):
        parsed = read_manifest_file(mf)
        if parsed and parsed.get('filename') == filename:
            return True

    legacy_manifest = read_manifest_file(LEGACY_MANIFEST_FILE)
    if legacy_manifest and legacy_manifest.get('filename') == filename:
        return True

    return False


# =============================================================================
# Board registry helpers
# =============================================================================

def load_boards() -> dict:
    """Load board registry {display_name: compatible} from boards.json."""
    if not BOARDS_FILE.exists():
        return {}
    try:
        return json.loads(BOARDS_FILE.read_text())
    except Exception:
        return {}


def compatible_to_board_name(compatible: str) -> str:
    """Reverse-lookup human display name from compatible string."""
    for name, compat in load_boards().items():
        if compat == compatible:
            return name
    return compatible


def bundle_meta_path(filename: str) -> Path:
    return BUNDLES_DIR / f'{filename}.meta.json'


def read_bundle_meta(filename: str) -> Optional[dict]:
    path = bundle_meta_path(filename)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_bundle_meta(filename: str, compatible: str) -> None:
    """Save bundle metadata sidecar alongside the .raucb file."""
    meta = {
        'compatible': compatible,
        'board_name': compatible_to_board_name(compatible),
        'uploaded_at': datetime.now().isoformat(),
    }
    bundle_meta_path(filename).write_text(json.dumps(meta, indent=2))


# =============================================================================
# Device API (mTLS required - port 8443)
# =============================================================================

@app.get('/api/v1/manifest.json')
async def api_manifest(request: Request):
    """Return update manifest for devices (mTLS required)."""
    require_mtls(request)
    compatible = get_requested_compatible(request)
    return get_manifest(compatible)


@app.get('/api/v1/manifest/{compatible}.json')
async def api_manifest_by_compatible(compatible: str, request: Request):
    """Return update manifest for a specific compatible (mTLS required)."""
    require_mtls(request)
    return get_manifest(compatible)


@app.get('/bundles/{filename}')
async def serve_bundle(filename: str, request: Request):
    """Serve bundle file (mTLS required)."""
    require_mtls(request)
    filepath = bundle_path(filename)
    return FileResponse(filepath, filename=filename, media_type='application/octet-stream')


# =============================================================================
# Dashboard (localhost only - port 8080)
# =============================================================================

@app.get('/')
async def dashboard():
    """Serve dashboard."""
    return FileResponse(STATIC_DIR / 'index.html')


@app.get('/api/boards')
async def api_boards():
    """Return board registry {display_name: compatible}."""
    return load_boards()


@app.get('/api/manifest')
async def api_manifest_dashboard():
    """Return manifest for dashboard."""
    return get_manifest(DEFAULT_COMPATIBLE)


@app.get('/api/manifests')
async def api_manifests():
    """List all manifests that have a bundle file present on disk."""
    manifests = []
    seen = set()

    for mf in sorted(MANIFESTS_DIR.glob('*.json')):
        try:
            parsed = json.loads(mf.read_text())
            compatible = parsed.get('compatible')
            filename = parsed.get('filename')
            if compatible in seen:
                continue
            # Skip orphaned manifests whose bundle file no longer exists.
            if filename and not (BUNDLES_DIR / filename).exists():
                continue
            manifests.append(parsed)
            seen.add(compatible)
        except Exception:
            continue
    return manifests


@app.get('/api/bundles')
async def api_bundles():
    """List all bundles with metadata."""
    bundles = []
    for b in BUNDLES_DIR.glob('*.raucb'):
        meta = read_bundle_meta(b.name) or {}
        bundles.append({
            'name': b.name,
            'size': b.stat().st_size,
            'mtime': datetime.fromtimestamp(b.stat().st_mtime).isoformat(),
            'compatible': meta.get('compatible'),
            'board_name': meta.get('board_name'),
        })
    return bundles


UPLOAD_FIELD_LIMIT = 4096        # bytes, for the small text fields
UPLOAD_BATCH = 1024 * 1024       # hand the parser ~1 MiB per threadpool hop


class BundleUploadSink:
    """python-multipart callbacks: stream the `bundle` file part straight into a
    staging file in UPLOAD_TMP_DIR (hashing as it goes) and collect the small
    text fields. One disk write per byte, no spool, nothing in bundles/ yet."""

    def __init__(self):
        self.fields: dict[str, str] = {}
        self.filename: Optional[str] = None
        self.tmp_path: Optional[Path] = None
        self.size = 0
        self._sha256 = hashlib.sha256()
        self._file = None
        self._reset_part()

    def _reset_part(self):
        self._headers: dict[bytes, bytes] = {}
        self._hfield = b''
        self._hvalue = b''
        self._name: Optional[str] = None
        self._buf: Optional[bytearray] = None
        self._is_file = False

    # header parsing
    def on_part_begin(self):
        self._reset_part()

    def on_header_field(self, data, start, end):
        self._hfield += data[start:end]

    def on_header_value(self, data, start, end):
        self._hvalue += data[start:end]

    def on_header_end(self):
        self._headers[self._hfield.strip().lower()] = self._hvalue.strip()
        self._hfield = self._hvalue = b''

    def on_headers_finished(self):
        _, params = parse_options_header(self._headers.get(b'content-disposition', b''))
        self._name = params.get(b'name', b'').decode('utf-8', 'replace')
        raw_filename = params.get(b'filename')
        if self._name == 'bundle' and raw_filename is not None:
            if self._file is not None:
                raise HTTPException(status_code=400, detail='Only one bundle per upload')
            name = raw_filename.decode('utf-8', 'replace')
            if not name:
                raise HTTPException(status_code=400, detail='No file selected')
            if not name.endswith('.raucb'):
                raise HTTPException(status_code=400, detail='File must be a .raucb bundle')
            self.filename = secure_filename(name)
            self._file = tempfile.NamedTemporaryFile(
                dir=UPLOAD_TMP_DIR, prefix='upload-', suffix='.part', delete=False)
            self.tmp_path = Path(self._file.name)
            self._is_file = True
        else:
            self._buf = bytearray()

    # body
    def on_part_data(self, data, start, end):
        chunk = data[start:end]
        if self._is_file:
            self._file.write(chunk)
            self._sha256.update(chunk)
            self.size += len(chunk)
        elif self._buf is not None:
            if len(self._buf) + len(chunk) > UPLOAD_FIELD_LIMIT:
                raise HTTPException(status_code=400, detail=f'Field {self._name!r} too large')
            self._buf += chunk

    def on_part_end(self):
        if self._is_file:
            self._file.flush()
            os.fchmod(self._file.fileno(), 0o644)  # nginx serves bundles as a different user
            self._file.close()
        elif self._name:
            self.fields[self._name] = self._buf.decode('utf-8', 'replace')

    def callbacks(self) -> dict:
        return {name: getattr(self, name) for name in (
            'on_part_begin', 'on_header_field', 'on_header_value', 'on_header_end',
            'on_headers_finished', 'on_part_data', 'on_part_end')}

    @property
    def sha256(self) -> str:
        return self._sha256.hexdigest()

    def discard(self):
        if self._file is not None and not self._file.closed:
            self._file.close()
        if self.tmp_path is not None:
            self.tmp_path.unlink(missing_ok=True)


@app.post('/upload')
async def upload_bundle(request: Request):
    """Upload a bundle (multipart: bundle file, optional compatible, activate=true).
    compatible is resolved from board registry on the dashboard, or passed
    directly by CI/CD pipelines.

    The body is parsed as it streams in rather than through UploadFile, which
    would spool the whole bundle to disk first and then need a second copy."""
    ctype, params = parse_options_header(request.headers.get('content-type', ''))
    boundary = params.get(b'boundary')
    if ctype != b'multipart/form-data' or not boundary:
        raise HTTPException(status_code=400, detail='Expected multipart/form-data')

    sink = BundleUploadSink()
    parser = MultipartParser(boundary, sink.callbacks())
    try:
        # Parsing writes to disk, so batch chunks and run the parser off the event loop.
        batch = bytearray()
        async for chunk in request.stream():
            batch += chunk
            if len(batch) >= UPLOAD_BATCH:
                await run_in_threadpool(parser.write, bytes(batch))
                batch.clear()
        if batch:
            await run_in_threadpool(parser.write, bytes(batch))
        parser.finalize()

        if sink.tmp_path is None or not sink._file.closed:
            raise HTTPException(status_code=400, detail='No file selected')

        compatible = sink.fields.get('compatible')
        activate = sink.fields.get('activate')
        resolved_compatible = normalize_compatible(compatible) if compatible and compatible.strip() else None
        # Validate before touching bundles/, so a rejected request changes nothing.
        if activate == 'true' and not resolved_compatible:
            raise HTTPException(status_code=400, detail='compatible is required when activating')

        filename = sink.filename
        filepath = BUNDLES_DIR / filename
        # Atomic: a failed or partial upload never clobbers a bundle being served.
        os.replace(sink.tmp_path, filepath)
    except ClientDisconnect:
        sink.discard()
        return Response(status_code=400)  # client is gone; nothing to report
    except MultipartParseError as e:
        sink.discard()
        raise HTTPException(status_code=400, detail=f'Malformed multipart body: {e}')
    except BaseException:
        sink.discard()
        raise

    digest = sink.sha256
    refresh_manifests_for(filename, filepath.stat().st_size, digest)

    # Always persist compatible metadata when provided so Activate needs no prompt.
    if resolved_compatible:
        save_bundle_meta(filename, resolved_compatible)

    if activate == 'true':
        save_manifest(
            {
                'bundle_url': manifest_bundle_url(filename),
                'filename': filename,
                'size': filepath.stat().st_size,
                'sha256': digest,
                'released_at': datetime.now().isoformat(),
            },
            compatible=resolved_compatible,
        )

    return RedirectResponse(url='/', status_code=303)


@app.post('/activate/{filename}')
async def activate_bundle(filename: str, compatible: Optional[str] = None):
    """Set bundle as active. Resolves compatible from: explicit arg > metadata sidecar."""
    filepath = bundle_path(filename)
    safe_filename = filepath.name

    # Prefer explicit compatible (CI/CD or URL path), fall back to stored metadata.
    if not compatible:
        meta = read_bundle_meta(safe_filename)
        compatible = meta.get('compatible') if meta else None

    save_manifest(
        {
            'bundle_url': manifest_bundle_url(safe_filename),
            'filename': safe_filename,
            'size': filepath.stat().st_size,
            'sha256': await run_in_threadpool(hash_file, filepath),
            'released_at': datetime.now().isoformat(),
        },
        compatible=compatible or DEFAULT_COMPATIBLE,
    )
    return RedirectResponse(url='/', status_code=303)


@app.post('/activate/{compatible}/{filename}')
async def activate_bundle_for_compatible(compatible: str, filename: str):
    """Set bundle as active for specific compatible (CI/CD / direct API use)."""
    return await activate_bundle(filename=filename, compatible=compatible)


@app.post('/deactivate/{compatible}')
async def deactivate_compatible(compatible: str):
    """Remove the active manifest for a compatible without deleting the bundle file."""
    resolved = normalize_compatible(compatible)
    manifest_path_for(resolved).unlink(missing_ok=True)
    return RedirectResponse(url='/', status_code=303)


@app.post('/delete/{filename}')
async def delete_bundle(filename: str, compatible: Optional[str] = None):
    """Delete a bundle."""
    safe_filename = secure_filename(filename)
    if not safe_filename.endswith('.raucb'):
        raise HTTPException(status_code=404, detail='Bundle not found')
    filepath = BUNDLES_DIR / safe_filename

    resolved_compatible = normalize_compatible(compatible or DEFAULT_COMPATIBLE)
    manifest_files = [manifest_path_for(resolved_compatible)]
    if resolved_compatible == DEFAULT_COMPATIBLE:
        manifest_files.append(LEGACY_MANIFEST_FILE)

    # Clear active manifest for selected compatible only.
    for manifest_file in manifest_files:
        parsed = read_manifest_file(manifest_file)
        if parsed and parsed.get('filename') == safe_filename:
            manifest_file.unlink(missing_ok=True)

    # Delete file and its metadata sidecar only when no manifest still references it.
    if filepath.exists() and not any_manifest_references(safe_filename):
        filepath.unlink()
        bundle_meta_path(safe_filename).unlink(missing_ok=True)

    return RedirectResponse(url='/', status_code=303)


@app.post('/delete/{compatible}/{filename}')
async def delete_bundle_for_compatible(compatible: str, filename: str):
    """Delete bundle and clear manifest for specific compatible if active."""
    return await delete_bundle(filename=filename, compatible=compatible)


@app.get('/health')
async def health():
    """Health check."""
    return {'status': 'ok'}
