#!/usr/bin/env bash
#
# build_clang_universal.sh
#
# Build an optimised LLVM/clang toolchain from source: a three-stage
# PGO + ThinLTO self-host.  stage1 -> instrumented stage2 -> training run ->
# final stage2, installed to a versioned prefix under $HOME.  No root is needed
# for the build itself; root is used only to install distro dependencies.
#
#   ./build_clang_universal.sh
#
# Everything is overridable from the environment:
#
#   PREFIX=$HOME/opt/clang-mine  SRC=/data/llvm-project  JOBS=8 \
#   LLVM_REF=llvmorg-21.1.0  INSTALL_DEPS=0  ./build_clang_universal.sh
#
# Expect several hours and roughly 60 GB of disk.  A three-stage build compiles
# LLVM three times on purpose: the middle one is instrumented, it is made to
# compile real code to record a profile, and the profile is what makes the final
# compiler faster than a plain -O3 build of the same source.
#
# Licence: public domain / CC0.  No warranty.

set -euo pipefail

# --- integrity self-check -------------------------------------------------
# This file must be pure ASCII.  Copying a script out of a web page or a chat
# window silently replaces ordinary spaces with U+00A0 (non-breaking space),
# which bash treats as part of a command name -- you get "command not found"
# on a line that looks perfectly correct.
#
# NOTE: the body below is deliberately NOT indented.  Leading whitespace is
# exactly what gets converted, so an indented error handler is unable to report
# the corruption it just detected -- it fails as "printf: command not found".
if LC_ALL=C tr -d '\11\12\40-\176' < "$0" | head -c 1 | LC_ALL=C grep -q .; then
printf 'ERROR: %s contains non-ASCII bytes.\n' "$0" >&2
printf 'It was almost certainly corrupted by copy-paste from a web page.\n' >&2
printf 'Repair it with:\n' >&2
printf "    sed -i 's/%s/ /g' %s\n" '\xc2\xa0' "$0" >&2
printf 'or, better, download the file directly instead of pasting it.\n' >&2
exit 1
fi
# --------------------------------------------------------------------------

# ---------------------------------------------------------------- configuration

SRC="${SRC:-$HOME/src/llvm-project}"          # source tree (cloned if absent)
LLVM_REF="${LLVM_REF:-main}"                  # branch or tag to build
BUILD="${BUILD:-$SRC/../llvm-build-pgo}"
PREFIX="${PREFIX:-$HOME/opt/clang-$(date +%Y%m%d)}"
JOBS="${JOBS:-$(nproc)}"
INSTALL_DEPS="${INSTALL_DEPS:-1}"             # 0 to skip the package step
LINK_CURRENT="${LINK_CURRENT:-1}"             # 0 to skip the clang-current symlink

# TUNING.  The default is -mtune only: it reorders and schedules for this CPU
# but emits NO instruction the baseline did not already allow, so the result
# still runs anywhere.  -march is deliberately NOT set, because a toolchain that
# SIGILLs after you copy it to another machine is a bad default.
#
# If this compiler will only ever run on machines you control, an ISA level is a
# real speed-up.  Prefer a LEVEL over a CPU name:
#
#   TUNE="-march=x86-64-v3 -mtune=native"     # AVX2-era, 2013+
#   TUNE="-march=x86-64-v2 -mtune=native"     # SSE4.2-era, 2009+
#
# Levels license exactly what a __builtin_cpu_supports("x86-64-v3") check
# verifies.  -march=<cpuname> licenses more than that (on Haswell: AES, RDRND,
# PCLMUL, INVPCID), and the gap is where a crash on somebody else's box lives.
TUNE="${TUNE:--mtune=native}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ dependencies

DNF_PKGS="clang clang-devel llvm llvm-devel lld cmake ninja-build git
          python3 python3-devel python3-pyyaml python3-psutil
          zlib-devel libxml2-devel libedit-devel libzstd-devel"
APT_PKGS="clang lld llvm-dev libclang-dev cmake ninja-build git
          python3 python3-dev python3-yaml python3-psutil
          zlib1g-dev libxml2-dev libedit-dev libzstd-dev"
