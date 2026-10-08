# SATELLITE TOOLKIT -- the plan (2026-10-08)

His request is in `PROMPT_2026-10-08.md` (word for word). The second message, after the
/clear of 2026-10-08 ~15:50, word for word:

> construct satellite_toolkit in the folder /home/madness/code/py/satellite_toolkit/* built out
> of all of the other pythons and .sh files in my Documents folder, almost all of the code is
> already written, but when you transfer everything into satellite toolkit, do not take out any
> features, and if some building piece doesn't have some feature, we need to add that feature to
> that build piece in the process...

So: everything moves in, nothing is lost, and every piece ends up with the features the others
have (section 5 is the list, piece by piece).

## 1. What it is

One package, `satellite_toolkit/`, whose main file builds -- on THIS machine or on ANOTHER one
over ssh -- the whole chain he named, in his order, and then BUILDS SATELLITE:

| # | step id | what (his words) | tool | done when |
|---|---|---|---|---|
| 1 | `system-clang` | install clang++ from dnf (apt on Debian/Ubuntu) | the main file | `/usr/bin/clang++` compiles and runs a C++17 program |
| 2 | `clang` | build clang++ with that clang++ | `tools/clang/enterprise_clang_tool.py` | `<opt>/clang-current/bin/clang` runs |
| 3 | `binutils` | build binutils | `tools/gcc/enterprise_gcc_tool.py --component binutils` | `<opt>/binutils-current/bin/ld` runs |
| 4 | `gcc` | build gcc | `tools/gcc/enterprise_gcc_tool.py --component gcc` (with that binutils) | `<opt>/gcc-current/bin/gcc` runs |
| 5 | `python` | build python, the latest, with that gcc/binutils | `tools/python/enterprise_python_tool.py --for general` | `<opt>/cpython-current/bin/python3` runs |
| 6 | `pypy2` | build pypy2 with that python/gcc/binutils | `tools/pypy/enterprise_pypy_tool.py --target pypy2` | `<opt>/pypy2-current/bin/pypy` runs and makes an ssl context |
| 7 | `pypy3` | build pypy3 with that pypy2 | `tools/pypy/enterprise_pypy_tool.py --target pypy3` | `<opt>/pypy3-current/bin/pypy3` runs and makes an ssl context |
| 8 | `kernel` | lastly the linux kernel, with AMDGPU, with everything | `tools/kernel/enterprise_linux_kernel_tool.py` | a kernel the tool built is installed (`/lib/modules/<rel>` + `/boot`) |
| 9 | `satellite` | BUILD SATELLITE | the main file + the satellite tree's `install/install_build_requirements.py` | satl built from the source, its hello proof passes |

`<opt>` is `--opt-root`, default `~/opt`. Every build lands in `<opt>/<name>-<version>` and is
never installed over; `<name>-current` is the symlink everything else reads.

**Recommended.** Every run shows the first step that is not done as RECOMMENDED -- on a new
machine that is step 1, then step 2, and so on, his order -- and offers **build all
recommendations** = every step not done yet, in that order, stopping at the first failure; run
it again and it resumes (finished steps are detected, not redone). The menu says exactly that.

**Local or remote, for each thing.** The menu asks where first (Enter = this machine); `w`
switches between steps. Remote = the toolkit is uploaded there and the step runs there, in a
session that survives a dropped ssh connection (section 3).

### Choices made here (reversible; each is one option)
- **pypy2's "with that python".** RPython -- what translates PyPy -- is Python 2 code: CPython 3
  cannot run it. So step 6 builds CPython 2.7.18 with that gcc/binutils (in the pypy tool's
  workdir, not in `<opt>`) and translates pypy2 with it; the C it emits is compiled by that gcc.
  `--host prebuilt` (the .sh's default: download the official portable PyPy2, faster) and
  `--host system|PATH` stay available. pypy3 is translated by the pypy2 of step 6, as asked.
- **binutils and gcc are bootstrapped by the system gcc** (GCC's own requirement, and what the
  .sh did -- a clang CC breaks GCC's in-tree GMP/MPFR LTO probe); gcc is configured
  `--with-as/--with-ld` = step 3's binutils, so "that gcc/binutils" is one pair from then on.
- **The kernel is built with step 2's clang** (his 8-step plan for her machine: "run the kernel
  tool there with that clang"), kbuild's python = step 5's CPython, `--gpu all` (AMDGPU and every
  other GPU as modules), then installed WITHOUT becoming the default boot entry. `--kernel-cc gcc`
  uses step 4's gcc + step 3's binutils instead.
- **Satellite is built with step 2's clang** (the Makefile reads `~/opt/clang-current`): `make
  window` (the carried GTK/VTE stack) and then a plain `make`. The plain-make-builds-GTK patch is
  not needed for this and still waits for his yes.

## 2. Layout

```
satellite_toolkit.py          the main file: menu, status, recommended, all, each step, env, package
satellite_remote.py           local-or-remote, ssh, upload, sessions -- embedded into every tool
sync_remote.py                writes satellite_remote.py into each tool between the markers
tools/clang/                  enterprise_clang_tool.py  + tests/ + README.md
tools/gcc/                    enterprise_gcc_tool.py     (NEW: binutils + gcc) + tests/ + README.md
tools/python/                 enterprise_python_tool.py  + tests/ + README.md
tools/pypy/                   enterprise_pypy_tool.py    (NEW: pypy2 + pypy3 + the 2.7 host) + tests/ + README.md
tools/kernel/                 enterprise_linux_kernel_tool.py + tests/ + README.md + PLAN.md + legacy/ + machines/
tools/satellite/              install_build_requirements.py (a copy of the satellite tree's; the tree's wins)
legacy/                       every .sh, byte for byte, with history/ (.bak files), HANDOFF.md, SHA256SUMS
tests/                        the main file's and satellite_remote.py's tests
```

The originals in `~/Documents` are left where they are (copied, not moved): checksums of every
one are in `~/.claude/projects/-home-madness-code-cxx-satellite/backups/2026-10-08-before-satellite-toolkit/`.

## 3. Contracts every tool keeps

**Style** (the three Python tools already share it): one file, standard library only,
`#!/usr/bin/python3 -I`, syntax held to Python 3.6 (checked with `ast.parse(...,
feature_version=(3, 6))` in its tests), re-exec under `/usr/bin/python3` when started by PyPy,
never run as root (sudo only for package installs and the kernel install), a scrubbed build
environment (allow-list; `/usr/bin` first; no CC/CXX/*FLAGS/LD_*/LLVM), a lock (one instance per
work directory), a recorded state + resume, logs per step and the FIRST real errors on failure,
install through a staging directory + one rename (a prefix is whole or absent, never installed
over), verification after install, stdlib unit tests with fake tools (no network, no root).

