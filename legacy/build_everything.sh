#!/usr/bin/env bash
#
# build_everything.sh
#
# Build and install the whole self-built stack on this machine, using whichever
# of the sibling scripts happen to be sitting next to this one:
#
#   gcc       build_gcc_universal.sh           -> ~/opt/gcc-<id>       + gcc-current
#   clang     build_clang_universal.sh         -> ~/opt/clang-<date>   + clang-current
#             build_clang_local.sh             (fallback, machine-specific)
#   clangenv  setup_clang_env.sh               -> a managed block in ~/.bashrc
#   python    build_python_universal.sh        -> ~/opt/cpython-*, ~/opt/pypy{2,3}-*
#   dkms      fix-amdgpu-dkms.sh               -> caps amdgpu-dkms below 7.x
#   kernel    universal_linux_kernel_build.sh  -> /boot + /lib/modules/<release>
#
# Nothing has to be present.  Whatever is missing is skipped and the plan says
# so, which is the entire point: drop this directory onto a fresh machine, run
# one command, walk away for a day.
#
#     ./build_everything.sh --dry-run          # print the plan, touch nothing
#     ./build_everything.sh                    # show the plan, ask, then do it
#     ./build_everything.sh --yes              # no prompt (tmux, cron, CI)
#
#     ./build_everything.sh --only=gcc,kernel
#     ./build_everything.sh --skip=python
#     ./build_everything.sh --force=clang      # rebuild even though one is installed
#     ./build_everything.sh --list             # stages, order, and what is installed
#
# WHAT "ALREADY DONE" MEANS.  Every sibling script maintains a `-current`
# symlink and refuses to install over an existing prefix.  So this script does
# not track its own idea of progress: for each stage it asks the machine
# directly -- does ~/opt/gcc-current/bin/gcc run and print a version? -- and
# skips the stage if the answer is yes.  That makes a re-run after a failure or
# a reboot resume rather than restart, with no state file to go stale, and it
# makes --force the only way to rebuild something that already works.
#
# WHY IT SANITISES THE ENVIRONMENT.  These scripts each read a variable called
# SRC, and they each mean something different by it (an LLVM checkout, a kernel
# tree, a tarball root).  Worse, a developer's ~/.bashrc usually exports CC, CXX
# and LLVM at a self-built clang -- and a stray LLVM= silently turns a requested
# GCC kernel build into a clang one, because kbuild only tests whether LLVM is
# non-empty.  Each stage therefore runs with those names explicitly cleared and
# only the variables that stage should see put back.  See run_stage().
#
# ORDER.  gcc and clang are independent of each other; each bootstraps from the
# system toolchain by design.  python comes after them because a fresh CPython
# or PyPy on PATH would shadow the /usr/bin/python3 that LLVM's PGO training
# stage needs, and the kernel comes last because it is the only stage that
# writes outside $HOME and the only one you have to reboot to test.
#
# Cost, if every stage runs: the better part of a day and ~150 GB of disk.
#
# Licence: public domain / CC0.  No warranty.  It installs a kernel; read it.

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

SCRIPTS_DIR="${SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
OPT_ROOT="${OPT_ROOT:-$HOME/opt}"
LOG_DIR="${LOG_DIR:-$HOME/src/build-everything/logs}"
LOCK_DIR="${LOCK_DIR:-$HOME/src/build-everything/lock}"

JOBS="${JOBS:-$(nproc)}"
INSTALL_DEPS="${INSTALL_DEPS:-1}"     # 0 = assume distro packages are already there
KEEP_GOING="${KEEP_GOING:-0}"         # 1 = carry on after a stage fails

# Per-stage knobs.  Deliberately prefixed: the sibling scripts reuse short names
# like SRC, PREFIX and MODE for different things, so nothing here is passed
# through by accident -- run_stage() translates these into each script's own
# names and clears everything else.
GCC_TRACK="${GCC_TRACK:-release}"          # release | snapshot | git
GCC_MODE="${GCC_MODE:-profiled-lto}"       # profiled-lto | lto | plain | quick
GCC_LANGUAGES="${GCC_LANGUAGES:-c,c++,lto}"
GCC_PREFIX="${GCC_PREFIX:-}"               # empty = let the script name it

CLANG_REF="${CLANG_REF:-main}"             # branch or tag of llvm-project
CLANG_TUNE="${CLANG_TUNE:--mtune=native}"  # -march=x86-64-v3 if it never leaves this box
CLANG_PREFIX="${CLANG_PREFIX:-}"           # empty = ~/opt/clang-<today>, uniquified

PY_TARGETS="${PY_TARGETS:-cpython,pypy2,pypy3}"

KERNEL_TOOLCHAIN="${KERNEL_TOOLCHAIN:-auto}"   # auto | llvm | gcc
KERNEL_SRC="${KERNEL_SRC:-}"                   # empty = download a tarball
KERNEL_VERSION="${KERNEL_VERSION:-}"           # empty = latest stable
KERNEL_GPU="${KERNEL_GPU:-auto}"
KERNEL_LOCALVERSION="${KERNEL_LOCALVERSION:--gpu}"
KERNEL_INSTALL="${KERNEL_INSTALL:-1}"          # 0 = build it but do not touch /boot
KERNEL_KEEP_DEBUG_INFO="${KERNEL_KEEP_DEBUG_INFO:-0}"

KERNEL_MAKE_DEFAULT="${KERNEL_MAKE_DEFAULT:-0}"   # 1 = let the new kernel become the default boot entry
SETUP_ENV_EXPORTS="${SETUP_ENV_EXPORTS:-0}"    # 1 = also export CC/CXX/LLVM in ~/.bashrc

# THE PATH EACH STAGE SEES.  Distro directories FIRST, and that order is
# deliberate.  These siblings are written for a fresh machine, where `gcc` is
# the distro gcc and `python3` is the distro python3.  On a machine that has
# been used to build toolchains, neither is true: a developer's PATH puts
# ~/opt/... ahead of everything, and /usr/local/bin is where hand-installed
# toolchains land.  On THIS machine, unsanitised, `python3` is PyPy3 and `gcc`
# is a GCC 17 trunk snapshot -- so an unattended build would silently compile
# CPython with an experimental compiler and run kbuild's helper scripts under
# PyPy.  Reproducibility beats convenience here.  KEEP_PATH=1 opts out.
KEEP_PATH="${KEEP_PATH:-0}"
STAGE_PATH="${STAGE_PATH:-/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin}"

# ------------------------------------------------------------------- vocabulary

