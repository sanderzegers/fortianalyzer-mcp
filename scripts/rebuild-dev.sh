#!/usr/bin/env bash
# rebuild-dev.sh — rebuild the local dev branch from upstream/main
#
# Usage:
#   ./scripts/rebuild-dev.sh           # rebuild dev only
#   ./scripts/rebuild-dev.sh --docker  # rebuild dev, then rebuild + restart Docker container
#
# Does NOT push dev to GitHub.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

RESOLVER="scripts/_resolve_conflicts.py"

# Ordered list of feature branches to merge into dev.
BRANCHES=(
    "feat/compact-responses"
    "feat/get-tool-schema"
    "feat/multi-value-filters"
    "feat/log-search-optimization"
    "feat/utm-log-search"
    "feat/utm-log-summarize"
    "docs/agent-system-prompt"
)

REBUILD_DOCKER=false
for arg in "$@"; do
    [[ "$arg" == "--docker" ]] && REBUILD_DOCKER=true
done

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "→ $*"; }

# --- Safety checks ---
if [[ -n "$(git status --porcelain | grep -v '^??')" ]]; then
    die "Uncommitted changes — stash or commit first (untracked files are fine)"
fi
for b in "${BRANCHES[@]}"; do
    git rev-parse --verify "$b" >/dev/null 2>&1 || die "Branch not found: $b"
done

# --- Reset dev ---
BASE="$(git rev-parse --short upstream/main)"
info "Resetting dev to upstream/main ($BASE)"

# Preserve untracked scripts/ across the branch delete+recreate.
_SCRIPTS_BACKUP="$(mktemp -d)"
cp -r scripts/. "$_SCRIPTS_BACKUP/"

git checkout -q main
git branch -D dev 2>/dev/null || true
git checkout -q -b dev upstream/main

# Restore scripts/.
mkdir -p scripts
cp -r "$_SCRIPTS_BACKUP/." scripts/
rm -rf "$_SCRIPTS_BACKUP"

# --- Merge each branch ---
for branch in "${BRANCHES[@]}"; do
    info "Merging $branch..."
    if git merge --no-edit --quiet "$branch" 2>/dev/null; then
        echo "    clean"
        continue
    fi

    conflicts=$(git diff --name-only --diff-filter=U)
    for f in $conflicts; do
        echo "    conflict: $f"
        case "$f" in
            src/fortianalyzer_mcp/server.py)
                python3 "$RESOLVER" server "$f" \
                    || die "Could not auto-resolve $f — add a resolver or fix manually"
                git add "$f"
                ;;
            src/fortianalyzer_mcp/tools/log_tools.py)
                python3 "$RESOLVER" log_tools "$f" \
                    || die "Could not auto-resolve $f — add a resolver or fix manually"
                git add "$f"
                ;;
            *)
                die "Unexpected conflict in $f — add a resolver to $RESOLVER"
                ;;
        esac
    done
    GIT_EDITOR=true git merge --continue
    echo "    resolved"
done

# --- Syntax check ---
echo ""
info "Syntax checks..."
python3 -c "
import ast, sys
files = [
    'src/fortianalyzer_mcp/tools/log_tools.py',
    'src/fortianalyzer_mcp/server.py',
]
ok = True
for f in files:
    try:
        ast.parse(open(f).read())
        print(f'    {f}  OK')
    except SyntaxError as e:
        print(f'    {f}  FAIL: {e}', file=sys.stderr)
        ok = False
sys.exit(0 if ok else 1)
"

# --- Summary ---
echo ""
info "dev rebuilt from upstream/main ($BASE)"
echo "    merged: ${BRANCHES[*]}"

# --- Optional Docker rebuild ---
if $REBUILD_DOCKER; then
    echo ""
    info "Rebuilding Docker container..."
    cd /home/zegerssa/LibreChat
    docker compose build fortianalyzer-mcp-GemueGlobal
    docker compose up -d fortianalyzer-mcp-GemueGlobal
    info "Container restarted"
    echo ""
    echo "Remember to paste the updated agent prompt into LibreChat if prompts/fortianalyzer_agent.md changed."
fi
