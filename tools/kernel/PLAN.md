# Universal Linux kernel build tool -- PLAN

Started 2026-09-18 on siege3 (AlmaLinux 10.2, Xeon E5-2670 v3, Radeon PRO W6600,
running the self-built `7.2.4-gpu`).

## 1. Decision: one Python file, not C++ and not more bash

**`enterprise_linux_kernel_tool/enterprise_linux_kernel_tool.py`** -- a single
file, standard library only, pinned to the platform interpreter
(`#!/usr/bin/python3 -I`).

| | bash (today) | C++ | **Python (chosen)** |
|---|---|---|---|
| Runs with nothing installed first | yes | no -- needs `g++`, which `gcc` does not pull in | yes -- `/usr/bin/python3` is on every dnf/apt/zypper system because the package manager needs it |
| Parse kernel.org JSON, INI profiles, `.config` | needs `jq`/awk, fragile | hand-rolled or a vendored library | stdlib: `json`, `configparser`, `hashlib`, `urllib` |
| The user can read what is about to install a kernel | yes | only the source, not the binary they run | yes, and it is the thing they run |
| Unit tests for the decision logic | impractical | yes | yes (`unittest`, stdlib) |
| Silent-failure traps | `set -e` + `pipefail` (bugs B2, B3 below are exactly this) | none of that kind | none of that kind |
| Interactive menus | painful | fine | fine |

The work is orchestration -- run `make`, read sysfs, edit a text file, call
`sudo`. C++ buys nothing there and costs a compile step on every target. Bash
has reached the size where its failure modes produce real bugs (section 2).
The legacy script stays in `legacy/` as the zero-dependency fallback and as the
reference for every hard-won workaround, all of which are ported.

**"Enterprise" means behaviour, not a distro whitelist.** Verified downloads,
reproducible profiles, never silently changing the default boot entry,
refusing to build something that cannot boot, DKMS kept working. It must *run*
everywhere: dnf, apt, zypper and pacman families; dracut and initramfs-tools.
Syntax is held to Python 3.6 so RHEL 8's platform Python can run it.

## 2. What testing the legacy script found (2026-09-18)

Run as you would run it, from this machine, build-only, isolated work dir
(`~/src/ulkb-test`). Evidence is in `test-runs/`.

