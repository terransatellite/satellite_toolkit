#!/usr/bin/env bash
#
# universal_linux_kernel_build.sh
#
# Download, configure, build and install a mainline Linux kernel that works on
# whatever graphics you actually have -- AMD, NVIDIA, Intel, a virtual machine,
# or a headless server -- with the GPU compute and NPU ("AI") paths turned on.
#
# It starts from your running distro's config so your existing hardware keeps
# working, then enforces the options GPU compute needs and that hand-rolled
# configs most often get wrong.
#
#   ./universal_linux_kernel_build.sh                 # detect, build only
#   INSTALL=1 ./universal_linux_kernel_build.sh       # build, then install
#
#   GPU=auto        detect from lspci (default)
#   GPU=all         every vendor -- a kernel that boots on anything
#   GPU=amd,intel   a comma-separated list: amd nvidia intel vm
#
#   KVER=6.12.9 ./universal_linux_kernel_build.sh     # a specific release
#   SRC=~/src/linux ./universal_linux_kernel_build.sh # an existing tree
#   TOOLCHAIN=llvm LLVM_PREFIX=$HOME/opt/clang-current/bin/ ./universal_...sh
#
#   CLEAN=auto      mrproper the tree only if the last attempt did not finish
#   CLEAN=always    mrproper first, every time
#   CLEAN=never     never clean; always build incrementally
#
#   CONFIG_BASE=auto      keep the tree's .config if it has one, else the distro's
#   CONFIG_BASE=distro    always reseed from /boot/config-$(uname -r)
#   CONFIG_BASE=defconfig start from the arch defconfig
#   CONFIG_BASE=<path>    start from a config file you name
#   FRAME_WARN=8192 -Wframe-larger-than=; 0 disables the check entirely
#
# Two files are written beside this script (override with STATE_FILE= / BUILD_LOG=):
#   kernel_build.ini   what was done to each source tree, one [section] each
#   kernel_build.log   full output of the last build (previous kept as .log.prev)
#
# THE ONE THING THIS SCRIPT EXISTS TO GET RIGHT: GPU drivers are MODULES.
# Built-in (=y) is the most common mistake in hand-rolled GPU kernel configs.
# A built-in driver probes before the root filesystem is mounted, so all of its
# firmware must already be in the initramfs -- but dracut and initramfs-tools
# decide which firmware to include by looking at which MODULES are installed,
# and a built-in driver is not a module. The result is a driver that cannot load
# its firmware: no display, no /dev/kfd. As modules they also stay unloadable
# and blacklistable, which the NVIDIA proprietary stack requires.
#
# Licence: public domain / CC0. No warranty. It installs a kernel; read it first.

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