say()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;36m--- %s\033[0m\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ok()    { printf '  \033[32mok\033[0m   %s\n' "$*"; }
skip()  { printf '  --   %s\n' "$*"; }

# A prefix counts as installed only if its main binary RUNS.  A directory that
# exists proves nothing: a build killed during `make install` leaves one behind.
runs() { [ -x "$1" ] && "$1" --version >/dev/null 2>&1; }

hms() {
    local s=$1
    printf '%dh%02dm%02ds' $(( s / 3600 )) $(( (s % 3600) / 60 )) $(( s % 60 ))
}

# First name in the series that does not exist yet: foo, foo-2, foo-3...
# The sibling scripts refuse to install over an existing prefix (correctly --
# something may be running out of it), so a same-day retry after a failure needs
# a name that has not been used.
unique_prefix() {
    local base="$1" n=2 p="$1"
    while [ -e "$p" ]; do p="$base-$n"; n=$(( n + 1 )); done
    printf '%s' "$p"
}

[ "$(id -u)" -ne 0 ] || die "do not run this as root.
       Every build here installs into \$HOME and would leave root-owned files
       behind.  Run it as yourself; it asks for sudo where it genuinely needs
       root (distro packages, and the kernel install)."

# ------------------------------------------------------------------- arguments

DRY=0; ASSUME_YES=0; LIST=0
ONLY=""; SKIP=""; FORCE=""

for a in "$@"; do case "$a" in
    --dry-run|-n)  DRY=1 ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --list|-l)     LIST=1 ;;
    --only=*)      ONLY="${a#*=}" ;;
    --skip=*)      SKIP="${a#*=}" ;;
    --force=*)     FORCE="${a#*=}" ;;
    --force)       FORCE="all" ;;
    --keep-going)  KEEP_GOING=1 ;;
    -j*)           JOBS="${a#-j}" ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown option: $a  (try --help)" ;;
esac; done

in_list() {  # in_list needle "a,b,c"
    case ",${2}," in *",${1},"*) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------- the stages
#
# ORDER MATTERS and is not alphabetical:
#
#   gcc       first because it is the longest single job, so a failure surfaces
#             while you are still watching.
#   clang     independent of gcc; bootstraps from whatever clang or gcc exists.
#   clangenv  immediately after clang, so the rc block points at something real.
#   python    after both compilers: a fresh PyPy ahead of /usr/bin on PATH is
#             the classic way to break LLVM's PGO training stage, so it is not
#             installed until nothing else needs to find a system python.
#   dkms      before the kernel install, not after.  Capping amdgpu-dkms first
#             means the kernel's own `make install` never triggers a DKMS build
#             against a kernel that has the driver built in already.
#   kernel    last: the only stage that writes outside $HOME, and the only one
#             whose result you cannot test without rebooting.

STAGES="gcc clang clangenv python dkms kernel"

stage_script() { case "$1" in
    gcc)      printf 'build_gcc_universal.sh' ;;
    clang)    printf '%s' "$CLANG_SCRIPT" ;;
    clangenv) printf 'setup_clang_env.sh' ;;
    python)   printf 'build_python_universal.sh' ;;
    dkms)     printf 'fix-amdgpu-dkms.sh' ;;
    kernel)   printf 'universal_linux_kernel_build.sh' ;;
esac; }

# The python stage builds ONLY what PY_TARGETS names, so a fixed "CPython + PyPy"
# was a claim the stage did not honour: PY_TARGETS=cpython is the ordinary case
# on this machine, chosen deliberately because rebuilding PyPy is a separate
# ~8 GB job that must not run while GCC has the cores.  A label that promises
# PyPy while the stage skips it is exactly the "checked one thing, used another"
# shape this script exists to avoid -- and here it would hide the fact that a
# known-broken PyPy is being left in place.  So state both halves: what will be
# built, and what will NOT be.
py_label() {
    local cpy=0 pypy=""
    in_list cpython "$PY_TARGETS" && cpy=1
    in_list pypy2   "$PY_TARGETS" && pypy="$pypy pypy2"
    in_list pypy3   "$PY_TARGETS" && pypy="$pypy pypy3"
    pypy="${pypy# }"

    if [ "$cpy" = 1 ] && [ -n "$pypy" ]; then
        printf 'CPython + PyPy (%s)' "$PY_TARGETS"
    elif [ "$cpy" = 1 ]; then
        printf 'CPython only (%s) -- NOT building PyPy' "$PY_TARGETS"
    elif [ -n "$pypy" ]; then
        printf 'PyPy only (%s) -- NOT building CPython' "$PY_TARGETS"
    else
        printf 'nothing selected -- PY_TARGETS=%s names no known target' "${PY_TARGETS:-<empty>}"
    fi
}

stage_label() { case "$1" in
    gcc)      printf 'GCC toolchain' ;;
    clang)    printf 'LLVM/clang toolchain (3-stage PGO)' ;;
    clangenv) printf 'clang on PATH via ~/.bashrc' ;;
    python)   py_label ;;
    dkms)     printf 'cap amdgpu-dkms below 7.x' ;;
    kernel)   printf 'Linux kernel' ;;
esac; }

stage_cost() { case "$1" in     # rough wall clock, rough disk
    gcc)      printf '2-5h|30G' ;;
    clang)    printf '3-8h|60G' ;;
    clangenv) printf '1s|0G' ;;
    python)   printf '2-7h|20G' ;;
    dkms)     printf '1s|0G' ;;
    kernel)   printf '45m-3h|40G' ;;
esac; }

# Optional stages warn and carry on when they fail.  A missing rc block or an
# inapplicable DKMS tweak is not a reason to abandon a kernel build.
stage_optional() { case "$1" in clangenv|dkms) return 0 ;; *) return 1 ;; esac; }

stage_needs_sudo() { case "$1" in
    dkms)   return 0 ;;
    kernel) [ "$KERNEL_INSTALL" = 1 ] && return 0 || return 1 ;;
    gcc|clang|python) [ "$INSTALL_DEPS" = 1 ] && return 0 || return 1 ;;
    *) return 1 ;;
esac; }

# --------------------------------------------------------------- is it done yet
#
# Each of these asks the machine, not a state file.

# The release this source tree would produce.  Knowable only when we were given
# a tree; the download path picks its version at run time.
kernel_expected_release() {
    local v
    [ -n "$KERNEL_SRC" ] && [ -f "$KERNEL_SRC/Makefile" ] || return 1
    v=$(timeout 30 make -s -C "$KERNEL_SRC" kernelversion 2>/dev/null) || return 1
    [ -n "$v" ] || return 1
    printf '%s%s' "$v" "$KERNEL_LOCALVERSION"
}

kernel_installed_release() {
    local r
    # The kernel script bakes LOCALVERSION into the release string, so anything
    # under /lib/modules carrying that suffix and a usable build/ symlink was
    # installed by a previous run.
    local d
    # With an empty suffix the glob is /lib/modules/* -- every installed kernel,
    # including the running distro one.  That would report the stage already
    # done and silently skip the whole kernel build.
    [ -n "$KERNEL_LOCALVERSION" ] || return 1

    # Prefer an exact match on the release this tree would build.  Globbing on
    # the suffix alone answers the wrong question: once 7.2.0-gpu is installed,
    # /lib/modules/*-gpu matches it forever, so pointing the script at a NEWER
    # tree would report "already installed" and skip the build entirely.
    local want
    if want=$(kernel_expected_release); then
        if [ -d "/lib/modules/$want" ] && [ -e "/lib/modules/$want/build" ] \
           && { [ -e "/boot/vmlinuz-$want" ] || [ -e "/boot/kernel-$want" ]; }; then
            printf '%s' "$want"; return 0
        fi
        return 1
    fi

    for d in /lib/modules/*"$KERNEL_LOCALVERSION"; do
        [ -d "$d" ] || continue
        [ -e "$d/build" ] || continue
        # modules_install creates the tree and the build symlink; `make install`
        # puts the image in /boot AFTERWARDS.  Testing only the module tree
        # would call a run that died between those two steps "installed", and
        # every later run would skip the kernel stage and never finish it.
        r="${d##*/}"
        [ -e "/boot/vmlinuz-$r" ] || [ -e "/boot/kernel-$r" ] || continue
        printf '%s' "$r"; return 0
    done
    return 1
}

