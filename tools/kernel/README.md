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
./enterprise_linux_kernel_tool.py dkms-cap --dry-run   # cap every amdgpu-dkms so DKMS skips newer kernels
./enterprise_linux_kernel_tool.py status           # trees on record, and the kernels installed here
./enterprise_linux_kernel_tool.py status --json    # the same as data (doctor --json too)
./enterprise_linux_kernel_tool.py --remote=me@host build ...   # the same, on another machine
```

**Status:** everything up to and including the build and its checks has been run
for real. `install` has only been run as `--dry-run` and against a fake `sudo` --
start with `install --dry-run`.

Never run it as root. The build runs as you; only `install` and `dkms-cap` call
`sudo`, once, up front (`doctor --json` run as root says so in its answer).

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
| `--keep-boot-default` | Debian/Ubuntu (no grubby): keep the current default boot entry -- see below | off |
| `--no-dkms-cap` | `install`: leave amdgpu-dkms uncapped -- see *AMDGPU and DKMS* | off |
| `--opt-root` | where the satellite toolkit's compilers live (`<dir>/<name>-<version>/bin`): searched by `toolchains`, `detect` and the wizard; nothing is installed there | `~/opt` |

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
`/opt/*`, `~/opt/*` (and `--opt-root DIR`'s `DIR/*/bin`, ahead of it), Red Hat
`gcc-toolset-*`, Debian's `/usr/lib/llvm-*`, ROCm, AOCC, Intel oneAPI, the NVIDIA HPC SDK. Each compiler is asked what it
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
- **The satellite toolkit's prefixes** are taken by their REAL paths, which is what it
  passes: `--cc <opt>/clang-21.1.0/bin/clang` -- or the realpath of `clang-current/bin/clang`,
  which ends in `clang-21`, the file that `clang` links to in an LLVM install -- gets
  `LLVM=<opt>/clang-21.1.0/bin/` and that prefix's `clang++`; `--binutils` takes a binutils
  `bin/` directory or its prefix (`<opt>/binutils-2.45`). The path recorded in the tree's
  `GNUmakefile` and in the state is then that version's, not a `-current` link a later build
  moves.

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

**`dkms-cap`: amdgpu-dkms kept off kernels it cannot build for** (`fix-amdgpu-dkms.sh`,
generalised). AMD's amdgpu-dkms targets the enterprise kernel it was packaged for; against a
newer kernel -- which has the newer in-tree amdgpu, built here as a module -- its build fails.
`dkms-cap` adds `BUILD_EXCLUSIVE_KERNEL_MAX="<series>.999"` to **every**
`/usr/src/amdgpu-*/dkms.conf` (the shell script handled one pinned version), where `<series>`
is that of the newest kernel the package is built or installed for -- the rule `dkms-check`
prints. dkms skips a kernel that sorts *above* the cap, and 6.18.52 sorts above `6.18`: the
script's `6.18` excluded every point release of the very series it served, hence `.999`.

- The cap goes into the **real file**: on AMD's packaging `dkms.conf` is a symlink into
  `amd/dkms/`. Before the first change it takes a **dereferenced** backup,
  `<real file>.pre-elkt-dkms-cap` (`cp -aL`; `cp -a` copies the symlink, a "backup" that then
  shows the changed text -- build_everything.sh's lesson). An existing backup is never
  overwritten: it is the vendor's file.
- A `BUILD_EXCLUSIVE_KERNEL_MAX` already there is left alone; `--force` replaces it.
  `--max 6.18` caps at `6.18.999` instead of the computed series. A cap below a kernel the
  package is INSTALLED for is refused (`--force` overrides).
- The stale `/var/lib/dkms/amdgpu/<version>/build` a failed build leaves is removed (never
  `dkms remove`).
- `dkms status` is shown before and after; then what the cap means for the newest tree this
  tool built. `--dry-run` prints the sudo commands and the lines it would add, and runs
  none. sudo is used for these writes only, asked once.
- No dkms, or no amdgpu-dkms: nothing to do, exit 0. A failed write or a refused cap: exit 1
  (the other packages still get their turn). `--json` prints `{tool, ok, packages: [{version,
  conf, real, backup, max_before, max_after, action, build_removed, error}],
  dkms_status_before, dkms_status_after, release, skips_release, dry_run, error}`.

**`install` caps it first.** When a registered amdgpu-dkms would try (and fail) to build for
the release being installed, `install` applies the same cap, in the same sudo session, BEFORE
`make modules_install` / `make install` -- whose hooks run `dkms autoinstall` for the new
kernel -- and says so (build_everything.sh's stage order: dkms before kernel). A release in
the series the package serves is not capped; a package with its own cap is left alone; a cap
that fails is said and the install goes on (a mitigation, not a dependency). `--no-dkms-cap`
leaves it uncapped.

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

## The default boot entry on Debian and Ubuntu

With grubby (Fedora, EL, openSUSE) `install` reads the default entry before the install and
puts it back afterwards -- even when `make install` fails half way. Debian and Ubuntu have no
grubby: `make install` runs installkernel, whose `zz-update-grub` hook rewrites `grub.cfg`,
and with `GRUB_DEFAULT=0` -- the stock setting -- entry 0 boots the **newest** kernel by
version. A newer kernel built here becomes the default just by sorting first.

The tool reads `GRUB_DEFAULT` the way `update-grub` does (`/etc/default/grub`, then
`/etc/default/grub.d/*.cfg`, the last one wins), `grubenv`'s `saved_entry` and `grub.cfg`'s
menu, and works out whether the new release becomes the default: entry 0, `saved` with
nothing saved, the *Advanced options* submenu itself -- by number, id or title alike, since
GRUB boots a submenu's first entry -- or a number that counts into the kernel list (that
moves) -- not a kernel's entry named by its id or title, which stays.

- **By default it changes nothing and warns, loudly**, before the build (`build --install`),
  before the install and after it, with the exact commands that keep the current kernel the
  default:

  ```
  sudo cp -a /etc/default/grub /etc/default/grub.bak-$(date +%Y%m%d)
  sudo sed -i -E 's/^([[:space:]]*(export[[:space:]]+)?)GRUB_DEFAULT=.*/\1GRUB_DEFAULT=saved/' /etc/default/grub
  sudo grub-set-default 'gnulinux-advanced-<uuid>>gnulinux-6.8.0-45-generic-advanced-<uuid>'
  sudo update-grub
  ```

- **`--keep-boot-default`** does exactly that, before `make install`: a dated backup
  (`/etc/default/grub.elkt-YYYYmmdd-HHMMSS`), `GRUB_DEFAULT=saved`, `grub-set-default` to
  the entry of the kernel that boots now (by its own id, inside *Advanced options*), and
  `update-grub`; afterwards it checks `saved_entry`. When it cannot -- `GRUB_DEFAULT` set in a
  `grub.d` file, which it does not edit, or no entry of that kernel in `grub.cfg` -- it stops
  BEFORE anything is installed; `build --install --keep-boot-default` checks the same before
  the build starts, not after it. `GRUB_DEFAULT=saved` with an entry saved needs nothing.
  `--keep-boot-default` and `--set-default` are refused together.
- `/etc/default/grub` is never changed without `--keep-boot-default`.

## The satellite toolkit

This tool is step 8 of the satellite toolkit (`../../PLAN.md`): "lastly the linux kernel, with
AMDGPU, with everything". What it keeps of the toolkit's contracts:

**Local or remote.** The first thing `main()` does is `where_to_run()` (satellite_remote,
embedded between the `# >>> satellite_remote >>>` markers by `sync_remote.py`; never edited
here). `--local` runs here; `--remote[=USER@HOST]` uploads the tool to
`~/.satellite_toolkit/code/` there and runs it in a session that survives a dropped ssh
connection (Ctrl-] detaches, the build goes on); `--attach[=SESSION]` comes back to it,
`--sessions` lists them. On a terminal with neither, it asks "run where?";
`SATELLITE_TOOLKIT_WHERE=local` means `--local`. These four are taken out before argparse
sees anything, so `--local` is never read as `--localversion`. The session short name is
`linux_kernel`. Exit codes: `0` done, `1` failed, `2` usage, `130` interrupted; a remote run
exits with the program's own code once it ended there, or `75` (detached with Ctrl-], it
keeps running there) or `255` (the connection dropped; `--attach` goes back).

