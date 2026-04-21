#!/usr/bin/env bash
# Builds Camoufox from source for macOS and Linux, then uploads
# the artifacts as a GitHub release to JWriter20/camoufox.
#
# Prerequisites:
#   - gh CLI authenticated (gh auth status)
#   - System deps: python3, go, aria2, p7zip-full, clang/lld (18+), msitools
#
# Usage:
#   bash scripts/build-and-release.sh                    # build all targets
#   bash scripts/build-and-release.sh --target linux     # linux only
#   bash scripts/build-and-release.sh --target macos     # macos only
#   bash scripts/build-and-release.sh --skip-build       # skip build, just release existing dist/
#   bash scripts/build-and-release.sh --no-release       # build only, don't create release

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
FORK_REPO="JWriter20/camoufox"

cd "$REPO_ROOT"

# ── Parse args ────────────────────────────────────────────────────────
TARGET="all"
SKIP_BUILD=false
NO_RELEASE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)   TARGET="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --no-release) NO_RELEASE=true; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Read version info ────────────────────────────────────────────────
source "$REPO_ROOT/upstream.sh"
FIREFOX_VERSION="$version"
CAMOUFOX_RELEASE="$release"
echo "Building Camoufox: Firefox $FIREFOX_VERSION, release $CAMOUFOX_RELEASE"

CF_SOURCE_DIR="$REPO_ROOT/camoufox-${FIREFOX_VERSION}-${CAMOUFOX_RELEASE}"

if [[ "$SKIP_BUILD" == false ]]; then
  # ── Fetch Firefox source + setup (with git repo for patch.py) ────
  if [[ ! -d "$CF_SOURCE_DIR/.git" ]]; then
    echo "=== Fetching Firefox source ==="
    make fetch

    echo "=== Setting up build tree (with git init) ==="
    make setup
  else
    echo "Source tree already present at $CF_SOURCE_DIR, skipping setup."
  fi

  # ── Bootstrap Mozilla toolchain (idempotent) ─────────────────────
  if [[ ! -d "$HOME/.mozbuild" ]]; then
    echo "=== Bootstrapping Mozilla toolchain ==="
    make mozbootstrap
  else
    echo "Mozilla toolchain already bootstrapped."
  fi

  # ── Build targets (skip if artifact already exists) ─────────────
  echo "=== Building (target=$TARGET) ==="
  mkdir -p dist

  ARTIFACT_PREFIX="camoufox-${FIREFOX_VERSION}-${CAMOUFOX_RELEASE}"

  if [[ "$TARGET" == "all" || "$TARGET" == "linux" ]]; then
    ARTIFACT="dist/${ARTIFACT_PREFIX}-lin.x86_64.zip"
    if [[ -f "$ARTIFACT" ]]; then
      echo "--- Skipping linux/x86_64: $ARTIFACT already exists ---"
    else
      echo "--- Building linux/x86_64 ---"
      python3 ./multibuild.py --target linux --arch x86_64
    fi
  fi

  if [[ "$TARGET" == "all" || "$TARGET" == "macos" ]]; then
    ARTIFACT="dist/${ARTIFACT_PREFIX}-mac.arm64.zip"
    if [[ -f "$ARTIFACT" ]]; then
      echo "--- Skipping macos/arm64: $ARTIFACT already exists ---"
    else
      echo "--- Building macos/arm64 ---"
      python3 ./multibuild.py --target macos --arch arm64
    fi
  fi
fi

# ── List built artifacts ───────────────────────────────────────────────
echo ""
echo "=== Built artifacts ==="
ls -lh "$REPO_ROOT/dist/"

if [[ "$NO_RELEASE" == true ]]; then
  echo ""
  echo "Skipping release (--no-release). Artifacts are in: $REPO_ROOT/dist/"
  exit 0
fi

# ── Create GitHub release ──────────────────────────────────────────────
RELEASE_TAG="v${FIREFOX_VERSION}-$(date +%Y%m%d)"
echo ""
echo "=== Creating GitHub release: $RELEASE_TAG ==="

if gh release view "$RELEASE_TAG" --repo "$FORK_REPO" &>/dev/null; then
  echo "Release $RELEASE_TAG already exists. Uploading/overwriting assets..."
  gh release upload "$RELEASE_TAG" "$REPO_ROOT"/dist/*.zip \
    --repo "$FORK_REPO" --clobber
else
  gh release create "$RELEASE_TAG" "$REPO_ROOT"/dist/*.zip \
    --repo "$FORK_REPO" \
    --title "Camoufox $FIREFOX_VERSION ($RELEASE_TAG)" \
    --notes "Built from \`main\` branch on $(date -u +%Y-%m-%d).

**Targets:**
- Linux x86_64
- macOS arm64"
fi

echo ""
echo "=== Done! Release URL: ==="
gh release view "$RELEASE_TAG" --repo "$FORK_REPO" --json url -q '.url'