GPU="${GPU:-auto}"
KVER="${KVER:-}"                                  # empty = latest stable
SRC="${SRC:-}"                                    # empty = download a tarball
WORK="${WORK:-$HOME/src}"
LOCALVERSION="${LOCALVERSION:--gpu}"
FRAME_WARN="${FRAME_WARN:-8192}"     # -Wframe-larger-than=; 0 disables, 8192 is the Kconfig max
# De-export it.  This is OUR knob, but kbuild has a variable of the SAME NAME
# and means something else by it: scripts/setlocalversion ends with
#     echo "${KERNELVERSION}${file_localversion}${config_localversion}${LOCALVERSION}${scm_version}"
# so a LOCALVERSION that is in make's ENVIRONMENT is appended on top of the
# CONFIG_LOCALVERSION this script sets below -- and the release comes out as
# 7.2.0-gpu-gpu.  It only bites when the value arrived from the environment
# (an orchestrator, or `LOCALVERSION=-x ./this.sh`), because a plain assignment
# does not export, but a value inherited from the environment stays exported
# through re-assignment.  The config setting below is the only path we want.
export -n LOCALVERSION 2>/dev/null || true
# ------------------------------------------------------------- build bookkeeping
# Two files live next to this script.  kernel_build.ini records what was done to
# each source tree so a later run can tell an interrupted or failed tree from a
# finished one; kernel_build.log is the full output of the last build.
#
# The .ini is keyed by SOURCE TREE, not by version -- one [section] per tree --
# because a single machine easily ends up with several (a KVER= build in ~/src
# beside an SRC=/KERNEL_SRC= tree elsewhere), and a state file that assumed one
# tree would confidently report the wrong one.  Delete it any time: a missing
# entry simply reads as "previous attempt unknown".
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)" || SELF_DIR="$PWD"
STATE_FILE="${STATE_FILE:-$SELF_DIR/kernel_build.ini}"
BUILD_LOG="${BUILD_LOG:-$SELF_DIR/kernel_build.log}"
CLEAN="${CLEAN:-auto}"    # auto: clean only an unfinished tree | always | never
# Where the configuration comes from.  "auto" keeps a .config the tree already
# has and only seeds from the running distro when there is none, so re-running
# no longer throws away work between runs.  The enforcement below re-applies to
# whatever base is chosen, and the verify block still refuses to install a
# config that did not come out right, so keeping an existing one is safe.
CONFIG_BASE="${CONFIG_BASE:-auto}"   # auto | distro | defconfig | /path/to/config
# Both are touched only AFTER this script has chdir'd into $WORK and then $SRC,
# so a relative override would land somewhere the user did not mean.  Resolve
# them against the directory we were invoked from, once, while that is still cwd.
case $STATE_FILE in /*) ;; *) STATE_FILE="$PWD/$STATE_FILE" ;; esac
case $BUILD_LOG  in /*) ;; *) BUILD_LOG="$PWD/$BUILD_LOG"   ;; esac
JOBS="${JOBS:-$(nproc)}"
INSTALL="${INSTALL:-0}"
INSTALL_DEPS="${INSTALL_DEPS:-1}"
TOOLCHAIN="${TOOLCHAIN:-gcc}"                     # gcc | llvm
LLVM_PREFIX="${LLVM_PREFIX:-}"                    # e.g. $HOME/opt/clang-current/bin/  (TRAILING SLASH)
KEEP_DEBUG_INFO="${KEEP_DEBUG_INFO:-0}"           # 1 keeps DWARF+BTF (far bigger, far slower)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- state helpers
# Loaded into an associative array and flushed whole, rather than edited in
# place.  Sections for OTHER trees are copied through verbatim, so two trees
# never clobber each other, and a flush after every set means the record
# survives a kill -9 mid-build -- which is exactly the case it exists to detect.
declare -A STATE=()
STATE_OK=0
STATE_STAGE=startup

state_init() {
    local d; d="$(dirname "$STATE_FILE")"
    if [ -d "$d" ] && [ -w "$d" ] && { [ ! -e "$STATE_FILE" ] || [ -w "$STATE_FILE" ]; }; then
        STATE_OK=1
    else
        warn "cannot write $STATE_FILE -- continuing without build-state tracking.
       Set STATE_FILE=<path> to somewhere writable, or pass CLEAN=always/never
       to decide about cleaning explicitly."
    fi
}

state_load() {
    [ "$STATE_OK" = 1 ] && [ -f "$STATE_FILE" ] || return 0
    local line k v inm=0
    while IFS= read -r line; do
        case "$line" in
            "[$SRC]") inm=1; continue ;;
            \[*)      inm=0; continue ;;
            ''|'#'*)  continue ;;
        esac
        [ "$inm" = 1 ] || continue
        case "$line" in *=*) ;; *) continue ;; esac
        k=${line%%=*}; v=${line#*=}
        k=${k// /}; k=${k//$'\t'/}; v=${v# }
        # An if, not `[ -n "$k" ] && ...`: a while-read returns the status of the
        # last command in its body, so a trailing blank key would make state_load
        # return 1 and `set -e` would kill the run silently at the call site.
        if [ -n "$k" ]; then STATE["$k"]="$v"; fi
    done < "$STATE_FILE"
    return 0
}

state_flush() {
    [ "$STATE_OK" = 1 ] || return 0
    local tmp k
    tmp="$(mktemp "${STATE_FILE}.XXXXXX" 2>/dev/null)" || return 0
    {
        printf '# universal_linux_kernel_build.sh -- build state, one [section] per source tree.\n'
        printf '# Written automatically; safe to delete.\n'
        # cat -s squeezes the blank line that the awk pass preserves and the
        # printf below re-emits; without it the file gains one blank per write.
        [ -f "$STATE_FILE" ] && awk -v sec="[$SRC]" '/^\[/ { keep = ($0 != sec) } keep' "$STATE_FILE" | cat -s
        printf '\n[%s]\n' "$SRC"
        for k in "${!STATE[@]}"; do printf '%s = %s\n' "$k" "${STATE[$k]}"; done | sort
    } > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE_FILE" || rm -f "$tmp"
}

state_set() { [ "$STATE_OK" = 1 ] || return 0; STATE["$1"]="$2"; state_flush; }

# Records a failure without changing the exit status.  `local rc=$?` must stay
# the first statement or it captures the wrong command.
_state_on_exit() {
    local rc=$?
    if [ "$STATE_OK" = 1 ] && [ "$rc" -ne 0 ]; then
        STATE[status]=failed
        STATE[stage]="$STATE_STAGE"
        STATE[exit_code]="$rc"
        STATE[finished]="$(date -Is 2>/dev/null || date 2>/dev/null || true)"
        state_flush
    fi
    return 0
}

DNF_PKGS="gcc make flex bison bc openssl-devel elfutils-libelf-devel dwarves
          ncurses-devel rsync cpio perl python3 xz zstd git tar pciutils"
APT_PKGS="build-essential flex bison bc libssl-dev libelf-dev dwarves
          libncurses-dev rsync cpio perl python3 xz-utils zstd git tar pciutils"

# Best-effort, deliberately.  A package manager exits non-zero for reasons that
# have nothing to do with the packages asked for -- one third-party repo with an
# unverifiable GPG key is enough, and dnf then refuses to load metadata and
# exits 1 in the first ten seconds.  Under `set -e` a bare install call would
# take a multi-hour build down with it for a reason the user cannot see from the
# error.  So warn and carry on: the Preflight loop immediately below is the real
# gate, and it names the tool that is actually missing.
if [ "$INSTALL_DEPS" = 1 ]; then
    say "Installing build dependencies (sudo)"
    if command -v dnf >/dev/null; then
        sudo dnf install -y $DNF_PKGS \
            || warn "dnf could not install every dependency; continuing.
       If the build fails on a missing header, that is where to look.  A single
       broken repo is the usual cause -- retry with
           sudo dnf install -y --disablerepo=<broken> $DNF_PKGS
       or set INSTALL_DEPS=0 if you know the packages are already present."
    elif command -v apt-get >/dev/null; then
        # Separate statements, not `update && install`: chained, a failed update
        # makes the whole list non-zero and set -e kills the script with nothing
        # printed.  A stale index is not a reason to refuse to build.
        sudo apt-get update || warn "apt-get update failed; using the cached package lists"
        sudo apt-get install -y $APT_PKGS \
            || warn "apt-get could not install every dependency; continuing."
    else
        warn "no dnf/apt-get -- install kernel build deps yourself, then re-run with INSTALL_DEPS=0"
    fi
fi

say "Preflight"
for t in make gcc flex bison bc rsync cpio; do command -v "$t" >/dev/null || die "missing: $t"; done
[ "$(id -u)" -ne 0 ] || die "do not run this whole script as root.
       The build runs as you; only the install steps use sudo."

# ------------------------------------------------------------------ GPU detect
#
# VM/legacy drivers are ALWAYS enabled. They are a few hundred KB of modules and
# they are the difference between "this kernel boots on my machine" and "this
# kernel boots anywhere" -- a VM, a server with an ASPEED BMC, a rescue boot on
# unfamiliar hardware. There is no reason to leave them out.

W_AMD=0; W_NV=0; W_INTEL=0
if command -v lspci >/dev/null; then
    while IFS= read -r line; do
        case "$line" in
            *[Aa][Mm][Dd]*|*ATI*) W_AMD=1;   printf '  found: %s\n' "$line" ;;
            *NVIDIA*|*nVidia*)    W_NV=1;    printf '  found: %s\n' "$line" ;;
            *Intel*)              W_INTEL=1; printf '  found: %s\n' "$line" ;;
        esac
    done < <(lspci -nn 2>/dev/null | grep -Ei 'vga|3d|display' || true)
fi

case "$GPU" in
  auto)
    if [ "$W_AMD$W_NV$W_INTEL" = "000" ]; then
        warn "no AMD/NVIDIA/Intel GPU detected (headless, VM, or unknown hardware).
         Enabling all vendors so the kernel boots anywhere. Use GPU= to narrow it."
        W_AMD=1; W_NV=1; W_INTEL=1
    fi ;;
  all) W_AMD=1; W_NV=1; W_INTEL=1 ;;
  *)
    W_AMD=0; W_NV=0; W_INTEL=0
    IFS=',' read -ra want <<< "$GPU"
    for v in "${want[@]}"; do case "${v// /}" in
        amd) W_AMD=1 ;; nvidia|nv) W_NV=1 ;; intel) W_INTEL=1 ;; vm|none) ;;
        *) die "unknown GPU vendor '$v' (use: auto all amd nvidia intel vm)" ;;
    esac; done ;;
esac
printf '  enabling  : %s%s%s%s\n' \
    "$([ "$W_AMD" = 1 ] && echo 'AMD ')" "$([ "$W_NV" = 1 ] && echo 'NVIDIA ')" \
    "$([ "$W_INTEL" = 1 ] && echo 'Intel ')" 'VM/legacy(always)'

MAKE_ARGS=(-j"$JOBS")
if [ "$TOOLCHAIN" = llvm ]; then
    [ -n "$LLVM_PREFIX" ] && case "$LLVM_PREFIX" in */) ;; *) die "LLVM_PREFIX must end in a slash -- kbuild treats it as a path prefix";; esac
    MAKE_ARGS+=(LLVM="${LLVM_PREFIX:-1}")
    # tools/lib/bpf can be compiled by gcc (non-PIC) while the host link runs
    # clang/ld.lld, which defaults to PIE. That mix fails building
    # tools/bpf/resolve_btfids with R_X86_64_32 relocation errors.
    MAKE_ARGS+=(HOSTLDFLAGS=-no-pie)
    printf '  toolchain : clang (%s)\n' "${LLVM_PREFIX:-from PATH}"