# clang/lld are the BOOTSTRAP compiler -- LLVM is built by an existing clang.
# clang-devel/llvm-devel are not needed to build LLVM; they are here because
# they are what you want afterwards to build things *against* LLVM.

install_deps() {
    [ "$INSTALL_DEPS" = 1 ] || { warn "INSTALL_DEPS=0, skipping package install"; return 0; }
    local mgr=""
    command -v dnf     >/dev/null && mgr=dnf
    [ -z "$mgr" ] && command -v apt-get >/dev/null && mgr=apt
    [ -z "$mgr" ] && { warn "no dnf or apt-get found -- install the deps yourself, then re-run with INSTALL_DEPS=0"; return 0; }

    say "Installing dependencies with $mgr (sudo)"
    case "$mgr" in
      dnf) sudo dnf install -y $DNF_PKGS ;;
      apt) sudo apt-get update && sudo apt-get install -y $APT_PKGS ;;
    esac
}

# ---------------------------------------------------------------------- preflight

install_deps

say "Preflight"
for t in cmake ninja git python3; do
    command -v "$t" >/dev/null || die "missing: $t (install it, or re-run without INSTALL_DEPS=0)"
done
# Pick a python3 that actually has yaml.  PATH commonly leads to PyPy or a pyenv
# build with no yaml module, and the PGO training stage is lit + perf-helper.py,
# which needs a full CPython.  Choosing here beats failing an hour in.
PY=""
for cand in "$(command -v python3 || true)" /usr/bin/python3 /usr/local/bin/python3; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import yaml' >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "no python3 with the yaml module found.
       Install it (dnf install python3-pyyaml / apt install python3-yaml),
       or set PY= to a CPython that has it."

# Bootstrap compiler: an existing clang is strongly preferred; gcc works.
if command -v clang++ >/dev/null; then
    BOOT_CC="$(command -v clang)"; BOOT_CXX="$(command -v clang++)"
elif command -v g++ >/dev/null; then
    BOOT_CC="$(command -v gcc)";   BOOT_CXX="$(command -v g++)"
    warn "no clang found; bootstrapping with gcc (slower stage 1, identical result)"
else
    die "no C++ compiler found to bootstrap from"
fi

# lld is optional but much faster at linking LLVM than GNU ld.
LINKER_ARGS=()
if command -v ld.lld >/dev/null; then
    LINKER_ARGS=(-DLLVM_USE_LINKER=lld -DBOOTSTRAP_LLVM_ENABLE_LLD=ON -DBOOTSTRAP_BOOTSTRAP_LLVM_ENABLE_LLD=ON)
else
    warn "ld.lld not found; using the default linker (slower, more memory)"
fi

[ -e "$PREFIX" ] && die "$PREFIX already exists. Never install over a prefix in use -- set PREFIX= to a new one."

# ThinLTO links are memory-hungry. Cap concurrent links so a big -j does not OOM.
RAM_GB=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 8)
LINK_JOBS=$(( RAM_GB / 8 )); [ "$LINK_JOBS" -lt 1 ] && LINK_JOBS=1
[ "$LINK_JOBS" -gt "$JOBS" ] && LINK_JOBS="$JOBS"

