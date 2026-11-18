#!/bin/sh
# SANDBOX-VAL entrypoint.  Runs as the unprivileged "sandbox" user inside a
# container whose root filesystem is read-only; /work/platform is a tmpfs
# owned by that user and is the only writable path.
set -eu

MODE="${1:-chain}"

echo "[sandbox] mode=${MODE}"
echo "[sandbox] user=$(id -un) uid=$(id -u) gid=$(id -g)"
echo "[sandbox] kernel=$(uname -srm)"

echo "[sandbox] baked-in /opt baseline verification:"
cd /opt/platform
sha256sum -c /opt/platform.sha256 --quiet
echo "[sandbox] BASELINE_OK"

echo "[sandbox] mounts:"
awk '$2 == "/work/platform" {print}' /proc/mounts || true

run_chain() {
    echo "[sandbox] copying /opt/platform -> /work/platform"
    mkdir -p /work/platform/.sandbox-tmp /work/platform/home
    export TMPDIR=/work/platform/.sandbox-tmp
    export TMP="$TMPDIR"
    export TEMP="$TMPDIR"
    export HOME=/work/platform/home
    export PYTHONDONTWRITEBYTECODE=1
    cp -R /opt/platform/. /work/platform/
    chmod -R u+w /work/platform
    cd /work/platform

    if [ "${SANDBOX_NET_PROBE:-0}" = "1" ]; then
        echo "[sandbox] network interfaces:"
        cat /proc/net/dev
        python - <<'PY'
import socket

try:
    socket.create_connection(("1.1.1.1", 80), timeout=3)
except OSError as exc:
    print(f"NET_CONNECT_BLOCKED: {type(exc).__name__}: {exc}")
else:
    print("NET_CONNECT_OK (unexpected under --network none)")
PY
    fi

    echo "[sandbox] == tools/run_mech.py =="
    python tools/run_mech.py

    echo "[sandbox] == C arm scripted repair =="
    python sandbox/c_arm_once.py

    echo "[sandbox] baked-in /opt re-verification after run:"
    cd /opt/platform
    sha256sum -c /opt/platform.sha256 --quiet
    echo "[sandbox] BASELINE_AFTER_RUN_OK"

    echo "[sandbox] workspace changes vs baked-in baseline:"
    (
        cd /opt/platform
        find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
    ) > /work/platform/.sandbox-tmp/opt.sha256
    (
        cd /work/platform
        find . -type f ! -path './.sandbox-tmp/*' -print0 \
            | LC_ALL=C sort -z | xargs -0 sha256sum
    ) > /work/platform/.sandbox-tmp/work.sha256
    if diff -u /work/platform/.sandbox-tmp/opt.sha256 \
                /work/platform/.sandbox-tmp/work.sha256; then
        echo "[sandbox] (no diff)"
    fi
    echo "[sandbox] files created under runs/:"
    find /work/platform/runs -maxdepth 3 -type f -print 2>/dev/null || true
    echo "[sandbox] runs/mech_summary.json:"
    cat /work/platform/runs/mech_summary.json 2>/dev/null || true
}

probe_write() {
    echo "[sandbox] write probes; only /work/platform is expected to accept writes"
    status=0
    for path in \
        /work/outside.txt \
        /work/platform-evil.txt \
        /opt/platform/PWNED \
        /etc/PWNED \
        /home/sandbox/PWNED \
        /root/PWNED \
        /tmp/PWNED \
        /var/tmp/PWNED \
        /run/PWNED \
        /dev/PWNED \
        /dev/shm/PWNED \
        /dev/mqueue/PWNED \
        /Users/hostuser/Desktop/grok-bot/platform/PWNED
    do
        if msg=$(touch "$path" 2>&1); then
            echo "[sandbox] WRITE_SUCCEEDED_UNEXPECTED ${path}"
            status=1
        else
            echo "[sandbox] WRITE_DENIED ${path} :: ${msg}"
        fi
    done
    if msg=$(touch /work/platform/.probe-write-ok 2>&1); then
        echo "[sandbox] WORKSPACE_WRITE_OK /work/platform/.probe-write-ok"
    else
        echo "[sandbox] WORKSPACE_WRITE_FAILED_UNEXPECTED :: ${msg}"
        status=1
    fi
    if [ -d /Users ]; then
        echo "[sandbox] HOST_PATH_VISIBLE_UNEXPECTED /Users"
        status=1
    else
        echo "[sandbox] HOST_PATH_ABSENT /Users"
    fi
    return "$status"
}

probe_secrets() {
    echo "[sandbox] secret visibility probes; all are expected absent"
    status=0
    for path in \
        /root/.ssh/id_rsa \
        /root/.ssh/id_ed25519 \
        /home/sandbox/.ssh/id_rsa \
        /home/sandbox/.ssh/id_ed25519 \
        /opt/platform/.env.local \
        /opt/platform/model_config.json \
        /work/platform/.env.local \
        /work/platform/model_config.json \
        /Users/hostuser/.ssh/id_rsa \
        /Users/hostuser/.ssh/id_ed25519
    do
        if [ -e "$path" ] || [ -L "$path" ]; then
            echo "[sandbox] VISIBLE_UNEXPECTED ${path}"
            status=1
        else
            echo "[sandbox] ABSENT ${path}"
        fi
    done

    echo "[sandbox] secret-like names under /opt/platform, /home/sandbox, /root, /work/platform:"
    found=$(
        find /opt/platform /home/sandbox /root /work/platform \
            \( -name '.env' -o -name '.env.*' \
               -o -name 'model_config*.json' -o -name 'auth.json' \
               -o -name 'service.json' -o -name 'id_rsa*' \
               -o -name 'id_ed25519*' -o -name '*.pem' \) \
            -print 2>/dev/null || true
    )
    if [ -n "$found" ]; then
        echo "$found"
        echo "[sandbox] SECRET_FILES_FOUND_UNEXPECTED"
        status=1
    else
        echo "[sandbox] (none)"
    fi

    echo "[sandbox] secret-like environment variables:"
    if env | grep -Eiq '(api[_-]?key|token|secret|password|credential)'; then
        env | grep -Ei '(api[_-]?key|token|secret|password|credential)'
        echo "[sandbox] SECRET_ENV_FOUND_UNEXPECTED"
        status=1
    else
        echo "[sandbox] (none)"
    fi
    return "$status"
}

case "$MODE" in
    chain) run_chain ;;
    probe-write) probe_write ;;
    probe-secrets) probe_secrets ;;
    *)
        echo "[sandbox] unknown mode: $MODE" >&2
        exit 2
        ;;
esac