else
    printf '  toolchain : %s\n' "$(gcc --version | head -1)"
fi

# ---------------------------------------------------------------------- source

if [ -z "$SRC" ]; then
    if [ -z "$KVER" ]; then
        KVER=$(curl -fsSL https://www.kernel.org/finger_banner 2>/dev/null \
            | awk '/latest stable/{print $NF; exit}') || true
        [ -n "$KVER" ] || die "could not determine the latest kernel version; set KVER="
    fi
    SRC="$WORK/linux-$KVER"
    if [ ! -d "$SRC" ]; then
        mkdir -p "$WORK"; cd "$WORK"
        say "Downloading linux-$KVER"
        curl -fL --progress-bar -O "https://cdn.kernel.org/pub/linux/kernel/v${KVER%%.*}.x/linux-$KVER.tar.xz"
        tar xf "linux-$KVER.tar.xz"
    fi
fi
[ -f "$SRC/Makefile" ] || die "no kernel source at $SRC"
cd "$SRC"
# Canonicalise now that we are standing in it, BEFORE it becomes a state key.
# SRC is the [section] name, and a trailing slash from tab-completion, a
# relative "." or a symlinked path would each key the same tree differently --
# and a key that misses reads as "no previous attempt", which under CLEAN=auto
# means mrproper on a tree that was perfectly good.  pwd -P resolves all three.
SRC="$(pwd -P)"
printf '  source    : %s (%s)\n  jobs      : %s\n' "$SRC" "$(make -s kernelversion)" "$JOBS"