# df cannot measure a path that does not exist yet, and on a fresh machine the
# build parent never does -- that produced a false low-disk warning.
mkdir -p "$(dirname "$BUILD")"
AVAIL_GB=$(df -BG --output=avail "$(dirname "$BUILD")" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
[ "${AVAIL_GB:-0}" -ge 60 ] || warn "only ${AVAIL_GB}G free where the build goes; ~60G is typical. It may fail late."

# ------------------------------------------------------------------------- source

if [ ! -d "$SRC/llvm" ]; then
    say "Cloning llvm-project ($LLVM_REF) -- several GB"
    mkdir -p "$(dirname "$SRC")"
    git clone --depth 1 --branch "$LLVM_REF" https://github.com/llvm/llvm-project.git "$SRC"
fi
[ -d "$SRC/llvm" ] || die "no LLVM source at $SRC"

printf '  bootstrap : %s\n' "$("$BOOT_CXX" --version | head -1)"
printf '  source    : %s @ %s\n' "$SRC" "$(git -C "$SRC" log -1 --format=%h 2>/dev/null || echo '?')"
printf '  prefix    : %s\n' "$PREFIX"
printf '  tuning    : %s\n' "$TUNE"
printf '  python    : %s\n' "$PY"
printf '  jobs      : %s (link jobs %s, %sG RAM)\n' "$JOBS" "$LINK_JOBS" "$RAM_GB"

# If an older build of this toolchain is on LD_LIBRARY_PATH it can shadow the one
# being built -- same sonames, and LD_LIBRARY_PATH beats a binary's RUNPATH.
unset LD_LIBRARY_PATH

# ---------------------------------------------------------------------- configure

say "Configure"
rm -rf "$BUILD"; mkdir -p "$BUILD"; cd "$BUILD"

cmake -G Ninja "$SRC/llvm" \
  -C "$SRC/clang/cmake/caches/PGO.cmake" \
  -DPGO_INSTRUMENT_LTO=Thin \
  -DPython3_EXECUTABLE="$PY" \
  -DLLVM_ENABLE_RUNTIMES="compiler-rt;libcxx;libcxxabi;libunwind;openmp" \
  "${LINKER_ARGS[@]}" \
  -DLLVM_PARALLEL_LINK_JOBS="$LINK_JOBS" \
  -DBOOTSTRAP_CMAKE_C_FLAGS="$TUNE" \
  -DBOOTSTRAP_CMAKE_CXX_FLAGS="$TUNE" \
  -DBOOTSTRAP_BOOTSTRAP_CMAKE_C_FLAGS="$TUNE" \
  -DBOOTSTRAP_BOOTSTRAP_CMAKE_CXX_FLAGS="$TUNE" \
  -DCLANG_BOOTSTRAP_TARGETS="generate-profdata;stage2;stage2-install" \
  -DCLANG_BOOTSTRAP_PASSTHROUGH="CMAKE_INSTALL_PREFIX;LLVM_TARGETS_TO_BUILD;LLVM_ENABLE_RUNTIMES;Python3_EXECUTABLE;CLANG_BOOTSTRAP_PASSTHROUGH" \
  -DCMAKE_C_COMPILER="$BOOT_CC" \
  -DCMAKE_CXX_COMPILER="$BOOT_CXX" \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" 2>&1 | tee "$BUILD/configure.log"

# Every one of these can fail SILENTLY and cost you the whole build.  The last is
# the one that matters most: without it the PGO cache did not take and you get an
# ordinary two-stage bootstrap after hours of work.
say "Verifying the configure"
fail=0
for want in 'lld project is enabled' 'Setting next clang stage to: stage2-instrumented'; do
    if grep -qF "$want" "$BUILD/configure.log"; then printf '  ok   %s\n' "$want"
    else printf '  MISS %s\n' "$want"; fail=1; fi
done
grep -qF 'Found Python module yaml' "$BUILD/configure.log" \
    || warn "python yaml module not found by cmake -- the training stage may misbehave"
[ "$fail" -eq 0 ] || die "configure degraded -- read $BUILD/configure.log before building"

# -------------------------------------------------------------------- build

say "Building. This is the long part -- use tmux/screen if over ssh."
ninja -j"$JOBS" stage2-install

say "Installed"
"$PREFIX/bin/clang" --version | head -1
if [ "$LINK_CURRENT" = 1 ]; then
    ln -sfn "$PREFIX" "$(dirname "$PREFIX")/clang-current"
    printf '  clang-current -> %s\n' "$(readlink -f "$(dirname "$PREFIX")/clang-current")"
    printf '\n  Point your tools at the SYMLINK, not the versioned directory:\n'
    printf '      export PATH="%s/clang-current/bin:$PATH"\n' "$(dirname "$PREFIX")"
    printf '  Then a future rebuild is one ln -sfn, and nothing goes stale.\n'
fi
