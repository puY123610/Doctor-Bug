#!/usr/bin/env bash
# Stage an explicit allowlist of platform files and build the SANDBOX-VAL
# image.  The build context contains no credentials: .env.local,
# model_config.json, .venv, runs/ and caches are never copied.
set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$(dirname "$SANDBOX_DIR")"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
IMAGE="${IMAGE:-sandbox-val:local}"
CONTEXT="$SANDBOX_DIR/.build/context"

if [ ! -x "$DOCKER_BIN" ]; then
    DOCKER_BIN="$(command -v docker)"
fi
export PATH="$(dirname "$DOCKER_BIN"):$PATH"

echo "== stage build context =="
rm -rf "$SANDBOX_DIR/.build"
mkdir -p "$CONTEXT/tools"

rsync -a \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    "$PLATFORM_DIR/acceptor" \
    "$PLATFORM_DIR/kb" \
    "$PLATFORM_DIR/core" \
    "$PLATFORM_DIR/demo_repo" \
    "$CONTEXT/"
cp "$PLATFORM_DIR/tools/run_mech.py" "$CONTEXT/tools/run_mech.py"
cp "$PLATFORM_DIR/requirements.txt" "$CONTEXT/requirements.txt"

echo "-- staged top level --"
ls -1 "$CONTEXT"
echo "-- staged file count --"
find "$CONTEXT" -type f | wc -l

echo "== secret scan of build context =="
matches="$(
    find "$CONTEXT" \
        \( -name '.env' -o -name '.env.*' \
           -o -name 'model_config*.json' -o -name 'auth.json' \
           -o -name 'service.json' -o -name '*.pem' \
           -o -name '*.key' -o -name 'id_rsa*' -o -name 'id_ed25519*' \
           -o -name '.venv' \) \
        -print
)"
if [ -n "$matches" ]; then
    echo "$matches"
    echo "SECRET_SCAN_FAILED" >&2
    exit 1
fi
echo "SECRET_SCAN_CLEAN"

echo "== base image =="
if "$DOCKER_BIN" image inspect python:3.12-slim >/dev/null 2>&1; then
    echo "python:3.12-slim already present locally"
else
    "$DOCKER_BIN" pull python:3.12-slim
fi
python_image_id="$("$DOCKER_BIN" image inspect --format '{{.Id}}' python:3.12-slim)"
echo "base_image_id=${python_image_id}"

echo "== build =="
"$DOCKER_BIN" build --pull=false -t "$IMAGE" -f "$SANDBOX_DIR/Dockerfile" "$SANDBOX_DIR"
image_id="$("$DOCKER_BIN" image inspect --format '{{.Id}}' "$IMAGE")"
echo "image=${IMAGE}"
echo "image_id=${image_id}"
