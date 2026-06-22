#!/usr/bin/env bash
# test-setup.sh — environment bootstrap for the verifier.
# Sourced (not exec'd) by the harbor verifier runner so env vars and
# anything else stay alive for vitest.
#
# This script is idempotent. The Dockerfile already pre-installs pnpm
# deps and pre-builds @better-auth/core; we re-run those steps as
# guards in case an agent removed node_modules / dist along the way.

set -e

REPO_DIR="${REPO_DIR:-/repo/better-auth}"
cd "$REPO_DIR"

# --------------------------------------------------------------- #
# 1. Sanity: Node, pnpm, and the workspace are on disk.            #
# --------------------------------------------------------------- #
node --version
pnpm --version

# --------------------------------------------------------------- #
# 2. Install workspace deps if missing. The Dockerfile baked them  #
#    in already; this is a no-op offline replay otherwise.         #
# --------------------------------------------------------------- #
if [ ! -d "node_modules/.pnpm" ]; then
    echo "[test-setup] pnpm install (offline, frozen)..."
    pnpm install --frozen-lockfile --ignore-scripts --offline 2>&1 | tail -10
fi

# --------------------------------------------------------------- #
# 3. Ensure every workspace package's dist/ exists. Vitest 4.x     #
#    requires every workspace dep to have a dist or a working      #
#    dev-source export; @better-auth/core/error specifically has   #
#    no dev-source export (see repo-notes). Build is fully cached  #
#    after the first run, so the steady-state cost is ~1s per      #
#    package via turbo. Only do a full rebuild if any dist is      #
#    missing.                                                      #
# --------------------------------------------------------------- #
need_build=0
for pkg_json in packages/*/package.json; do
    pkg_dir=$(dirname "$pkg_json")
    if [ ! -d "$pkg_dir/dist" ] && [ -f "$pkg_dir/tsconfig.json" ]; then
        need_build=1
        break
    fi
done
if [ "$need_build" = "1" ]; then
    echo "[test-setup] building all workspace packages..."
    pnpm build 2>&1 | tail -3
fi

# --------------------------------------------------------------- #
# 4. Rebuild better-sqlite3 native binding if missing. The         #
#    Dockerfile pre-rebuilds it but an agent's pnpm install can    #
#    erase the binary.                                             #
# --------------------------------------------------------------- #
if [ ! -f "node_modules/.pnpm/better-sqlite3"*"/node_modules/better-sqlite3/build/Release/better_sqlite3.node" ]; then
    echo "[test-setup] rebuilding better-sqlite3..."
    pnpm --filter better-auth rebuild better-sqlite3 2>&1 | tail -3 || true
fi

# Make pnpm bin and node_modules/.bin available
export PATH="$REPO_DIR/node_modules/.bin:$PATH"

echo "[test-setup] ready: $REPO_DIR (node=$(node --version), pnpm=$(pnpm --version))"
