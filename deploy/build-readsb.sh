#!/usr/bin/env bash
# RETINA ADS-B Phase A — reproducible pinned readsb build (ADR-0002, DESIGN.md §3).
#
# Builds vendor/readsb at the EXACT pinned commit and installs it as
# /usr/local/bin/readsb-retina — deliberately NOT /usr/local/bin/readsb, to
# avoid colliding with a distro/readsb-package binary on the same host.
#
# Usage: deploy/build-readsb.sh [--install-deps] [--build-only] [--candidate <sha>]
#   --install-deps    apt-get install missing build packages (default: fail and list)
#   --build-only      build + verify, skip the /usr/local/bin install (no root needed)
#   --candidate <sha> SHADOW BUILD of exactly this commit for the §12.5/§26.6
#                     shadow-replay gate (runbooks/readsb-upgrade-rollback.md).
#                     Only valid together with --build-only: a candidate is
#                     NEVER installed; the ADR-0002 pin check is replaced by a
#                     HEAD==<sha> check against the named commit.

set -euo pipefail

PIN_SHORT="d9a4c62"
PIN_FULL="d9a4c62655490e70d07704e207738bb9c6cffde1"
PIN_VERSION="3.16.16"
INSTALL_PATH="/usr/local/bin/readsb-retina"
# DESIGN.md §3 build deps (network-only build, all SDR backends off)
REQUIRED_PKGS=(build-essential pkg-config libncurses-dev zlib1g-dev libzstd-dev)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
READSB_DIR="$REPO_ROOT/vendor/readsb"

INSTALL_DEPS=0
BUILD_ONLY=0
CANDIDATE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-deps) INSTALL_DEPS=1 ;;
        --build-only)   BUILD_ONLY=1 ;;
        --candidate)
            shift
            [ "$#" -gt 0 ] || { echo "build-readsb: --candidate requires a commit sha" >&2; exit 1; }
            CANDIDATE="$1"
            ;;
        -h|--help)      grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-readsb: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

die() { echo "build-readsb: ERROR: $*" >&2; exit 1; }

[ -d "$READSB_DIR" ] || die "vendor/readsb not found at $READSB_DIR"

# --- 1. Pin verification (hard fail — ADR-0002) -----------------------------
# --candidate replaces the pin check with a HEAD==<candidate> check, and is
# valid ONLY for --build-only shadow builds (runbook step 2): the install
# path below stays reachable exclusively for the ADR-0002 pinned commit.
head="$(git -C "$READSB_DIR" rev-parse HEAD 2>/dev/null)" \
    || die "vendor/readsb is not a git checkout; cannot verify the ADR-0002 pin"
if [ -n "$CANDIDATE" ]; then
    [ "$BUILD_ONLY" -eq 1 ] || die "--candidate is shadow-build only: combine it
with --build-only. A candidate readsb is NEVER installed (ADR-0002); it gets
installed only after the shadow-replay gate passes and ADR-0002 is revised to
pin it."
    candidate_full="$(git -C "$READSB_DIR" rev-parse --verify --quiet "$CANDIDATE^{commit}")" \
        || die "--candidate $CANDIDATE does not name a commit in vendor/readsb (git fetch first?)"
    if [ "$head" != "$candidate_full" ]; then
        die "vendor/readsb HEAD is $head, not the named candidate $candidate_full.
Check out exactly the candidate first:
  git -C vendor/readsb checkout $candidate_full"
    fi
    EXPECT_SHORT="$(git -C "$READSB_DIR" rev-parse --short=7 "$candidate_full")"
    echo "=============================================================="
    echo "build-readsb: SHADOW BUILD of CANDIDATE $EXPECT_SHORT"
    echo "  ($candidate_full)"
    echo "  NOT the ADR-0002 pin ($PIN_SHORT v$PIN_VERSION). This binary is"
    echo "  for the off-path shadow-replay gate only and will NOT be installed."
    echo "=============================================================="
elif [ "$head" != "$PIN_FULL" ]; then
    die "vendor/readsb is at commit $head, but the pinned build is $PIN_FULL
($PIN_SHORT, v$PIN_VERSION). Refusing to build an unpinned readsb: see
docs/adr/ ADR-0002 (exact pinned readsb build). Check out the pin with:
  git -C vendor/readsb checkout $PIN_FULL
(For an off-path shadow build of an upgrade candidate, use
  deploy/build-readsb.sh --build-only --candidate <sha>
per runbooks/readsb-upgrade-rollback.md.)"
else
    EXPECT_SHORT="$PIN_SHORT"
fi
if ! git -C "$READSB_DIR" diff --quiet HEAD -- 2>/dev/null; then
    die "vendor/readsb has local modifications to tracked files — the build
would not be the named commit ($EXPECT_SHORT). Revert or stash them first."
fi
if [ -z "$CANDIDATE" ]; then
    echo "build-readsb: pin OK: vendor/readsb @ $PIN_SHORT (v$PIN_VERSION)"
fi

# --- 2. Build dependency check (no installs unless --install-deps) ----------
missing=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    status="$(dpkg-query -W -f '${Status}' "$pkg" 2>/dev/null || true)"
    [ "$status" = "install ok installed" ] || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
    if [ "$INSTALL_DEPS" -eq 1 ]; then
        echo "build-readsb: installing missing build deps: ${missing[*]}"
        apt-get install -y "${missing[@]}"
    else
        die "missing build packages: ${missing[*]}
Install them yourself or rerun with --install-deps:
  apt-get install -y ${missing[*]}"
    fi
fi

# --- 3. Reproducible build (DESIGN.md §3: plain make, -O2, no SDR, no UUIDs) -
echo "build-readsb: building (make -j$(nproc) OPTIMIZE=\"-O2\")"
make -C "$READSB_DIR" clean >/dev/null
make -C "$READSB_DIR" -j"$(nproc)" OPTIMIZE="-O2"

# --- 4. Binary verification --------------------------------------------------
# readsb prints --version on stderr
version_out="$("$READSB_DIR/readsb" --version 2>&1)"
echo "build-readsb: built: $version_out"
case "$version_out" in
    *"$EXPECT_SHORT"*) : ;;
    *) die "built binary does not report git $EXPECT_SHORT (got: $version_out) — wrong commit built" ;;
esac

if [ "$BUILD_ONLY" -eq 1 ]; then
    if [ -n "$CANDIDATE" ]; then
        echo "build-readsb: SHADOW BUILD done ($EXPECT_SHORT, not installed); binary at $READSB_DIR/readsb"
    else
        echo "build-readsb: --build-only: skipping install; binary at $READSB_DIR/readsb"
    fi
    exit 0
fi

# --- 5. Install as readsb-retina (NOT readsb: avoid distro collision) --------
# Invariant: only the ADR-0002 pin ever reaches this point (--candidate
# requires --build-only above).
[ -z "$CANDIDATE" ] || die "internal: candidate build must never reach the install path"
[ "$(id -u)" -eq 0 ] || die "installing to $INSTALL_PATH requires root (or use --build-only)"
install -m 0755 "$READSB_DIR/readsb" "$INSTALL_PATH"
installed_out="$("$INSTALL_PATH" --version 2>&1)"
case "$installed_out" in
    *"$PIN_SHORT"*) : ;;
    *) die "installed $INSTALL_PATH does not report git $PIN_SHORT (got: $installed_out)" ;;
esac
echo "build-readsb: installed $INSTALL_PATH ($installed_out)"