# An interpreter counts as present if it RUNS -- under the -current symlink or
# under any versioned prefix the sibling would have made.  This matters in both
# directions: build_python_universal.sh WARNS AND SKIPS (exit 0) when a prefix
# already exists without repointing -current, so a strict -current-only test
# would call a successful run a failure.
py_target_present() {
    local gen e
    case "$1" in
      cpython)
        runs "$OPT_ROOT/cpython-current/bin/python3" && return 0
        for e in "$OPT_ROOT"/cpython-*/bin/python3; do runs "$e" && return 0; done
        return 1 ;;
      pypy2|pypy3)
        gen="${1#pypy}"
        for e in "$OPT_ROOT/$1-current"/bin/pypy* \
                 "$OPT_ROOT"/pypy"$gen"*/bin/pypy* \
                 "$OPT_ROOT"/pypy"$gen"*/*/bin/pypy*; do
            runs "$e" && return 0
        done
        return 1 ;;
    esac
    return 1
}

stage_done() { case "$1" in
    gcc)      runs "$OPT_ROOT/gcc-current/bin/gcc" ;;
    clang)    runs "$OPT_ROOT/clang-current/bin/clang" ;;
    clangenv) grep -qF 'clang toolchain (setup_clang_env.sh)' "$HOME/.bashrc" 2>/dev/null ;;
    python)
        local t
        for t in $(printf '%s' "$PY_TARGETS" | tr ',' ' '); do
            py_target_present "$t" || return 1
        done
        return 0 ;;
    dkms)
        # Done if the cap is already in place -- or if there is nothing to cap.
        command -v dkms >/dev/null 2>&1 || return 0
        dkms status 2>/dev/null | grep -q '^amdgpu' || return 0
        grep -qs '^BUILD_EXCLUSIVE_KERNEL_MAX=' /usr/src/amdgpu-*/dkms.conf ;;
    kernel)
        [ "$KERNEL_INSTALL" = 1 ] || return 1     # build-only: never "already done"
        kernel_installed_release >/dev/null ;;
esac; }

# What is installed right now, for --list and the final summary.
stage_state() { case "$1" in
    gcc)      runs "$OPT_ROOT/gcc-current/bin/gcc" \
                  && "$OPT_ROOT/gcc-current/bin/gcc" --version | head -1 \
                  || printf 'not installed\n' ;;
    clang)    runs "$OPT_ROOT/clang-current/bin/clang" \
                  && "$OPT_ROOT/clang-current/bin/clang" --version | head -1 \
                  || printf 'not installed\n' ;;
    clangenv) stage_done clangenv && printf 'block present in ~/.bashrc\n' \
                  || printf 'no block in ~/.bashrc\n' ;;
    python)
        local out="" t p
        for t in cpython pypy2 pypy3; do
            p="$OPT_ROOT/$t-current"
            [ -e "$p" ] && out="$out $t"
        done
        [ -n "$out" ] && printf '%s\n' "installed:$out" || printf 'not installed\n' ;;
    dkms)
        command -v dkms >/dev/null 2>&1 || { printf 'no dkms on this machine\n'; return 0; }
        dkms status 2>/dev/null | grep -q '^amdgpu' || { printf 'no amdgpu-dkms registered\n'; return 0; }
        stage_done dkms && printf 'already capped\n' || printf 'amdgpu-dkms present, uncapped\n' ;;
    kernel)
        local r; r=$(kernel_installed_release) \
            && printf '%s installed\n' "$r" || printf 'not installed\n' ;;
esac; }

# --------------------------------------------------------------------- discovery

# clang has two implementations.  The universal one is portable and builds from
# a clone it makes itself; the local one is faster but hard-wired to this
# machine's paths and REQUIRES an already-installed clang to bootstrap from, so
# it is only a candidate when that bootstrap is actually there.
CLANG_SCRIPT=""
if [ -f "$SCRIPTS_DIR/build_clang_universal.sh" ]; then
    CLANG_SCRIPT="build_clang_universal.sh"
elif [ -f "$SCRIPTS_DIR/build_clang_local.sh" ]; then
    if [ -x "${BOOTSTRAP:-$HOME/opt/clang-24-3}/bin/clang++" ]; then
        CLANG_SCRIPT="build_clang_local.sh"
    else
        warn "only build_clang_local.sh is present and it needs an existing clang
         prefix to bootstrap from.  Skipping the clang stage.  Add
         build_clang_universal.sh, or set BOOTSTRAP= to an installed prefix."
    fi
fi

have_stage() {
    local s; s=$(stage_script "$1")
    [ -n "$s" ] && [ -f "$SCRIPTS_DIR/$s" ]
}

# ------------------------------------------------------------------- selection

[ -n "$ONLY" ] && for s in $(printf '%s' "$ONLY" | tr ',' ' '); do
    in_list "$s" "$(printf '%s' "$STAGES" | tr ' ' ',')" || die "--only: unknown stage '$s' (valid: $STAGES)"
done
[ -n "$SKIP" ] && for s in $(printf '%s' "$SKIP" | tr ',' ' '); do
    in_list "$s" "$(printf '%s' "$STAGES" | tr ' ' ',')" || die "--skip: unknown stage '$s' (valid: $STAGES)"
done

SELECTED=""; PRESENT=""
for s in $STAGES; do
    have_stage "$s" || continue
    PRESENT="$PRESENT $s"
    [ -n "$ONLY" ] && { in_list "$s" "$ONLY" || continue; }
    [ -n "$SKIP" ] && { in_list "$s" "$SKIP" && continue; }
    SELECTED="$SELECTED $s"
done

forced() { [ "$FORCE" = all ] || in_list "$1" "$FORCE"; }

# TODO_STAGES is what will actually run; the rest are reported as skipped.
TODO=""
for s in $SELECTED; do
    if stage_done "$s" && ! forced "$s"; then continue; fi
    TODO="$TODO $s"
done

if [ "$LIST" = 1 ]; then
    say "Stages"
    printf '  %-9s %-34s %-9s %s\n' STAGE SCRIPT COST STATE
    for s in $STAGES; do
        if have_stage "$s"; then scr=$(stage_script "$s"); else scr="(not present)"; fi
        c=$(stage_cost "$s")
        printf '  %-9s %-34s %-9s %s\n' "$s" "$scr" "${c%|*}" "$(stage_state "$s")"
    done
    exit 0
fi

# -------------------------------------------------------------------- preflight

say "Preflight"

[ -d "$SCRIPTS_DIR" ] || die "no scripts directory at $SCRIPTS_DIR"
[ -n "$PRESENT" ] || die "found none of the build scripts in $SCRIPTS_DIR.
       This script drives its siblings; put it in the same directory as them,
       or set SCRIPTS_DIR= to where they live."