# ------------------------------------------------------- previous build attempt

state_init
state_load
trap _state_on_exit EXIT

say "Previous build attempt"
prev_status="${STATE[status]:-}"
# -print -quit stops at the first hit: on a fully built tree this answers in
# milliseconds, where counting all ~16000 objects would walk 4.6G of tree.
objects="$(find . -name '*.o' -not -path './scripts/*' -print -quit 2>/dev/null || true)"

if [ -n "$prev_status" ]; then
    printf '  recorded  : %s' "$prev_status"
    [ -n "${STATE[finished]:-}" ] && printf ' (%s)' "${STATE[finished]}"
    [ "$prev_status" = failed ] && printf ' at stage %s' "${STATE[stage]:-unknown}"
    printf '\n'
else
    printf '  recorded  : nothing -- no entry for this tree in %s\n' "$STATE_FILE"
fi

do_clean=0; clean_why=""
case "$CLEAN" in
  never)  clean_why="CLEAN=never" ;;
  always) do_clean=1; clean_why="CLEAN=always" ;;
  auto)
    if [ -z "$objects" ]; then
        clean_why="no object files present -- nothing to clean"
    else
        case "$prev_status" in
          built|installed) clean_why="the previous run finished ($prev_status); rebuilding incrementally is correct and far faster" ;;
          building)        do_clean=1; clean_why="a previous run started and never recorded finishing -- interrupted or killed" ;;
          failed)          do_clean=1; clean_why="the previous run failed at stage ${STATE[stage]:-unknown}" ;;
          "")              do_clean=1; clean_why="objects are present but nothing recorded how they got there" ;;
          *)               do_clean=1; clean_why="recorded status '$prev_status' is not a completed build" ;;
        esac
    fi ;;
  *) die "CLEAN must be auto, always or never (got '$CLEAN')" ;;
esac

# Back up BEFORE announcing or doing anything, and re-test afterwards.  If the
# configuration cannot be saved then cleaning would destroy it outright, and not
# cleaning is always the safer of the two -- an incremental build on a suspect
# tree is a slow problem, a deleted config is an unrecoverable one.
if [ "$do_clean" = 1 ]; then
    _bk=""
    if [ -f .config ]; then
        # The FIRST backup is the valuable one: by the second cleaning run
        # .config may no longer be the user's file, so overwriting a fixed name
        # would replace the only copy of their original.  Keep the first,
        # timestamp the rest.  --remove-destination because a plain `cp -a` onto
        # an existing symlink writes THROUGH it, clobbering its target.
        _bk=.config.before-clean
        [ -e "$_bk" ] && _bk=".config.before-clean.$(date +%Y%m%d-%H%M%S 2>/dev/null || echo prev)"
        if cp -a --remove-destination .config "$_bk" 2>/dev/null; then
            printf '  saved     : .config -> %s\n' "$_bk"
        else
            do_clean=0; _bk=""
            clean_why="could not save .config, so cleaning would destroy it"
            warn "cannot back up $SRC/.config -- NOT cleaning this tree.
       Fix its permissions, or move it aside yourself, or pass CLEAN=never to
       stop being asked.  Building incrementally instead."
        fi
    fi
fi

# A SECOND test, not an else: $do_clean can have been cleared just above, and
# the previous version of this block set it there and then cleaned anyway --
# printing "not cleaning" immediately before deleting the file it could not save.
if [ "$do_clean" = 1 ]; then
    printf '  cleaning  : %s\n' "$clean_why"
    # mrproper, not clean: the failure this recovers from is a tree left
    # inconsistent, and `make clean` keeps include/config and the generated
    # headers that are the usual culprits.  It deletes .config too, which is
    # incidental -- cleaning is about build PRODUCTS, not your configuration --
    # so that is restored immediately afterwards.
    STATE_STAGE=clean
    make -s mrproper
    printf '  cleaned   : %s\n' "$SRC"
    # Only for CONFIG_BASE=auto: the other modes are an explicit instruction
    # about where the config comes from, and restoring one would contradict it.
    if [ "$CONFIG_BASE" = auto ] && [ -n "$_bk" ] && [ -f "$_bk" ]; then
        cp -a --remove-destination "$_bk" .config \
            && printf '  restored  : .config kept across the clean (from %s)\n' "$_bk"
    fi