| # | Severity | Finding |
|---|---|---|
| B1 | **high** | **Base config is lost the second time you use the script.** It looks in `/boot/config-$(uname -r)` and `/proc/config.gz` only. Once you are *running* a kernel this script built, neither exists (EL's `make install` does not copy the config to /boot; IKCONFIG is off), so it falls back to `make defconfig`: 32 modules, **no XFS** -- this machine's root is XFS on LVM. Run 1 did exactly this. The right base was sitting at `/lib/modules/7.2.4-gpu/build/.config`. |
| B2 | **high** | **Verify dies silently.** `check()`/`note()` do `got=$(grep ... \| head -1)` under `set -euo pipefail`; when the symbol is absent grep fails, the assignment fails, errexit kills the script *before* the `FAIL ... <absent>` line prints. Run 1 ended with `==> Verifying the configuration` and a bare exit 1. Reproduced in isolation. Also fires for any symbol a chosen kernel does not have -- `DRM_ACCEL_AMDXDNA` before 6.14, `DRM_XE` before 6.8 -- so picking an older kernel kills it. |
| B3 | **high** | **Wrong kernel release on every first build.** `make -s kernelrelease` runs before `include/config/auto.conf` exists, so the LOCALVERSION suffix is missing: run 2 printed and recorded `7.2.6`, the tree built `7.2.6-ulkbtest`. With `INSTALL=1` every post-install check then looks at `/lib/modules/7.2.6` and `/boot/initramfs-7.2.6.img`: the firmware check is silently skipped and it reports amdgpu.ko missing and `build` missing, all falsely. |
| B4 | medium | **A failed configure poisons the next run.** Run 1 left its defconfig-derived `.config` in the tree; `CONFIG_BASE=auto` adopts "the .config already in this tree" next time as if the user had chosen it. |
| B5 | medium | **A failed *install* wipes a finished build.** Any failure after `built` (sudo password timeout after a long unattended build, /boot full) records `status=failed`; `CLEAN=auto` then runs `mrproper` on a complete tree. (By reading; install cannot be exercised here, sudo needs a password.) |
| B6 | medium | **`mrproper` on a tree that a installed kernel depends on.** `/lib/modules/7.2.4-gpu/build` is a symlink to `~/src/linux-7.2.4`. Cleaning that tree breaks every DKMS / out-of-tree build for the *running* kernel until a rebuild succeeds. `CLEAN=auto` never checks. |
| B7 | medium | **No integrity check on the download, and extraction is not atomic.** No sha256, no signature. An interrupted `tar xf` leaves `linux-X/` with a `Makefile` (it sorts early in the archive) but no `drivers/`; the next run sees the directory, skips the download, and fails somewhere confusing. |
| B8 | medium | **Compiler is whatever `gcc` is first on PATH** -- here GCC 17.0.0 *experimental*. DKMS later builds as root with `/usr/bin/gcc` (14.3.1): a compiler mismatch between kernel and out-of-tree modules. Same shape as handoff traps 1, 2, 5: kbuild's `python3` is PyPy here, `LD_RUN_PATH` leaks into host tools. |
| B9 | low | Default boot entry is not preserved standalone (only `build_everything.sh` does it). No Secure Boot check -- an unsigned kernel on an SB-enabled box installs "successfully" and cannot boot. No /boot or work-dir free-space check. Preflight does not check headers (`openssl/opensslv.h`, `libelf.h`), so a missing `-devel` package fails minutes in. `DRM_MGAG200` (Matrox -- the BMC video on most Dell/HPE servers) is missing from the always-on rescue list. curl's progress bar floods non-TTY logs. |
| B10 | medium | **The verified config is not the built config under `TOOLCHAIN=llvm`.** `make olddefconfig` and `make kernelrelease` are called without `MAKE_ARGS`, so Kconfig's `cc-option` tests run against gcc; the build then passes `LLVM=` and kbuild silently re-resolves the config for clang *after* verify approved it. (By reading.) |

**Run 1** (`./universal_linux_kernel_build.sh`, nothing overridden but the
directories): dead in 38 s at `==> Verifying the configuration`, exit 1, no
message -- B1 then B2. B2 is what stopped B1 from building a kernel with no XFS.

**Run 2** (`CONFIG_BASE=/lib/modules/7.2.4-gpu/build/.config` supplied by hand to
get past B1): **built cleanly** -- 23 m 24 s, 21,695 steps, 0 warnings, tree
4.7 GB. **Correction (2026-09-20):** the script announced `toolchain : gcc (GCC)
17.0.0`, and this file said so too -- but the tree's `CONFIG_CC_VERSION_TEXT` is
**clang 24**. `~/.bashrc:176` exports `LLVM=.../clang-current/bin/`, kbuild reads
`LLVM` from the environment, and the script never knew. That is **B11**, and it
is also why the running 7.2.4-gpu is clang-built while `kernel_build.ini` says
`toolchain = gcc`. Fixed in `legacy/...fixed.sh`; the new tool was never
exposed (scrubbed environment: its gcc builds are verified gcc 14.3.1). The part of the script that matters most, the
config enforcement, is sound. It recorded `release = 7.2.6` for a kernel whose
`kernel.release` says `7.2.6-ulkbtest` (B3).

## 2a. The running kernel, checked on request (7.2.4-gpu, 2026-09-18)

amdgpu is healthy: SMU, DMUB, Display Core 3.2.384 and VCN all initialise,
`/dev/kfd` exists, no oops, BUG, WARNING or call trace this boot. Four things
worth acting on, one of them caused by the legacy script:

| | What | Cause | Fix |
|---|---|---|---|
| R1 | `systemd: bpf-restrict-fs: Failed to load BPF object` twice per boot; no `/sys/kernel/btf/vmlinux` | The script turns `DEBUG_INFO_BTF` off while the config keeps `BPF_LSM=y` and `bpf` in `CONFIG_LSM`. The distro kernel has BTF on. | New `--debug-info btf` mode; a post-build note whenever BTF is off with BPF_LSM on. |
| R2 | `sep5.service` failed: "Failed to build the drivers" for 7.2.4-gpu (Intel VTune's out-of-tree module) | 7.2.4-gpu was built with `~/opt/clang-current`; root builds modules with `/usr/bin/gcc`, which is handed clang-only flags. The mechanism is **reproduced** by `dkms-check --source ~/src/linux-7.2.4`: a ten-line module fails with `gcc: error: unrecognized command-line option '-mretpoline-external-thunk'`, and compiles once given `LLVM=`. (sep5's own build log was not read; this is the expected cause, not a confirmed one.) This is B8 on a live system. | Build kernels with the system compiler (the new default), or run root-side builds with `LLVM=/home/madness/opt/clang-current/bin/` in the environment. |
| R3 | `x86/CPU: Running old microcode`, `MDS: ... no microcode`; revision 0x3d | `/lib/firmware/intel-ucode` is empty -- no microcode package. Not a kernel problem; affects every kernel here. | `sudo dnf install microcode_ctl && sudo dracut -f --kver $(uname -r)`. `doctor` now reports it. |
| R4 | `SELinux: systemd (1) wrote to disable. This is no longer supported.` | `SELINUX=disabled` in `/etc/selinux/config`; kernels >= 6.4 removed runtime disable. | `sudo grubby --update-kernel=ALL --args selinux=0` if it is meant to be off. |

Not problems: taint 4 is `mcelog` writing an MSR from userspace; EDAC "ECC is
disabled" is the DIMMs; TDX/VMSCAPE/MDS notices are this CPU generation;
amdgpu-dkms 6.19.14 is already capped at 6.18, so DKMS skips 7.x cleanly.

## 3. Requirements

From the request, in the user's words where possible:

1. **Pull a list of available kernels; the user picks one.**
2. **Ask the user manually for a few options.**
3. **Configure for that machine automatically** where it can be detected.
4. **Run on all machines** -- enterprise-grade, not enterprise-only.
5. **Working AMDGPU**, and **DKMS still compiles** against what it builds.

## 4. Design

### 4.1 Commands

```
enterprise_linux_kernel_tool.py              interactive wizard (on a TTY)
  kernels [--series 6.12] [--json]           what kernel.org offers right now
  detect  [--json]                           what this machine is
  doctor                                     preflight: tools, headers, space, env traps
  plan    [options]                          resolve everything, print it, touch nothing
  build   [options]                          fetch, verify, extract, configure, verify, build, check
  install [options]                          modules_install + install + post-install checks
  status                                     recorded state of every known tree
  dkms-check [--source DIR]                  can an out-of-tree module compile against this tree?
  toolchains [options]                       every compiler, binutils set and python here; what would be used
```

Every question the wizard asks is also a flag, and every answer can be saved to
and loaded from an INI **profile** (`--save-profile`, `--profile`), so a build
chosen interactively once is reproducible unattended across a fleet.

### 4.2 Where kernels come from (requirement 1)

- `https://www.kernel.org/releases.json` -- mainline / stable / longterm, EOL
  flags, release dates, tarball URLs. Replaces scraping `finger_banner`.
- `https://cdn.kernel.org/pub/linux/kernel/vN.x/sha256sums.asc` -- one fetch
  gives **every point release in a series and its checksum**. `--series 6.12`
  lists them all; `--kernel 6.12` means "newest 6.12.y".
- Selectors: `stable` (default), `longterm`, `mainline`, `X.Y`, `X.Y.Z`.
- Download to `*.part`, check sha256, rename; extract to a temp dir, rename.
  PGP: if `gpg` can verify `sha256sums.asc` it says so; `--require-pgp` makes
  an unverified signature fatal. (Honest scope: without PGP the checksum proves
  integrity, not authenticity -- it comes from the same host over the same TLS.)
- `-rc` tarballs come from git.kernel.org and have no checksum file: allowed,
  with a warning. `linux-next` is listed but not buildable (no tarball).

### 4.3 Asked manually (requirement 2)

Kernel version · GPU vendors · toolchain · module set (full / trimmed) · CPU
tuning (generic / native) · preemption model · timer HZ · debug info ·
LOCALVERSION · install or build-only · make it the default boot entry (default
**no**). Each has a default good enough to press Enter through.

### 4.4 Detected automatically (requirement 3)

| Detected | From | Drives |
|---|---|---|
| GPUs, NPUs, BMC/VM video | sysfs PCI class + vendor id (no `lspci` text matching) | which DRM drivers are forced `=m` |
| Base config | tree `.config` -> `/boot/config-$(uname -r)` -> `/proc/config.gz` -> `/lib/modules/$(uname -r)/build/.config` -> `/lib/modules/$(uname -r)/config` -> newest `/boot/config-*`; **defconfig only when asked for** | fixes B1 |
| Root filesystem, LVM/dm-crypt/md under it | `/proc/mounts`, `/sys/block/*/dm/uuid`, `/sys/block/md*/md/level` | **boot-critical symbols**: build refuses if the config cannot mount this machine's root |
| Loaded modules | `/proc/modules` | `--modules trimmed` (`make localmodconfig`) and the post-build coverage check: every module loaded now must exist in the new build, as `.ko` or built-in |
| Toolchains | `/usr/bin/gcc`, PATH `gcc`, clang candidates, with versions | default is the **system** compiler; experimental/non-system compilers are flagged (B8) |
| Secure Boot, EFI | efivars | warn + require `--allow-unsigned` to install when SB is on |
| Boot stack | grubby / BLS, dracut vs initramfs-tools | default-entry preservation, post-install checks |
| Distro family | `/etc/os-release` | dependency package names: dnf, apt, zypper, pacman |
| Free space | statvfs on work dir and /boot | refuse early rather than fail late |
| Virtualisation, CPU vendor/flags, RAM | `/proc`, DMI | report; `-j` default; native-CPU advice |

Symbols are checked against **this tree's Kconfig** before being requested, so
"not in this kernel version" (a note) is told apart from "present but dropped
-- a dependency is off" (a failure for required symbols). That is what makes
picking an older kernel safe (B2).

### 4.5 The AMDGPU + DKMS contract (requirement 5)

Ported unchanged from the legacy script, because it is the part that was right:
GPU drivers are **modules**, never built-in; `HSA_AMD` + `HSA_AMD_SVM` +
`HSA_AMD_P2P` with their real dependencies (`ZONE_DEVICE`, `DEVICE_PRIVATE`,
`PCI_P2PDMA`, `HMM_MIRROR`, and now `MEMORY_HOTPLUG`/`MEMORY_HOTREMOVE` so a
defconfig base works too); `MODULE_SIG_FORCE` off; distro key/SBAT paths
cleared; `WERROR` off; the DWARF-choice ordering trap.

New, for "DKMS still compiles":

1. **Same compiler for kernel and DKMS.** DKMS runs as root with root's PATH,
   i.e. the system compiler. Default toolchain is therefore the system `gcc`.
   Choosing anything else prints exactly what DKMS will use instead.
2. **Out-of-tree smoke test** after every build, no root needed: a ten-line
   module is compiled with `make -C <tree> M=<tmp>` using the environment DKMS
   will have (root's PATH, no leaked CC/LLVM). Proves `/lib/modules/<rel>/build`
   will be usable *before* anything is installed. Also standalone: `dkms-check`.
3. **A tree in service is never auto-cleaned** (B6): if any
   `/lib/modules/*/build` points at the tree, cleaning needs `--clean=force`.
4. **Failures after `built` never trigger a clean** (B5).
5. **amdgpu-dkms awareness**: reads `dkms status` and the package's
   `BUILD_EXCLUSIVE_KERNEL_MAX`; says whether DKMS will skip the new kernel
   cleanly (as on this box: capped at 6.18) or try and fail, and what to do.
6. Post-install: `dkms status -k <rel>`, the `build` symlink, amdgpu firmware in
   the initramfs, `.config` copied to `/boot/config-<rel>` so B1 cannot recur,
   and `IKCONFIG_PROC` on so the running kernel always carries its own config.

### 4.6 Safety rails

Scrubbed build environment (allow-list; `/usr/bin` first; no CC/CXX/
LD_RUN_PATH/LOCALVERSION/PYTHON*) · re-exec under `/usr/bin/python3` if started
under PyPy · never run as root · atomic `.config` writes · config backed up
before any clean · release read from `include/config/kernel.release` *after*
the build (B3) · default boot entry restored unless asked · on build failure,
print the first real error from the log, not the tail of 24 interleaved jobs.

## 5. Layout

```
universal_linux_kernel_script/
  PLAN.md                         this file
  legacy/universal_linux_kernel_build.sh        byte-identical baseline
  legacy/universal_linux_kernel_build.fixed.sh  the same + B1, B2, B3, B10 fixed (59 lines)
  enterprise_linux_kernel_tool/
    enterprise_linux_kernel_tool.py
    tests/test_tool.py            stdlib unittest, no network, no root
    README.md
  test-runs/                      logs and state from the runs in section 2
```

## 6. Milestones

- [x] M1 kernels / detect / doctor / plan
- [x] M2 fetch + sha256 verify + atomic extract (PGP reported; `--require-pgp` enforces)
- [x] M3 config engine: base discovery, enforcement, Kconfig-aware verify, boot-critical symbols
- [x] M4 build: scrubbed env, log, first-error summary, correct release, module coverage check
- [x] M5 DKMS: out-of-tree probe, in-service tree protection, amdgpu-dkms report
- [x] M6 wizard + profiles -- **written and unit-tested, never driven by a human at a terminal**
- [x] M7 install + boot-entry preservation + post-install checks -- **run for real by
      the user on 2026-09-20** (section 9e; evidence in `test-runs/first-real-install-2026-09-20.txt`).
      Not yet done: a reboot into the result; the `--set-default` = no path for real.
- [x] M9 compiler and build-tool discovery and choice (section 9)
- [x] M10 profiles first; Secure Boot config + image signing; toolchain pin; packaging (sections 9c, 9d)
- [x] M11 her machine and any machine: the disk path (VMD), NVIDIA, CPU levels and hybrid
      CPUs, space, microcode and pahole up front, and ten of the 9e findings (section 9f, 2026-10-08)
- [ ] M8 (remaining) Secure Boot signing (MOK + pesign/sbsign); `binrpm-pkg` / `bindeb-pkg`
      output for fleet rollout; default-entry preservation without grubby;
      a real `dkms build` trial of a named module (needs root); Python 3.6 on a
      real RHEL 8 box (syntax is checked with `ast`, the runtime is not)

### What you need to test, in this order

1. ~~The wizard on a real terminal~~ and ~~the first real install~~ -- done 2026-09-20 (9e).
2. **Reboot into `7.2.6-xeon-haswell-all-gpu-server-latency`.** It is the default entry
   (the profile said `set_default = yes`); 7.2.4-gpu is still in the menu. Check
   `uname -r`, `dmesg | grep -i amdgpu`, `/dev/kfd`.
3. Secure Boot for real: `sudo mokutil --import ~/.config/enterprise_linux_kernel_tool/keys/secureboot.der`,
   reboot, enrol in MOK Manager, enable Secure Boot in firmware, boot the new kernel.
4. `package --source ~/src/linux-7.2.6 --cc ~/opt/clang-current/bin/clang` -- the first
   package of a BTF tree: check the RPM's size (modules should be stripped of DWARF).
5. The publishable gcc kernel needs **its own tree** now (9e, BLD-2): `--workdir ~/src/publish`.

## 7. Not doing

No third-party Python packages. No curses UI (plain prompts work over any ssh
and in any log). No distro whitelist. No automatic reboot, ever.

## 8. Test results of the new tool (2026-09-18/19, siege3)

| Run | What | Result |
|---|---|---|
| `kernels`, `--series 6.12`, `detect`, `doctor`, `plan`, `status` | read-only | correct; found W6600, XFS-on-LVM, xz firmware, 4 compilers, amdgpu-dkms cap |
| `build --kernel 6.12 --configure-only` | download 6.12.110, sha256, extract, configure | first attempt **correctly refused**: `HSA_AMD_P2P` needs `DMABUF_MOVE_NOTIFY` before 7.x, which a 7.2 base config cannot carry. Fixed by requesting it and by preferring a same-series base (`/boot/config-6.12.0-...`): all required options pass, release `6.12.110-elkt` |
| `build --kernel stable --modules trimmed` | full pipeline, system gcc 14 | 2514 -> 169 modules; **9 m 33 s**, 8,796 steps, 0 warnings; release `7.2.6-elkt` (B3 fixed); all 120 loaded modules present; amdgpu a module; out-of-tree probe compiles, vermagic correct |
| same, with `#error` injected in `kernel/smpboot.c` | failure path | error shown live, summary points at log line 662 (not the tail), state `failed/build`, `.config` survived the trim swap |
| same, error reverted | retry | **incremental: 1 m 41 s, 478 steps**; all checks green |
| `dkms-check --source ~/src/linux-7.2.4` | the *running* kernel's tree | reproduces R2: system gcc rejects clang-only flags; prints the working `LLVM=... dkms` line |
| `install --dry-run` | | prints 4 sudo commands, runs none, writes no state |
| legacy `.fixed.sh`, `CONFIG_BASE=distro` | B1/B3 fix | finds `/lib/modules/7.2.4-gpu/build/.config` unaided; records `7.2.6-ulkbtest` |
| `tests/test_tool.py` | 111 unit tests | pass; clean under `-W error::ResourceWarning` |

**Review.** A fresh reader audited the first version: 19 findings, 13 proven
with runnable snippets, all fixed, each proven one now has a regression test
(`ReviewFindings`, `Install` in the test file). The ones that mattered most:
a failed `make install` left the *new* kernel as the default boot entry (the
restore was not in a `finally`); `--localversion -gpu` was rejected by argparse;
a second clean of a tree restored the config from the *first* clean; two
instances sharing a state file could mark a finished tree `building`, and the
next run would `mrproper` it; checksum lines outside the PGP-signed block were
parsed; a missing `--cc` silently became `/usr/bin/gcc`; SIGHUP (a dropped ssh
session) ran no cleanup at all.

Measured, for planning: full 2,514-module build 23.5 min / 4.7 GB (clang 24 --
see the correction in section 2);
trimmed 9.5 min / 3.3 GB (GCC 14). E5-2670 v3, `-j24`.

Test trees, safe to delete: `~/src/ulkb-test` (legacy runs, 4.9 GB) and
`~/src/elkt-test` (new tool: 7.2.6 built, 6.12.110 configured, 5 GB).

## 9. Compilers and build tools (added 2026-09-19, on request)

Asked for: *check for compilers on the system, then ask which compiler and
build tools to use -- Intel's DPC++ too; Python versions, ld and binutils; what
else are we forgetting?*

**What the first version got wrong.** It looked for `gcc` and `clang` in
`/usr/bin` and on PATH, and asked one question. On this machine that missed
Red Hat's gcc-toolset-15, ROCm's clang, and two of the three binutils sets; and
it told gcc from clang by looking for the word "clang" in the banner, so
Intel's `icx` -- "Intel(R) oneAPI DPC++/C++ Compiler" -- would have been filed
as a **gcc**.

**Found on siege3** (`toolchains`, 0.8 s):

| | |
|---|---|
| compilers | `/usr/bin/gcc` 14.3.1 (system) - `/usr/bin/clang` 21.1.8 (system, full LLVM set) - `~/opt/gcc-17` 17.0.0 experimental - `/opt/rh/gcc-toolset-15` gcc 15.2.1 with its own binutils 2.44 - `~/opt/clang-current` 24.0.0git, full LLVM set - `/opt/rocm/llvm` clang 23 (fork: not recommended) - Intel oneAPI `icx`/`dpcpp` 2026.1.1 (**refused**) |
| binutils sets | 2.41 `/usr/bin` - 2.44 gcc-toolset-15 - 2.46 `~/opt/binutils-246` |
| linkers | ld.bfd 2.41 - ld.lld 21.1.8 - ld.gold (the kernel refuses it) - no mold |
| python3 | CPython 3.12.14 (platform, the default) - CPython 3.14.7 x2 - PyPy 3.11.15 (what `python3` means on PATH) |
| other | make 4.4.1, pahole 1.31, rustc 1.92.0, **no bindgen** (so no CONFIG_RUST / NOVA), no ccache |

**Design.**
- *Discovery* is by directory and name pattern, then by asking the binary:
  `--version`, `-dumpmachine` (a cross-compiler is not a candidate), and a
  compile in the scrubbed build environment (needs-`LD_LIBRARY_PATH` = broken).
  One compiler under two names (`amdclang`/`clang`, `icx`/`dpcpp`) is listed
  once; hard-linked binutils directories are one set.
- *Classification*: Intel first, then clang, then gcc. kbuild supports GCC and
  Clang only -- ICC support was removed in 6.2 -- and `icx` defines `__clang__`
  (measured here: `__clang_major__` 22), so kbuild's own probe would accept it
  as Clang 22. The tool refuses instead. ROCm/AOCC forks: selectable, flagged.
- *Four independent choices*: compiler (`--cc`), binutils (`--binutils auto |
  gnu | llvm | <dir>`), linker (`--linker default | bfd | lld`), python
  (`--python`); plus `--ccache` when installed, and `--jobs`. `auto` binutils
  = what was built to go with the compiler: LLVM's for a clang with the full
  set beside it (`LLVM=1`, `LLVM=/dir/` or Debian's `LLVM=-18`), a
  gcc-toolset's own, the system's otherwise.
- *A chosen binutils directory goes first on PATH*, which is how gcc finds its
  assembler -- and is why `CC=` is then always an **absolute path** with
  `HOSTCC=` pinned: that directory may hold a `gcc` of its own.
- *The plan shows what will really run*, by asking the compiler
  (`-print-prog-name=as`) inside the build's PATH.
- *Minimum versions come from the tree*: the requirements table in its own
  `Documentation/process/changes.rst` is parsed and the **chosen** tools are
  checked against it (7.2.6: 14 rows apply; 6.12.110: 12). Runtime rows
  (e2fsprogs, GRUB, iptables ...) are ignored.
- *Also new, from "what else are we forgetting"*: the compressor the config
  selects (`CONFIG_KERNEL_*`, `CONFIG_MODULE_COMPRESS_*`) must exist -- here
  `lz4` and `lzop` do not, harmless until a config picks them, and then fatal
  at the last step of the build; install refuses without an initramfs
  generator; `doctor` warns about an empty GPU firmware directory and about
  `-j` beyond what RAM carries (about 1 GB per job, 2 with debug info);
  `dkms status` (3 s of bash on every start-up) replaced by reading
  `/var/lib/dkms` directly.
- *Wizard*: numbered compiler list (unusable ones shown with the reason, not
  numbered; a path may be typed), then the build tools that go with the choice
  and "change any of these?" -- so the common case is two keystrokes.

**Tested (2026-09-19).**

| | |
|---|---|
| `toolchains` with 9 option combinations | 6 resolve and name the real assembler/linker/python; 3 are refused with the reason (`icx`, `--binutils llvm` with gcc, a Python 2 as `--python`) |
| **real build, non-default choice**: `--cc /opt/rh/gcc-toolset-15/root/usr/bin/gcc` | 9 m 57 s, 0 warnings. The kernel's own record: `CONFIG_CC_VERSION_TEXT="gcc (GCC) 15.2.1"`, `CONFIG_AS_VERSION=24400`, `CONFIG_LD_VERSION=24400` -- the toolset's assembler and linker were the ones that ran. The DKMS probe still compiles with root's gcc 14. |
| the wizard, driven through a real pty | chose clang 24, changed python to 3.14, `-j16`, btf; the plan showed `LLVM=/home/madness/opt/clang-current/bin/ HOSTLDFLAGS=-no-pie PYTHON3=/usr/local/bin/python3`; 6.12.110 checked against *its* table; declined at "Proceed?" -- nothing written |
| 18 new unit tests, fake compilers (shell scripts with chosen banners) | pass. They found one bug on the way in: an `ld` was taken for GNU if its banner *contained* "GNU" -- and lld's says "compatible with GNU linkers". |

Not tested: a clang/LLVM build with the *new* code path (the mechanism is the
old `LLVM=` one, which built your 7.2.4-gpu); `--ccache on` (ccache is not
installed here); the Debian `LLVM=-18` suffix form outside the unit test.
The test tree `~/src/elkt-test/linux-7.2.6` was rebuilt with the system gcc
afterwards, so it is in the recommended state for the first `install`.

### 9a. C++ compilers, and installs the list was hiding (2026-09-20, reported by the user)

*"It didn't allow me to pick a C++ compiler ... multiple clangs it needs to pick
up ... two GCCs it needs to show."* Both were real.

- **C++.** The kernel is C, but kbuild has a C++ compiler of its own, `HOSTCXX`:
  it compiles the **GCC plugins** -- against the chosen gcc's plugin headers, so
  it should be the g++ that came with that gcc -- and `xconfig`. The tool never
  mentioned it, so a build with `~/opt/gcc-17` compiled gcc 17's plugins with
  the system g++ 14. Now: every `g++`, `clang++`, `icpx`, `dpcpp`,
  `amdclang++` is found (scanned separately from C: `clang++` is a symlink to
  the very binary `clang` is, and a shared "seen" set dropped it); the wizard
  asks right after the C compiler; `--cxx` sets it; the default is the C++
  driver installed beside `--cc` (under `LLVM=` kbuild picks `clang++` itself).
  A mismatched choice is allowed and said out loud. Intel `icpx`/`dpcpp`:
  listed, refused, same reason as `icx`. Here: g++ 14.3.1 / 15.2.1 / 17.0.0,
  clang++ 21.1.8 / 23 (ROCm) / 24.0.0.
- **Hidden installs.** Aliases were merged on the version banner alone, to fold
  `amdclang` into ROCm's `clang` and `dpcpp` into `icx`. Two installs built from
  one commit share a banner too -- `~/opt/clang-24-3` and `-24-4` would have
  shown as one. The key is now (banner, clang's `InstalledDir`); gcc-family
  compilers are only ever merged by real path.
- On disk today there is one oneAPI, **2026.1.1** (`/opt/intel/oneapi/compiler/2026.1`;
  no 2025), three clangs (21 system, 23 ROCm, 24 yours -- `clang-current` is a
  symlink to `clang-24-4`, the only 24 left) and three gccs (14, 15 toolset, 17).
- Not tested: a real build with `HOSTCXX=` set and `CONFIG_GCC_PLUGINS=y`.
  `HOSTCXX` is a standard kbuild variable; the pairing logic is unit-tested.

### 9b. From the user's first real run (clang 24, `-march=native`, 2026-09-20)

The build was fine: 12 m 20 s, 0 warnings, all 120 loaded modules covered,
amdgpu a module. Three defects in the tool showed up around it.

- **The status line scrolled the whole build past as fragments.** It was up to
  ~100 characters; the terminal was 80. `\r` returns to the start of the *last
  wrapped row* only, so every redraw left a copy behind. Now cut to the
  terminal's width (`fit_line`) and redrawn at most ten times a second.
- **`LOCALVERSION` typed without its dash** gave `7.2.6machine-native-gpu`. A
  value with no leading separator now gets `-`, and says so.
- **The DKMS check blocked the install, and tested the wrong thing.** It ran a
  plain `make` (gcc). dkms 3.4.3 here reads the kernel config and, for a
  clang-built kernel, adds `LLVM=1` on its own -- so it would use *root's*
  clang. The probe now runs what this dkms runs. Result for this tree, and
  worth knowing: root's clang is 21, the kernel was configured by clang 24, and
  clang 21 rejects `-fms-anonymous-structs` -- so **dkms's own clang detection
  does not rescue a kernel built by a newer clang than the system's**. A plain
  `make` (Intel's sep5, VirtualBox, vendor installers) fails too, with gcc.
  Both are now *warnings* with the working command line
  (`sudo env LLVM=/home/madness/opt/clang-current/bin/ <build command>`), and
  the check blocks an install **only when something needs it**: a DKMS module
  that will be built for that release, or an out-of-tree module loaded now.
  Here neither exists (amdgpu-dkms is capped at 6.18), so choosing clang 24 on
  purpose is no longer treated as an error.

### 9c. Profiles first, and Secure Boot (2026-09-20, the two blockers the user named)

*"It doesn't bring up the list of saved profiles as the first thing ... so I
have to continuously answer the same questions."* A profile saved under a bare
name was written into the current directory and never looked for again (three
of them sat in the tool's own directory). Now: a profiles directory
(`~/.config/<tool>/profiles/`); the wizard's **first screen is the list**,
newest first, Enter = reuse the newest **as it is** (no questions; plan, then
one "Proceed?"), `e` = edit with its values as defaults, `n` = new; strays in
the current and the tool's directory are found by their header line; `install`
and `set_default` are saved too; `profiles` lists them; `--profile NAME`.

*"It never asks to turn secure_boot on, and it should be on as the default."*
`--secure-boot on|off`, default **on**, asked in the wizard.
- *Config*: `EFI_STUB`, `MODULE_SIG` + `MODULE_SIG_ALL` required; lockdown LSM,
  platform / machine (MOK) / secondary keyrings, `LOAD_UEFI_KEYS` wanted.
  The AlmaLinux base already had all of it -- a defconfig base would not, and
  now it is guaranteed and verified. `MODULE_SIG_FORCE` stays off: lockdown
  enforces under Secure Boot, and with it off an unsigned module still loads.
  In-tree modules keep kbuild's per-tree throw-away key (as distros do); only
  the IMAGE uses the persistent key.
- *Image*: signed after every build with a key made once
  (`~/.config/<tool>/keys/secureboot.{key,crt,der}`, RSA 2048, key 0600), via
  `sbsign` or `pesign` + a throw-away NSS database. `bzImage.unsigned` is kept.
  `sign --source TREE` does it for a tree that is already built.
- *Install*, where Secure Boot is enabled: refuses an unsigned image, and a
  certificate that `mokutil --test-key` says is not enrolled.
- **Tested**: key generation; a real `bzImage` signed and independently read
  back with `pesign -S` (signer = the key's CN; +1,512 bytes); re-run says
  "already signed". **Not tested**: booting it with Secure Boot enabled -- this
  machine has Secure Boot off (firmware in Setup Mode) and no reboot was done;
  `sbsign` path (not installed here); arm64 images (x86_64 only so far).
- For a kernel other people download: ship `secureboot.der` with it; they run
  `sudo mokutil --import secureboot.der` once. Not yet: packaging
  (`binrpm-pkg`/`bindeb-pkg`) -- the tool still installs from the tree.

### 9d. clang 24 "for both", packaging, and a correction (2026-09-20)

**"Let me select clang 24 for both."** Both = the kernel, and everything built
against it afterwards. The second half is what failed: root's builds use gcc,
or (dkms 3.4.3 adds `LLVM=1` by itself) root's clang 21, and clang 21 rejects
`-fms-anonymous-structs`, which the clang-24-made config demands.
Fix: the tool writes **`GNUmakefile`** into the tree. GNU make reads it before
`Makefile`, so `make -C /lib/modules/<rel>/build M=...` lands there first.
- `override LLVM := <prefix>` beats a caller's `LLVM=1` in that make; but kbuild
  immediately re-invokes itself with `-f Makefile`, which does *not* read the
  wrapper -- so the caller's definitions are filtered out of `MAKEOVERRIDES`
  and the pinned ones appended, which is how make passes command-line variables
  down. Variables *not* pinned are `unexport`ed: a caller's exported `LLVM`
  must not turn a gcc-built tree's modules into clang ones.
- Guards: only where the compiler exists (`$(wildcard ...)`), off with
  `ELKT_NO_PIN=1`, never touches a `GNUmakefile` it did not write, rewritten or
  removed on every build so a stale pin cannot overrule a new choice.
- **Tested on the user's real clang-24 tree:** the two probes that failed the
  day before -- plain `make` (gcc) and dkms-style `LLVM=1` (clang 21) -- both
  compile. Mechanics unit-tested against a twenty-line miniature of kbuild's
  sub-make. `pin --source TREE --cc ...` for already-built trees; the same
  command would make Intel's sep5 build for the running 7.2.4-gpu.

**Packaging** (`package`): the kernel's own `binrpm-pkg` / `bindeb-pkg` /
`tarxz-pkg`, plus what a downloader needs: `secureboot.der`, `INSTALL.txt`
(install steps, MOK enrolment, what CPU it needs, whether DKMS will work),
`SHA256SUMS`. Found by experiment, not assumed: `scripts/build-version`
**increments on every call**, the spec's `%build` passes it as
`KBUILD_BUILD_VERSION`, vmlinux is relinked, `bzImage` rewritten -- and the
package ships an **unsigned** kernel however carefully it was signed before.
So the wrapper's second job: a goal-less in-tree `make` (exactly what `%build`
is) ends by re-signing; builds with a goal and `M=` builds are untouched; the
tool's own builds set `ELKT_NO_SIGN`. `package` then **opens the package and
checks the image inside** and refuses to publish an unsigned one.
kbuild omits `kernel-devel` when `CC != HOSTCC` (a gcc-toolset pin): INSTALL.txt
says so, because without it nobody can build a module.

**Correction to section 2.** Run 2 of the legacy script was reported here as a
GCC 17 build. It was **clang 24**: `~/.bashrc:176` exports `LLVM=`, kbuild
reads it from the environment, and the script announced gcc regardless (B11).
Noticed only because a unit test for the pin inherited that variable. `doctor`
now lists an exported `LLVM`; `legacy/...fixed.sh` ignores it under
`TOOLCHAIN=gcc` and says so.

### 9e. The first real install, and an adversarial review (2026-09-20)

**The first real install** was the user's own wizard run: profile
`xeon-haswell-all-gpu-server-latency` (clang 24, full modules, BTF, preempt full,
HZ 100, Secure Boot on, `install = yes`, `set_default = yes`). Build 32 m 10 s
(a niced 128-thread job of the user's ran alongside), 0 warnings, 2,562 modules;
install 1 m 38 s. Checked from a second session without root: `/boot/vmlinuz-<rel>`
is byte-identical to the tree's signed `bzImage` and carries this key's
certificate; the initramfs exists; `/boot/config-<rel>` equals the tree's
`.config`; `/lib/modules/<rel>/build` points at the tree; `amdgpu.ko` has no
DWARF, keeps `.BTF`, is signed; **no root-owned file was left in the user's
tree**; amdgpu-dkms (capped at 6.18) left the new kernel alone, as predicted.
Not checked (needs root): the default entry, the initramfs contents. Not done:
a reboot.

**The review.** While that build ran, five fresh readers went through the tool
read-only (install · pin + signing · packaging · build/state/profiles · everything
else + Python 3.6 runtime), each told to report only a concrete state leading to
a wrong outcome; a second agent per area was told to REFUTE. 40 findings. The
install and build ones were verified by agents (15 confirmed, 1 refuted); the
verifiers for pin/sign and packaging died on the account's session limit, so
those -- and the fifth reader's -- were checked by hand before anything was
changed. All of it, with the experiments: `test-runs/review-2026-09-20.json`.

Fixed, each with a test unless noted:

| id | what was wrong | now |
|---|---|---|
| INS-1 | `--set-default` switched OFF the restore of the default boot entry on the FAILURE path: a `make install` dying in dracut left the machine defaulting to a kernel with no initramfs. This is the path the first real install took; it did not fail | restored whenever the install did not complete |
| BLD-2 | in-service protection stopped at mrproper: building another profile in a tree that `/lib/modules/<rel>/build` points at swapped pin, `.config` and `kernel.release` under the installed kernel -- DKMS then builds modules it refuses to load. **The advice in 9d ("build the publishable kernel with gcc") led straight into it**, all profiles sharing `~/src/linux-7.2.6` | refused when LOCALVERSION or compiler differ; says `--workdir`. Checked against the real tree |
| BLD-1 | no per-tree lock: a second `build` read a LIVE 'building' record as a kill -9 and ran mrproper under the running make | `.elkt-lock`, flock for the life of the process; build, install, sign, package |
| PIN-3 | `signing_key()` made a NEW private key over the old one when only the `.der` or `.crt` was missing (moved to a USB stick to enrol it...) | never; the `.der` is derived again, a missing `.crt` is an error |
| PIN-1 PKG-2 INS-4 | "signed by my key" was a substring match on the CN. Two keys made on one machine share a CN, and an unparsable certificate gave CN '' -- which is "in" any output, so an UNSIGNED image passed | certificate compared by SHA-256 (`pesign -e` + `openssl pkcs7`; `sbverify --cert`). Checked on five real images. The sign hook never creates a key; `XDG_CONFIG_HOME` reaches it |
| PIN-2 | the pin split `MAKEOVERRIDES` on blanks: `CC="ccache gcc"` left `gcc` behind as a GOAL of the sub-make -- "Nothing to be done", exit 0, nothing built, old image reported as built. Reproduced in the miniature | escaped blanks hidden while filtering; the tool's own makes set `ELKT_NO_PIN` (they name their toolchain) |
| PKG-1 | INSTALL.txt for a tarball said `sudo tar -C / -xf`: GNU tar replaces the `/lib` SYMLINK with a directory. On Debian that removes ld-linux from its path and nothing runs any more. Reproduced | `--keep-directory-symlink --no-overwrite-dir`, and the reason, in INSTALL.txt (no unit test: it is text) |
| PKG-3 PKG-6 | the publish directory is per release and was never emptied; SHA256SUMS hashed whatever was in it; packages were copied BEFORE the signature check | check first, then remove what earlier runs wrote, then copy; SHA256SUMS over exactly this run's files (no unit test) |
| PKG-4 | `has_devel` was always true for RPMs (`kernel-headers` matches 'headers'): INSTALL.txt promised a kernel-devel that kbuild had omitted | matches `kernel-devel-` / `linux-headers-` only; the publisher is warned too (no unit test) |
| PIN-4 PKG-5 | the re-sign hook cannot fire for `bindeb-pkg` (its build step has goals) and is bypassed by `tarxz-pkg` (`-f Makefile`). deb + Secure Boot always failed after the whole run, blaming a removed GNUmakefile | tar: the image is signed before packaging and goes in as it is. deb + Secure Boot: refused BEFORE the run, honestly. Way forward: `KDEB_PKGVERSION=<ver>-<current .version>` avoids the relink -- needs a Debian box |
| PKG-7 | `package` never checked that the tree was built, or not being built | checks vmlinux; takes the tree lock |
| PKG-8 | a `--debug-info btf` tree was packaged with full DWARF in every module (install strips, the spec does not) | `INSTALL_MOD_STRIP=1` on the package make. **Not yet exercised by a real package run** |
| PIN-5 PIN-6 | key creation failed for user@host over 45 characters, after the build; on non-x86_64 the default build ended in ERROR after compiling and left a hook that broke every goal-less make | CN capped; decided and said before the build, no hook written (no unit test; `doctor` still asks for a signing tool there) |
| INS-5 BLD-6 | ^C after the build recorded 'failed', so `install`/`package`/`sign` without `--source` passed over the tree | only a ^C during configure/build is a failed build |
| INS-6 INS-8 BLD-3 | grubby present but unable to name the default: silent. ^C under a root-owned sudo child: a PermissionError traceback. `nohup tool build &`: aborted at logout | warned before and after · taken as ^C · an ignored SIGHUP stays ignored (no unit tests) |
| LFT-1 | `--require-pgp` bypass: unsigned text BEFORE the signed block was parsed while gpg said GOODSIG | regex anchored; a file that has a signed block but does not begin with it is refused. (Stop-gap: the full fix is to parse what `gpg --output` verified) |
| LFT-2 | a `--modules full` build could be seeded from this tool's own TRIMMED `/boot/config-<rel>` via the same-series rule | trimmed candidates dropped when a full build is wanted |
| LFT-3 | the build was refused over pahole's version although `--debug-info off` never runs pahole (RHEL 8, Debian 12, Ubuntu 24.04) | optional unless debug info is on |

Refuted: BLD-7 (orphaned compilers after SIGTERM).

**Open** -- real, not fixed yet, most important first.  (2026-10-08: the ones struck through
are fixed, section 9f.)
- ~~LFT-4 / LFT-8: the "will it boot here" verdict never requires the root disk's controller/transport driver (a missing one is only a warning), and root on a PARTITION of an md array (Intel RST, `/dev/md126pN`) loses the RAID-level requirement.~~
- ~~INS-2: `install --source TREE` ignores the recorded state (a tree that failed at the build stage, or was reconfigured after its last build). The lock now covers "being built".~~
- INS-3: `--yes` silently switches off four unrelated guards, and every refusal recommends it.
  (Still open: splitting it changes the command line, which is the user's call.)
- ~~BLD-4: `--debug-info on` means "whatever the last profile left in this tree".~~
- ~~BLD-5: `--profile NAME` prefers a same-named stray in the current directory over the profiles directory.~~
- ~~LFT-6: `LLVM=<dir>/` and `LLVM=-N` are passed to kernels older than 5.18, which read them as `LLVM=1`.~~
- ~~LFT-5: `--gpu auto` on a non-x86 machine with no GPU makes `DRM_I915` a REQUIRED symbol.~~
- ~~LFT-7: `doctor` predates signing (no install line for a signing tool; still says "a kernel built here is unsigned").~~
- PIN-7: variables the pin leaves loose are only neutralised in kbuild's sub-make.
- From the fifth reader's low list: ~~an executable named gcc/clang/python3 that the kernel cannot exec
  (no shebang, foreign architecture) in `~/opt` or `~/.local/bin` raises an uncaught `OSError(ENOEXEC)`
  inside `detect_machine()`, so EVERY command dies; `--require-pgp` is satisfied silently when the tree
  already exists~~, and accepts a good signature from ANY key in the keyring (still open: the
  autosigner's fingerprint should be pinned -- and checked against kernel.org first, not from memory).
- INS-7, PIN-8, BLD-8 and the readers' dropped notes (dry-run fidelity; a failed `install` leaves 'installing' forever; the deb install line includes `linux-libc-dev`): low.
- Python 3.6 runtime: the fifth reader reported no API newer than 3.6 in use; still not run on a real RHEL 8.


### 9f. Her machine, and any machine (2026-10-08)

Asked for: *"make it work perfectly on this machine, her machine, and any machine"* -- the six
points of `machines/moms-machine-2026-10-08/NOTE.md` (ASUS PRIME Z790-P, i7-13700K, GTX 1630
on nouveau, SATA behind Intel VMD, AlmaLinux 10.2 with its root 98% full).

| Point | Now |
|---|---|
| **VMD** | `storage_stack()` walks from every mounted filesystem and every swap device through dm/md to the whole disks, then up sysfs, collecting each driver on the way (`sd_mod`, `ahci`, `vmd`, `nvme`, `uas`, `xhci_pci` ...). Each module's source path comes from the RUNNING kernel (`modules.dep`, `modules.builtin.modinfo`); the NEW tree's Makefile in that directory says which option builds it (the reading localmodconfig does); each becomes a REQUIRED `m|y`. A VMD domain (PCI domain >= 0x10000: five hex digits) requires `CONFIG_VMD` even when its driver is not seen. NVMe with native multipath is followed through `multipath/`; a `/dev/root` mount through `/sys/dev/block`. This is also LFT-4 and LFT-8. |
| **NVIDIA** | The chip from pci.ids (`TU117` -> `/lib/firmware/nvidia/tu117`), its firmware and GSP checked; NVIDIA's own driver for the running kernel found and classified (DKMS / akmods / prebuilt kmod); nouveau blacklists in modprobe.d and on the kernel command line. `plan`, `doctor` and the post-build checks say who drives the GPU on the new kernel -- and warn when the answer is nobody (prebuilt or akmods driver + blacklisted nouveau). Measured in 7.2.6: nouveau runs Turing through GSP by default (`NvGspRm` true) and without the files falls back to code that cannot reclock. |
| **Hybrid CPU** | `cpu_core`/`cpu_atom` -> "hybrid: 16 P-core threads + 8 E-cores"; SCHED_MC_PRIO and INTEL_HFI_THERMAL wanted. `--cpu` takes `x86-64-v2/v3/v4` and CPU names (`raptorlake`) through KCFLAGS, which kbuild appends after its own `-march=x86-64` (the `-mno-sse -mno-avx` stay); gcc 14 and clang 21 take every level with `-m32` (the vDSO32). `native` uses X86_NATIVE_CPU where the tree has it -- now REQUIRED, because as `wanted` an old clang turned it into a warning and a generic kernel -- and KCFLAGS before 6.16. The compiler's predefined macros give the level a name implies: the plan warns, and `install` refuses, a kernel this CPU cannot run. The tree's GNUmakefile exports the flags to any make that sets none. |
| **Space** | `doctor` names writable disk filesystems with room (`--workdir ...`), refuses FAT/NTFS work dirs (no symlinks), and checks /boot up front (400 MB; hers had 123 MB). |
| **Microcode, pahole** | Named for the package manager in front of it, with the initramfs command: `sudo dnf install -y microcode_ctl && sudo dracut -f --regenerate-all` (a warning now, not a note); pahole and ncurses (menuconfig) are named even when this build does not need them. CONFIG_MICROCODE wanted. |
| **Wi-Fi, r8169** | Loaded on her machine (iwlwifi, iwlmvm, r8169, realtek -- `proc-modules.txt`), so a trimmed build keeps them. What could still have lost their firmware: FW_LOADER_COMPRESS_XZ was required only when a GPU was asked for; now whenever the machine's firmware is compressed. |

From the 9e list, fixed: LFT-4/8, INS-2 (`build` records a SHA-256 of the .config it built;
`install` refuses a tree whose last build failed, never finished, or whose .config changed
since), BLD-4, BLD-5, LFT-5, LFT-6 (trees before 5.18: LLVM=1 with the chosen clang first on
PATH; clang refused before 5.7), LFT-7, ENOEXEC, `--require-pgp` on an existing tree.

New, found on the way: **`install`, `package`, `pin` and `dkms-check` take the compiler, its
tools and the CPU flags from the tree's record** -- or, for a tree with no record, the installed
compiler its CONFIG_CC_VERSION_TEXT names and the CPU flags its GNUmakefile carries.
`package` and `pin` refuse another compiler or other CPU flags (packaging compiles everything
again); `install` only says so -- kbuild lists `%install` among its no-sync-config targets, so
`make modules_install` and `make install` compile nothing and never re-run Kconfig.  (A first
version of this refused install too, and this file and the chat claimed a bare install of the
clang tree would have re-run Kconfig as root: wrong, found by the review below.)

**CPU microcode, found on the way.** On EL, `microcode_ctl` keeps `/lib/firmware/intel-ucode`
empty on purpose and gives a kernel early microcode only through its caveat check,
`check_caveats -e -k <version> -c intel`, which compares versions the way RHEL numbers them.
Measured here: `ok_cfgs intel` for the running 6.12.0-211, **`fail_cfgs intel` for
7.2.6-xeon-haswell-all-gpu-server-latency** -- every kernel this tool builds boots on the BIOS's
microcode (the "Running old microcode" of section 2a).  `install` now writes the README's own
per-kernel override, `/lib/firmware/<release>/force-early-intel`, when the running kernel passes
and the new one does not (proved with FW_DIR pointed at a scratch directory: the check flips to
`ok_cfgs intel`), and `post_install_checks` looks for `kernel/x86/microcode/GenuineIntel.bin` in
the new initramfs.  For her i7-13700K (06-b7-01) microcode_ctl 20260812 carries revision 0x137
(2026-02-18) -- past Intel's 0x129/0x12B fixes for the 13th/14th-gen instability, which her BIOS
1402 (2023-09) predates.  `doctor` no longer calls microcode missing where microcode_ctl serves it.

**The review.** One fresh reader, read-only, on its own copy (77 tool calls): 11 findings, all
checked and all fixed, each with a test that fails when the fix is taken out:
F1 install refused a good tree after any compiler change (high) -- now a warning, see above;
F2 packages built with CPU flags claimed "any CPU" in INSTALL.txt; F3 `package --cpu X`
silently recompiled the tree, and install then trusted the old record -- refused now; F4 the
record described the last ATTEMPT (a `--configure-only` cleared the .config checksum, so a
reconfigured tree installed its old kernel) -- written only when a build finishes now; F5 the
microcode false alarm (above); F6 `--debug-info on/btf` refused on 5.13-5.17 trees, where
DEBUG_INFO itself is the switch (and `off` left it on there); F7 `--cpu native` through KCFLAGS
slipped past the kernel's own refusal of clang before 19.1; F8 a tree without a record lost its
CPU flags to `package`/`pin`; F9 `dkms-check` did not fit LLVM= to trees before 5.18; F10
`--require-pgp` on an existing tree gave advice that could not work; F11 the `--cpu` help text.
Mutation-testing the new tests found one more gap of mine: nothing tested that `--require-pgp`
stops a FRESH download when the key is missing -- it does, and now a test says so.

**Tested (this workstation; her machine was off the network all afternoon).**

| | |
|---|---|
| unit tests | 166 (45 new), each new behaviour mutation-tested: breaking it fails a test. Her VMD layout is a fake sysfs built from her lspci (`10000:e0:17.0` behind `0000:00:0e.0`), not the machine itself. |
| `detect` | `disk path: sdd: sd_mod <- ahci`, `x86-64-v3` |
| `build --cpu x86-64-v3 --modules trimmed --secure-boot off` of 7.2.6, scratch work dir | BLK_DEV_SD and SATA_AHCI resolved from the tree's own Makefiles and verified; MICROCODE, FW_LOADER_COMPRESS_* verified; **9 m 27 s, 8,831 steps, 0 warnings** (gcc 14, -j16, nice 19); every compile line ends in `-march=x86-64-v3`; `lib/bitmap.o` has 34 BMI/LZCNT/MOVBE instructions, the generic gcc build of the same file 0; the DKMS probe compiles through the wrapper |
| `install --dry-run` on that tree | adopts `--cpu x86-64-v3`; both `sudo make` lines carry the KCFLAGS; `sudo mkdir -p /lib/firmware/7.2.6-elkt` + `touch .../force-early-intel` come before `make install`; an edited .config is refused |
| `install --dry-run --source ~/src/linux-7.2.6` (no record) | adopts `~/opt/clang-current/bin/clang` from the tree's config; `sudo make ... LLVM=<that>/` as built; the tree untouched |
| Python 3.6 | grammar checked with `ast` at feature_version (3, 6); no f-strings, walrus or 3.7 arguments |

Not done: running the new version on her machine (`detect`, `doctor`, `plan` -- read-only)
once it is reachable; a boot of an x86-64-v3 kernel; NVIDIA's own driver through DKMS on a
clang-built kernel (no NVIDIA DKMS anywhere here).
