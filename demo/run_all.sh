#!/usr/bin/env bash
# One command that produces every frame the demo video needs.
#
#   ./run_all.sh offline           # tests + offline chain (no key, ~2 s)
#   ./run_all.sh live              # + a real Lux3D task, bench, receipt (~5 min)
#   ./run_all.sh live crate_live   # same, custom asset name
#
# Logs land in demo/logs/, artifacts in lux3d-embodied-bridge/out/live/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE="$ROOT"
ENGINE="${ORIERA_ENGINE:-$ROOT/engine}"
MODE="${1:-offline}"
NAME="${2:-crate_live}"
OUT="$BRIDGE/out/live"
LOGS="$ROOT/demo/logs"
PROMPT="a small wooden shipping crate with metal corner brackets, single object, centered"

export PATH="$HOME/.cargo/bin:$PATH"
mkdir -p "$LOGS" "$OUT"

section() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

cd "$BRIDGE"

section "1/5  unit tests"
python3 -m unittest discover -s tests -v 2>&1 | tee "$LOGS/01_tests.log" | tail -4

section "2/5  offline chain (fixture GLB -> STL -> URDF -> bench command)"
python3 bridge.py demo 2>&1 | tee "$LOGS/02_demo.log" | tail -6

if [ "$MODE" = "offline" ]; then
  section "done (offline)"
  exit 0
fi

section "3/5  real Lux3D task: text-to-3D -> poll -> download -> ingest"
python3 bridge.py generate \
  --prompt "$PROMPT" \
  --version G1-Turbo --style photorealistic --face-count 20000 --format glb \
  --name "$NAME" --out "$OUT/assets" --mesh-dir "$OUT/meshes" \
  2>&1 | tee "$LOGS/03_generate.log"

section "4/5  inject the collision mesh into the URDF link the bench collides with"
python3 bridge.py urdf fixtures/desk_arm.urdf --out "$OUT/urdf/desk_arm.urdf" \
  --manifest "$OUT/meshes/assets.lock.json" --asset "$NAME" --link tool \
  2>&1 | tee "$LOGS/04_urdf.log"
grep -nE 'oriera:asset|package://' "$OUT/urdf/desk_arm.urdf" | tee "$LOGS/04_urdf_grep.log"

section "5/5  bench: two episodes, per-episode fingerprints, determinism re-run"
python3 bridge.py bench --engine "$ENGINE" --out "$OUT" --robot-name desk_arm \
  --package generated --episodes 2 --run 2>&1 | tee "$LOGS/05_bench.log"

section "receipts"
ls -la "$OUT"/*.json 2>/dev/null || true
python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
for name in ("robot.json", "fingerprints.v2.json"):
    path = os.path.join(out, name)
    if os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
        keys = list(data)[:6]
        print(f"{name}: {os.path.getsize(path)} bytes, top-level keys {keys}")
PY

section "done (live) — artifacts in $OUT"