else
    printf '  keeping   : %s\n' "$clean_why"
fi

state_set tree      "$SRC"
state_set version   "$(make -s kernelversion)"
state_set toolchain "$TOOLCHAIN"
state_set jobs      "$JOBS"
state_set log       "$BUILD_LOG"
state_set started   "$(date -Is 2>/dev/null || date 2>/dev/null || true)"
state_set status    configuring
state_set stage     ""
state_set exit_code ""
state_set finished  ""      # or the previous run's finish time reads as this one's
STATE_STAGE=configure

# --------------------------------------------------------------------- configure

say "Configuring"

# Copy the running distro's config in.  Factored out because CONFIG_BASE=auto
# calls it only as a fallback, where the old code called it unconditionally and
# so destroyed a tuned .config on every single run.
# Never write .config in place.  A redirect truncates the target before the
# first byte arrives, so a corrupt source or a killed run leaves a FRAGMENT --
# and under CONFIG_BASE=auto the next run would adopt that fragment as its base,
# with olddefconfig silently defaulting every symbol the fragment never reached.
# Writing to a temp and renaming makes the swap atomic; rename also replaces a
# .config SYMLINK rather than writing through it to whatever it points at.
install_config() {
    local src="$1" tmp
    tmp=".config.seed.$$"
    case "$src" in
      *.gz) zcat "$src" > "$tmp" ;;
      *)    cp    "$src"   "$tmp" ;;
    esac || { rm -f "$tmp"; die "could not read the config at $src"; }
    [ -s "$tmp" ] || { rm -f "$tmp"; die "the config at $src is empty"; }
    mv -f "$tmp" .config
}

seed_from_distro() {
    local c base=""
    for c in "/boot/config-$(uname -r)" /proc/config.gz; do
        [ -r "$c" ] && { base="$c"; break; }
    done
    if [ -n "$base" ]; then
        printf '  base: %s\n' "$base"
        install_config "$base"
    else
        warn "no distro config found; starting from defconfig -- review before installing"
        make defconfig >/dev/null
    fi
}

case "$CONFIG_BASE" in
  auto)
      # Cheap sanity gate before adopting it: a truncated or empty file would
      # otherwise be taken as authoritative and quietly defaulted by
      # olddefconfig into a config nobody chose.
      if [ -s .config ] && grep -q '^CONFIG_[A-Z0-9_]*=' .config 2>/dev/null; then
          printf '  base: the .config already in this tree -- kept, not replaced\n'
          printf '        (CONFIG_BASE=distro to reseed from the running kernel)\n'
      else
          [ -e .config ] && warn "$SRC/.config is empty or has no CONFIG_ lines -- ignoring it
       and seeding from the running kernel instead.  The old file is kept as
       .config.rejected if you want to look at it."
          [ -e .config ] && mv -f .config .config.rejected
          seed_from_distro
      fi ;;
  distro)
      seed_from_distro ;;
  defconfig)
      printf '  base: make defconfig\n'
      make defconfig >/dev/null ;;
  *)
      [ -r "$CONFIG_BASE" ] || die "CONFIG_BASE=$CONFIG_BASE is not a readable file.
       Expected: auto, distro, defconfig, or a path to a kernel config."
      printf '  base: %s\n' "$CONFIG_BASE"
      install_config "$CONFIG_BASE" ;;
esac

# Bring whatever base was chosen up to date with THIS tree's Kconfig before the
# enforcement below.  A config from another kernel version is missing symbols
# this one has; without this, scripts/config sets options against a stale symbol
# table and the second olddefconfig quietly drops them again.
make olddefconfig >/dev/null
state_set config_base "$CONFIG_BASE"

cfg() { ./scripts/config "$@"; }

# --- shared GPU-compute plumbing ---------------------------------------------
# These are DEPENDENCIES, not preferences. HSA_AMD_SVM depends on DEVICE_PRIVATE
# and HSA_AMD_P2P depends on PCI_P2PDMA; with those off, asking for them
# silently does nothing and olddefconfig quietly drops them. The same machinery
# backs NVIDIA UVM/GPUDirect and Intel's shared virtual memory.
cfg --enable ZONE_DEVICE
cfg --enable DEVICE_PRIVATE          # unaddressable device memory (HMM)
cfg --enable PCI_P2PDMA              # peer-to-peer DMA between GPUs over PCIe
cfg --enable HMM_MIRROR
cfg --enable MMU_NOTIFIER
cfg --enable DMA_SHARED_BUFFER
cfg --enable DRM_ACCEL               # the accel subsystem both NPUs live under

# --- always: a console before any GPU driver, and the VM/BMC drivers ---------
cfg --enable SYSFB_SIMPLEFB
cfg --module DRM_SIMPLEDRM           # EFI/VESA framebuffer -- your rescue console
cfg --module DRM_VIRTIO_GPU          # QEMU/KVM, virt-manager, most Linux VMs
cfg --module DRM_VMWGFX              # VMware
cfg --module DRM_QXL                 # SPICE
cfg --module DRM_BOCHS               # QEMU -vga std
cfg --module DRM_AST                 # ASPEED BMC -- almost every rack server
cfg --module DRM_HYPERV              # Hyper-V / WSL2
cfg --module DRM_UDL                 # DisplayLink USB adapters

