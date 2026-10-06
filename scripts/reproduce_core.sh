#!/usr/bin/env bash
# Reproduce the core results of the paper (Sections 5.1–5.7) from THIS snapshot
# alone, without the sibling repositories.
#
# Why this script exists (2026-10-06): `nowcast-core` has no path dependencies,
# but Cargo loads EVERY workspace member before building any of them, and the
# provider crates point to sibling engines outside the snapshot
# (../snowmelt-rs, ../surtgis, …). So inside the archived snapshot even
# `cargo build -p nowcast-core` fails with "failed to load manifest for
# workspace member". This script builds a throwaway workspace that contains
# only the core — the same root Cargo.toml (same edition, versions, profile)
# with its member list reduced to `crates/nowcast-core` — and runs the tests and
# the paper's examples there. The snapshot's sources are not touched; two
# examples (backtest_distributed, synthetic_resolution) write the CSVs behind the
# figures into data/, as they do in a normal checkout.
#
# Usage:
#   scripts/reproduce_core.sh            # tests + every paper example
#   scripts/reproduce_core.sh --tests    # tests only
#
# The examples read the derived input CSVs from data/ (not versioned; regenerate
# them with the extraction scripts in scripts/, see the README).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/nowcast-core-XXXXXX")
OUT="$WORK/outputs"
mkdir -p "$WORK/crates" "$OUT"
cp -r "$ROOT/crates/nowcast-core" "$WORK/crates/"
[[ -f "$ROOT/Cargo.lock" ]] && cp "$ROOT/Cargo.lock" "$WORK/"
# examples resolve data as $CARGO_MANIFEST_DIR/../../data
ln -s "$ROOT/data" "$WORK/data"

python3 - "$ROOT/Cargo.toml" "$WORK/Cargo.toml" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
one = 'members = [\n    "crates/nowcast-core",\n]'
s, n1 = re.subn(r"(?ms)^members\s*=\s*\[.*?\]", one, s, count=1)
s, n2 = re.subn(r"(?ms)^default-members\s*=\s*\[.*?\]",
                'default-members = [\n    "crates/nowcast-core",\n]', s, count=1)
if n1 != 1:
    sys.exit("reproduce_core: could not find the workspace member list in Cargo.toml")
open(dst, "w").write(s)
PY

cd "$WORK"
echo "[reproduce] core-only workspace: $WORK"
cargo test --release -p nowcast-core
[[ "${1:-}" == "--tests" ]] && { echo "[reproduce] tests OK"; exit 0; }

if [[ ! -d "$ROOT/data" ]] || ! compgen -G "$ROOT/data/*.csv" >/dev/null; then
    echo "[reproduce] data/ has no derived CSVs: regenerate them with scripts/extract_*.py" >&2
    exit 1
fi

# One example per result in Sections 5.1–5.7 (see the paper's Code availability).
EXAMPLES=(
    backtest                  # 5.1 lumped Maipo backtest
    published_thresholds      # 5.1 35 published I–D curves
    backtest_distributed      # 5.2 distributed daily backtest (honest null)
    null_diagnosis            # 5.2 null diagnostics
    synthetic_resolution      # 5.3 controlled resolution experiment
    resolution_headtohead     # 5.4 head-to-head
    multi_event_leadtime      # 5.5 multi-event sub-daily lead time
    single_factor_resolution  # 5.6 product vs resolution
    intercept_sensitivity     # 5.7 intercept sensitivity
)
cargo build --release -p nowcast-core --examples
for ex in "${EXAMPLES[@]}"; do
    echo "[reproduce] $ex"
    cargo run --release -q -p nowcast-core --example "$ex" > "$OUT/$ex.txt"
done
echo "[reproduce] done — outputs in $OUT"