for s in $PRESENT; do
    f="$SCRIPTS_DIR/$(stage_script "$s")"
    [ -r "$f" ] || die "cannot read $f"
    # Not being executable is the single most common way a downloaded script
    # fails, and it is trivially fixable, so fix it rather than complaining.
    [ -x "$f" ] || { chmod +x "$f" 2>/dev/null && warn "made $(basename "$f") executable"; }
    bash -n "$f" || die "$f does not parse -- refusing to run it"
done
ok "$(printf '%s' "$PRESENT" | wc -w) build scripts found and parse cleanly"

for t in bash make curl tar git; do
    command -v "$t" >/dev/null || warn "missing: $t -- some stages install it themselves, others will fail"
done

# Another build of one of these already running is the failure mode that wastes
# the most time: two bootstraps fighting over the same source and build trees.
# Match a SHELL that is EXECUTING one of these, not any process that merely
# mentions the filename -- an editor with the script open would otherwise look
# exactly like a build in progress.
running=$(pgrep -af '^(/[^ ]*/)?(ba|da|k|z)?sh +[^ ]*(build_(gcc|clang|python)_universal|build_clang_local|universal_linux_kernel_build)\.sh' 2>/dev/null \
          | grep -vE "^($$|$PPID) " || true)
if [ -n "$running" ]; then
    printf '%s\n' "$running" | sed 's/^/    /'
    if [ "$DRY" = 1 ]; then
        warn "one of these build scripts is already running (above).  A real run
         would refuse to start; --dry-run only reports it."
    else
        die "one of these build scripts is already running (above).
       Wait for it, or kill it.  Two bootstraps sharing a source tree corrupt
       each other in ways that surface hours later as a mystery link error."
    fi
fi

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_DIR")" "$OPT_ROOT"

# A partially installed prefix is invisible to the -current test but fatal to the
# next build, because every sibling refuses to install over an existing
# directory.  Say so now rather than three hours in.
for p in "$OPT_ROOT"/gcc-* "$OPT_ROOT"/clang-*; do
    case "$p" in *-current|*'*') continue ;; esac
    [ -d "$p" ] || continue
    if ! runs "$p/bin/gcc" && ! runs "$p/bin/clang"; then
        warn "$p exists but has no working compiler in it -- a leftover from an
         interrupted build.  The next build will refuse to install over it.
         Remove it, or pass GCC_PREFIX= / CLANG_PREFIX= to name a new one."
    fi
done

# ------------------------------------------------- package manager health
#
# WHY THIS EXISTS.  Three of the four siblings run
#
#     sudo dnf install -y $DNF_PKGS
#
# with nothing after it, under `set -e`.  So ANY dnf failure kills that build in
# its first ten seconds -- including a failure that has nothing whatever to do
# with the packages being asked for.  One third-party repo with an unverifiable
# GPG key is enough: dnf refuses to load its metadata, exits 1, and a five-hour
# build dies before it compiles a single file.  That is a miserable thing to
# discover in the morning, so find out now.

PKG_MGR=""
command -v dnf     >/dev/null 2>&1 && PKG_MGR=dnf
[ -z "$PKG_MGR" ] && command -v apt-get >/dev/null 2>&1 && PKG_MGR=apt
BROKEN_REPOS=""; MISSING_PKGS=""; PKG_DISABLE=""; NOSUCH_PKGS=""

# Installed BY NAME OR BY PROVIDES.  zlib-devel, for one, is a virtual provide
# satisfied by zlib-ng-compat-devel on el10: `rpm -q zlib-devel` says no while
# zlib.h is sitting in /usr/include.  Querying the name alone invents work.
pkg_present() { rpm -q --whatprovides "$1" >/dev/null 2>&1; }

# The package lists live in the siblings.  Read them from there rather than
# copying them here, where the copy would quietly drift out of date.
required_pkgs() {
    local st f
    for st in $TODO; do
        f="$SCRIPTS_DIR/$(stage_script "$st")"
        [ -f "$f" ] || continue
        sed -n '/^DNF_PKGS=/,/"[[:space:]]*$/p' "$f" | sed 's/^DNF_PKGS=//' | tr -d '"'
    done | tr ' \t' '\n\n' | sed '/^$/d' | sort -u
}

# Every dnf call here is bounded and has stdin closed.  dnf will happily stop
# and ask "Importing GPG key ... Is this ok [y/N]:" on a repo it cannot verify,
# and an unattended run sitting at that prompt waits forever.  </dev/null makes
# it take the default instead, and timeout means a slow mirror cannot stall the
# preflight either.
dnf_q() { timeout 90 dnf -q "$@" </dev/null 2>&1; }

# dnf4 skips unmatched arguments with strict=0; dnf5 renamed it.
DNF_SKIP="--setopt=strict=0"
case "$(dnf --version 2>/dev/null | head -1)" in 5.*) DNF_SKIP="--skip-unavailable" ;; esac

if [ "$PKG_MGR" = dnf ] && [ "$INSTALL_DEPS" = 1 ] && [ -n "$TODO" ]; then
    if probe=$(dnf_q repoquery --qf '%{name}' bash) && [ -n "$probe" ]; then
        ok "dnf metadata loads cleanly"
    else
        # dnf names the repo it could not read, in the error it just printed.
        # Reading that is free; probing every repo in turn costs a full
        # metadata load each, which can run to minutes on a cold cache.
        BROKEN_REPOS=$(printf '%s\n' "$probe" \
            | sed -n "s/.*for repo '\([^']*\)'.*/\1/p" | sort -u | tr '\n' ' ' | sed 's/ *$//')
        for r in $BROKEN_REPOS; do PKG_DISABLE="$PKG_DISABLE --disablerepo=$r"; done
        warn "dnf exits non-zero on this machine, and every sibling calls it with
         no error handling under set -e.  Left alone, the gcc and kernel stages
         would die in their first ten seconds.
         Broken repo(s): ${BROKEN_REPOS:-could not tell which}"
    fi

    # THE ORCHESTRATOR TAKES OVER DEPENDENCY INSTALLATION -- always, not only
    # when dnf is broken.  Three reasons, and each has already bitten:
    #
    #   1. A sibling's `sudo dnf install -y $PKGS` has no error handling, so any
    #      dnf failure kills a five-hour build in its first ten seconds.
    #   2. dnf fails a whole transaction over ONE unmatched argument, and
    #      build_python_universal.sh's list contains `find`, which is not a
    #      package on any distro.  With it, dnf installs NOTHING -- and that
    #      sibling's `|| warn ... continuing` then walks straight into a build
    #      missing every -devel package it just failed to install.
    #   3. Three stages each paying for their own dnf run is three chances to
    #      hit either of the above.
    #
    # Doing it once, here, with broken repos excluded and unmatched names
    # skipped, removes all three.  rpm answers what is missing: a local query,
    # no metadata, and it resolves PROVIDES as well as names.
    for pk in $(required_pkgs); do
        pkg_present "$pk" || MISSING_PKGS="$MISSING_PKGS $pk"
    done
    INSTALL_DEPS=0

    if [ -z "$MISSING_PKGS" ]; then
        ok "every package the selected stages need is already installed"
        printf '       running with INSTALL_DEPS=0 -- no stage will call dnf.\n'
    else
        warn "not installed:$MISSING_PKGS
         Installed once, up front, before any build starts.
         (Some may not be real package names; that is handled.)"
    fi
fi

