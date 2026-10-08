# enterprise_linux_kernel_tool.py

Pick a kernel from kernel.org, configure it for the machine you run it on,
build it, prove that DKMS modules will still compile against it, and install it
without taking over your boot menu.

One file. Standard library only. Runs under the Python your package manager
already depends on (`/usr/bin/python3`, 3.6 or newer) on dnf, apt, zypper and
pacman systems. It installs a kernel: read it before you trust it.

```
./enterprise_linux_kernel_tool.py                  # saved profiles first; Enter reuses the newest as it is
./enterprise_linux_kernel_tool.py profiles         # the saved profiles
./enterprise_linux_kernel_tool.py sign --source T  # sign a built tree's image for Secure Boot
./enterprise_linux_kernel_tool.py pin  --source T --cc CLANG   # out-of-tree builds use the tree's own compiler
./enterprise_linux_kernel_tool.py package --source T           # RPM / deb / tarball + certificate + INSTALL.txt
./enterprise_linux_kernel_tool.py kernels          # what kernel.org offers right now
./enterprise_linux_kernel_tool.py kernels --series 6.12
./enterprise_linux_kernel_tool.py detect           # what this machine is
./enterprise_linux_kernel_tool.py toolchains       # every compiler, binutils set and python; what would be used
./enterprise_linux_kernel_tool.py doctor           # can it build? what is missing?
./enterprise_linux_kernel_tool.py plan  --kernel longterm      # resolve everything, touch nothing
./enterprise_linux_kernel_tool.py build --kernel 6.12 --modules trimmed
./enterprise_linux_kernel_tool.py build --configure-only       # stop after a verified .config
./enterprise_linux_kernel_tool.py install --dry-run            # print the sudo commands
./enterprise_linux_kernel_tool.py install
./enterprise_linux_kernel_tool.py dkms-check --source ~/src/linux-7.2.6
./enterprise_linux_kernel_tool.py status
```

**Status:** everything up to and including the build and its checks has been run
for real. `install` has only been run as `--dry-run` and against a fake `sudo` --
start with `install --dry-run`.

Never run it as root. The build runs as you; only `install` calls `sudo`, once,
up front.

## What you choose

| Flag | Values | Default |
|---|---|---|
| `--kernel` | `stable` `longterm` `mainline`, a series (`6.12` = its newest), a version (`6.12.9`) | `stable` |
| `--gpu` | `auto` `all` `none` or a list: `amd,nvidia,intel` | `auto` |
| `--cc` | any gcc or clang, by path or name -- `toolchains` lists what is installed | the **system** gcc |
| `--cxx` | the C++ compiler, kbuild's `HOSTCXX` (GCC plugins, xconfig) | the one installed beside `--cc` |
| `--binutils` | `auto`, `gnu`, `llvm`, or a directory holding `ld` and `as` | `auto`: what goes with the compiler |
| `--linker` | `default` `bfd` `lld` | `default` |
| `--python` | the python3 kbuild's scripts run under | `/usr/bin/python3` |
| `--ccache` | `off` `on` (offered only when ccache is installed) | `off` |
| `--jobs` | parallel make jobs | all CPUs |
| `--modules` | `full`, `trimmed` (only what is loaded now + GPU + a rescue set) | `full` |
| `--cpu` | `generic`, `x86-64-v2`, `x86-64-v3`, `x86-64-v4`, `native`, or a CPU name the compiler knows (`raptorlake`, `znver4`) -- x86_64 | `generic` |
| `--preempt` | `keep` `none` `voluntary` `full` `lazy` | `keep` |
| `--hz` | `keep` `100` `250` `300` `1000` | `keep` |
| `--debug-info` | `off`, `btf`, `on` | `off` |
| `--secure-boot` | `on`, `off` | **`on`** |
| `--config-base` | `auto` `running` `defconfig` or a file | `auto` |
| `--localversion` | suffix for the release | `-elkt` |
| `--clean` | `auto` `always` `never` `force` | `auto` |

**Profiles.** The wizard's first screen is the list of saved profiles, newest
first: Enter takes the newest *as it is* -- no questions, straight to the plan
and one "Proceed?" -- `e` edits it, `n` starts fresh. A profile saved under a
bare name goes to `~/.config/enterprise_linux_kernel_tool/profiles/`; it also
remembers whether to install. `build --profile NAME` replays one unattended.
Flags beat the profile.