if [ "$W_AMD" = 1 ]; then
    # --- AMD: display, ROCm compute, and the Ryzen AI NPU --------------------
    cfg --module DRM_AMDGPU              # MODULE, not built-in -- see the header
    cfg --enable  DRM_AMD_DC             # modern display stack: atomic KMS, DP/HDMI audio
    cfg --enable  DRM_AMDGPU_USERPTR     # required by ROCm and by GL/Vulkan interop
    cfg --enable  DRM_AMDGPU_SI          # Southern Islands (older GCN)
    cfg --enable  DRM_AMDGPU_CIK         # Sea Islands
    cfg --enable  HSA_AMD                # KFD -- creates /dev/kfd, ROCm's entry point
    cfg --enable  HSA_AMD_SVM            # shared virtual memory: HIP managed memory
    cfg --enable  HSA_AMD_P2P            # direct GPU-to-GPU transfers
    cfg --enable  AMD_IOMMU              # DRM_ACCEL_AMDXDNA depends on it
    cfg --module  DRM_ACCEL_AMDXDNA      # "AMD AI Engine" NPU, Ryzen AI 300+
fi

if [ "$W_NV" = 1 ]; then
    # --- NVIDIA --------------------------------------------------------------
    # nouveau is the in-tree open driver. Build it as a MODULE even if you intend
    # to use NVIDIA's proprietary or open-kernel modules: those require nouveau
    # to be absent at load time, and you can only blacklist a module.
    #
    # NVIDIA's own modules are out-of-tree and built by DKMS against
    # /lib/modules/$KREL/build, which `make modules_install` creates. Nothing
    # else in the config is needed for them -- except that an unsigned
    # out-of-tree module must be loadable, hence MODULE_SIG_FORCE off below.
    #
    # NOVA (the Rust GSP driver for Turing and later) is deliberately NOT
    # enabled: it depends on CONFIG_RUST and a matching rustc/bindgen, and is
    # still early. Enable RUST first, then NOVA_CORE and DRM_NOVA.
    cfg --module DRM_NOUVEAU
fi

if [ "$W_INTEL" = 1 ]; then
    # --- Intel: graphics, compute, and the Meteor Lake+ NPU ------------------
    # i915 and xe overlap on Tiger Lake .. Meteor Lake. Building BOTH as modules
    # is correct and safe: only one binds a given device, chosen by the driver's
    # own device table and overridable with i915.force_probe / xe.force_probe.
    cfg --module DRM_I915                # Gen2 .. Gen12: everything up to Alder/Raptor Lake
    cfg --module DRM_XE                  # Xe: Arc, Lunar Lake, Battlemage, newer iGPUs
    cfg --module DRM_ACCEL_IVPU          # "Intel NPU", Meteor Lake (14th gen) and newer
fi

# --- things that break a copied distro config --------------------------------
# Distro configs point at signing keys and revocation lists shipped in their own
# source package. Those files are not in the upstream tarball and the build dies
# late with a confusing openssl error.
cfg --set-str SYSTEM_TRUSTED_KEYS ""
cfg --set-str SYSTEM_REVOCATION_KEYS ""
# Same shape, one kernel release later: distro configs point EFI_SBAT_FILE at
# kernel.sbat, which also lives only in their kernel SOURCE package.  The rule
# in arch/x86/boot/compressed/Makefile is a bare file dependency --
#     $(obj)/sbat.o: $(CONFIG_EFI_SBAT_FILE)
# -- so a missing file is not a warning but a hard stop before a single object
# of the boot stub links:
#     No rule to make target 'kernel.sbat', needed by '.../compressed/sbat.o'.
# EFI_SBAT is `def_bool y if EFI_SBAT_FILE!=""`, so it cannot be switched off
# directly; clearing the string is what turns it off.  Nothing is lost -- SBAT
# generations are Secure Boot revocation policy owned by whoever holds the
# signing certificate, and upstream defines no components at all.
cfg --set-str EFI_SBAT_FILE ""
cfg --disable EFI_SBAT
cfg --disable MODULE_SIG_FORCE       # or no out-of-tree GPU module will load
# Distro configs set CONFIG_WERROR=y, which is reasonable for the exact kernel
# and exact compiler that distro tests -- and wrong for every other pairing.
# This script deliberately feeds a distro config into a DIFFERENT kernel
# version, and often with a different compiler; the new warnings that follow
# are normal, and with WERROR they are all fatal, hours into the build.
cfg --disable WERROR