**Every subcommand** takes `--yes`, `--json`, `--opt-root DIR` and `--state-dir DIR`. In this
tool `--yes` also OVERRIDES the safety refusals (installing over the running release, an
unenrolled MOK key, a CPU level this machine lacks, failed post-build checks), so the main
file never passes `--yes` -- nor `--set-default` -- here. A plain `build` asks nothing anyway.

**`--json`**: stdout is the JSON and nothing else; every message, an `ERROR:` included, goes
to stderr. `doctor --json` prints exactly

```
{"tool": "enterprise_linux_kernel_tool", "ok": false, "package_manager": "dnf",
 "missing": [{"what": "flex", "package": "flex", "level": "required"},
             {"what": "pahole (`--debug-info btf` needs it)", "package": "dwarves", "level": "wanted"},
             {"what": "room: /home/me/src has 3.1 GB free; ...", "package": "", "level": "required"}],
 "enable_repos": [], "install_command": "sudo dnf install -y flex dwarves"}
```

-- `doctor`'s checks as data: build tools, headers, the signing tool (Secure Boot on), pahole
(required with `--debug-info btf|on`, wanted otherwise), GPU firmware and CPU microcode
(wanted), ncurses (optional), and what no package fixes (`package: ""`): room in the work
directory and `/boot`, a path with spaces, a FAT/NTFS work directory, the toolchain chosen
-- and running as root, which `doctor --json` REPORTS (`ok: false`, exit 1) instead of
refusing before the answer. `install_command` installs what is required and wanted, nothing
else; `enable_repos` names CRB / PowerTools / codeready-builder when a package needs it (none
of the kernel's does on EL 8-10). Exit 0 when `ok`, else 1. No package manager is run.

`status --json` prints the kernel tool's status shape (PLAN.md section 3) at the top level,
plus what `status` shows beyond it:

```
{"tool": "enterprise_linux_kernel_tool",
 "installed": [{"release", "modules": bool, "build_link": bool, "boot_image": bool,
                "built_by_tool": bool, "default": bool,
                "complete": bool, "image", "tree", "running": bool}],
 "trees": [{"tree", "status", "stage", "release", "error",
            "exists", "started", "finished", "toolchain", "cc", "running", "in_service"}],
 "state_file", "running", "default", "default_known",
 "amdgpu_dkms": [{"version", "conf", "max"}]}
```

`installed` is every release in `/lib/modules` or `/boot`. `complete` = modules, the build
link and the image all there (modules_install alone is not "installed"). `built_by_tool`: a
tree on record has that release, or its build link is a tree with this tool's `GNUmakefile`
pin, or the release ends in the default LOCALVERSION `-elkt`. `default` is read without root
(grubby, a BLS `saved_entry`, or GRUB_DEFAULT on Debian); `default_known: false` when it
cannot be (EL keeps `grubenv` root's). No sudo, no network.

**What the main file runs for step 8**, toolchain inputs as REAL paths (the `-current` link's
target when the step starts):

```
/usr/bin/python3 -I enterprise_linux_kernel_tool.py --local build --kernel stable --gpu all \
    --cc <opt>/clang-21.1.0/bin/clang --python <opt>/cpython-3.14.0/bin/python3 \
    --workdir <work>/kernel-clang --install --opt-root <opt>
```

(`--kernel-cc gcc`: `--cc <opt>/gcc-16.2.0/bin/gcc --binutils <opt>/binutils-2.46` in its own
`--workdir`.) The step is done when `status --json` lists a `complete` release that is
`built_by_tool`.

## It will refuse to

build from `make defconfig` unless you ask for it by name · build a config in
which a required option did not come out as asked (the tree's `.config` is left
alone; the rejected one is kept beside it) · install over the running kernel's
release without `--yes` · install an unsigned kernel with Secure Boot on
without `--allow-unsigned` · change your default boot entry without
`--set-default` (and with it, a `make install` that FAILS still gets the old
default back) · change `/etc/default/grub` without `--keep-boot-default` · install
with `--keep-boot-default` when it cannot keep the default · install a tree whose last build failed, or whose `.config`
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
/usr/bin/python3 -I tests/test_tool.py
```

215 tests, no network, no root, no kernel tree; they pass as root (one test that needs a
read-only directory to stop a write is skipped there) and as an unprivileged user. Disk paths are tested on fake
sysfs trees laid out like real ones (SATA behind VMD, NVMe multipath, an md
partition, USB through a built-in xHCI). Compiler discovery is tested with
fake compilers -- shell scripts that print a chosen banner. `install` is tested against a
fake `sudo`, including the case where `make install` fails half way.

For the satellite toolkit: the Python 3.6 grammar and ASCII of the tool and its tests; the
remote block is what `sync_remote.py` writes and `where_to_run` is the first statement of
`main()`; `--local` never reaches argparse; every subcommand takes `--yes`, `--json`,
`--opt-root`; `doctor --json` and `status --json` in the contract's exact shape, stdout
JSON alone (doctor as root included) against a fake `/lib/modules`, `/boot`, BLS entry and
state; the package names per package manager, CRB, GPU firmware; `--opt-root` searched
ahead of `~/opt`; the realpath of an LLVM install's clang and a binutils prefix; the main
file's step-8 command line. `dkms-cap` runs its `cp` / `tee` / `rm` for real in a fake root
(a symlinked `dkms.conf` capped in the file and backed up dereferenced, an existing cap kept
unless `--force`, two packages, the stale build directory, the first backup never
overwritten, a cap below an installed kernel refused, a dry run, a failed write, the command
with nothing to do and as root); `install` caps amdgpu-dkms BEFORE `make modules_install`
(and not with `--no-dkms-cap` or within the series). GRUB: an Ubuntu `grub.cfg` parsed,
`GRUB_DEFAULT` from `grub.d`, entry 0 / saved / by title / by number, no readable `grub.cfg`;
`install` on Debian without the flag (nothing changed, the warning with the commands), with
it (backup, `GRUB_DEFAULT=saved`, `grub-set-default` before `make install`), a `grub.d`
override (nothing installed), a dry run, a saved default.

## Not done yet

A signed `.deb`; image signing on anything but x86_64 (said before the build,
not after it); keeping the default boot entry on systems with neither `grubby` nor
Debian's `update-grub` (Arch, systemd-boot: the tool says to check); a boot with Secure
Boot actually enabled; `--keep-boot-default` and `dkms-cap` run on a real Debian/Ubuntu
GRUB and a real amdgpu-dkms (tested against fakes only). The open findings of the 2026-09-20
review are listed in `../PLAN.md`, section 9e.
