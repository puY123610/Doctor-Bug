#!/bin/bash
# Doctor Bug cleanroom gate （重制计划第五节）: one command that proves the
# shipped packages work in a fresh user environment.
#
#   bash scripts/cleanroom.sh
#
# Two passes, each in a throwaway directory with a contained npm prefix
# and a brand-new HOME:
#   pass 1 (plain):   first run bootstraps the Python environment, then
#                     scripted setup, health check, tree reconciliation,
#                     and the three pilots (gates / resize / commands).
#   pass 2 (bundled): only when npm/dist-bundled has a bundled tarball —
#                     same checks, but the run must NOT create ~/.drbug
#                     (the environment ships inside the package).
#
# Red anywhere = not done, not publishable.  Green ends with
# CLEANROOM_PASS.
set -u
fail() { echo "CLEANROOM_FAIL: $1"; exit 1; }

PLATFORM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBES="$PLATFORM_DIR/scripts/cleanroom"
command -v npm >/dev/null || fail "npm not on PATH"
command -v node >/dev/null || fail "node not on PATH"

echo "== pack (plain)"
(cd "$PLATFORM_DIR/npm" && npm pack >/dev/null 2>&1) || fail "npm pack"
PLAIN_TARBALL="$PLATFORM_DIR/npm/drbug-0.1.0.tgz"
[ -f "$PLAIN_TARBALL" ] || fail "plain tarball missing"
BUNDLED_TARBALL="$(ls "$PLATFORM_DIR"/npm/dist-bundled/drbug-bundled-*.tgz 2>/dev/null | head -1 || true)"

run_pass() {
  local label="$1" tarball="$2" expect_venv="$3"
  local root home prefix bin installed py
  root="$(mktemp -d)"
  home="$root/home"
  prefix="$root/prefix"
  mkdir -p "$home"
  echo "== pass $label: $root"

  HOME="$home" npm install -g --prefix "$prefix" "$tarball" >/dev/null 2>&1 \
    || fail "$label: npm install"
  bin="$prefix/bin/drbug"
  [ -x "$bin" ] || fail "$label: drbug binary missing"
  installed="$prefix/lib/node_modules/drbug/platform"
  [ -d "$installed" ] || fail "$label: installed platform missing"

  HOME="$home" DRBUG_SETUP_PROVIDER=deepseek DRBUG_SETUP_API_KEY=sk-cleanroom \
    "$bin" setup --scripted >"$root/setup.log" 2>&1 || fail "$label: setup --scripted"
  grep -q "配置完成" "$root/setup.log" || fail "$label: setup output"

  HOME="$home" "$bin" check >"$root/check.log" 2>&1 || fail "$label: drbug check"
  grep -q "已设置" "$root/check.log" || fail "$label: check key"
  grep -q "通过" "$root/check.log" || fail "$label: check verdict"

  if [ "$expect_venv" = "yes" ]; then
    [ -x "$home/.drbug/venv/bin/python" ] || fail "$label: bootstrap venv missing"
    py="$home/.drbug/venv/bin/python"
    export PYTHONPATH="$installed"
  else
    [ ! -e "$home/.drbug" ] || fail "$label: bundled run created ~/.drbug"
    py="$installed/runtime/python/bin/python3"
    [ -x "$py" ] || fail "$label: bundled python missing"
    export PYTHONPATH="$installed/runtime/site-packages:$installed"
  fi

  HOME="$home" "$py" "$PROBES/probe_tree.py" "$installed" \
    || fail "$label: tree probe"
  HOME="$home" "$py" "$PROBES/probe_setup_gates.py" >"$root/gates.log" 2>&1 \
    || fail "$label: gates pilot"
  grep -q "AGREE_PATH_OK" "$root/gates.log" || fail "$label: gates agree"
  grep -q "DECLINE_PATH_OK" "$root/gates.log" || fail "$label: gates decline"
  HOME="$home" "$py" "$PROBES/probe_resize.py" >"$root/resize.log" 2>&1 \
    || fail "$label: resize pilot"
  grep -q "REFLOWED: True" "$root/resize.log" || fail "$label: resize grow"
  grep -q "SHRUNK_OK: True" "$root/resize.log" || fail "$label: resize shrink"
  HOME="$home" "$py" "$PROBES/probe_commands.py" >"$root/commands.log" 2>&1 \
    || fail "$label: commands pilot"
  grep -q "COMMAND_ACCEPTANCE: PASS" "$root/commands.log" \
    || fail "$label: commands acceptance"
  echo "== pass $label OK"
}

run_pass plain "$PLAIN_TARBALL" yes
if [ -n "$BUNDLED_TARBALL" ]; then
  run_pass bundled "$BUNDLED_TARBALL" no
else
  echo "== no bundled tarball found, bundled pass skipped"
fi
echo "CLEANROOM_PASS"
