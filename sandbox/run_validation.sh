#!/usr/bin/env bash
# SANDBOX-VAL end-to-end validation on the host.
#   1. hash the host demo_repo before anything runs
#   2. build the image (copy-only, no host mounts)
#   3. run the scripted chain twice: default network, then --network none
#   4. run the out-of-bounds-write and secret-visibility probes
#   5. hash the host demo_repo again and diff
set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_DIR="$(dirname "$SANDBOX_DIR")"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
IMAGE="${IMAGE:-sandbox-val:local}"
LOG_DIR="$SANDBOX_DIR/logs"
TMPFS_OPTS="rw,exec,size=1g,mode=0755,uid=10001,gid=10001"
DEV_SHM_OPTS="rw,noexec,nosuid,size=64m,mode=0555"
DEV_MQUEUE_OPTS="rw,noexec,nosuid,size=64m,mode=0555"
CONTAINER_FLAGS=(
    --read-only
    --tmpfs "/dev/shm:$DEV_SHM_OPTS"
    --tmpfs "/dev/mqueue:$DEV_MQUEUE_OPTS"
    --tmpfs "/work/platform:$TMPFS_OPTS"
)
HOST_DEMO="$PLATFORM_DIR/demo_repo"

if [ ! -x "$DOCKER_BIN" ]; then
    DOCKER_BIN="$(command -v docker)"
fi
export PATH="$(dirname "$DOCKER_BIN"):$PATH"

rm -rf "$LOG_DIR"
mkdir -p "$LOG_DIR"

hash_tree() {
    (
        cd "$1"
        find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256
    )
}

echo "== host demo_repo manifest BEFORE ==" | tee "$LOG_DIR/hash_before.txt"
hash_tree "$HOST_DEMO" | tee "$LOG_DIR/demo_repo.before.sha256"
echo "host_demo_repo_file_count=$(find "$HOST_DEMO" -type f | wc -l | tr -d ' ')" \
    | tee -a "$LOG_DIR/hash_before.txt"
echo "manifest_sha256=$(shasum -a 256 "$LOG_DIR/demo_repo.before.sha256" | awk '{print $1}')" \
    | tee -a "$LOG_DIR/hash_before.txt"

echo "== build image ==" | tee "$LOG_DIR/build.log"
"$SANDBOX_DIR/build_image.sh" >>"$LOG_DIR/build.log" 2>&1
tee -a "$LOG_DIR/build.log" <<'EOF'
BUILD_OK
EOF

"$DOCKER_BIN" image inspect --format '{{json .Config.Env}}' "$IMAGE" \
    >"$LOG_DIR/image_env.json"
echo "image_env=$(cat "$LOG_DIR/image_env.json")"

echo "== container shape (no host mounts) =="
cid="$("$DOCKER_BIN" create "${CONTAINER_FLAGS[@]}" "$IMAGE" chain)"
{
    echo "user=$("$DOCKER_BIN" inspect --format '{{.Config.User}}' "$cid")"
    echo "mounts=$("$DOCKER_BIN" inspect --format '{{json .Mounts}}' "$cid")"
    echo "tmpfs=$("$DOCKER_BIN" inspect --format '{{json .HostConfig.Tmpfs}}' "$cid")"
} | tee "$LOG_DIR/container_shape.txt"
"$DOCKER_BIN" rm "$cid" >/dev/null

echo "== run 1: scripted chain, default network ==" | tee "$LOG_DIR/chain.log"
"$DOCKER_BIN" run --rm "${CONTAINER_FLAGS[@]}" \
    "$IMAGE" chain 2>&1 | tee -a "$LOG_DIR/chain.log"

echo "== run 2: scripted chain, --network none ==" | tee "$LOG_DIR/chain_network_none.log"
"$DOCKER_BIN" run --rm --network none "${CONTAINER_FLAGS[@]}" \
    -e SANDBOX_NET_PROBE=1 \
    "$IMAGE" chain 2>&1 | tee -a "$LOG_DIR/chain_network_none.log"

echo "== probe: out-of-bounds writes ==" | tee "$LOG_DIR/probe_write.log"
"$DOCKER_BIN" run --rm "${CONTAINER_FLAGS[@]}" \
    "$IMAGE" probe-write 2>&1 | tee -a "$LOG_DIR/probe_write.log"

echo "== probe: secret visibility ==" | tee "$LOG_DIR/probe_secrets.log"
"$DOCKER_BIN" run --rm "${CONTAINER_FLAGS[@]}" \
    "$IMAGE" probe-secrets 2>&1 | tee -a "$LOG_DIR/probe_secrets.log"

echo "== host demo_repo manifest AFTER ==" | tee "$LOG_DIR/hash_after.txt"
hash_tree "$HOST_DEMO" | tee "$LOG_DIR/demo_repo.after.sha256"
echo "host_demo_repo_file_count=$(find "$HOST_DEMO" -type f | wc -l | tr -d ' ')" \
    | tee -a "$LOG_DIR/hash_after.txt"
echo "manifest_sha256=$(shasum -a 256 "$LOG_DIR/demo_repo.after.sha256" | awk '{print $1}')" \
    | tee -a "$LOG_DIR/hash_after.txt"

echo "== host demo_repo diff ==" | tee "$LOG_DIR/demo_repo.hash.diff"
if diff -u "$LOG_DIR/demo_repo.before.sha256" \
            "$LOG_DIR/demo_repo.after.sha256" \
            | tee -a "$LOG_DIR/demo_repo.hash.diff"; then
    echo "HOST_DEMO_REPO_UNCHANGED" | tee -a "$LOG_DIR/demo_repo.hash.diff"
else
    echo "HOST_DEMO_REPO_CHANGED" | tee -a "$LOG_DIR/demo_repo.hash.diff"
    exit 1
fi

echo "SANDBOX_VAL_ALL_CHECKS_DONE"