**Secure Boot is on by default.** That means (1) a config that can live under
Secure Boot: a PE image (`EFI_STUB`), every module signed, lockdown available,
and the MOK keyring trusted so DKMS-signed modules load; and (2) the kernel
image **signed with your own key** after every build (`sbsign`, or `pesign` +
nss-tools). The key is made once in `~/.config/enterprise_linux_kernel_tool/keys/`
and reused. A machine trusts it by enrolling the certificate once:
`sudo mokutil --import secureboot.der`, reboot, *Enroll MOK*. If you publish a
kernel, publish `secureboot.der` with it -- never `secureboot.key`. On a
machine with Secure Boot enabled, `install` refuses an unsigned image and an
unenrolled certificate.

**`--debug-info off` has a cost you should know about.** With no BTF, a kernel
that has `BPF_LSM` logs `bpf-restrict-fs: Failed to load BPF object` from
systemd on every boot, and bpftrace and CO-RE agents (Cilium, Falco, Tetragon)
cannot run. `btf` keeps BTF and strips DWARF from the installed modules: a
slower build, nothing missing.

**`--modules trimmed` builds a kernel for *this* machine.** A device you plug
in later whose driver was not loaded at build time will have no driver. The
rescue set (USB storage, NVMe, AHCI, virtio, vfat/ext4/xfs, loop, squashfs) is
kept regardless; add more with `--keep-symbol USB_SERIAL`.

## Compilers and build tools

`toolchains` looks well beyond PATH: `/usr/bin`, every PATH entry, `/usr/local`,
`/opt/*`, `~/opt/*`, Red Hat `gcc-toolset-*`, Debian's `/usr/lib/llvm-*`,
ROCm, AOCC, Intel oneAPI, the NVIDIA HPC SDK. Each compiler is asked what it
is (`--version`, `-dumpmachine`) and then made to compile a program *in the
scrubbed environment the build uses* -- one that needs `LD_LIBRARY_PATH` to
start is reported as broken there, not an hour into a build.

- **GCC and Clang** are selectable, any version, anywhere. A gcc that ships its
  own `ld` and `as` beside it (every gcc-toolset) gets them by default.
- **Vendor forks of clang** (ROCm, AOCC) are listed and selectable, marked *not
  recommended*: nobody tests kernels with them.
- **Intel oneAPI DPC++/C++ (`icx`, `dpcpp`), `icc`, `nvc`** are listed and
  **refused**. kbuild supports GCC and Clang only; ICC support was removed in
  Linux 6.2. `icx` is LLVM underneath and defines `__clang__`, so kbuild's own
  probe would take it for "Clang 22" and carry on -- the tool does not.
- **Binutils**: every directory holding both `ld` and `as` is a set you can
  choose. `ld.gold` and `mold` are shown as found and unusable (the kernel
  refuses gold; kbuild has no mold support).
- **Python**: every python3 found, CPython or PyPy. The default is the platform
  interpreter regardless of what `python3` means on your PATH.

The plan then names the assembler, linker, python and make the build will
*really* run -- by asking the chosen compiler (`-print-prog-name=as`) in the
build's own PATH -- and checks them against **the tree's own requirements
table** (`Documentation/process/changes.rst`), so nothing here hardcodes a
minimum version. After configuring, it checks that the compressor the config
selects for the image and the modules (`gzip`, `xz`, `zstd`, `lz4` ...) exists:
that failure otherwise arrives at the very last step of the build.

## What it works out for itself

GPUs, NPUs and BMC/VM video adapters from sysfs PCI class and vendor ids · the
base config, preferring one from the same series as the kernel you picked · the
filesystems and block layers (LVM, dm-crypt, md) this machine mounts, **and every
driver between those disks and the CPU** -- `sd_mod`, `ahci`, `nvme`, and on boards
like an ASUS Z790 an **Intel VMD** in front of the SATA controller -- all of which
become *required* options (the new tree's own Makefiles say which option builds
each driver) · how your distro compresses firmware, which the kernel must then be
able to unpack: for the GPU, and just as much for Wi-Fi, the network card and
Bluetooth · the CPU's x86-64 level and whether it is hybrid (P- and E-cores: the
scheduler options that put work on P-cores are wanted) · every compiler present,
and which one is the system's · Secure Boot state · grubby/BLS, dracut or
initramfs-tools · DKMS modules and their kernel caps · NVIDIA: see below · free
space in the work dir and /boot, and where else there is room.

