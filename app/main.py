#!/usr/bin/env python3
"""
RAUC Simple Server - OTA update server with mTLS and web dashboard.
"""
import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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

# Ensure directories exist
BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

# Static files
STATIC_DIR = Path('/app/static')
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


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


def hash_file(filepath: Path, chunk_size: int = 8192) -> str:
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
    filepath = BUNDLES_DIR / secure_filename(filename)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail='Bundle not found')
    return FileResponse(filepath, filename=filename, media_type='application/octet-stream')


# =============================================================================
# Dashboard (localhost only - port 8080)
# =============================================================================

@app.get('/')
async def dashboard():
    """Serve dashboard."""
    return FileResponse(STATIC_DIR / 'index.html')


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
    """List all bundles."""
    return [
        {
            'name': b.name,
            'size': b.stat().st_size,
            'mtime': datetime.fromtimestamp(b.stat().st_mtime).isoformat(),
        }
        for b in BUNDLES_DIR.glob('*.raucb')
    ]


@app.post('/upload')
async def upload_bundle(
    bundle: UploadFile = File(...),
    activate: Optional[str] = Form(None),
    compatible: Optional[str] = Form(None),
):
    """Upload a bundle."""
    if not bundle.filename:
        raise HTTPException(status_code=400, detail='No file selected')
    if not bundle.filename.endswith('.raucb'):
        raise HTTPException(status_code=400, detail='File must be a .raucb bundle')

    filename = secure_filename(bundle.filename)
    filepath = BUNDLES_DIR / filename

    # Stream to disk
    sha256 = hashlib.sha256()
    with open(filepath, 'wb') as f:
        while chunk := await bundle.read(8192):
            f.write(chunk)
            sha256.update(chunk)

    if activate == 'true':
        if not compatible or not compatible.strip():
            raise HTTPException(status_code=400, detail='compatible is required when activating')
        resolved_compatible = normalize_compatible(compatible)
        save_manifest(
            {
                'bundle_url': manifest_bundle_url(filename),
                'filename': filename,
                'size': filepath.stat().st_size,
                'sha256': sha256.hexdigest(),
                'released_at': datetime.now().isoformat(),
            },
            compatible=resolved_compatible,
        )

    return RedirectResponse(url='/', status_code=303)


@app.post('/activate/{filename}')
async def activate_bundle(filename: str, compatible: Optional[str] = None):
    """Set bundle as active."""
    safe_filename = secure_filename(filename)
    filepath = BUNDLES_DIR / safe_filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail='Bundle not found')

    save_manifest(
        {
            'bundle_url': manifest_bundle_url(safe_filename),
            'filename': safe_filename,
            'size': filepath.stat().st_size,
            'sha256': hash_file(filepath),
            'released_at': datetime.now().isoformat(),
        },
        compatible=compatible or DEFAULT_COMPATIBLE,
    )
    return RedirectResponse(url='/', status_code=303)


@app.post('/activate/{compatible}/{filename}')
async def activate_bundle_for_compatible(compatible: str, filename: str):
    """Set bundle as active for specific compatible."""
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

    # Delete file only when no manifest still references it.
    if filepath.exists() and not any_manifest_references(safe_filename):
        filepath.unlink()

    return RedirectResponse(url='/', status_code=303)


@app.post('/delete/{compatible}/{filename}')
async def delete_bundle_for_compatible(compatible: str, filename: str):
    """Delete bundle and clear manifest for specific compatible if active."""
    return await delete_bundle(filename=filename, compatible=compatible)


@app.get('/health')
async def health():
    """Health check."""
    return {'status': 'ok'}
