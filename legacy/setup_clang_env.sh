#!/usr/bin/env bash
#
# setup_clang_env.sh
#
# Put a clang toolchain built by build_clang_universal.sh onto your PATH, by
# adding one guarded block to your shell rc file.
#
#   ./setup_clang_env.sh                 # add or update the block
#   ./setup_clang_env.sh --dry-run       # print what would change, touch nothing
#   ./setup_clang_env.sh --remove        # take the block back out
#
#   PREFIX=$HOME/toolchains/clang-current ./setup_clang_env.sh
#   WITH_CC_EXPORTS=1 ./setup_clang_env.sh          # also export CC/CXX/LLVM
#
# This is a SEPARATE script on purpose.  A build script that edits files you own
# is a build script you cannot trust; this one does exactly one thing, says so
# first, backs up before writing, and can undo itself.
#
# It is idempotent: the block lives between two markers and is replaced on a
# re-run rather than appended again.
#
# Licence: public domain / CC0.  No warranty.

set -euo pipefail

PREFIX="${PREFIX:-$HOME/opt/clang-current}"
WITH_CC_EXPORTS="${WITH_CC_EXPORTS:-0}"
BEGIN="# >>> clang toolchain (setup_clang_env.sh) >>>"
END="# <<< clang toolchain (setup_clang_env.sh) <<<"

DRY=0; REMOVE=0
for a in "$@"; do case "$a" in
    --dry-run) DRY=1 ;;
    --remove)  REMOVE=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) printf 'unknown option: %s (try --help)\n' "$a" >&2; exit 2 ;;
esac; done

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Which rc file.  Bash reads .bashrc for interactive non-login shells; zsh reads
# .zshrc.  We do not touch .profile or .bash_profile -- login-shell files have
# different semantics and are more likely to be hand-managed.
if [ -z "${RC:-}" ]; then
  case "${SHELL##*/}" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) RC="$HOME/.bashrc" ;;
    *)    RC="$HOME/.bashrc"
          printf 'note: $SHELL is %s; defaulting to %s (override with RC=)\n' "$SHELL" "$RC" ;;
  esac
fi
[ -e "$RC" ] || { [ "$DRY" = 1 ] || : > "$RC"; }

# ------------------------------------------------------------------ the block

read -r -d '' BLOCK <<BLOCK_EOF || true
$BEGIN
# Managed by setup_clang_env.sh -- re-run it rather than editing by hand.
#
# Point at the SYMLINK, never a versioned directory.  A version directory is
# replaced the next time you rebuild, and a stale path here fails silently:
# the guard below simply does nothing and you keep using the system compiler
# while believing you are not.
_clang_base="$PREFIX"
if [ -x "\$_clang_base/bin/clang" ]; then
    PATH="\$_clang_base/bin:\$PATH"
    export PATH
$( [ "$WITH_CC_EXPORTS" = 1 ] && cat <<'CC_EOF'
    # Exported compiler variables.  CONVENIENT BUT OPINIONATED: any build system
    # that checks "did the user choose a compiler?" will now see yes, and skip
    # its own detection.  GNU make's \$(origin CXX) becomes "environment"
    # instead of "default", which silently overrides Makefiles that select a
    # compiler themselves.  Drop these if a project starts using the wrong one.
    export CC="$_clang_base/bin/clang"
    export CXX="$_clang_base/bin/clang++"
    export LLVM="$_clang_base/bin/"   # trailing slash = kbuild's LLVM_PREFIX
CC_EOF
)
fi
unset _clang_base

# Switch to a freshly installed toolchain:  clang-use ~/opt/clang-24-5
clang-use() {
    [ -x "\$1/bin/clang" ] || { echo "clang-use: no bin/clang under \$1" >&2; return 1; }
    ln -sfn "\$(readlink -f "\$1")" "$PREFIX" &&
        echo "$PREFIX -> \$(readlink -f "$PREFIX")" &&
        echo "open a new shell to pick it up"
}
$END
BLOCK_EOF

# ------------------------------------------------------------------- apply

strip_block() { awk -v b="$BEGIN" -v e="$END" '
    $0==b {skip=1} !skip {print} $0==e {skip=0}' "$RC"; }

if [ "$REMOVE" = 1 ]; then
    grep -qF "$BEGIN" "$RC" || die "no managed block found in $RC"
    if [ "$DRY" = 1 ]; then printf '%s\n' "would remove the managed block from $RC"; exit 0; fi
    cp -a "$RC" "$RC.bak.$(date +%Y%m%d%H%M%S)"
    strip_block > "$RC.tmp" && mv "$RC.tmp" "$RC"
    printf 'removed the block from %s (backup alongside it)\n' "$RC"; exit 0
fi

[ -d "$PREFIX" ] || printf 'note: %s does not exist yet -- the block is written anyway and\n      activates itself once it does.\n' "$PREFIX"

if [ "$DRY" = 1 ]; then
    printf '\n--- would %s in %s ---\n\n' \
        "$(grep -qF "$BEGIN" "$RC" 2>/dev/null && echo 'REPLACE the existing block' || echo 'APPEND a block')" "$RC"
    printf '%s\n' "$BLOCK"
    printf '\n--- nothing was written (--dry-run) ---\n'; exit 0
fi

cp -a "$RC" "$RC.bak.$(date +%Y%m%d%H%M%S)"
if grep -qF "$BEGIN" "$RC"; then strip_block > "$RC.tmp"; else cat "$RC" > "$RC.tmp"; fi
{ printf '\n%s\n' "$BLOCK"; } >> "$RC.tmp"
mv "$RC.tmp" "$RC"

bash -n "$RC" 2>/dev/null || printf 'warning: %s does not parse cleanly -- check it, a backup is alongside\n' "$RC" >&2
printf 'updated %s (backup alongside it)\n' "$RC"
printf 'run:  exec %s -l    or open a new terminal\n' "${SHELL##*/}"
