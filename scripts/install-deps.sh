#!/usr/bin/env bash
#
# install-deps.sh - Auto-install all build dependencies for Camoufox. Note: only tested on MacOS.
#
# Installs everything needed to run `make dir` and `make bootstrap`
# (and, subsequently, `make build`) on a fresh machine.
#
# Supported platforms:
#   - macOS      (Homebrew)
#   - Debian/Ubuntu (apt)
#   - Fedora/RHEL   (dnf)
#   - Arch          (pacman)
#
# Usage:
#   bash scripts/install-deps.sh
#
# This script is invoked automatically by `make bootstrap`, but can also be
# run standalone.
#
# ---------------------------------------------------------------------------
# Dependencies installed (and *why* each is needed):
#
#   python3 (>= 3.11)  Firefox 150's ./mach requires the stdlib `tomllib`
#                      module, which only exists in Python 3.11+. The system
#                      python3 on macOS (3.9) is too old and mach will crash
#                      with `ModuleNotFoundError: No module named 'tomllib'`.
#   python3-dev/pip    Building/patching helper scripts + mach venv.
#   rust + cargo       Required by `./mach bootstrap` / the build. Installed
#                      via rustup (not available as a Homebrew keg we control
#                      the toolchain version of).
#   p7zip (`7z`)       scripts/package.py + package-helper.sh use `7z`.
#   aria2 (`aria2c`)   `make fetch` downloads the Firefox source tarball.
#   go / golang        Building the launcher (legacy/launcher).
#   msitools           `msiextract` — Windows font/redist extraction.
#   wget               scripts/mozfetch.sh + setup-wasi.
#   sqlite             libsqlite3 headers for the Linux build target.
#   git, curl, make,   Core build tooling. Present by default on macOS via
#   clang, unzip,      the Xcode Command Line Tools; installed explicitly on
#   rsync              Linux.
# ---------------------------------------------------------------------------

set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> WARNING:\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2; }

# Minimum Python version mach requires.
PY_MIN_MAJOR=3
PY_MIN_MINOR=11

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# rustup / cargo (all platforms)
# ---------------------------------------------------------------------------
install_rust() {
  if have rustc && have cargo; then
    log "Rust already installed ($(rustc --version))"
  else
    # cargo may be installed but not on PATH yet in this shell.
    if [ -f "$HOME/.cargo/env" ]; then
      # shellcheck disable=SC1091
      . "$HOME/.cargo/env"
    fi
    if have rustc && have cargo; then
      log "Rust found via ~/.cargo/env ($(rustc --version))"
    else
      log "Installing Rust via rustup..."
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable
      # shellcheck disable=SC1091
      . "$HOME/.cargo/env"
      log "Installed $(rustc --version)"
    fi
  fi

  # Rust cross-compilation targets the build needs. `mach configure` verifies it
  # can compile Rust for the build target; without the matching std target it
  # aborts with "Cannot compile for <target>". scripts/patch.py adds the aarch64
  # and i686 Linux targets but NOT x86_64, so building the Linux x86_64 target
  # fails without this. `rustup target add` is idempotent (no-op if already
  # present or if it is the host's native target).
  if have rustup; then
    log "Ensuring Rust Linux cross-compile targets (x86_64/aarch64/i686)..."
    rustup target add x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu \
                      i686-unknown-linux-gnu \
      || warn "Could not add Rust Linux targets; if cross-compiling to Linux, run: rustup target add x86_64-unknown-linux-gnu"
  fi
}

# ---------------------------------------------------------------------------
# Check that a Python >= 3.11 is available (mach needs tomllib).
# ---------------------------------------------------------------------------
check_python() {
  local py
  for py in python3.14 python3.13 python3.12 python3.11 python3; do
    if have "$py"; then
      if "$py" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
        log "Found suitable Python: $py ($($py --version 2>&1))"
        return 0
      fi
    fi
  done
  warn "No Python >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR} found on PATH."
  warn "mach requires it (stdlib 'tomllib'). Ensure a newer python3 comes first on PATH."
  return 1
}

# ---------------------------------------------------------------------------
# macOS (Homebrew)
# ---------------------------------------------------------------------------
install_macos() {
  log "Detected macOS."

  # Xcode Command Line Tools (clang, make, git, curl, rsync, unzip, tar).
  if ! xcode-select -p >/dev/null 2>&1; then
    log "Installing Xcode Command Line Tools..."
    xcode-select --install || warn "Trigger the CLT install dialog manually if this failed."
  else
    log "Xcode Command Line Tools present."
  fi

  # Homebrew
  if ! have brew; then
    log "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Make brew available in this shell for both Apple Silicon and Intel.
    if [ -x /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  else
    log "Homebrew present ($(brew --version | head -1))."
  fi

  # Note: `p7zip` provides the `7z` binary the scripts call; the newer
  # `sevenzip` formula only ships `7zz`.
  local formulae=(python@3.14 aria2 p7zip go msitools wget sqlite)
  log "Installing Homebrew formulae: ${formulae[*]}"
  brew install "${formulae[@]}"

  install_rust
}

# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
install_linux() {
  log "Detected Linux."

  local debs="python3 python3-dev python3-pip p7zip-full golang-go msitools wget aria2 libsqlite3-dev build-essential make git curl unzip rsync ca-certificates"
  local rpms="python3 python3-devel p7zip golang msitools wget aria2 sqlite-devel gcc gcc-c++ make git curl unzip rsync ca-certificates"
  local pacman_pkgs="python python-pip p7zip go msitools wget aria2 sqlite base-devel git curl unzip rsync ca-certificates"

  if have apt-get; then
    log "Using apt-get..."
    sudo apt-get update
    # shellcheck disable=SC2086
    sudo apt-get -y install $debs
  elif have dnf; then
    log "Using dnf..."
    # shellcheck disable=SC2086
    sudo dnf -y install $rpms
  elif have pacman; then
    log "Using pacman..."
    # shellcheck disable=SC2086
    sudo pacman -Sy --noconfirm $pacman_pkgs
  else
    err "No supported package manager (apt-get/dnf/pacman) found."
    exit 1
  fi

  install_rust
}

# ---------------------------------------------------------------------------
main() {
  case "$(uname -s)" in
    Darwin) install_macos ;;
    Linux)  install_linux ;;
    *)
      err "Unsupported platform: $(uname -s)"
      exit 1
      ;;
  esac

  echo
  check_python || true
  echo
  log "Dependency installation complete."
  log "If rustup was just installed, run: source \"\$HOME/.cargo/env\" (or open a new shell)."
}

main "$@"
