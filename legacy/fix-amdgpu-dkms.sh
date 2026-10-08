#!/usr/bin/bash
# Stop amdgpu-dkms from attempting (and failing) to build against kernel 7.x,
# while leaving the working 6.12.0 AlmaLinux install completely untouched.
#
# Rationale: linux-7.2 has CONFIG_DRM_AMDGPU=y -- amdgpu and the whole DRM
# helper stack (drm, drm_exec, drm_buddy, drm_ttm_helper, drm_suballoc_helper)
# are compiled into vmlinux. The DKMS package ships out-of-tree replacements
# for exactly those (amdgpu, amdttm, amd-sched, amddrm_*), which can never
# load over built-in versions. The in-tree 7.2 driver serves the W6600.
set -uo pipefail

MOD=amdgpu
VER=6.19.14-2364437.el10
CONF="/usr/src/${MOD}-${VER}/dkms.conf"

if [[ $EUID -ne 0 ]]; then echo "run me with sudo" >&2; exit 1; fi
[[ -f $CONF ]] || { echo "not found: $CONF" >&2; exit 1; }

echo "== before =="
dkms status

# 1. Back up the vendor dkms.conf.
cp -a "$CONF" "${CONF}.bak"
echo "backed up -> ${CONF}.bak"

# 2. Cap autoinstall at 6.18. dkms skips a kernel when
#    VER(kernel) > VER(BUILD_EXCLUSIVE_KERNEL_MAX)  [/usr/sbin/dkms:848]
#    so 6.12.0 still builds/updates normally, 7.x is skipped cleanly.
if grep -q '^BUILD_EXCLUSIVE_KERNEL_MAX=' "$CONF"; then
    echo "BUILD_EXCLUSIVE_KERNEL_MAX already present, leaving as-is"
else
    cat >> "$CONF" <<'CONFEOF'

# Added manually: kernels >= 7.0 are built with CONFIG_DRM_AMDGPU=y (in-tree
# driver built into vmlinux), which supersedes this out-of-tree package.
BUILD_EXCLUSIVE_KERNEL_MAX="6.18"
CONFEOF
    echo "appended BUILD_EXCLUSIVE_KERNEL_MAX=\"6.18\" to $CONF"
fi

# 3. Drop the stale failed build tree from the 7.2.0 attempt.
#    (Deliberately NOT running 'dkms remove -k 7.2.0': nothing is installed
#     for 7.2.0, and a scoped remove that finds no state is a needless risk
#     to the working 6.12.0 entry.)
if [[ -d /var/lib/dkms/$MOD/$VER/build ]]; then
    rm -rf "/var/lib/dkms/$MOD/$VER/build"
    echo "removed stale /var/lib/dkms/$MOD/$VER/build"
fi

echo
echo "== after =="
dkms status
echo
echo "Verify the skip is clean (should say 'is not supported by this kernel' or similar, and exit 0-ish):"
echo "  sudo dkms autoinstall -k 7.2.0"