**Local or remote.** `satellite_remote.py` is embedded verbatim in every tool between

    # >>> satellite_remote >>> ...
    # <<< satellite_remote <<<

and the first line of each tool's `main()` is

    argv = where_to_run(argv, __file__, TOOL)

It strips its own options before argparse sees them: `--local`, `--remote[=USER@HOST]`,
`--attach[=SESSION]`, `--sessions`. On a terminal with neither `--local` nor `--remote` it asks
"run where? local (this machine) or remote (another machine over ssh) [local]". Remote: the
ssh target is asked (the last one is the default, kept in `~/.config/satellite_toolkit/remote.json`),
the login uses `~/code/py/satellite_ssh` when it keeps a password for that target (else plain
ssh: a key, or the password typed), the tool file is uploaded to
`~/.satellite_toolkit/code/<tool>-<sha12>/` there, and it runs there as
`/usr/bin/python3 -I <tool> <args> --local` inside a SESSION: a small keeper process on the
remote holds the program on a pseudo-terminal and keeps its output in a log, so a dropped ssh
connection does not stop a build. This side attaches to it: output streams here, keys go there,
`Ctrl-]` detaches (the build keeps going), `--attach` comes back, `--sessions` lists them.
When the remote asks `[sudo] password for <user>:`, the kept password is typed for it (never
shown, never written anywhere new). Children (the main file running a tool) pass `--local`, and
`SATELLITE_TOOLKIT_WHERE=local` in the environment means the same.