# The AMD display DML files (display_mode_vba_31 / _314 and friends) build ~2.4K
# frames doing floating-point mode validation, so a distro config's 2048 warns on
# every single build.  Raised here so the setting survives -- .config is copied
# fresh from /boot/config-$(uname -r) on every run, which silently reverts a
# hand-edit.  Note what this does NOT do: -Wframe-larger-than is a diagnostic
# threshold, not an allocation.  No function gets a byte more room, vmlinux is
# unchanged, and the real bound is THREAD_SIZE (16K on x86_64, set by
# THREAD_SIZE_ORDER in arch/x86/include/asm/page_64_types.h).  It is consumed
# once, globally, at scripts/Makefile.warn:25.  FRAME_WARN=3072 keeps the check
# useful while still covering the DML files; FRAME_WARN=0 turns it off entirely.
cfg --set-val FRAME_WARN "$FRAME_WARN"

if [ "$KEEP_DEBUG_INFO" != 1 ]; then
    # Full DWARF makes the build far slower and modules roughly 10x bigger. Set
    # KEEP_DEBUG_INFO=1 if you need BTF, bpftrace, or kernel debugging.
    cfg --disable DEBUG_INFO_BTF
    cfg --disable DEBUG_INFO_BTF_MODULES
    # The DWARF-version choice has to be cleared BEFORE selecting NONE, and the
    # order is not cosmetic.  scripts/config edits in place, so enabling
    # DEBUG_INFO_NONE only rewrites the '# CONFIG_DEBUG_INFO_NONE is not set'
    # line where it already sits -- which in a distro config is the line ABOVE
    # CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y.  Kconfig resolves a choice by
    # "the last one read wins" (confdata.c), so the DWARF entry won, selected
    # DEBUG_INFO, and full debug info stayed on for every module -- silently,
    # because nothing below checked for it.
    cfg --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT
    cfg --disable DEBUG_INFO_DWARF4
    cfg --disable DEBUG_INFO_DWARF5
    cfg --enable  DEBUG_INFO_NONE
fi

cfg --set-str LOCALVERSION "$LOCALVERSION"
cfg --disable LOCALVERSION_AUTO
make olddefconfig >/dev/null

# --------------------------------------------------------------------- verify

say "Verifying the configuration"
fail=0
check() {
    got=$(grep -E "^(# )?CONFIG_$1[ =]" .config | head -1)
    if [ "$got" = "CONFIG_$1=$2" ]; then printf '  ok   CONFIG_%-24s = %s\n' "$1" "$2"
    else printf '  FAIL CONFIG_%-24s want %s, got: %s\n' "$1" "$2" "${got:-<absent>}"; fail=1; fi
}
note() {  # informational: hardware- or arch-gated, absence is not an error
    got=$(grep -E "^(# )?CONFIG_$1[ =]" .config | head -1)
    printf '  --   CONFIG_%-24s %s\n' "$1" "${got:-<absent>}"
}
check DEVICE_PRIVATE y
check PCI_P2PDMA y
check DRM_SIMPLEDRM m
check DRM_VIRTIO_GPU m
# Verified, not assumed: this one failed silently for a long time, and the only
# symptom was a build that took far longer and modules ten times the size.
[ "$KEEP_DEBUG_INFO" = 1 ] || check DEBUG_INFO_NONE y
if [ "$W_AMD" = 1 ]; then
    check DRM_AMDGPU m; check DRM_AMD_DC y; check DRM_AMDGPU_USERPTR y
    check HSA_AMD y;    check HSA_AMD_SVM y; check HSA_AMD_P2P y
    note  DRM_ACCEL_AMDXDNA          # x86_64 + AMD_IOMMU only
fi
[ "$W_NV" = 1 ]    && check DRM_NOUVEAU m
if [ "$W_INTEL" = 1 ]; then
    check DRM_I915 m
    note  DRM_XE                     # newer trees only
    note  DRM_ACCEL_IVPU             # x86_64 only
fi
[ "$fail" -eq 0 ] || die "the config did not come out as intended -- do NOT install this kernel.
       Each FAIL is a dependency that is off, not a typo. Run 'make menuconfig'
       and search (/) for the symbol to see what it needs."

KREL=$(make -s kernelrelease)
printf '  kernel release: %s\n' "$KREL"
state_set release "$KREL"
state_set modules "/lib/modules/$KREL"

# ------------------------------------------------------------------------ build

say "Building -- this takes a while. Use tmux/screen over ssh."
STATE_STAGE=build
state_set status building
# The previous log is kept as .prev rather than appended to, so the file cannot
# grow without bound across runs.  pipefail (set at the top) is what makes the
# tee honest: without it the pipeline would report tee's exit status and a
# failed build would look like a successful one.
if [ -f "$BUILD_LOG" ]; then mv -f "$BUILD_LOG" "$BUILD_LOG.prev" 2>/dev/null || true; fi
if { : > "$BUILD_LOG"; } 2>/dev/null; then    # braces: else the error escapes fd2
    printf '  logging to: %s\n' "$BUILD_LOG"
    make "${MAKE_ARGS[@]}" 2>&1 | tee "$BUILD_LOG"
else
    warn "cannot write $BUILD_LOG -- building without a log.  Set BUILD_LOG=<path>."
    make "${MAKE_ARGS[@]}"
fi
state_set status built
state_set finished "$(date -Is 2>/dev/null || date 2>/dev/null || true)"

