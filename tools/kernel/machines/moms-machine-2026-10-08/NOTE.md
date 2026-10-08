# Mom's machine -- what the kernel build tool needs to know (gathered 2026-10-08 13:36 over ssh)

Raw files beside this note: `facts.txt` (everything below, unedited), `kernel-config-running`
(the running 6.12.0-211.61.1.el10_2 config), `proc-modules.txt` (for localmodconfig),
`detect.json` / `doctor.txt` / `toolchains.txt` (enterprise_linux_kernel_tool.py run THERE).

## Getting in
- `ssh claude_code@2603:6010:6706:8aaf:5a11:22ff:feba:9340` -- the name
  syn-2603-...-9340.res6.spectrum.com does NOT resolve (a reverse-DNS name); use the address.
- The password is 32 random characters, changed 2026-10-08 13:36, kept ONLY in
  `/home/madness/code/py/satellite_ssh/claude_code@2603:6010:6706:8aaf:5a11:22ff:feba:9340.password.txt.c4` (mode 600).
  `python3 ~/code/py/satellite_ssh/satellite_ssh.py claude_code@<address> terminal | run CMD | copy FILE [DIR] | rotate | password`
- The reformat deletes the account: re-create claude_code, then `satellite_ssh.py claude_code@<address> setpassword`.

## The machine
| | |
|---|---|
| Board | ASUS PRIME Z790-P WIFI D4, BIOS 1402 (2023-09-11), UEFI, **Secure Boot off** |
| CPU | Intel Core **i7-13700K** (Raptor Lake, hybrid 8P+8E = 16 cores / **24 threads**, 5.5 GHz, 30 MB L3) |
| CPU level | **x86-64-v3** (AVX2, AVX-VNNI, SHA, VAES); **no AVX-512, so not v4**. The standard AlmaLinux 10.3 x86_64 ISO is the right one (not the x86_64_v2 one this workstation runs). Native tuning: `-march=raptorlake`; portable: `-march=x86-64-v3` |
| RAM | **32 GB** (31 GiB usable) |
| GPU | **NVIDIA GeForce GTX 1630** (TU117, Turing, MSI board), driver now: **nouveau** (+ nvidia-gpu-firmware); NVIDIA HDMI audio on snd_hda_intel |
| Disk controller | **Intel VMD** in front of the SATA controller (PCI `10000:e0:17.0`; `CONFIG_VMD=y`, vmd bound). **A kernel without VMD cannot see these disks.** |
| sda | Patriot P210 **512 GB** SATA SSD: TWO AlmaLinux installs -- the RUNNING one on a 28.7 GB slice (root XFS on LVM, **98% full, 569 MB free**; /boot 1 GB, 87% full), and an unmounted one (LVM: 130 GB root, 300 GB home, 15.6 GB swap) |
| sdb | Patriot P210 **256 GB** SATA SSD: ext4 3.7 + 59.6 + 160.2 GB, 14.9 GB swap -- **an older system or her files? check before wiping** |
| sdc | USB 3.2 stick, 465.8 GB vfat, labelled "BIOS", mounted by jsadlier |
| Network | Realtek RTL8125 2.5 GbE (`r8169`); Intel AX201 CNVi Wi-Fi (`iwlwifi`, needs firmware) |
| Microcode | `/lib/firmware/intel-ucode` is **empty**: no CPU microcode update loads (install `microcode_ctl`) |

## The software on it now
AlmaLinux 10.2, kernel 6.12.0-211.61.1.el10_2.x86_64 (3 kernels + rescue in /boot); root XFS on LVM; SELinux
enforcing; GNOME 49 on GDM (graphical.target); grubby + BLS + dracut; firmware is .xz-compressed.
Compilers: gcc 14.3.1, **clang 21.1.8** (+ lld, the full LLVM set), gcc-toolset-15 (gcc 15.2.1, binutils 2.44);
make 4.4.1; CPython 3.12.14. Missing: **pahole** (dwarves, needed for BTF), ncurses-devel. Present:
elfutils-libelf-devel, openssl-devel, kernel-devel (only for 211.7.4).
GDM `/etc/gdm/custom.conf` already says `AutomaticLogin=claude_code` with `AutomaticLoginEnable=False`.
Password policy: yescrypt, 8 to 511 characters, no expiry on claude_code.

