# Pinned by tag + digest (multi-arch index) so base-image updates are deliberate.
# To bump: pick the new tag, `skopeo inspect --raw docker://docker.io/library/python:<tag> | sha256sum`.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn[standard] python-multipart

# Copy application
COPY app/main.py /app/
COPY static/ /app/static/

# Create data directories
RUN mkdir -p /data/bundles /data/devices /certs

EXPOSE 8081

# Run with uvicorn for production
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--access-log"]
