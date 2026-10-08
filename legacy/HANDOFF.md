# Handoff -- siege3, 2026-08-27 (early hours)

## Running right now
Nothing. `build_everything.sh` WAS started and **failed at the gcc stage after
84 seconds** -- a variable-name collision, diagnosed and fixed (trap 6 below).
It has NOT been re-run since the fix; that is the next thing to do.

The gcc stage deletes its own build dir (`rm -rf "$BUILD"`), so the 4.2 GB of
dead profiledbootstrap under `~/src/gcc-build/build-gcc-16.2.0` needs no manual
cleanup. The source tree and its in-tree GMP/MPFR/MPC/ISL survive, so no
re-download. `~/opt/gcc-16.2.0` was never created -- nothing blocks a fresh run.

## The one command
    cd ~/Documents && tmux new -s build
    KERNEL_SRC=/home/madness/source-opt/kernel/linux-7.2 PY_TARGETS=cpython \
      ./build_everything.sh --skip=clangenv

`build_everything.sh` is new: it drives whichever sibling scripts are present,
in dependency order, and resumes rather than restarts (it asks the machine what
is installed -- does `~/opt/gcc-current/bin/gcc` run? -- rather than keeping
state). `--dry-run` prints the plan and touches nothing; `--list` shows what is
installed; `--force=<stage>` rebuilds something that already works.
Tonight's plan: gcc -> python(cpython) -> dkms -> kernel, roughly 5-9 h.

**Do not edit anything in `~/Documents` while a run is in flight.** Bash reads a
script lazily by byte offset, so rewriting a file under a running shell makes it
resume mid-command inside the new content.

## Done overnight (2026-08-27, early hours)
- **The gcc stage failure found and fixed** -- see trap 6. One line added to
  `build_gcc_universal.sh`: `export -n LANGUAGES`. Verified by dumping GCC's
  `BASE_FLAGS_TO_PASS` with and without the variable exported, and by
  reproducing the exact error from a bare command-line override.
- **`find` -> `findutils`** in `build_python_universal.sh` DNF_PKGS. This was
  the open typo from the previous handoff; it is now closed. A bare
  `build_everything.sh --dry-run` no longer warns about an unmatched package.
- **The python stage label no longer lies.** `stage_label` hardcoded
  "CPython + PyPy" regardless of PY_TARGETS. It now derives the text: with
  PY_TARGETS=cpython it reads `CPython only (cpython) -- NOT building PyPy`,
  and an unrecognised PY_TARGETS is caught in the plan rather than hours later
  by run_stage's "exited 0 but not detectable" check.
- **SHA256SUMS regenerated.** It was ALREADY stale before any of tonight's
  edits -- `universal_linux_kernel_build.sh` changed at 22:55, after the sums
  were made at 21:30. Now covers all eight scripts including
  `build_everything.sh` itself, which had never been listed. Verifies clean.
- Confirmed `./build_everything.sh` with **no arguments already builds
  everything** -- all six stages. The `KERNEL_SRC=... PY_TARGETS=cpython
  --skip=clangenv` in "The one command" are overrides for THIS machine, not
  requirements. On a fresh box KERNEL_SRC empty downloads the latest stable
  from kernel.org via curl, and PY_TARGETS defaults to all three interpreters.

## Done today
- **clang-24-4** built + installed, `clang-current` -> it. 121 MB vs 24-3's 196 MB
  (the earlier build wrongly inherited LLVM_TARGETS_TO_BUILD=all; now X86 only).
- **Kernel 7.2.0 installed** -- this was open in the previous handoff and is now
  done: 326 modules in `/lib/modules/7.2.0`, `build` -> the linux-7.2 tree,
  `vmlinuz-7.2.0` + `initramfs-7.2.0.img` in /boot (17:02 / 17:28).
  NOT yet verified: whether that initramfs actually carries amdgpu firmware.
  Check before trusting it:
      sudo lsinitrd /boot/initramfs-7.2.0.img | grep -c 'firmware/amdgpu'
- **build_everything.sh** written and tested (success / resume / failure /
  "exited 0 but installed nothing" all exercised against stub scripts).
- **Three fixes to `universal_linux_kernel_build.sh`**; original kept as
  `universal_linux_kernel_build.sh.orig-before-claude`, so `diff` them:
  1. `export -n LOCALVERSION` -- kbuild has a variable of the same name and
     `scripts/setlocalversion` appends `$LOCALVERSION` from the ENVIRONMENT on
     top of `CONFIG_LOCALVERSION`. The release was coming out `7.2.0-gpu-gpu`.
  2. `cfg --disable WERROR` -- distro configs set it, and this script
     deliberately feeds a distro config into a different kernel version, often
     with a different compiler. Every new warning was fatal, hours in.
  3. Debug info actually gets disabled now. `cfg --enable DEBUG_INFO_NONE` was a
     no-op: scripts/config edits in place, so it rewrote the
     `# CONFIG_DEBUG_INFO_NONE is not set` line that sits directly ABOVE
     `CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y`, and Kconfig resolves a
     choice by "last one read wins". Full DWARF stayed on for every module.
     The DWARF members are now cleared first, and the verify block checks it.