# build_gcc_universal.sh hard-codes $HOME/opt for both the prefix default and
# the gcc-current symlink; it never reads OPT_ROOT.  So a non-default OPT_ROOT
# makes the done-test look somewhere the build will never write.
if [ "$OPT_ROOT" != "$HOME/opt" ] && in_list gcc "$(printf '%s' "$TODO" | tr ' ' ',')"; then
    warn "OPT_ROOT is $OPT_ROOT, but build_gcc_universal.sh always installs into
         $HOME/opt and links $HOME/opt/gcc-current.  Set GCC_PREFIX= to place
         the prefix, and expect gcc-current under \$HOME/opt regardless."
fi

RAM_GB=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)
CPUS=$(nproc)
printf '  host      : %s, %s cores, %sG RAM\n' \
    "$( . /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-unknown}" )" "$CPUS" "$RAM_GB"
printf '  kernel    : %s\n' "$(uname -r)"
printf '  scripts   : %s\n' "$SCRIPTS_DIR"
printf '  jobs      : %s\n' "$JOBS"

# Disk.  Sum only what is actually going to run.
need_gb=0
for s in $TODO; do c=$(stage_cost "$s"); d="${c#*|}"; need_gb=$(( need_gb + ${d%G} )); done
build_root="$HOME/src"; mkdir -p "$build_root"
avail_gb=$(df -BG --output=avail "$build_root" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
printf '  disk      : %sG free under %s, ~%sG wanted\n' "${avail_gb:-?}" "$build_root" "$need_gb"
[ "${avail_gb:-0}" -ge "$need_gb" ] || warn "that is not enough headroom.  Builds fail LATE when they run out
         of disk -- during a final link, after hours.  Free space, or use
         --skip= to drop a stage."

if [ "$RAM_GB" -lt 8 ] && [ -n "$TODO" ]; then
    warn "${RAM_GB}G of RAM.  LTO and ThinLTO links are the memory-hungry part;
         consider -j$(( RAM_GB > 2 ? RAM_GB / 2 : 1 )) instead of -j$JOBS."
fi

# --- the kernel needs decisions made before anything starts -------------------
KERNEL_LLVM_PREFIX=""
KERNEL_TC="$KERNEL_TOOLCHAIN"
if in_list kernel "$(printf '%s' "$TODO" | tr ' ' ',')"; then
    if [ "$KERNEL_TC" = auto ]; then
        # After the clang stage runs, clang-current will exist even if it does
        # not yet.  Decide on the END state, not the current one.
        if runs "$OPT_ROOT/clang-current/bin/clang" || in_list clang "$(printf '%s' "$TODO" | tr ' ' ',')"; then
            KERNEL_TC=llvm
        else
            KERNEL_TC=gcc
        fi
    fi
    [ "$KERNEL_TC" = llvm ] && KERNEL_LLVM_PREFIX="$OPT_ROOT/clang-current/bin/"
    printf '  kernel cc : %s%s\n' "$KERNEL_TC" \
        "$( [ "$KERNEL_TC" = llvm ] && printf ' (%s)' "$KERNEL_LLVM_PREFIX" )"

    # THE TRAP.  GNU make reads GNUmakefile in preference to Makefile, and a
    # kernel tree that has one usually uses it to pin LLVM.  kbuild decides
    # clang-vs-gcc on `ifneq ($(LLVM),)` alone, so that pin beats anything we
    # put in the environment -- a requested GCC build silently becomes a clang
    # one.  In llvm mode there is no problem: the kernel script passes LLVM= on
    # the make COMMAND LINE, which does override a makefile assignment.
    if [ -n "$KERNEL_SRC" ] && [ -f "$KERNEL_SRC/GNUmakefile" ] && [ "$KERNEL_TC" = gcc ]; then
        die "$KERNEL_SRC/GNUmakefile exists and this is a GCC kernel build.
       GNU make reads GNUmakefile before Makefile, and that file pins LLVM.
       kbuild only tests whether LLVM is non-empty, so the build would use
       clang while reporting gcc.  Move it aside first:
           mv $KERNEL_SRC/GNUmakefile $KERNEL_SRC/GNUmakefile.disabled
       or run this with KERNEL_TOOLCHAIN=llvm."
    fi
    if [ -n "$KERNEL_SRC" ] && [ -f "$KERNEL_SRC/.config" ] && [ "$KERNEL_INSTALL" = 1 ]; then
        warn "$KERNEL_SRC/.config will be OVERWRITTEN -- the kernel script starts
         from /boot/config-\$(uname -r) every time.  A copy is taken first, to
         $KERNEL_SRC/.config.saved-by-build-everything (once; a later run will
         not overwrite that copy with a generated one)."
    fi
    boot_avail=$(df -BM --output=avail /boot 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
    [ "${boot_avail:-0}" -ge 300 ] || warn "only ${boot_avail}M free on /boot.  A kernel plus its initramfs is
         typically 150-250M; installing into a full /boot leaves you with a
         half-written boot entry.  Remove an old kernel first."
fi

# ------------------------------------------------------------------------ plan

say "Plan"
printf '  %-9s %-9s %s\n' STAGE COST WHAT
for s in $SELECTED; do
    c=$(stage_cost "$s")
    if in_list "$s" "$(printf '%s' "$TODO" | tr ' ' ',')"; then
        mark="\033[1m RUN\033[0m"
        what="$(stage_label "$s")"
    else
        mark=" skip"
        what="$(stage_label "$s") -- already there: $(stage_state "$s")"
    fi
    printf '  %b %-9s %-9s %s\n' "$mark" "$s" "${c%|*}" "$what"
done
for s in $STAGES; do
    have_stage "$s" && continue
    printf '  %-9s %-9s %s\n' " none  $s" "-" "$(stage_script "$s" 2>/dev/null || printf '?') not in $SCRIPTS_DIR"
done

if [ -z "$TODO" ]; then
    say "Nothing to do"
    printf '  Every selected stage is already installed.  Use --force=<stage> to\n'
    printf '  rebuild one anyway, or --list to see what is on this machine.\n\n'
    exit 0
fi

printf '\n  logs      : %s/<stage>.log\n' "$LOG_DIR"
printf '  order     :%s\n' "$TODO"
sudo_stages=""
for s in $TODO; do stage_needs_sudo "$s" && sudo_stages="$sudo_stages $s"; done
[ -n "$sudo_stages" ] && printf '  sudo for  :%s\n' "$sudo_stages"
in_list clangenv "$(printf '%s' "$TODO" | tr ' ' ',')" && \
    printf '  will edit : %s (backed up alongside, and reversible with\n              %s/setup_clang_env.sh --remove)\n' "$HOME/.bashrc" "$SCRIPTS_DIR"
in_list kernel "$(printf '%s' "$TODO" | tr ' ' ',')" && [ "$KERNEL_INSTALL" = 1 ] && \
    printf '  will install a kernel into /boot and add a boot entry%s.\n' \
        "$( [ "$KERNEL_MAKE_DEFAULT" = 1 ] && printf ', AND MAKE IT THE DEFAULT' \
             || printf ' (the existing default boot entry is put back afterwards)' )"

if [ "$DRY" = 1 ]; then
    printf '\n  --dry-run: nothing was done.\n\n'
    exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
    printf '\n'
    read -r -p "  Proceed? [y/N] " reply
    case "$reply" in y|Y|yes|YES) ;; *) printf '  aborted.\n'; exit 1 ;; esac
