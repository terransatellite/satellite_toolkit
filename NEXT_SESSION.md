# SATELLITE TOOLKIT -- start here after the /clear of 2026-10-08 ~16:30

Read `PROMPT_2026-10-08.md` (his first request, verbatim) and `PLAN.md` (the design: the 9 steps,
the contracts, every feature per piece; his second message is quoted at its top). HIS WORDS WIN.
(The note this replaces is kept in backups/2026-10-08-before-satellite-toolkit/toolkit-folder-as-it-was/.)

## His newest words (2026-10-08, verbatim)
- ~16:00: "just gotta reinstall almalinux 10.2 from boot.iso to get the other machine going..."
- ~16:25: "finally got the remote machine to boot... I also have a computer sitting right next to me,
  that is connected to the internet, and we have to build satellite on it too, it's a simple DELL
  OPTIPLEX 7020 and we have to do this entire process on it as well, it doesn't have a graphics
  card... So I haven't created the account claude_code yet on the other machine, but when I do,
  i'll report back to you.. gotta run clear the context is out"

## DONE
- Every build script COPIED in (byte for byte; ~/Documents untouched): tools/clang, tools/python,
  tools/kernel (+ PLAN.md, legacy/, machines/, test-runs/), legacy/ (all .sh + history/ .bak +
  HANDOFF.md + SHA256SUMS + notes). Their tests pass from here: clang 79, python 64, kernel 166.
- Backups: ~/.claude/projects/-home-madness-code-cxx-satellite/backups/2026-10-08-before-satellite-toolkit/
  (satellite tree tar + config.ini + this folder as it was + sha256 of every Documents source).
- PLAN.md written. satellite_remote.py (the local/remote block, ~1050 lines) TESTED FOR REAL over ssh
  to a private sshd: upload to ~/.satellite_toolkit/code/<tool>-<sha12>/, a session keeper (pty + log,
  survives a dropped ssh), attach, Ctrl-] detach, --attach, --sessions, exit codes, a question answered
  there, sudo answered from satellite_ssh's kept password (stub). sync_remote.py written (not yet run).
- tests/support/: pty_harness.py, stub_satellite_ssh.py, sshd_config.example (the private sshd:
  127.0.0.1:2222, key login, ForceCommand to a scratch home; STOPPED at the clear).

## IN FLIGHT -- check FIRST
Workflow **wf_fc6dbfea-db3** (task wbe0xr2fs), 4 builders, each in its own copy:
`/tmp/claude-1000/-home-madness-code-cxx-satellite/932f3d36-f2a1-4f4a-b0bf-9851dbd87eac/scratchpad/agents/<name>/satellite_toolkit/`
(name = gcc, pypy, python, clangkernel); real runs in `/home/madness/src/satellite_toolkit_tests/<name>/`
(HOME there). Results: `~/.claude/projects/-home-madness-code-cxx-satellite/932f3d36-f2a1-4f4a-b0bf-9851dbd87eac/subagents/workflows/wf_fc6dbfea-db3/journal.jsonl`.
- gcc: NEW tools/gcc/enterprise_gcc_tool.py (binutils + gcc, every feature of build_gcc_universal.sh)
- pypy: NEW tools/pypy/enterprise_pypy_tool.py (pypy2/3, CPython 2.7.18 host, smoke translation)
- python: --for general, private gcc/binutils, profiles/wizard/detect/toolchains/verify
- clangkernel: --opt-root, doctor/status --json, kernel `dkms-cap`, parity tables
**/tmp dies at a reboot: copy their results out first.** Merge = copy each agent's tools/<x>/ into
this folder (they touch only their tools/ dirs; the journal lists `files`), run `./sync_remote.py`
(their where_to_run is a stand-in between the markers), then every test file.

## NOT DONE (next, in order)
1. satellite_remote.py: a `before=` hook in where_to_run (the main uploads the satellite source
   before a remote satellite/all/next).
