#!/usr/bin/env bash
set -euo pipefail

# Build a distributable Quaid release package.
# Usage: ./build-release.sh [--output-dir DIR] [--skip-tests] [--skip-sanitize]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/quaid"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
OUTPUT_DIR="$BUILD_DIR/quaid-release"
ARCHIVE_NAME="quaid-$(date +%Y%m%d).tar.gz"
SKIP_TESTS=false
SKIP_SANITIZE=false

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[build]${NC} $*"; }
ok()    { echo -e "${GREEN}[build]${NC} $*"; }
warn()  { echo -e "${YELLOW}[build]${NC} $*"; }
error() { echo -e "${RED}[build]${NC} $*" >&2; }

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --output-dir DIR    Output directory (default: build/quaid-release)"
    echo "  --archive-name NAME Archive filename (default: quaid-YYYYMMDD.tar.gz)"
    echo "  --skip-tests        Skip pytest validation"
    echo "  --skip-sanitize     Skip sanitization (copy raw files)"
    echo "  -h, --help          Show this help"
    exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --output-dir)    OUTPUT_DIR="$2"; shift 2 ;;
        --archive-name)  ARCHIVE_NAME="$2"; shift 2 ;;
        --skip-tests)    SKIP_TESTS=true; shift ;;
        --skip-sanitize) SKIP_SANITIZE=true; shift ;;
        -h|--help)       usage ;;
        *)               error "Unknown option: $1"; usage ;;
    esac
done

info "Building Quaid release package"
info "  Source: $PLUGIN_DIR"
info "  Output: $OUTPUT_DIR"
echo ""

# Step 1: Pre-flight checks
info "Step 1/6: Pre-flight checks"

if [[ ! -d "$PLUGIN_DIR" ]]; then
    error "Plugin directory not found: $PLUGIN_DIR"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    error "python3 not found"
    exit 1
fi

# Check for uncommitted changes in plugin dir
if git -C "$REPO_ROOT" diff --quiet "$PLUGIN_DIR" 2>/dev/null; then
    ok "  No uncommitted plugin changes"
else
    warn "  Uncommitted changes in plugin directory"
    warn "  Consider committing before building a release"
fi

ok "  Pre-flight passed"
echo ""

# Step 2: Run tests
if [[ "$SKIP_TESTS" == "false" ]]; then
    info "Step 2/6: Running tests"
    if (cd "$PLUGIN_DIR" && python3 -m pytest tests/ -x -q 2>&1 | tail -3); then
        ok "  Tests passed"
    else
        error "  Tests failed — aborting build"
        exit 1
    fi
else
    warn "Step 2/6: Tests SKIPPED (--skip-tests)"
fi
echo ""

# Step 3: Clean output directory
info "Step 3/6: Preparing output directory"
if [[ -d "$OUTPUT_DIR" ]]; then
    rm -rf "$OUTPUT_DIR"
    info "  Cleaned previous build"
fi
mkdir -p "$OUTPUT_DIR"
ok "  Output directory ready"
echo ""

# Step 4: Run sanitization or copy raw
if [[ "$SKIP_SANITIZE" == "false" ]]; then
    info "Step 4/6: Running sanitization"
    if [[ -f "$SCRIPT_DIR/sanitize-release.py" ]]; then
        # Sanitize the plugin directory into plugins/quaid/ inside output
        mkdir -p "$OUTPUT_DIR/plugins/quaid"
        python3 "$SCRIPT_DIR/sanitize-release.py" \
            --source-dir "$PLUGIN_DIR" \
            --output-dir "$OUTPUT_DIR/plugins/quaid" \
            --force
        ok "  Sanitization complete"
    else
        error "  sanitize-release.py not found"
        exit 1
    fi