**`--opt-root DIR`** (default `~/opt`) on every tool that installs into `~/opt`, and
`--link-current` / `--no-link-current` (or the tool's existing `--activate` / `--no-activate`)
for the `<name>-current` symlink.

**`doctor --json`** on every tool: `{"tool": ..., "ok": bool, "package_manager": "dnf|apt-get|zypper|pacman|null",
"missing": [{"what": ..., "package": ..., "level": "required|wanted|optional"}], "enable_repos": ["crb"],
"install_command": "sudo dnf install ..."}` -- the main file installs everything the selected
steps lack ONCE, up front (build_everything.sh's rule, below).

**`status --json`** on every tool: what is installed where, with versions -- the menu shows it.

**Exit codes**: 0 done, 1 failed, 2 usage, 130 interrupted.  **`--yes`**: ask nothing.

## 4. The main file

```
satellite_toolkit.py                    the menu (asks local or remote first)
satellite_toolkit.py status [--json]    every step, done or not, and the recommended one
satellite_toolkit.py next               run the recommended step
satellite_toolkit.py all                build all recommendations
satellite_toolkit.py STEP               one step: system-clang clang binutils gcc python pypy2 pypy3 kernel satellite
satellite_toolkit.py plan               what `all` would do, touching nothing (--dry-run is the same)
satellite_toolkit.py doctor             every tool's doctor, and the one install command
satellite_toolkit.py env [--dry-run|--remove]   the ~/.bashrc block (setup_clang_env.sh's, for every -current)
satellite_toolkit.py dkms-cap           the amdgpu-dkms cap (fix-amdgpu-dkms.sh), also run before a kernel install
satellite_toolkit.py tool NAME ARGS...  run one tool directly
satellite_toolkit.py package            dist/satellite_toolkit-<version>.tar.xz + SHA256SUMS
  --only a,b  --skip a,b  --force[=a,b]  --keep-going  -j N  --yes  --list  --opt-root DIR
  --local | --remote[=USER@HOST]  --attach  --sessions
```

Kept from `build_everything.sh` (every one): the plan with RUN/skip per step and costs, `--dry-run`,
`--yes`, `--list`, `--only/--skip/--force/--keep-going/-j`; "done" asked of the machine (does the
binary RUN), never a state file that can go stale; the environment scrubbed per step and PATH =
distro directories first (`--keep-path` opts out); refusing to start while another build of the
same thing runs; warning about leftover half-installed prefixes; the package-manager health check
(a repo with a bad key found from dnf's own error and passed as `--disablerepo`, dnf4's
`--setopt=strict=0` vs dnf5's `--skip-unavailable`, presence asked of rpm `--whatprovides` / dpkg,
everything missing installed once, then checked, "no such package" told apart from "failed to
install"); disk summed over the steps that will run; the RAM warning; the kernel decisions
(toolchain, the GNUmakefile trap, the .config copy, /boot space); a lock with the pid; sudo asked
once up front and kept fresh, re-checked before each privileged step; a log per step with the
error lines on failure; "exited 0 but the result is not there" treated as a failure; the default
boot entry put back after a kernel install; the summary with the PATH / LD_LIBRARY_PATH lines and
the after-reboot checks.

## 5. Every piece, every feature

Legend: **have** = already there; **ADD** = added in this work; **n/a** = with the reason.

| feature | clang | gcc+binutils (new) | python | pypy (new) | kernel | main |
|---|---|---|---|---|---|---|
| local / remote, sessions, `--attach` | ADD | ADD | ADD | ADD | ADD | ADD |
| one file, stdlib, 3.6 syntax, platform-python re-exec | have | ADD | have | ADD | have | ADD |
| never root; sudo only for installs | have | ADD | have | ADD | have | ADD |
| doctor + the one install command (dnf/apt/zypper/pacman, CRB) | have | ADD | have | ADD | have | ADD (all at once) |
| `doctor --json` | ADD | ADD | ADD | ADD | ADD | ADD |
| `--install-deps` | have | ADD | have | ADD | have | ADD (`--install-deps` per step) |
| plan / `--dry-run` | have | ADD | have | ADD | have | ADD |
| resumable build, state, lock | have | ADD | have | ADD | have | ADD |
| scrubbed environment | have | ADD | have | ADD | have | ADD |
| logs + first real errors | have | ADD | have | ADD | have | ADD |
| disk + RAM checks | have | ADD | have | ADD | have | ADD |
| staging + one rename install | have | ADD | have | ADD | n/a (make install as root) | n/a |
| verify command | have | ADD | have (`check`) | ADD | have (`dkms-check`, post-install) | n/a |
| `-current` symlink + `--opt-root` | have (`--activate`); ADD `--opt-root` | ADD | ADD (`--link-current`, `--opt-root`) | ADD | n/a | uses them |
| status (+ `--json`) | have; ADD json | ADD | have; ADD json | ADD | have (`--json`) | ADD |
| versions (what upstream has) | have | ADD | have | ADD | have (`kernels`) | n/a |
| detect | have | ADD | ADD | ADD | have | ADD (in status) |
| toolchains (compilers/binutils/pythons found) | have | ADD | ADD | ADD | have | n/a |
| profiles + `--profile/--save-profile` + wizard | have | ADD | ADD | ADD | have | n/a (the menu) |
| clean (own directories only) | have | ADD | have | ADD | have (`--clean`) | n/a |
| unit tests with fakes | have | ADD | have | ADD | have | ADD |
| README | have | ADD | have; ADD general mode | ADD | have | ADD |

From the shell scripts, every feature, and where it lives now:

- **build_gcc_universal.sh -> tools/gcc** (all): TRACK release|snapshot|git, SERIES, GIT_REF,
  GCC_GIT, GCC_VERSION auto|pinned; the three mirrors with fallback; listings fetched
  compressed; `xz -t`; `contrib/download_prerequisites`; out-of-tree build; MODE
  profiled-lto|lto|plain|quick and the disk each needs; LANGUAGES (and never exported -- trap 6);
  MULTILIB and its 32-bit packages; BOOT_ARCH; WITH_ARCH / WITH_TUNE; HOST_CC / HOST_CXX (CC from
  the environment ignored, with the warning); CONFIGURE_EXTRA; the RAM warning; the development-
  compiler banner; "failed in stage 1: changing MODE will not help"; install-strip, else install;
  the C++20 compile+link+run check with the rpath; the default -march check; gcc-current; the
  PATH / LD_LIBRARY_PATH / rpath advice.
- **reboot.txt's binutils line -> tools/gcc**: `binutils-with-gold-<ver>` (gold left the main
  tarball in 2.44), `--enable-plugins --enable-lto --enable-gold`, `-O3 -march=native` (as the
  `--tune native` default for the binutils binaries), `~/opt/binutils-<ver>`.
- **build_python_universal.sh -> tools/python (CPython) and tools/pypy (PyPy)** (all): TARGETS
  (the toolkit's steps 5-7), SRC_ROOT / OPT_ROOT, CPY_VERSION / PYPY_VERSION auto, HOST_MODE
  prebuilt|system|cpython and PYPY_HOST, cached downloads through `.part`, the newest CPython
  confirmed by a HEAD, the PyPy index fetched compressed, `.tar.bz2` or `.tar.gz`, the host binary
  per architecture, the RAM warning for a translation, `verify_interpreter` per flavour (CORE and
  NICE lists, an ssl CONTEXT, a crash told apart from a failure), CPython's configure line and
  altinstall and the `python3` link inside the prefix, `--opt=jit`, `PYPY_USESSION_DIR`,
  `build_cffi_imports.py` found wherever it is, `package.py` then the by-hand assembly, the
  interpreter found in the prefix, pypy{2,3}-current, the BUILT / FAILED summary, the PATH caution.
- **build_clang_universal.sh / build_clang_local.sh -> tools/clang** (the tool is their
  successor; its README lists what it keeps): LINK_CURRENT (`--no-activate`), TUNE / MARCH
  (`--tune`), LLVM_REF (`--ref`), SRC (`--source`), PREFIX, JOBS, INSTALL_DEPS, BOOTSTRAP = build N
  with N-1 (`--host-cc`), PY with yaml (`--python`), the configure verification, LD_LIBRARY_PATH
  out of the way, link jobs from RAM, the disk check. "Use tmux/screen over ssh" -> the sessions.
- **setup_clang_env.sh -> main `env`**: the same markers (an existing block is recognised), add or
  replace, `--dry-run`, `--remove`, a dated backup before writing, RC from $SHELL (zsh/bash),
  PREFIX, WITH_CC_EXPORTS, the `clang-use` function, `bash -n` afterwards; and the same for
  gcc-current, binutils-current, cpython-current, pypy2/3-current.
- **fix-amdgpu-dkms.sh -> kernel `dkms-cap` + main `dkms-cap`**: BUILD_EXCLUSIVE_KERNEL_MAX in
  every `/usr/src/amdgpu-*/dkms.conf` (not one pinned version), the backup -- dereferenced, as
  build_everything.sh learned --, the stale build directory removed, `dkms status` before and after.
- **universal_linux_kernel_build.sh -> tools/kernel** (done before: PLAN.md 4.5, "ported
  unchanged"; the legacy copies stay in tools/kernel/legacy/).
- **build_everything.sh -> the main file** (section 4).
- **The `.sh` integrity self-check** (non-ASCII from copy-paste) -> the package's SHA256SUMS
  and `satellite_toolkit.py package --verify`; a Python file with a stray U+00A0 is refused by
  Python itself, by name.

## 6. Testing

- Unit tests per tool (fakes; no network, no root) and for the main file and the remote block.
- Real runs on this workstation go to scratch prefixes and work directories under
  `~/src/satellite_toolkit_tests/` -- never `~/opt`, never his `-current` links.
- Remote: a user-mode sshd on 127.0.0.1 (key login) for the session machinery; then her machine
  once it is reinstalled.
- Debian, Ubuntu, AlmaLinux: their official container root filesystems, entered with
  `unshare --map-auto` (no root, no podman): status, doctor, plan, step 1 for real (apt / dnf
  install clang), install_build_requirements.py for real.