**CPU tuning.** `native` uses the kernel's own switch (`CONFIG_X86_NATIVE_CPU`,
6.16 and later; older trees get `-march=native` through `KCFLAGS`). An x86-64
level or a CPU name goes through `KCFLAGS`, which kbuild appends after its own
`-march`, so the `-mno-sse -mno-avx` that keep SIMD out of kernel code still
apply. The compiler is asked what level a name implies, and the plan warns when
the kernel would not boot on the machine building it; `install` refuses it there.
The tree remembers its flags (in its `GNUmakefile`), so a `make` run by hand or by
a package build does not recompile everything generic.

**CPU microcode on EL.** `microcode_ctl` keeps `/lib/firmware/intel-ucode` empty
on purpose and puts Intel microcode only into the initramfs of kernels whose
version it understands -- a kernel built here fails that check by its name alone
and would run on the BIOS's microcode. `install` writes `microcode_ctl`'s own
per-kernel override, `/lib/firmware/<release>/force-early-intel`, when the
running kernel passes the check and the new one does not, and afterwards looks
in the new initramfs for the microcode.

## NVIDIA

nouveau is always built as a module. From Maxwell's second generation on it needs
NVIDIA's firmware (`/lib/firmware/nvidia/<chip>`), and from Turing on it runs the
GPU through GSP firmware -- without that, a Turing card stays at its boot clocks,
and an Ada card does not run at all. The plan names the chip and says what is
missing, with the package for your distro.

NVIDIA's own driver follows a machine onto a new kernel only through **DKMS**
(`nvidia-open-dkms`, `kmod-nvidia-open-dkms`, `nvidia-dkms`) -- built with the
compiler the kernel was built with, which the tree's `GNUmakefile` sees to.
akmods builds against an RPM-installed kernel-devel, which a kernel installed
from a tree does not have, and a prebuilt kmod is made for one kernel. Where
NVIDIA's driver cannot follow, nouveau takes over -- unless nouveau is blacklisted
(NVIDIA's packages do that), and then the GPU has no driver at all. The plan says
which of these is coming, before anything is built.

Every `make` runs in a scrubbed environment: an exported `CC`, `LD_RUN_PATH`,
`LOCALVERSION` or a non-CPython `python3` on PATH cannot leak into the build.
`doctor` lists what it found in yours.

## AMDGPU and DKMS

GPU drivers are always built as **modules**. Built in, a driver probes before
the root filesystem exists and cannot find its firmware; as a module it is
also replaceable, which amdgpu-dkms and NVIDIA's driver both need.

After every build the tool compiles a ten-line out-of-tree module against the
new tree **the way DKMS will**: root's PATH, system compiler, no arguments. If
that fails, DKMS will fail, and you find out before installing anything. The
commonest cause is building the kernel with a compiler that is not the
system's; the check says so and prints the `dkms` command line that works.

The probe runs what *your* dkms runs: dkms 3.x adds `LLVM=1` by itself for a
clang-built kernel, which means root's clang, not necessarily the one that built
the kernel. A failure blocks an install only when something needs it -- a DKMS
module that will be built for that release, or an out-of-tree module loaded
now; otherwise it is a warning with the command line that works.

**A kernel built with your own clang, and DKMS still compiling.** Build with a
compiler that is not the system's and root's module builds fall back to gcc, or
(dkms 3.x) to root's *older* clang -- and both choke on flags the kernel's config
recorded. So the tool writes a `GNUmakefile` into the tree. GNU make reads it
before `Makefile`, which means every build that comes through
`/lib/modules/<rel>/build` -- DKMS, Intel's sep5, VirtualBox, a vendor installer
-- uses **the compiler the kernel was built with**, whatever the caller passes
(`LLVM=1`, `CC=cc`) or has exported. It switches itself off where that compiler
does not exist, and with `ELKT_NO_PIN=1`. `pin --source TREE --cc ...` does it
for a tree that is already built; `build` refreshes it every time.

A source tree that an installed kernel's `/lib/modules/<rel>/build` points at
is never cleaned automatically. DKMS compiles against that directory.

## Packaging

`package --source TREE` runs the kernel's own `binrpm-pkg` (or `bindeb-pkg`,
`tarxz-pkg`) and collects, in `~/kernel-packages/<release>/`: the packages,
`secureboot.der`, an `INSTALL.txt` written for the person downloading it, and
`SHA256SUMS`. With Secure Boot on it **opens the package and checks the image
inside is signed** before calling it ready -- because the kernel's packaging
step relinks the kernel under a new build number, which silently replaces a
signed image with an unsigned one. (The tree's `GNUmakefile` re-signs at the end
of that step.) "Signed" means signed with **your certificate**, compared by its
SHA-256 -- not a signer with the same name. Nothing is copied to the publish
directory until that check has passed, and what an earlier run left there
(packages, `INSTALL.txt`, `SHA256SUMS`, the certificate) is removed first, so a
rebuilt release is never published beside its predecessor. A `--debug-info btf`
tree is packaged as `install` installs it: BTF kept, DWARF stripped.