else
    warn "Step 4/6: Sanitization SKIPPED (--skip-sanitize)"
    info "  Copying raw plugin files..."

    # Copy plugin Python files
    mkdir -p "$OUTPUT_DIR/plugins/quaid/lib" "$OUTPUT_DIR/plugins/quaid/prompts"
    for f in "$PLUGIN_DIR"/*.py; do
        [[ -f "$f" ]] && cp "$f" "$OUTPUT_DIR/plugins/quaid/"
    done
    for f in "$PLUGIN_DIR"/lib/*.py; do
        [[ -f "$f" ]] && cp "$f" "$OUTPUT_DIR/plugins/quaid/lib/"
    done
    # Copy prompts (txt and md)
    if [[ -d "$PLUGIN_DIR/prompts" ]]; then
        cp "$PLUGIN_DIR"/prompts/*.txt "$OUTPUT_DIR/plugins/quaid/prompts/" 2>/dev/null || true
        cp "$PLUGIN_DIR"/prompts/*.md "$OUTPUT_DIR/plugins/quaid/prompts/" 2>/dev/null || true
    fi
    # Copy TS/JS
    cp "$PLUGIN_DIR"/index.ts "$PLUGIN_DIR"/index.js "$OUTPUT_DIR/plugins/quaid/" 2>/dev/null || true
    cp "$PLUGIN_DIR"/logger.ts "$OUTPUT_DIR/plugins/quaid/" 2>/dev/null || true
    # Copy schema
    cp "$PLUGIN_DIR"/schema.sql "$OUTPUT_DIR/plugins/quaid/" 2>/dev/null || true

    ok "  Raw copy complete"
fi
echo ""

# Step 5: Add release templates
info "Step 5/6: Adding release templates"

# Copy release files to root of output
for f in README.md ROADMAP.md LICENSE package.json install.sh install.ps1 setup-quaid.mjs setup-quaid.sh quaid memory.json.example gateway-hooks.patch; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$OUTPUT_DIR/"
        info "  + $f"
    fi
done

# Copy docs directory
if [[ -d "$SCRIPT_DIR/docs" ]]; then
    mkdir -p "$OUTPUT_DIR/docs"
    cp "$SCRIPT_DIR"/docs/*.md "$OUTPUT_DIR/docs/" 2>/dev/null || true
    info "  + docs/ ($(ls "$OUTPUT_DIR/docs/" 2>/dev/null | wc -l | tr -d ' ') files)"
fi

# Make scripts executable
chmod +x "$OUTPUT_DIR/install.sh" 2>/dev/null || true
chmod +x "$OUTPUT_DIR/setup-quaid.mjs" 2>/dev/null || true
chmod +x "$OUTPUT_DIR/setup-quaid.sh" 2>/dev/null || true
chmod +x "$OUTPUT_DIR/quaid" 2>/dev/null || true

# Create config directory with example
mkdir -p "$OUTPUT_DIR/config"
if [[ -f "$OUTPUT_DIR/memory.json.example" ]]; then
    cp "$OUTPUT_DIR/memory.json.example" "$OUTPUT_DIR/config/memory.json.example"
fi

# Create empty directories that the system expects
mkdir -p "$OUTPUT_DIR/data"
mkdir -p "$OUTPUT_DIR/journal"
mkdir -p "$OUTPUT_DIR/logs"

ok "  Release templates added"
echo ""

# Step 6: Validation
info "Step 6/6: Validating release package"
ERRORS=0

# Check required files exist
REQUIRED_FILES=(
    "README.md"
    "LICENSE"
    "setup-quaid.mjs"
    "setup-quaid.sh"
    "quaid"
    "memory.json.example"
    "plugins/quaid/api.py"
    "plugins/quaid/memory_graph.py"
    "plugins/quaid/janitor.py"
    "plugins/quaid/config.py"
    "plugins/quaid/llm_clients.py"
    "plugins/quaid/schema.sql"
    "plugins/quaid/index.ts"
    "plugins/quaid/index.js"
    "plugins/quaid/lib/database.py"
    "plugins/quaid/lib/config.py"
    "plugins/quaid/lib/embeddings.py"
    "plugins/quaid/lib/similarity.py"
    "plugins/quaid/lib/tokens.py"
)

for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$OUTPUT_DIR/$f" ]]; then
        error "  MISSING: $f"
        ERRORS=$((ERRORS + 1))
    fi
done

# Check no personal data leaked
LEAK_CHECK=$(grep -rl "memory\.db" "$OUTPUT_DIR" --include="*.db" 2>/dev/null || true)
if [[ -n "$LEAK_CHECK" ]]; then
    error "  LEAK: memory.db found in release"
    ERRORS=$((ERRORS + 1))
fi

# Check no test directory shipped
if [[ -d "$OUTPUT_DIR/plugins/quaid/tests" ]]; then
    error "  LEAK: tests/ directory found in release"
    ERRORS=$((ERRORS + 1))
fi

# Check no __pycache__
PYCACHE_CHECK=$(find "$OUTPUT_DIR" -name "__pycache__" -type d 2>/dev/null || true)
if [[ -n "$PYCACHE_CHECK" ]]; then
    error "  LEAK: __pycache__ found in release"
    ERRORS=$((ERRORS + 1))
fi

# Check Python syntax
PY_FILES=$(find "$OUTPUT_DIR" -name "*.py" -type f 2>/dev/null)
PY_ERRORS=0
for f in $PY_FILES; do
    if ! python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null; then
        error "  SYNTAX ERROR: $f"
        PY_ERRORS=$((PY_ERRORS + 1))
    fi
done
if [[ $PY_ERRORS -eq 0 ]]; then
    ok "  All Python files have valid syntax"
fi
ERRORS=$((ERRORS + PY_ERRORS))

# File count and size
FILE_COUNT=$(find "$OUTPUT_DIR" -type f | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" | cut -f1)
info "  Files: $FILE_COUNT | Size: $TOTAL_SIZE"

if [[ $ERRORS -gt 0 ]]; then
    error ""
    error "Validation FAILED with $ERRORS error(s)"
    exit 1
fi

ok "  Validation passed"
echo ""

# Clean up build artifacts before archiving
find "$OUTPUT_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$OUTPUT_DIR" -name "*.pyc" -delete 2>/dev/null || true

# Create archive
ARCHIVE_DIR="$(dirname "$OUTPUT_DIR")"
ARCHIVE_PATH="$ARCHIVE_DIR/$ARCHIVE_NAME"
info "Creating archive: $ARCHIVE_PATH"
(cd "$ARCHIVE_DIR" && tar -czf "$ARCHIVE_NAME" "$(basename "$OUTPUT_DIR")")
ARCHIVE_SIZE=$(du -sh "$ARCHIVE_PATH" | cut -f1)
ok "Archive created: $ARCHIVE_PATH ($ARCHIVE_SIZE)"

echo ""
ok "Build complete!"
echo ""
info "To test the release:"
echo "  mkdir /tmp/quaid-test && cd /tmp/quaid-test"
echo "  tar xzf $ARCHIVE_PATH"
echo "  cd quaid-release && node setup-quaid.mjs"