fi

# ---------------------------------------------------------------------- locking
#
# mkdir is atomic on every filesystem worth using; a lock FILE plus a test is
# not.  The PID inside lets a stale lock be diagnosed rather than just deleted.

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    other=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?')
    if [ "$other" != '?' ] && kill -0 "$other" 2>/dev/null; then
        die "another build_everything.sh is running (pid $other)."
    fi
    warn "removing a stale lock from pid $other"
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR"
fi
printf '%s' "$$" > "$LOCK_DIR/pid"

SUDO_KEEPALIVE=""
# The keep-alive is best-effort and exits silently the first time `sudo -n`
# fails -- a PAM hiccup, someone running `sudo -k`, a sudoers timeout policy.
# So do not trust it: re-check immediately before every privileged step, while
# there is still something useful to say about it.
ensure_sudo() {
    sudo -n true 2>/dev/null && return 0
    warn "the sudo timestamp expired (the background refresher is gone).
         $1 needs root.  If nobody is at the keyboard this will wait here."
    sudo -v
}
cleanup() {
    [ -n "$SUDO_KEEPALIVE" ] && kill "$SUDO_KEEPALIVE" 2>/dev/null || true
    rm -rf "$LOCK_DIR"
}
# EXIT does the cleaning.  INT and TERM must EXIT rather than merely clean up:
# a trap handler that returns normally RESUMES the script where it was
# interrupted, so 'trap cleanup INT' would delete the lock, stop refreshing the
# sudo timestamp, and then carry on building for another four hours.
trap cleanup EXIT
trap 'printf "\n"; warn "interrupted -- stopping."; exit 130' INT
trap 'warn "terminated -- stopping."; exit 143' TERM

# ------------------------------------------------------------------------ sudo
#
# The problem: sudo's timestamp expires after ~15 minutes, but the kernel
# install happens hours after the last time anyone typed a password.  Without
# this, an unattended overnight run gets to the very last step and then sits at
# a password prompt until morning.  So: authenticate once, up front, while a
# human is still here, and refresh the timestamp in the background.

if [ -n "$sudo_stages" ] || [ -n "$MISSING_PKGS" ]; then
    step "sudo"
    [ -n "$sudo_stages" ] && printf '  Needed for:%s\n' "$sudo_stages"
    [ -n "$MISSING_PKGS" ] && printf '  Needed to install:%s\n' "$MISSING_PKGS"
    printf '  Authenticating now so nothing stalls at a password prompt hours from now.\n'
    if sudo -v; then
        ( while true; do sudo -n true 2>/dev/null || exit 0; sleep 50; kill -0 "$$" 2>/dev/null || exit 0; done ) &
        SUDO_KEEPALIVE=$!
        ok "sudo primed, timestamp refreshed every 50s"
        if [ -n "$MISSING_PKGS" ]; then
            # Done here, once, rather than left to the siblings: they would call
            # a dnf that cannot load metadata and die on the spot.
            printf '  Installing the packages dnf could not reach:%s\n' "$MISSING_PKGS"
            # strict=0 so that ONE unmatched name cannot abort the whole
            # transaction.  build_python_universal.sh's list contains `find`,
            # which is not a package on any distro -- /usr/bin/find belongs to
            # findutils -- and without this dnf installs NOTHING AT ALL.
            # shellcheck disable=SC2086 -- deliberate splitting of built lists
            dnf_out=$(sudo dnf install -y $DNF_SKIP $PKG_DISABLE $MISSING_PKGS \
                      </dev/null 2>&1) || true
            printf '%s\n' "$dnf_out" | tail -5 | sed 's/^/      /'

            # Trust rpm, not dnf's exit code.
            still=""
            for pk in $MISSING_PKGS; do pkg_present "$pk" || still="$still $pk"; done
            if [ -z "$still" ]; then
                ok "dependencies installed"
            else
                # dnf says which arguments matched nothing.  Anything it did not
                # say that about is a package that EXISTS and failed to install,
                # which is a real problem worth stopping for.
                # FIXED-STRING matching, not a regex: package names contain
                # regex metacharacters.  'gcc-c++' as an ERE is a nested
                # quantifier -- grep errors out, the test fails, and a package
                # that merely does not exist is reported as a hard failure.
                real=""
                for pk in $still; do
                    printf '%s\n' "$dnf_out" | grep -F 'No match for argument' \
                        | grep -qF "$pk" || real="$real $pk"
                done
                if [ -n "$real" ]; then
                    die "available but did not install:$real
       Something is wrong with the package manager, and a build that starts
       now would fail hours from here.  Fix it and re-run:
           sudo dnf install -y$PKG_DISABLE$real"
                fi
                warn "no such package:$still
         A typo in a sibling's package list, not a missing dependency -- the
         tool itself is present.  Continuing."
            fi
        fi
    else
        # Distro packages are the soft case; a kernel install is not.
        for s in $sudo_stages; do
            case "$s" in
                kernel|dkms) die "sudo is required for the $s stage and it is not available." ;;
            esac
        done
        warn "no sudo -- setting INSTALL_DEPS=0.  Every build dependency must
         already be installed or the builds will fail in configure."
        INSTALL_DEPS=0
    fi
fi

# -------------------------------------------------------------------- run_stage
#
# ENVIRONMENT HYGIENE.  Every sibling reads short, generic variable names, and
# they disagree about what they mean: SRC is an LLVM checkout to one, a kernel
# tree to another, and a tarball root to a third.  A developer's ~/.bashrc makes
# it worse by exporting CC, CXX and LLVM at a self-built clang.  So each stage
# starts from a cleared slate -- every name any sibling reads is removed -- and
# only the values that stage should see are put back.  Without the `env -u`
# list, running this from an ordinary interactive shell silently builds the
# wrong things with the wrong compiler.

SHARED_UNSET=(
    SRC PREFIX BUILD SRC_ROOT OPT_ROOT TARGETS MODE VERSION TUNE BOOTSTRAP
    INSTALL GPU KVER WORK LOCALVERSION TOOLCHAIN LLVM_PREFIX LLVM
    CC CXX HOST_CC HOST_CXX PY LANGUAGES LINK_CURRENT KEEP_DEBUG_INFO
    WITH_ARCH WITH_TUNE HOST_MODE PYPY_HOST RC WITH_CC_EXPORTS
    TRACK SERIES GIT_REF GCC_GIT MULTILIB CPY_VERSION PYPY_VERSION GCC_VERSION
    LLVM_REF MARCH CONFIGURE_EXTRA BOOT_ARCH JOBS INSTALL_DEPS
    CFLAGS CXXFLAGS LDFLAGS LD_LIBRARY_PATH MAKEFLAGS
    # LD_RUN_PATH is the quiet one: ld copies it into the RPATH of every binary
    # it links, so without clearing it the new toolchain ships hard-coded
    # references to whatever else happens to be in ~/opt -- including, on this
    # machine, a root-owned clang-24-2 that may not exist tomorrow.
    LD_RUN_PATH LIBRARY_PATH CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LD_PRELOAD
)
unset_args() { local v; for v in "${SHARED_UNSET[@]}"; do printf -- '-u\n%s\n' "$v"; done; }

