#!/bin/bash
# Build the bundled drbug tarball for this machine's platform:
# the npm package plus a ready-made Python runtime and preinstalled
# libraries under platform/runtime, so the first run needs no setup.
#
#   bash npm/build_bundled.sh            # from the platform root
#
# Output: npm/dist-bundled/drbug-bundled-<version>-<platform>-<arch>.tgz
# The plain tarball (npm/drbug-<version>.tgz) is rebuilt afterwards so
# the working tree is left exactly as a plain pack leaves it.
set -eu

NPM_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$(cd "$NPM_DIR/.." && pwd)"
VERSION="$(node -p "require('$NPM_DIR/package.json').version")"
KEY="$(uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/darwin/')-$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/')"
case "$KEY" in
  darwin-arm64)
    ASSET="cpython-3.12.16+20261110-aarch64-apple-darwin-install_only.tar.gz"
    SHA256="e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f"
    ;;
  *)
    echo "build_bundled: no pinned runtime asset for $KEY" >&2
    exit 1
    ;;
esac
BASE="https://github.com/astral-sh/python-build-standalone/releases/download/20261110"

CACHE="$NPM_DIR/.runtime-cache"
STAGE="$CACHE/stage-$KEY"
mkdir -p "$CACHE" "$NPM_DIR/dist-bundled"

if [ ! -f "$CACHE/$ASSET" ]; then
  echo "== download pinned python ($ASSET)"
  curl -fL --retry 3 -o "$CACHE/$ASSET" "$BASE/$ASSET"
fi
echo "$SHA256  $CACHE/$ASSET" | shasum -a 256 -c - >/dev/null
echo "== runtime archive sha256 ok"

rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "$CACHE/$ASSET" -C "$STAGE"
PY="$STAGE/python/bin/python3"
[ -x "$PY" ] || { echo "build_bundled: staged python missing" >&2; exit 1; }

echo "== preinstall libraries into site-packages"
"$PY" -m pip install --disable-pip-version-check --no-input \
  --target "$STAGE/site-packages" \
  -r "$PLATFORM_DIR/requirements.txt" >/dev/null
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

DIGEST="$(node -e '
  const crypto = require("crypto");
  const fs = require("fs");
  const req = fs.readFileSync(process.argv[1]);
  const version = process.argv[2];
  console.log(crypto.createHash("sha256").update(req).update(`\ndrbug=${version}\n`).digest("hex"));
' "$PLATFORM_DIR/requirements.txt" "$VERSION")"

echo "== plain pack (assembles npm/platform)"
(cd "$NPM_DIR" && npm pack >/dev/null)

echo "== inject runtime"
rm -rf "$NPM_DIR/platform/runtime"
mkdir -p "$NPM_DIR/platform/runtime"
cp -R "$STAGE/python" "$NPM_DIR/platform/runtime/python"
cp -R "$STAGE/site-packages" "$NPM_DIR/platform/runtime/site-packages"
# npm pack silently drops symlinks (bin/python3 -> python3.12 vanished
# in the first bundled build, and the launcher fell back to bootstrap).
# Materialize every symlink in the runtime as a real file.
find "$NPM_DIR/platform/runtime" -type l | while read -r link; do
  resolved="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$link")"
  if [ -f "$resolved" ]; then
    rm "$link"
    cp "$resolved" "$link"
  else
    echo "WARN: symlink target not a file, left as-is: $link"
  fi
done
[ -f "$NPM_DIR/platform/runtime/python/bin/python3" ] || {
  echo "build_bundled: runtime python3 missing after materialize" >&2
  exit 1
}
cat > "$NPM_DIR/platform/runtime/bundled.json" <<EOF
{"platform": "$KEY", "requirements_sha256": "$DIGEST", "python": "3.12.16"}
EOF

echo "== bundled pack"
(cd "$NPM_DIR" && npm pack --ignore-scripts >/dev/null)
OUT="$NPM_DIR/dist-bundled/drbug-bundled-$VERSION-$KEY.tgz"
mv "$NPM_DIR/drbug-$VERSION.tgz" "$OUT"

echo "== restore plain tree"
rm -rf "$NPM_DIR/platform/runtime"
(cd "$NPM_DIR" && npm pack >/dev/null)

echo "BUNDLED_OK $OUT"
du -h "$OUT" | cut -f1