2. **satellite_toolkit.py, the main file -- not started.** PLAN.md section 4 is its spec. Decided:
   steps run tools as `/usr/bin/python3 -I <tool> --local ...` on a pty tee'd to <workdir>/logs/<step>.log;
   deps for all selected steps installed ONCE up front (build_everything.sh's package-manager health
   logic: broken repo -> --disablerepo, dnf4 strict=0 vs dnf5 --skip-unavailable, rpm --whatprovides /
   dpkg presence); sudo primed + kept fresh; "done" = the binary RUNS. The satellite step:
   install_build_requirements.py, `make window` (the carried GTK/VTE), `make install` (builds, installs
   into ~/.satl, PROVES itself on examples/hello_world.satl with stdout to a file -- no window, works
   headless; plain `make` ASKS, so not plain make). Locally that is HIS tree and HIS ~/.satl: say so,
   ask Proceed. Remote source = his release set (satellite_backup's satellite.upload/ignore lists,
   collect_local(root, upload, ignore)) synced to ~/src/satellite there with a size+mtime manifest.
3. The satellite tree's `install/install_build_requirements.py` (snapshot already taken) + a copy in
   tools/satellite/. EL10: gcc-c++ make python3 pkgconf-pkg-config clang ninja-build cmake bison perl
   patch nasm wayland-devel (+ llvm lld), ninja-build/nasm in CRB. Debian/Ubuntu: g++ make python3
   pkg-config clang ninja-build cmake bison perl patch nasm libwayland-dev libwayland-bin (+ llvm lld).
   WARNING: vendor/build_stack.py reads wayland .pc files only from /usr/lib64/pkgconfig (~line 296):
   `make window` will likely fail on Debian/Ubuntu -- his code: tell him, do not edit.
4. tests/test_remote.py, tests/test_toolkit.py, `package` (dist/*.tar.xz + SHA256SUMS), README.md.
5. Debian/Ubuntu/AlmaLinux tests in `unshare --map-auto` chroots (works here: userns on, subuid
   524288:65536, SELinux off; podman not needed).
6. ONE fresh-reader review, the patch beside the backup, memory.

## The machines
- **Mom's** (the IPv6 one): reinstalled AlmaLinux 10.2 from boot.iso, boots now; **claude_code NOT
  created yet -- he will report.** Then: he gives the password -> `python3 ~/code/py/satellite_ssh/satellite_ssh.py
  claude_code@<addr> setpassword`; its host key changed -> `ssh-keygen -R <addr>` (ask him first); the
  address may have changed (was ...:5a11:22ff:feba:9340, EUI-64) -- ask. Facts:
  ~/Documents/kernel_build_tool/machines/moms-machine-2026-10-08/NOTE.md (i7-13700K, 32 GB, GTX 1630, Intel VMD).
- **NEW: a DELL OPTIPLEX 7020** beside him, on the internet, NO graphics card (Intel's built-in only,
  presumably): "we have to do this entire process on it as well". Unknown: address, account, RAM, disk --
  check RAM/disk first (clang ~95 GB disk; PyPy translation 8 GB+ RAM).

## Choices made (his yes/no pending; each is one option)
A reformat: done by him. B podman: not needed (unshare chroots). C a tarball package first. D latest
upstream, with fallbacks. E ~/opt/<name>-<version> + -current. F remote = upload + session (built).
G CPython general mode (being built). H stop at the first failure, resume. I the kernel with step 2's
clang (his 8-step plan), --gpu all, installed but NOT the default boot entry. J pypy2 translated by a
CPython 2.7.18 built with that gcc (RPython is Python 2; --host prebuilt is faster).

## Rules
NO GIT anywhere in the satellite tree (snapshot before, patch after). Replies 3-6 lines, lettered
questions. <= 4 workflows; read code myself; review = ONE fresh reader. Long runs in the background.
python3 here is PyPy: /usr/bin/python3 -I. Tests never touch ~/opt, his -current links or tool
~/.config: scratch HOME. Private sshd for remote tests: `/usr/sbin/sshd -D -e -f <cfg>` (see
tests/support/sshd_config.example) + SATELLITE_SSH_OPTIONS="-p 2222 -i KEY -o UserKnownHostsFile=FILE".

## Copy and paste after /clear
    Read /home/madness/code/py/satellite_toolkit/NEXT_SESSION.md, then carry on building the satellite toolkit.