FAILED=""; DONE_LIST=""
run_stage() {
    local id="$1"; shift
    local script="$SCRIPTS_DIR/$(stage_script "$id")"
    local log="$LOG_DIR/$id.log"
    local start=$SECONDS

    step "$id -- $(stage_label "$id")"
    printf '  script : %s\n  log    : %s\n  path   : %s\n  started: %s\n' \
        "$script" "$log" "$( [ "$KEEP_PATH" = 1 ] && printf '%s' "$PATH" || printf '%s' "$STAGE_PATH" )" \
        "$(date '+%Y-%m-%d %H:%M:%S')"

    local envargs=()
    mapfile -t envargs < <(unset_args)
    # PATH is ASSIGNED, never unset: with no PATH at all, execvp falls back to a
    # confstr default and what the stage finds becomes anybody's guess.
    local pathargs=()
    [ "$KEEP_PATH" = 1 ] || pathargs=(PATH="$STAGE_PATH")

    local rc=0
    if env "${envargs[@]}" "${pathargs[@]}" "$@" "$script" 2>&1 | tee "$log"; then rc=0
    else rc=${PIPESTATUS[0]}; fi

    local took=$(( SECONDS - start ))
    if [ "$rc" -eq 0 ]; then
        # A zero exit is a claim, not proof.  Check the machine.
        # In build-only mode stage_done deliberately answers "no" (nothing was
        # installed, so nothing is detectable).  That is the right answer to
        # "is it already done?" and the wrong answer to "did it just succeed?",
        # and without this guard a perfect build-only run is called a failure.
        if stage_done "$id" || stage_optional "$id" \
           || { [ "$id" = kernel ] && [ "$KERNEL_INSTALL" != 1 ]; }; then
            ok "$id finished in $(hms $took)"
            DONE_LIST="$DONE_LIST $id"
            return 0
        fi
        warn "$id exited 0 but the result is not detectable
         (expected something under $OPT_ROOT, or a kernel in /lib/modules).
         Treating it as a failure.  Full log: $log"
        rc=1
    fi

    printf '\n'
    warn "$id FAILED after $(hms $took) (exit $rc).  End of the log:"
    # Prefer lines that look like errors, but NEVER print an empty section: a
    # build can fail with nothing matching the pattern (killed, OOM, a tool that
    # says only 'boom'), and a header followed by silence tells you nothing at
    # the hour you are least able to go digging.
    local errs
    errs=$(grep -nE 'error:|Error [0-9]+|configure: error|ERROR:|fatal|Killed|No space' "$log" 2>/dev/null | tail -6 || true)
    [ -n "$errs" ] || errs=$(tail -8 "$log" 2>/dev/null || true)
    [ -n "$errs" ] || errs="(the log is empty -- the stage produced no output at all)"
    printf '%s\n' "$errs" | sed 's/^/      /'
    printf '      full log: %s\n' "$log"
    FAILED="$FAILED $id"
    return "$rc"
}

# ------------------------------------------------------------------------- stage
#                                                                       arguments
#
# One function per stage, purely so the translation from this script's prefixed
# knobs into each sibling's own variable names is visible in one place.

args_gcc() {
    printf 'JOBS=%s\n'          "$JOBS"
    printf 'INSTALL_DEPS=%s\n'  "$INSTALL_DEPS"
    printf 'LINK_CURRENT=1\n'
    printf 'TRACK=%s\n'         "$GCC_TRACK"
    printf 'MODE=%s\n'          "$GCC_MODE"
    printf 'LANGUAGES=%s\n'     "$GCC_LANGUAGES"
    [ -n "$GCC_PREFIX" ] && printf 'PREFIX=%s\n' "$GCC_PREFIX"
    return 0
}

args_clang() {
    printf 'JOBS=%s\n'          "$JOBS"
    printf 'INSTALL_DEPS=%s\n'  "$INSTALL_DEPS"
    printf 'LINK_CURRENT=1\n'
    if [ "$CLANG_SCRIPT" = build_clang_universal.sh ]; then
        printf 'LLVM_REF=%s\n'  "$CLANG_REF"
        printf 'TUNE=%s\n'      "$CLANG_TUNE"
        # ~/opt/clang-<today> is the script's default, and it refuses to install
        # over an existing prefix -- so a second attempt on the same day needs a
        # name that has not been used yet.
        printf 'PREFIX=%s\n'    "${CLANG_PREFIX:-$(unique_prefix "$OPT_ROOT/clang-$(date +%Y%m%d)")}"
    else
        # build_clang_local.sh names its own prefix from VERSION and bootstraps
        # from the previous one; it has no PREFIX knob.  VERSION is not
        # optional: its default is a fixed string, and the script dies outright
        # if that prefix already exists.
        printf 'VERSION=%s\n' "${CLANG_LOCAL_VERSION:-$(date +%Y%m%d)}"
        [ -n "${BOOTSTRAP:-}" ] && printf 'BOOTSTRAP=%s\n' "$BOOTSTRAP"
    fi
    return 0
}

args_clangenv() {
    printf 'PREFIX=%s\n'           "$OPT_ROOT/clang-current"
    printf 'WITH_CC_EXPORTS=%s\n'  "$SETUP_ENV_EXPORTS"
    return 0
}

args_python() {
    printf 'JOBS=%s\n'          "$JOBS"
    printf 'INSTALL_DEPS=%s\n'  "$INSTALL_DEPS"
    printf 'LINK_CURRENT=1\n'
    printf 'OPT_ROOT=%s\n'      "$OPT_ROOT"
    printf 'TARGETS=%s\n'       "$PY_TARGETS"
    return 0
}

args_kernel() {
    printf 'JOBS=%s\n'              "$JOBS"
    printf 'INSTALL_DEPS=%s\n'      "$INSTALL_DEPS"
    printf 'INSTALL=%s\n'           "$KERNEL_INSTALL"
    printf 'GPU=%s\n'               "$KERNEL_GPU"
    printf 'LOCALVERSION=%s\n'      "$KERNEL_LOCALVERSION"
    printf 'KEEP_DEBUG_INFO=%s\n'   "$KERNEL_KEEP_DEBUG_INFO"
    printf 'TOOLCHAIN=%s\n'         "$KERNEL_TC"
    [ "$KERNEL_TC" = llvm ] && printf 'LLVM_PREFIX=%s\n' "$KERNEL_LLVM_PREFIX"
    [ -n "$KERNEL_SRC" ]     && printf 'SRC=%s\n'         "$KERNEL_SRC"
    [ -n "$KERNEL_VERSION" ] && printf 'KVER=%s\n'        "$KERNEL_VERSION"
    return 0
}

args_dkms() { return 0; }

# ------------------------------------------------------------------------- run

TOTAL_START=$SECONDS
say "Building${TODO}"
printf '  Started %s.  Safe to detach if this is a tmux/screen session.\n' "$(date '+%Y-%m-%d %H:%M:%S')"