[ "$INSTALL" = 1 ] || {
    say "Built, not installed."
    printf '  Re-run with INSTALL=1, or install by hand:\n'
    printf '      sudo make %s modules_install\n      sudo make %s install\n' "${MAKE_ARGS[*]}" "${MAKE_ARGS[*]}"
    exit 0
}

# ---------------------------------------------------------------------- install

if command -v dkms >/dev/null && dkms status 2>/dev/null | grep -q '^amdgpu'; then
    warn "amdgpu-dkms is installed. It exists to backport amdgpu to OLD enterprise
         kernels; the in-tree driver you just built is newer. It will try, and
         fail, to build against $KREL. To stop it without breaking your distro
         kernel, cap it:
             sudo sh -c 'echo BUILD_EXCLUSIVE_KERNEL_MAX=\"$(uname -r | cut -d- -f1)\" >> /usr/src/amdgpu-*/dkms.conf'"
fi
if command -v dkms >/dev/null && dkms status 2>/dev/null | grep -q '^nvidia'; then
    printf '  note: nvidia-dkms will rebuild for %s automatically once\n' "$KREL"
    printf '        modules_install creates /lib/modules/%s/build.\n' "$KREL"
    printf '        Remember to blacklist nouveau if you use the proprietary driver.\n'
fi

say "Installing modules and kernel (sudo)"
STATE_STAGE=install
state_set status installing
sudo make "${MAKE_ARGS[@]}" modules_install
sudo make "${MAKE_ARGS[@]}" install
state_set status installed
state_set finished "$(date -Is 2>/dev/null || date 2>/dev/null || true)"

say "Post-install checks"
img=""
for i in "/boot/initramfs-$KREL.img" "/boot/initrd.img-$KREL"; do [ -f "$i" ] && img="$i"; done
if [ -n "$img" ]; then
    lsi=""; command -v lsinitrd >/dev/null && lsi=lsinitrd
    [ -z "$lsi" ] && command -v lsinitramfs >/dev/null && lsi=lsinitramfs
    if [ -n "$lsi" ]; then
        for fw in amdgpu nvidia i915 xe; do
            [ "$fw" = amdgpu ] && [ "$W_AMD"   != 1 ] && continue
            [ "$fw" = nvidia ] && [ "$W_NV"    != 1 ] && continue
            { [ "$fw" = i915 ] || [ "$fw" = xe ]; } && [ "$W_INTEL" != 1 ] && continue
            n=$(sudo "$lsi" "$img" 2>/dev/null | grep -c "firmware/$fw" || true)
            [ "${n:-0}" -gt 0 ] && printf '  ok   %s %s firmware files in the initramfs\n' "$n" "$fw" \
                                || warn "no $fw firmware in $img -- install linux-firmware and
         regenerate the initramfs BEFORE rebooting, or that GPU will not come up."
        done
    fi
fi
[ -e "/lib/modules/$KREL/build" ] \
    && printf '  ok   /lib/modules/%s/build present -- DKMS and out-of-tree modules can build\n' "$KREL" \
    || warn "/lib/modules/$KREL/build is missing; DKMS modules cannot build against this kernel"
# Driver presence check.
#
# NOTE ON SPELLING: these are FILE names, and kernel module files keep their
# hyphens -- virtio-gpu.ko, snd-hda-intel.ko, xen-blkfront.ko.  Nearly 40% of
# the modules in a typical kernel have a hyphen.  It is the module's internal
# NAME that uses underscores, which is what lsmod, modinfo -F name, /proc/modules
# and /sys/module/ all display.  Since this searches with find -name, the hyphen
# is correct; "fixing" virtio-gpu to virtio_gpu here finds nothing.
#
# A driver absent from /lib/modules is not necessarily a failure -- it may be
# built in (=y), which is a perfectly good outcome and produces no .ko at all.
# The old version of this loop printed nothing in that case, so a driver that
# genuinely failed to build looked identical to one that succeeded as built-in.
# Consult .config to tell those two apart.
for entry in "amdgpu:DRM_AMDGPU" "nouveau:DRM_NOUVEAU" "i915:DRM_I915" \
             "xe:DRM_XE" "simpledrm:DRM_SIMPLEDRM" "virtio-gpu:DRM_VIRTIO_GPU"; do
    m="${entry%%:*}"; sym="${entry#*:}"
    if find "/lib/modules/$KREL" -name "$m.ko*" 2>/dev/null | head -1 | grep -q .; then
        printf '  ok   %-12s installed as a module (%s.ko)\n' "$m" "$m"
    elif grep -q "^CONFIG_$sym=y" .config 2>/dev/null; then
        printf '  ok   %-12s built into the kernel image (CONFIG_%s=y)\n' "$m" "$sym"
    elif grep -q "^CONFIG_$sym=m" .config 2>/dev/null; then
        warn "$m: CONFIG_$sym=m was requested but no $m.ko was installed.
       That is a real failure -- check the modules_install step."
    else
        printf '  --   %-12s not enabled in this config\n' "$m"
    fi
done

say "Done. Keep your current kernel in the boot menu until $KREL is proven."
