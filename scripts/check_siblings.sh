#!/usr/bin/env bash
# Verify that the sibling engine repositories the adapter crates depend on
# (path dependencies) are checked out where the workspace expects them.
# See "Building" in README.md. Exits non-zero listing anything missing.
set -u

root="$(cd "$(dirname "$0")/.." && pwd)"
parent="$(dirname "$root")"

# adapter crate → sibling path required by its Cargo.toml (relative to parent)
declare -A siblings=(
    [nowcast-rainflow]="rainflow/crates/rainflow-core"
    [nowcast-snowmelt]="snowmelt-rs/crates/snowmelt-core"
    [nowcast-surtgis]="surtgis/crates/core"
    [nowcast-swarm]="swarm-abm/crates/swarm-abm"
    [nowcast-insar]="insar-rs/crates/core"
    [nowcast-firespread]="firespread/crates/firespread-core"
    [nowcast-hydroflux]="postdoc/hydroflux/solver-2d"
)

missing=0
for crate in "${!siblings[@]}"; do
    path="$parent/${siblings[$crate]}"
    if [ -f "$path/Cargo.toml" ]; then
        echo "ok      $crate → $path"
    else
        echo "MISSING $crate → $path"
        missing=$((missing + 1))
    fi
done

if [ "$missing" -gt 0 ]; then
    echo
    echo "$missing sibling(s) missing: the full workspace will not build."
    echo "The core alone always works: cargo test -p nowcast-core"
    exit 1
fi
echo
echo "All siblings present: the full workspace should build (cargo test)."