for s in $TODO; do
    mapfile -t sargs < <("args_$s")

    if [ "$s" = dkms ]; then
        # The only stage that must run AS root rather than merely use sudo, and
        # the only one whose "not applicable" is a success, not a failure: the
        # script is pinned to one vendor package version and simply is not for
        # this machine if that is absent.
        step "dkms -- $(stage_label dkms)"
        ensure_sudo "the amdgpu-dkms cap"
        # fix-amdgpu-dkms.sh backs the vendor file up with `cp -a`, but on this
        # packaging /usr/src/amdgpu-*/dkms.conf is a SYMLINK into amd/dkms/.
        # cp -a implies -d, so it copies the LINK; the heredoc then appends
        # through that link to the real file, and the "backup" points at the
        # modified content.  Take a dereferenced copy first so there is
        # something to actually restore.
        for dc in /usr/src/amdgpu-*/dkms.conf; do
            [ -e "$dc" ] || continue
            real_dc=$(readlink -f "$dc")
            if [ ! -e "$real_dc.pre-build-everything" ]; then
                sudo cp -aL "$real_dc" "$real_dc.pre-build-everything" \
                    && ok "real backup: $real_dc.pre-build-everything" \
                    || warn "could not back up $real_dc"
            fi
        done
        if sudo "$SCRIPTS_DIR/$(stage_script dkms)" 2>&1 | tee "$LOG_DIR/dkms.log"; then
            ok "amdgpu-dkms capped"
            DONE_LIST="$DONE_LIST dkms"
        else
            warn "the amdgpu-dkms cap did not apply (it is pinned to one package
         version).  Continuing -- it is a mitigation, not a dependency.  If
         DKMS then fails against the new kernel, cap it by hand:
             sudo sh -c 'echo BUILD_EXCLUSIVE_KERNEL_MAX=\"6.18\" >> /usr/src/amdgpu-*/dkms.conf'
         Log: $LOG_DIR/dkms.log"
        fi
        continue
    fi

    # Take the copy here rather than in the preflight: --dry-run and an aborted
    # confirmation must leave the tree untouched.  Only ever create it once --
    # on a second run .config is the GENERATED one, and copying that over the
    # backup would destroy the original it exists to protect.
    if [ "$s" = kernel ] && [ -n "$KERNEL_SRC" ] && [ -f "$KERNEL_SRC/.config" ]; then
        kbak="$KERNEL_SRC/.config.saved-by-build-everything"
        if [ -e "$kbak" ]; then
            skip "keeping the existing $kbak"
        elif cp -a "$KERNEL_SRC/.config" "$kbak"; then
            ok "saved your .config -> $kbak"
        else
            die "could not back up $KERNEL_SRC/.config -- refusing to overwrite it"
        fi
    fi

    # GRUB_DEFAULT=saved plus BLS means the kernel-install hooks point
    # saved_entry at whatever was just installed -- so an untested kernel built
    # while nobody was watching becomes what the machine boots by default.
    # Remember what the default was, and put it back afterwards unless asked
    # not to.  The new kernel is still in the menu; it just is not the default.
    if [ "$s" = kernel ] && [ "$KERNEL_INSTALL" = 1 ] && [ "$KERNEL_MAKE_DEFAULT" != 1 ]; then
        ensure_sudo "reading the current default boot entry"
        PREV_DEFAULT=$(sudo grubby --default-kernel 2>/dev/null || true)
        [ -n "$PREV_DEFAULT" ] && printf '  current default boot entry: %s\n' "$PREV_DEFAULT"
    fi

    if ! run_stage "$s" "${sargs[@]}"; then
        if stage_optional "$s"; then
            # Drop it from FAILED too, or the run ends with a non-zero exit
            # after having explicitly decided the failure did not matter.
            FAILED=$(printf '%s' "$FAILED" | tr ' ' '\n' | grep -vx "$s" | tr '\n' ' ')
            warn "$s is optional -- carrying on."
            continue
        fi
        if [ "$KEEP_GOING" = 1 ]; then
            warn "--keep-going: carrying on to the next stage."
            continue
        fi
        printf '\n'
        say "Stopped at $s"
        printf '  Nothing after it ran.  Fix the cause, then re-run this script:\n'
        printf '  completed stages are detected and skipped, so it resumes rather\n'
        printf '  than starting over.\n\n'
        exit 1
    fi

    if [ "$s" = kernel ] && [ -n "${PREV_DEFAULT:-}" ]; then
        if sudo grubby --set-default="$PREV_DEFAULT" >/dev/null 2>&1; then
            ok "default boot entry left as $PREV_DEFAULT -- the new kernel is in
       the menu but is NOT the default.  Pick it deliberately, and once it has
       proved itself:  sudo grubby --set-default=/boot/vmlinuz-<release>"
        else
            warn "could not restore the default boot entry.  The kernel you just
         built is probably now the DEFAULT, untested.  Check with
         'sudo grubby --default-kernel' before you reboot."
        fi
    fi

    # The kernel is configured from the CURRENT toolchain state, so a clang
    # installed a moment ago must be visible before the kernel stage reads it.
    [ "$s" = clang ] && [ "$KERNEL_TC" = llvm ] && KERNEL_LLVM_PREFIX="$OPT_ROOT/clang-current/bin/"
done

# --------------------------------------------------------------------- summary

TOTAL=$(( SECONDS - TOTAL_START ))
say "Summary  (total $(hms $TOTAL))"
printf '\n  %-9s %s\n' STAGE RESULT
for s in $SELECTED; do
    if in_list "$s" "$(printf '%s' "$DONE_LIST" | tr ' ' ',')"; then
        printf '  \033[32m%-9s\033[0m built now -- %s\n' "$s" "$(stage_state "$s")"
    elif in_list "$s" "$(printf '%s' "$FAILED" | tr ' ' ',')"; then
        printf '  \033[31m%-9s\033[0m FAILED -- see %s/%s.log\n' "$s" "$LOG_DIR" "$s"
    else
        printf '  %-9s already present -- %s\n' "$s" "$(stage_state "$s")"
    fi
done

cat <<SUMMARY_EOF

  Prefixes live under $OPT_ROOT, each with a -current symlink.  Point tools at
  the SYMLINK, never at a versioned directory: the next rebuild replaces the
  directory, and a stale path fails silently -- you keep using the system
  compiler while believing you are not.

      export PATH="$OPT_ROOT/clang-current/bin:$OPT_ROOT/gcc-current/bin:\$PATH"

  Anything built with the new gcc also needs its newer libstdc++ at RUN time:

      export LD_LIBRARY_PATH="$OPT_ROOT/gcc-current/lib64\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"

SUMMARY_EOF

if in_list kernel "$(printf '%s' "$DONE_LIST" | tr ' ' ',')" && [ "$KERNEL_INSTALL" = 1 ]; then
    krel=$(kernel_installed_release || printf 'the new kernel')
    cat <<KERNEL_EOF
  A kernel was installed: $krel

  Do NOT remove your current kernel until this one has booted and you have
  checked that graphics, networking and any DKMS modules came up.  Reboot and
  pick it from the boot menu; if it misbehaves, pick the old one and nothing is
  lost.  Afterwards:

      uname -r                       # is it running?
      ls /dev/dri /dev/kfd           # did the GPU stack come up?
      dmesg --level=err,warn | head  # what complained?

KERNEL_EOF
fi

[ -z "$FAILED" ] || die "these stages failed:$FAILED"
say "Done"