## Six traps on this machine, all the same shape
Every one is "the script checked one thing and used another":
1. `python3` on PATH is **PyPy3**, not CPython. Pin `/usr/bin/python3`.
2. `.bashrc` exports **CC/CXX -> clang-current**. Silently breaks GCC builds:
   MPFR's LTO probe runs clang bitcode through the system LLVMgold.so
   (llvm-libs 21) and dies with "Invalid record" in stage 1.
   `build_gcc_universal.sh` ignores env CC and picks /usr/bin/gcc.
3. Kernel `.ko` FILES keep hyphens (`virtio-gpu.ko`); only the module NAME uses
   underscores. 897 of 2424 modules on this box have hyphenated filenames.
4. **`dnf` exits 1** -- the enabled `oneAPI` repo has an unverifiable GPG key.
   Any script doing a bare `sudo dnf install -y ...` under `set -e` dies in its
   first ten seconds, for a reason unrelated to the packages it asked for.
   Work around with `--disablerepo=oneAPI`. `build_everything.sh` detects this
   and takes over dependency installation itself.
5. **`LD_RUN_PATH` is set, and long** -- `ld` copies it into the RPATH of every
   binary it links, including paths into the root-owned `~/opt/clang-24-2`.
   Also `gcc` on PATH is GCC 17 trunk and `/usr/local/bin` holds a root-installed
   LLVM plus a self-built Python 3.14. Non-interactive builds must clear the
   environment and put `/usr/bin` FIRST.
6. **`LANGUAGES` means something else to GCC.** Fixed, but recorded because it
   is the same shape as the rest and cost a run. `build_everything.sh` passes
   `LANGUAGES=c,c++,lto` in the ENVIRONMENT for `--enable-languages`. GCC's
   toplevel Makefile forwards it inside `BASE_FLAGS_TO_PASS`, guarded by
       "`echo 'LANGUAGES=$(LANGUAGES)' | sed -e s'/[^=][^=]*=$$/XFOO=/'`"
   whose sed rewrites the override to a harmless `XFOO=` ONLY when the value is
   empty -- the normal case, since nothing defines it at toplevel. Non-empty it
   survives, reaches the sub-make on its COMMAND LINE, and so beats
   gcc/Makefile's own `LANGUAGES = c $(CONFIG_LANGUAGES)`. `native` then depends
   on a target literally named `c,c++,lto`:
       make[3]: *** No rule to make target 'c,c++,lto', needed by 'native'.
   Stage 1 dies in 84 seconds. Fix: `export -n LANGUAGES` in
   `build_gcc_universal.sh` -- literally the `export -n LOCALVERSION` fix from
   the kernel script, one script over. The env var is the ONLY vector: `JOBS`,
   `MODE`, `TRACK`, `PREFIX`, `INSTALL_DEPS` and `LINK_CURRENT` appear nowhere
   in GCC's build system.

## Open work
- **PyPy SSL is broken.** Both `~/opt/pypy2` and `~/opt/pypy3` SEGFAULT (signal 11)
  on `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`; HTTPS fails entirely. PyPy3 is what
  answers `python3` on PATH, so anything shelling out to python3 for network work
  is broken. Likely cffi _ssl bindings built against an older OpenSSL than the
  loaded 3.5.5. Fix: `TARGETS=pypy3 ~/Documents/build_python_universal.sh`
  (needs ~8 GB RAM; do not run while GCC has the cores). `PY_TARGETS=cpython`
  in the command above deliberately does NOT touch this.
- **The siblings still call dnf with no error handling.**
  `build_gcc_universal.sh`, `build_python_universal.sh` and
  `universal_linux_kernel_build.sh` each do a bare `sudo dnf install -y $PKGS`
  under `set -e`. Driven by `build_everything.sh` this is harmless -- it detects
  the broken repo, installs the deps itself and passes INSTALL_DEPS=0. Run
  STANDALONE on any box with an unverifiable repo they die in ten seconds, for a
  reason unrelated to what they asked for. Worth hardening before the scripts go
  on the website for people to run individually.
- **GCC 17** if wanted: `TRACK=snapshot ~/Documents/build_gcc_universal.sh`
  -> 17-20260823, installs to its own prefix. `~/opt/gcc-17` is the older 20260816.
- **`.bashrc` hardcodes `~/opt/gcc-17/bin`**, not `~/opt/gcc-current`. So after
  building GCC 16.2.0, `gcc` in a new shell is still the trunk snapshot.
- `~/opt/clang-24-2` is **root-owned** from a past run under sudo.
- `~/code/cxx/satellite`: main has 6 unpushed commits.

## After the kernel stage
The new kernel does NOT become the default boot entry -- `build_everything.sh`
records the current default via `grubby` and puts it back, because
`GRUB_DEFAULT=saved` + BLS otherwise points `saved_entry` at whatever was just
installed. Pick it from the menu deliberately. Once it has proved itself:
    sudo grubby --set-default=/boot/vmlinuz-7.2.0-gpu
Note the kernel stage builds from the AlmaLinux config (2507 modules) rather
than the hand-tuned tree config (331). Your tuned one is saved on first run to
`linux-7.2/.config.saved-by-build-everything`.
