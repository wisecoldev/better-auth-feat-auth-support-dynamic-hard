#!/usr/bin/env bash
# validation-setup.sh — environment bootstrap for the validation agent's
# story scripts. Runs AFTER tests/test-setup.sh and BEFORE the validation
# orchestrator dispatches CC. Idempotent.
#
# The Dockerfile pre-installs pnpm deps and pre-builds every workspace
# package (including @better-auth/core, whose `/error` subpath has no
# dev-source export and is required by every getTestInstance() consumer).
# This script is a guard against agent edits that may have erased
# node_modules or dist directories.

set -e

REPO_DIR="${REPO_DIR:-/repo/better-auth}"
cd "$REPO_DIR"

# Sanity: Node + pnpm + vitest are reachable.
node --version
pnpm --version
npx vitest --version

# Re-install workspace deps if missing (offline replay).
if [ ! -d "node_modules/.pnpm" ]; then
    echo "[validation-setup] re-installing pnpm deps..."
    pnpm install --frozen-lockfile --ignore-scripts --offline 2>&1 | tail -10
fi

# Re-build every workspace package's dist/ if any are missing. From repo
# notes: vitest 4.x + vite 7.x doesn't reliably resolve workspace deps
# from src/ via the root config's `dev-source` condition, so dist/ must
# be present.
need_build=0
for pkg_json in packages/*/package.json; do
    pkg_dir=$(dirname "$pkg_json")
    if [ ! -d "$pkg_dir/dist" ] && [ -f "$pkg_dir/tsconfig.json" ]; then
        need_build=1
        break
    fi
done
if [ "$need_build" = "1" ]; then
    echo "[validation-setup] rebuilding workspace packages..."
    pnpm build 2>&1 | tail -3
fi

# Smoke-import: verify that the key test utilities are importable via their
# relative paths (the validation test files use relative imports, not the
# package name, so we test those same paths).
node -e '
const path = require("path");
const repoDir = process.env.REPO_DIR || "/repo/better-auth";
// The jest driver places test files 2 levels under packages/better-auth/src/
// (in packages/better-auth/src/auth/__validation__/) so relative imports
// resolve like: ../../test-utils/test-instance, ../../client. Verify the
// compiled dist is present.
const testUtilsDist = path.join(repoDir, "packages/better-auth/dist/test-utils/test-instance.js");
const clientDist = path.join(repoDir, "packages/better-auth/dist/client.js");
const fs = require("fs");
const missing = [testUtilsDist, clientDist].filter(p => !fs.existsSync(p));
if (missing.length > 0) {
  console.error("[validation-setup] Missing dist files:", missing.join(", "));
  process.exit(1);
}
console.log("[validation-setup] dist files OK (test-utils, client)");
'

echo "[validation-setup] ready: $REPO_DIR"