## What the kernel tool must learn from this machine ("perfectly here, there, and anywhere")
1. **NVIDIA.** The tool is AMDGPU-centric (PLAN 4.5). Needs an NVIDIA path: nouveau as `=m` (+ its GSP
   firmware), and/or NVIDIA's open kernel modules (they support Turing) through DKMS/akmods -- built with the
   SAME compiler as the kernel (`LLVM=1` for a clang kernel), or they will not load.
2. **VMD.** `detect` found XFS-on-LVM but not VMD. Disks behind a VMD domain (PCI domain >= 0x10000, or
   `/sys/bus/pci/drivers/vmd` bound) must add `CONFIG_VMD` (+ AHCI/NVMe) to the boot-critical symbols, or the
   new kernel cannot find its root.
3. **Hybrid CPU.** 24 threads -> `-j24`; `-march=raptorlake` for this machine only, `x86-64-v3` to carry it.
4. **Space.** `doctor` FAILED: 568 MB free, it wants about 10 GB. The clang tool wants about 95 GB. Plan the
   reformat's partitions for that.
5. **Microcode** (`doctor` warns) and **pahole** for BTF -- the tool should name both packages up front.
6. Wi-Fi firmware (iwlwifi) and r8169 must survive a trimmed (localmodconfig) build -- `proc-modules.txt`.

## Status 2026-10-08 ~15:00 -- step 2 DONE (details: ../../PLAN.md, section 9f)
- The tool now requires VMD (and every driver between her disks and the CPU), knows her GTX 1630
  (TU117: nouveau + GSP firmware), her hybrid CPU (`--cpu raptorlake` or `x86-64-v3` work), names
  the microcode/pahole packages, and checks /boot and space up front.  166 tests; a real 7.2.6
  build with `--cpu x86-64-v3` passed on the workstation.  NOT yet run on her machine (it was off
  the network all afternoon): first thing there, `detect`, `doctor`, `plan` (read-only).
- **Microcode matters for her CPU.** BIOS 1402 (2023-09) predates Intel's fixes for the
  13th/14th-gen instability.  After the reformat: `sudo dnf install microcode_ctl` (it carries
  revision 0x137 for her 06-b7-01), and ideally a newer BIOS.  On EL, microcode_ctl gives a
  self-built kernel NO microcode by itself (its version check fails on the name) -- the tool's
  `install` now writes `/lib/firmware/<release>/force-early-intel` and checks the initramfs.

## The plan, in his order (2026-10-08)
1. `/clear`.
2. Work on the kernel build tool (`~/Documents/kernel_build_tool/enterprise_linux_kernel_tool/`): make it work
   perfectly on this workstation, on her machine, and on any machine -- the six points above.
3. Reformat her machine: AlmaLinux 10.3, the x86_64 ISO (being downloaded on it). Give the new install the
   whole 512 GB disk; check sdb for her files first.
4. **Auto login** on her machine (she does not use computers much): `/etc/gdm/custom.conf`, `[daemon]`
   `AutomaticLoginEnable=True` and `AutomaticLogin=<the user>` -- which user is HIS call (it says claude_code now).
5. **The ssh password changes itself every 6 hours to 32 random characters** ("so it remains completely
   uncrackable": 71 characters ^ 32 = about 197 bits). Re-create claude_code, `setpassword`, then a timer on THIS
   workstation, e.g. crontab `0 */6 * * * /usr/bin/python3 /home/madness/code/py/satellite_ssh/satellite_ssh.py
   claude_code@<address> rotate >> /home/madness/code/py/satellite_ssh/rotate.log 2>&1` (a run while her machine
   is off fails harmlessly; the kept password never changes unless the server took the new one).
6. Build clang there: `~/Documents/clang_build_tool/enterprise_clang_tool.py` (`doctor` first, then `build`:
   several hours, about 95 GB, needs internet; installs to ~/opt/clang-<version> + clang-current).
7. Use that clang to run the kernel build tool there.
8. Still open from before: upload satellite with satellite_backup and run plain make there; the satellite patch
   waits for his yes (`~/.claude/projects/-home-madness-code-cxx-satellite/backups/2026-10-08-before-plain-make-builds-gtk/plain-make-builds-gtk.patch`);
   satellite_backup needs the IPv6 bracket fix for rsync and an ssh terminal; the satl program
   `~/code/satl/satellite_password` ("enter a character amount [16]: ") must read the OS random source
   (satellite.random is PCG -- guessable).