| format | Secure Boot | status |
|---|---|---|
| `rpm` | signed, checked inside the package | run for real |
| `tar` | the signed image goes in as it is; checked | not yet run for real |
| `deb` | **refused with `--secure-boot on`**, before the long run: its build step re-links the kernel where the tool cannot re-sign it. `--secure-boot off` gives an unsigned .deb | untested (no dpkg on the machine this was written on) |

`INSTALL.txt` for a tarball says `tar -C / --keep-directory-symlink ...` and says
why: the archive has a `lib/` directory, `/lib` is a symlink on every usr-merged
distro, and plain `tar -C / -xf` replaces that symlink with a directory.

Build a kernel meant for other people with the **system compiler**: their DKMS
builds with theirs.

## It will refuse to

build from `make defconfig` unless you ask for it by name · build a config in
which a required option did not come out as asked (the tree's `.config` is left
alone; the rejected one is kept beside it) · install over the running kernel's
release without `--yes` · install an unsigned kernel with Secure Boot on
without `--allow-unsigned` · change your default boot entry without
`--set-default` (and with it, a `make install` that FAILS still gets the old
default back) · install a tree whose last build failed, or whose `.config`
changed since it was built · package or pin a tree with another compiler or
other CPU flags than it was built with (`install`, `package`, `pin` and
`dkms-check` take them from the tree's record by themselves; `install`, which
compiles nothing, only says so when the compiler has changed since) · install a
kernel built for a CPU level this CPU does not have · build on FAT or NTFS ·
work in a tree another instance is working in (a lock file,
`.elkt-lock`, held for the life of the process) · **turn a tree that serves an
installed kernel into a different kernel**: if `/lib/modules/<rel>/build` points
at the tree, a build with another LOCALVERSION or another compiler is refused
-- give it its own `--workdir` -- because DKMS would then build modules the
installed kernel will not load · make a new signing key over an existing one ·
reboot, ever.

## Files

State and build logs live in `~/.local/state/enterprise_linux_kernel_tool/`
(`--state-dir` to move them). Sources go to `~/src` (`--workdir`). In a tree it
leaves `.config.elkt-prev` (the config it replaced) and, after a refused
configure, `.config.elkt-rejected`.

## Tests

```
/usr/bin/python3 tests/test_tool.py
```

166 tests, no network, no root, no kernel tree. Disk paths are tested on fake
sysfs trees laid out like real ones (SATA behind VMD, NVMe multipath, an md
partition, USB through a built-in xHCI). Compiler discovery is tested with
fake compilers -- shell scripts that print a chosen banner. `install` is tested against a
fake `sudo`, including the case where `make install` fails half way.

## Not done yet

A signed `.deb`; image signing on anything but x86_64 (said before the build,
not after it); restoring the default boot entry on systems without `grubby`; a
boot with Secure Boot actually enabled. The open findings of the 2026-09-20
review are listed in `../PLAN.md`, section 9e.
