# enterprise_python_tool.py

Builds a CPython 3, in one of two modes (`--for`):

| mode | what | prefix | proved by |
|---|---|---|---|
| `--for scraper` (the default) | a CPython **for claude_scraper**, as this tool always built it | `<opt>/cpython-<ver>-claude_scraper` | the scraper's smoke test |
| `--for general` | **the satellite toolkit's step 5**: "build python, the latest, with that gcc/binutils" | `<opt>/cpython-<ver>`, then `<opt>/cpython-current` | verify (incl. the compiler's `.comment`) + a venv smoke test |

In both modes it gets a prefix of its own and its own pip, is optimised with PGO + LTO,
installs through a staging directory and one rename, and is verified before it counts.
Successor to `build_python_universal.sh`: general mode is that script's CPython part
(PyPy went to `tools/pypy`); scraper mode is what the scraper needs on top of it.

One file. Standard library only. Runs under `/usr/bin/python3`, with syntax held
to 3.6 and pure ASCII (both tested). If it is started with a `python3` that is PyPy,
it re-runs itself under `/usr/bin/python3`. Never run a build as root: only
`--install-deps` uses `sudo`. (`doctor --json`, `status`, `versions`, `detect`,
`toolchains`, `verify` and `profiles` only look, and answer as root too; `build`,
`plan`, `wizard`, `venv`, `check`, `clean` and text `doctor` refuse root.)

```
./enterprise_python_tool.py doctor           # what is missing, and the command that installs it ([--json])
./enterprise_python_tool.py plan             # version, paths, configure line -- builds nothing (= build --dry-run)
./enterprise_python_tool.py build            # download, build (PGO+LTO), install, verify, smoke-test the scraper
./enterprise_python_tool.py venv --replace   # give the scraper a .venv made from it (the old one is kept)
./enterprise_python_tool.py check            # smoke-test the scraper's .venv (or --venv DIR / --python EXE)
./enterprise_python_tool.py status | versions | clean
./enterprise_python_tool.py detect | toolchains | verify [PREFIX] | profiles
./enterprise_python_tool.py                  # on a terminal: the wizard

./enterprise_python_tool.py build --for general --cc GCC --binutils DIR    # the toolkit's step 5
```

## General mode (the toolkit's step 5)

The main file runs, with the REAL paths of the `-current` links of steps 4 and 3 (the
toolkit's child contract: a later rebuild of gcc or binutils must not silently change
what this build recorded):

```
enterprise_python_tool.py --local build --for general --yes \
    --opt-root ~/opt \
    --cc "$(realpath ~/opt/gcc-current)/bin/gcc" \
    --binutils "$(realpath ~/opt/binutils-current)"
```

and step 5 is done when `<opt>/cpython-current/bin/python3` runs and `status --json` says
`built_by_tool` and `"for": "general"` for it.

What general mode does differently from scraper mode:

| | scraper mode | general mode |
|---|---|---|
| `--version auto` (the default) | the newest release of the newest series that has had a bugfix release (3.15.0 waits for 3.15.1) | **the newest final release** (his words: "the latest"); `latest`, `X.Y`, `X.Y.Z` as in scraper mode |
| prefix | `<opt>/cpython-<ver>-<scraper dir>` | `<opt>/cpython-<ver>` (the .sh's name) |
| build tree, stage, logs, record | `build-<ver>`, `stage-<ver>`, `logs/<ver>`, `state.json` key `<ver>` | `build-<ver>-general`, `stage-<ver>-general`, `logs/<ver>-general`, key `<ver>-general`: the two modes never share or thrash a tree (one `Python-<ver>` source tree serves both) |
| after verify | the scraper's smoke test | the **general smoke test**: a throwaway venv made from the new interpreter, that venv's OWN `pip --version`, and 18 stdlib imports inside it (json sqlite3 ssl hashlib ctypes decimal lzma bz2 zlib socket select asyncio multiprocessing urllib.request xml.etree.ElementTree csv venv ensurepip). It is part of the install transaction: a failure puts the previous build back (or keeps the new one as `.failed-<date>`) and links nothing |
| `cpython-current` | left alone (unless `--link-current`) | pointed at the prefix once it has verified and passed the smoke test (`--no-link-current` leaves it): a symlink made beside it and renamed over, never over a real directory |
| verify | the checks below | the same, **plus**: the binary's `.comment` must name the compiler that was asked for (`gcc (GCC) 16.2.0` leaves `GCC: (GNU) 16.2.0`; the crt files keep the system gcc's entry too, so "contains", never "only") |
| record (`.python_build_tool.json`, `BUILD-INFO.txt`) | `"for": <scraper path>` | `"for": "general"`; both modes record `cc`, `cxx`, the compiler's banner, `binutils`, and the `as`/`ld` the compiler really runs |
| `venv` | makes the scraper's `.venv` | refused (exit 2): there is no scraper |
| `check` | the scraper smoke test | `verify` + the general smoke test |
| the summary | the next `venv` command | the .sh's: BUILT, `export PATH="<opt>/cpython-current/bin:$PATH"`, and its caution about a PyPy shadowing `python3` |

**The toolchain.** `--cc PATH` is the C compiler (its C++ partner is found beside it:
`gcc` -> `g++`, `gcc-14` -> `g++-14`, `x86_64-linux-gnu-gcc-13` -> `x86_64-linux-gnu-g++-13`,
`clang-18` -> `clang++-18`; or name it with `--cxx PATH`); its directory is taken by its real
path, so `<opt>/gcc-current/bin/gcc` becomes `<opt>/gcc-16.2.0/bin/gcc` and `/usr/bin/gcc`
stays `/usr/bin/gcc` (`--cxx` is taken the same way). CPython records `CXX` in sysconfig and
pip compiles C++ extensions with it, so a `--cc` with NO partner beside it is never paired
quietly with the distro's `g++`: general mode refuses it (preflight, `plan`, `build`, and
`doctor --json` as a required entry) and says to name one with `--cxx` (`--cxx /usr/bin/g++`
takes the system's knowingly); scraper mode warns and uses the system's, as before. Without
`--cc`, the default compiler keeps the system's pair. `--binutils DIR`
is a binutils prefix (`DIR/bin` holds `as` and `ld`), taken by its real path. Its `bin/`
goes FIRST on the build's PATH (configure, make, make install, the compiler and header
probes; `/usr/bin` follows, nothing else from your shell), and its `ar` is named to
configure (`AR=DIR/bin/ar`, recorded in sysconfig). A gcc configured `--with-as/--with-ld`
(the gcc tool pairs gcc with its binutils that way) names its own `as`/`ld`: `plan` and the
preflight show what `gcc -print-prog-name=as/ld` resolves to, and warn when it is not inside
`--binutils`. Without `--cc`, the system gcc builds it, as in scraper mode.

**Parity with build_python_universal.sh's CPython part:**

| the .sh | here |
|---|---|
| `CPY_VERSION=auto`: the FTP listing, newest first, each confirmed by a HEAD | `--version auto` = latest in general mode: python.org's API (finals only), the FTP listing + HEAD as the fallback |
| `OPT_ROOT`, `SRC_ROOT`, `JOBS` | `--opt-root`, `--workdir`, `--jobs` |
| `INSTALL_DEPS=1` (default on) | `--install-deps` (opt-in; `doctor` prints the one command; the main file installs once, up front) |
| `LINK_CURRENT=1`: `ln -sfn $prefix $OPT_ROOT/cpython-current` | the default in general mode, made atomically, only after verify + smoke |
| skip when the prefix exists | an existing prefix of this tool's is VERIFIED instead of rebuilt -- in general mode against the compiler its record names, as `verify` does (when the same `--cc` path answers another version by now, a distro update, that is said, not failed); `--rebuild` replaces it (the old one is kept until the new one proves itself); a prefix the tool did not make is never touched |
| `xz -t` on the tarball | the SHA-256 python.org publishes |
| configure `--prefix --enable-optimizations --with-lto --with-computed-gotos --with-ensurepip=install --enable-loadable-sqlite-extensions` | the same line, `+ --with-system-libmpdec` when libmpdec is there, `+ CC= CXX=` (and `AR=` with `--binutils`); still no `--enable-shared` |
| `make -j | tail -40` (an error swallowed) | a log per step, the FIRST real errors on failure |
| `make altinstall`, then `ln -sfn python3.X $prefix/bin/python3` | `make install` into a staging directory, then one rename (in a private prefix there is nothing to shadow); `make install` makes `bin/python3 -> python3.X` itself, and the tool makes that link if it is ever missing |
| verify_interpreter: CORE `zlib bz2 lzma ctypes sqlite3 hashlib decimal select socket ssl`, NICE `curses readline dbm.gnu uuid`, ssl context with a crash (exit > 128) told apart from a failure | the same lists by the .sh's names (CORE missing fails, NICE missing is a note), on top of the tool's own 34 required modules; the same ssl context check; plus pip, HTTPS when online, no RPATH, `ldd`, the compiler in `.comment` |
| DNF_PKGS/APT_PKGS incl. `tk-devel`/`tk-dev` | the DEPS table (below) with dnf, apt-get, zypper and pacman columns; tk is an optional row. `expat-devel` (CPython uses its bundled expat) and `gmp-devel` (PyPy's) are not needed here |
| the BUILT / FAILED summary and the PATH caution | general mode's summary |

## Commands

| command | what |
|---|---|
| `build` | Download, configure, make, install, verify, smoke-test, link (general mode). A finished step is not redone; an existing prefix is re-verified. `--dry-run` is `plan`. |
| `plan` | Resolves the version, paths, toolchain and configure line, runs the preflight. Builds nothing. |
| `doctor [--json]` | What is missing, and the one install command. `--install-deps` runs it (enabling CRB first on Enterprise Linux when mpdecimal-devel is wanted). |
| `status [--json]` | Every `<opt>/cpython-*` prefix (both modes, and ones this tool did not build), `cpython-current`, leftovers (`.old-`/`.failed-`/`.partial-`), and the builds recorded in the work directory with their status. Scraper mode's text is as it was; `--for general` shows the general view. |
| `versions [--count N]` | CPython releases on python.org, and what `auto` picks in each mode. |
| `detect [--json]` | This machine: OS, CPUs, RAM, package manager, the `-current` links, every compiler, binutils set and python found, build tools, headers. |
| `toolchains [--cc PATH] [--cxx PATH] [--binutils DIR] [--no-check]` | Every gcc/clang (test-compiled unless `--no-check`; "no C++ partner beside it" when there is none), binutils set and python found, `<opt>/*-current` first and shown with their real paths; which would be used (with its C++ compiler), the `as`/`ld` that compiler runs, and the exact `--cc/--binutils` the toolkit's step 5 would pass. `--json` too. |
| `verify [PREFIX] [--json]` | The post-install checks against a prefix (default `<opt>/cpython-current`); a general build of this tool's must also carry its compiler in `.comment`. The result is recorded in the prefix's marker. |
| `profiles` | Lists the saved profiles. |
| `wizard` | Asks the questions, then builds (see below). |
| `venv`, `check` | Scraper mode (below). `check --for general [--python NAME|PATH]` = verify + the general smoke test of that interpreter's prefix (default `cpython-current`): a bare name is looked up on your PATH, never in the current directory, and the binary's real path must be its prefix's `bin/python3`. |
| `clean [--all]` | Source/build trees and throwaway copies (below). |

Every subcommand takes `--for scraper|general`, `--opt-root DIR` (default `~/opt`),
`--workdir DIR`, `--scraper DIR`, `--profile NAME` and `--yes` (ask nothing). `build`,
`plan`, `doctor` and `wizard` take every build option: `--version`, `--prefix`, `--cc`,
`--cxx`, `--binutils`, `--jobs`, `--no-optimize`, `--link-current`/`--no-link-current` (aliases
`--activate`/`--no-activate`), `--install-deps`, `--offline`, `--constraints`, `--verbose`,
`--save-profile NAME`; `build` and `wizard` also `--rebuild`, `--no-check`, `--keep`,
`--dry-run`.

Exit codes: `0` done, `1` failed, `2` usage, `130` interrupted (^C, SIGTERM, SIGHUP).

With `--json` (`doctor`, `status`, `detect`, `toolchains`, `verify`), stdout is the JSON and
nothing else. Every message goes to stderr, an `ERROR:` included.

### Doctor contract

`doctor --json` prints exactly (PLAN.md section 3):

    {"tool": "enterprise_python_tool", "ok": bool, "package_manager": "dnf|apt-get|zypper|pacman"|null,
     "missing": [{"what": ..., "package": ..., "level": "required|wanted|optional"}],
     "enable_repos": ["crb" | "powertools" | "codeready-builder-for-rhel-N-ARCH-rpms"] | [],
     "install_command": "sudo dnf install -y ..."}

It makes no network call and creates nothing (the main file asks every tool this up front,
before steps 3 and 4 have run). Presence is probed: the compiler builds and runs a
hello-world, each header is preprocessed (or asked of pkg-config), each tool is on the
system PATH; rpm and dpkg are not asked. `ok` is false when anything required is missing,
the compiler does not work, a `--cc`/`--cxx`/`--binutils` given is not there, a general-mode
`--cc` has no C++ partner beside it and no `--cxx`, or this runs as root.
`install_command` covers the required and wanted packages. `enable_repos` names CodeReady
Builder on Enterprise Linux (not Fedora) when `mpdecimal-devel` is wanted: `crb` on
AlmaLinux/Rocky/CentOS 9-10, `powertools` on EL 8, the subscription-manager channel on RHEL.
The package manager is the one that fits `/etc/os-release` first (yum counts as dnf). The
scraper's lxml "fallback" rows are `optional` in scraper mode and left out in general mode.

| key | probe | level | dnf | apt-get | zypper | pacman |
|---|---|---|---|---|---|---|
| a C compiler | gcc runs a hello-world | required | gcc | build-essential | gcc | gcc |
| make, tar, xz | on PATH | required | make tar xz | make tar xz-utils | make tar xz | make tar xz |
| pkg-config, readelf, ldd | on PATH | wanted | pkgconfig binutils glibc-common | pkg-config binutils libc-bin | pkg-config binutils glibc | pkgconf binutils glibc |
| openssl | openssl/ssl.h | required | openssl-devel | libssl-dev | libopenssl-devel | openssl |
| sqlite | sqlite3.h | required | sqlite-devel | libsqlite3-dev | sqlite3-devel | sqlite |
| zlib | zlib.h | required | zlib-devel | zlib1g-dev | zlib-devel | zlib |
| libffi | ffi.h | required | libffi-devel | libffi-dev | libffi-devel | libffi |
| bzip2 | bzlib.h | required | bzip2-devel | libbz2-dev | libbz2-devel | bzip2 |
| xz | lzma.h | required | xz-devel | liblzma-dev | xz-devel | xz |
| mpdecimal | mpdecimal.h | wanted (CRB on EL) | mpdecimal-devel | libmpdec-dev | mpdecimal-devel | mpdecimal |
| zstd | zstd.h | wanted | libzstd-devel | libzstd-dev | libzstd-devel | zstd |
| libuuid | uuid/uuid.h | optional | libuuid-devel | uuid-dev | libuuid-devel | util-linux-libs |
| readline | readline/readline.h | optional | readline-devel | libreadline-dev | readline-devel | readline |
| ncurses | ncurses.h / curses.h | optional | ncurses-devel | libncurses-dev | ncurses-devel | ncurses |
| gdbm | gdbm.h | optional | gdbm-devel | libgdbm-dev | gdbm-devel | gdbm |
| tk | pkg-config tk / tk.h | optional | tk-devel | tk-dev | tk-devel | tk |
| libxml2, libxslt | pkg-config | scraper only (lxml without a wheel) | libxml2-devel libxslt-devel | libxml2-dev libxslt1-dev | libxml2-devel libxslt-devel | libxml2 libxslt |

The zypper and pacman names are from those distributions' package lists; no zypper or
pacman machine has run the tool yet.

### Status contract

`status --json` prints the toolkit's status shape at the top level:

    {"tool": "enterprise_python_tool", "opt_root": DIR,
     "current": {"cpython-current": TARGET|null},
     "installed": [{"name": "cpython", "version", "prefix", "runs": bool, "built_by_tool": bool,
                    "compiler": STR|null, "for": "general"|<scraper path>|null, ...}],
     "builds": [{"id", "status", "stage", "error", ...}],
     "workdir": DIR, "link": {...}, "leftovers": [...], "checks": [...], "mode": "scraper|general"}

- `current`: the realpath of `<opt>/cpython-current` (a dangling link still names its
  target), `null` when there is none.
- `installed`: every `<opt>/cpython-*` directory with a `bin/python3` (not `cpython-current`,
  not `.old-`/`.failed-`/`.partial-`). `runs`: `bin/python3 -I` answered its version.
  `built_by_tool`: the prefix's `.python_build_tool.json` is this tool's; the main file's
  third state, "present but not built by the toolkit", is `runs` and not `built_by_tool`.
  `compiler`: what the record says built it ("PATH (banner)"); `for`: `"general"` or the
  scraper's path (null when not this tool's). Extra keys: `verified`, `optimized`, `built`,
  `binutils`, `compiler_proof` (the `.comment` entry verify matched), `current`.
- `builds`: one per record in `<workdir>/state.json`; `id` is its key (`3.15.0` for the
  scraper, `3.15.0-general`). `status` is `done`, `running`, `killed` (recorded running while
  nobody holds the work directory's lock), `failed`, `interrupted`, `incomplete` (a record
  from before the status was kept) or `new`; `stage` is preflight, source, configure, make,
  install, verify, smoke or link; `error` the first line of what stopped it. Extra keys:
  `version`, `for`, `prefix`, `started`.
- `link` (`cpython-current` in detail), `leftovers`, the last smoke tests (`checks`), `mode`
  and `workdir` are extra keys.

### Local or remote

Every invocation first goes through `where_to_run()` (satellite_remote, embedded between
the `# >>> satellite_remote >>>` markers by `sync_remote.py`; never edit that block here).
`--local` runs the tool here; `--remote[=USER@HOST]` uploads the tool file to
`~/.satellite_toolkit/code/` there and runs it with `--local` inside a session that survives
a dropped ssh connection (output streams here, `Ctrl-]` detaches, the build keeps going).
`--attach[=SESSION]` returns to such a session and `--sessions` lists them. On a terminal
with neither flag the tool asks "run where?"; only `-h`, `--help` and `--tool-version` skip
that question (`--version` is the CPython version here, and still asks).
`SATELLITE_TOOLKIT_WHERE=local` means `--local`. A remote run exits with the program's own
code once it ended there; while it still runs there, `75` (detached with Ctrl-]) or `255`
(the connection dropped; `--attach` goes back). Only the tool file goes: profiles stay on
the machine that saved them, and paths (`--opt-root ~/opt`) are expanded where it RUNS. The
session short name is `python`.

### Profiles and the wizard

Profiles live in `~/.config/enterprise_python_tool/profiles/<name>.ini` (`$XDG_CONFIG_HOME`
respected), a `[build]` section under the line `# enterprise_python_tool profile`. They hold
the choices -- `for`, version, scraper, workdir, opt_root, prefix, cc, cxx, binutils, jobs,
constraints, `optimize`, `link_current` -- and never `--rebuild`, `--yes`, `--install-deps`
or `--offline`. Paths are stored absolute. Precedence: the defaults, then `--profile NAME`,
then the command line. `--save-profile NAME` (on `build`/`plan`) saves the merged choices.

With no command on a terminal the wizard starts. It offers the saved profiles first (use one
as it is, or edit it), then asks: what the CPython is for; the version; the opt root; the
compiler (every gcc/clang found, numbered, `s` = the system's, or a path -- in general mode
the default is `<opt>/gcc-current` by its real path when it runs); the C++ compiler, only
when none is beside the chosen one or a `--cxx` was given (`a` = the one beside it, or a
path); the binutils (every set
found, `n` = the system's, or a directory -- in general mode `<opt>/binutils-current`); PGO +
LTO; cpython-current; and a profile name to save the answers under. Then it builds. Every
question is also a flag. Without a terminal the wizard exits 2; `wizard --yes` asks nothing
and builds what the flags and profile say.

## Running the scraper with it

```
./enterprise_python_tool.py build            # ~10 min here; the scraper's .venv is not touched
./enterprise_python_tool.py venv --replace   # .venv -> .venv.replaced-<date>, new .venv, smoke test
cd /run/media/madness/purple10/claude_scraper && .venv/bin/python main.py
```

Start the crawler with **`.venv/bin/python`** (or `source .venv/bin/activate` first).
A bare `python3.12 main.py` is the *system* Python. On 2026-10-03 that is how the
crawler was running: no venv at all, with lxml and trafilatura from
`~/.local/lib/python3.12` and psutil and requests from the RPMs. A bare `python3`
on this machine is PyPy.

`venv --replace` follows four rules:

- It refuses to run while the crawler is running, however it was started:
  out of `.venv`, with `VIRTUAL_ENV` pointing at it, or as any python working
  in the scraper directory. It prints the `kill -INT <pid>` that stops it
  cleanly; the crawler resumes from its queue.
- It keeps the old venv as `.venv.replaced-<date>`. Delete that once you are happy.
  It shows up in `git status` until you do.
- If pip or the smoke test fails, it **puts the old `.venv` back**. The new one
  is kept as `.venv.failed-<date>` so you can look at it.
- It writes the exact package versions it installed to `.venv/requirements.lock.txt`.

Every command in the scraper's README keeps working, because it is still
`.venv/bin/python` and `.venv/bin/pip`.

## Is `build_python_universal.sh` enough to run the scraper?

**No.** Its RPM list is fine, but four other things stop it.

1. **On this machine it does not build CPython at all.** It passes your shell's
   environment to configure. That means `CC`/`CXX` = clang trunk from `~/.bashrc`,
   `gcc` on PATH = GCC 17 trunk, `LDFLAGS=-fuse-ld=lld`, and a long `LD_RUN_PATH`.
   `python_log.txt` (2026-08-29) shows the result: configure picked clang, the first
   link died on `LLVMgold.so: cannot open shared object file`, and the script ended
   with `no python3.x in ~/opt/cpython-3.14.7/bin`. It also carried on into
   `make altinstall` after `make` had failed: the error went through `| tail -40`
   inside a function that runs under `|| true`, where `set -e` does not apply.
   Your current `~/opt/cpython-3.14.7` was built **by hand**, not by the `.sh`. Its
   configure arguments show clang 24 + lld, `-march=native` and `--enable-shared`,
   none of which the `.sh` passes.
2. **It stops at the interpreter.** It makes no venv and runs no
   `pip install -r requirements.txt`. The scraper's current `.venv` was made with
   `--system-site-packages` from `/usr/bin/python3`. `requests`, `urllib3`, `idna`,
   `psutil`, `python-dateutil` and `six` come from AlmaLinux RPMs, not from the venv.
   `check` against that venv shows it: `package-origin: psutil from
   /usr/lib64/python3.12/site-packages`. A Python you build sees none of them, so
   all of them must be pip-installed.
3. **Its verification is about Python, not the scraper.** It never imports lxml,
   trafilatura, requests or psutil. It never runs the scraper's code, SQLite
   schema or worker processes. It never reads CPython's own "could not build
   these modules" report.
4. **`auto` means "newest".** CPython 3.15.0 is at rc3 today. The day it ships,
   the `.sh` builds a `.0`. This tool waits for 3.15.1 unless you ask for
   `--version latest`.

**RPMs:** on this machine nothing is missing. All of the `.sh`'s list is installed,
plus `mpdecimal-devel` and `libzstd-devel`. Those two are *not* in the `.sh`'s
list, and they give CPython 3.14 the system libmpdec and `compression.zstd`.
`libxslt-devel` (with `libxml2-devel`, already installed) is needed **only** if pip
ever has to compile lxml. Today lxml publishes wheels for 3.14 and 3.15, so it
does not. `doctor` checks all of this and prints the exact `dnf install` line.

## What changes for the scraper on Python 3.14

- **Workers start by `forkserver`, not `fork`.** Python 3.14 changed the Linux
  default. `main.py`'s comment ("Worker processes are forked") assumes fork. The
  workers still work, because `WorkerPool` passes only a module-level function,
  `Path`s and a `multiprocessing.Queue`. The smoke test starts the scraper's
  real `WorkerPool` and a real `Worker` to prove it. One side effect: workers no
  longer share `main.py`'s command line, so `pkill -f '.venv/bin/python main.py'`
  reaches only the main process. That is fine, because the main process stops
  its workers itself.
- **Package versions move up**, because the RPMs are no longer in play. requests
  2.32 → 2.34, urllib3 1.26 → 2.8, psutil 5.9 → 7.2, trafilatura 2.2 → 2.3,
  htmldate 1.10 → 1.11. The scraper uses only stable APIs of these:
  `requests.Session`, `psutil.virtual_memory`/`cpu_percent`, `trafilatura.extract`.
  Extracted text can differ by a few characters from page to page; one sample
  page gave 5591 chars before and 5601 after.
- **`zstd`** is now in requests' `Accept-Encoding` (3.14's `compression.zstd`
  + urllib3 2), so servers may send zstd-compressed pages. They are decoded
  transparently.

**To keep today's package versions exactly**, pin them with a constraints file
taken from the Python the crawler runs on now:

```
python3.12 -m pip freeze > ~/scraper-versions-2026-10-03.txt
./enterprise_python_tool.py venv --replace --constraints ~/scraper-versions-2026-10-03.txt
```

Tested: on 3.14.8 that gives requests 2.32.4, urllib3 1.26.19, psutil 5.9.8,
trafilatura 2.2.0, htmldate 1.10.0, idna 3.7 and regex 2026.9.10, and the smoke
test passes. Packages in the file that the scraper does not need are ignored.

## What `build` does

| step | what | why |
|---|---|---|
| preflight | system gcc works; every header CPython's modules need; tools; disk | a missing `-devel` package should cost 2 seconds, not 20 minutes |
| version | python.org's API: CPython 3 finals only | the FTP listing has directories for unreleased betas |
| download | `Python-X.Y.Z.tar.xz`, checked against python.org's **SHA-256** | |
| configure | `--enable-optimizations --with-lto --with-computed-gotos --with-ensurepip=install --enable-loadable-sqlite-extensions --with-system-libmpdec CC=/usr/bin/gcc` | the `.sh`'s flags, plus the system libmpdec. Still no `--enable-shared` |
| make | PGO: instrumented build → training run (CPython's test suite) → optimised rebuild + LTO | ~10-20% faster Python. 8m39s here (24 CPUs) |
| module report | CPython's own "not built / failed on import" list | if `_ssl`, `_sqlite3` and the like are missing, the build stops here, before install |
| install | `make install DESTDIR=stage`, then **one rename** into the prefix | a prefix with the final name is always whole |
| verify | modules import; ssl builds a context (a crash is reported as one); pip runs; an HTTPS fetch; no RPATH; `.comment` names the compiler; `ldd` finds every library | a Python that "built" without ssl is worse than a failed build |
| smoke test | throwaway venv + `pip install -r requirements.txt` + the scraper check below | "done" means the scraper runs |

The build runs in a **scrubbed environment**. It keeps HOME, the proxy and CA
variables, PATH=`/usr/bin:/usr/sbin:/bin:/sbin`, and LC_ALL=C. Nothing else from
your shell gets through: no CC/CXX/LDFLAGS/LLVM, no LD_RUN_PATH/LD_LIBRARY_PATH,
no CONFIG_SITE/MAKEFLAGS, and no `/usr/local/bin`. That last one holds a
root-installed python3.14 and LLVM tools that configure goes looking for. pip runs
in the same scrubbed environment, plus your `PIP_*` mirror and cert variables. So
if anything ever has to be compiled, the system gcc compiles it.

A finished build is never redone. Run `build` again and it **verifies** the
existing prefix instead. It refuses a prefix that holds another version, other
options, or a half-finished install, and tells you to use `--rebuild`.

`--rebuild` replaces the tool's **own** build. The old one is set aside before
`make install`, so that ensurepip does not see its pip and leave the new prefix
without one. It is kept until the new build has verified **and** passed the
smoke test, and is put back if either fails, or if the run is stopped. A build
that fails verification is kept as `<prefix>.failed-<date>` for a look. A
directory the tool did not make is **never moved or deleted**, not even with
`--rebuild`: the tool refuses before it builds anything.

## The smoke test (`check`)

It runs in a **throwaway copy of the scraper's code**, which is just its `.py`
files, with the interpreter under test. It runs in your own environment, because
that is the one the crawler will run in. The scraper's directory is only *read*:
three saved pages from `html/`, and its free disk space. `crawler_data.db` is
never opened, and no file there is written. One HTTPS request goes to
www.python.org.

| check | proves |
|---|---|
| stdlib | the 26 standard modules the scraper imports, and `_ssl _hashlib _sqlite3 zlib _bz2 _lzma _json _multiprocessing _socket select pyexpat` |
| packages | lxml, psutil, requests, trafilatura import. It says so if one comes from **outside** the environment |
| scraper-modules | all 64 modules reachable from `main.py`, `extract_existing.py`, `fetch_repos.py` and `reseed.py` import. The list is found by reading the code's imports, not by hand |
| ssl / https | a TLS context; `requests.get("https://www.python.org/")` |
| text-extraction | the scraper's `html_to_text` turns a known page into Markdown that keeps its code block. `extract_document` swallows every exception, so "no exception" would prove nothing |
| saved-pages | three of your real `html/*.html.gz` pages are re-extracted |
| database | a **new** database gets the scraper's schema; two URLs are queued and one popped; WAL mode |
| psutil | the scraper's `ResourceMonitor` runs |
| worker-pool | the scraper's own `WorkerPool` starts 2 workers with the interpreter's default start method, they stay up, and its `shutdown()` stops them |
| worker | a real `Worker` is built in a separately started process and reads its (empty) queue |
| git | `fetch_repos.py` has its `git` |

## Where things go

| path | what |
|---|---|
| `<opt>/cpython-<version>-claude_scraper/` | scraper mode's interpreter, with its pip, `BUILD-INFO.txt` and `.python_build_tool.json` |
| `<opt>/cpython-<version>/`, `<opt>/cpython-current` | general mode's interpreter, and the link to the newest verified one |
| `~/src/python_build_tool/` | downloads, source and build trees (`build-<ver>`, `build-<ver>-general`), `logs/<version>[-general]/{configure,make,install}.log`, smoke-test logs, `state.json` (one record per mode and version) |
| `<scraper>/.venv/` | after `venv`: made from the prefix, plus `requirements.lock.txt` and `.python_build_tool-venv.json` |
| `~/.config/enterprise_python_tool/profiles/` | saved profiles |

`<opt>` is `--opt-root`, default `~/opt`.

`clean` removes source/build trees and throwaway copies (`--all`: downloads, logs
and the record too). It never touches an installed prefix or a venv. It works
only in a directory that is demonstrably its own: the default one, or one with
its `state.json`. Even there it deletes only names it gives things, such as
`Python-3.*`, `build-3.*` and `check-xxxxxxxx`. It refuses the scraper's
directory, your home directory, and anything that contains either.

Options worth knowing: `--version auto|latest|X.Y|X.Y.Z`, `--prefix`, `--cc`, `--cxx`,
`--jobs`, `--no-optimize` (about one minute instead of ten, for trying the tool
out), `--constraints FILE`, `--install-deps`, `--offline`, `--verbose`,
`--scraper DIR` (default `/run/media/madness/purple10/claude_scraper`),
`--workdir`, and from the toolkit: `--for`, `--opt-root`, `--binutils`,
`--link-current`, `--profile`/`--save-profile`, `--yes`.

## What has been tested (2026-10-03, siege3, AlmaLinux 10.2)

- `build` with the defaults: CPython 3.14.8, PGO + LTO, `make` 8m39s. 114 modules
  were checked, with 0 missing and 0 failed on import. Installed at
  `~/opt/cpython-3.14.8-claude_scraper` (534 MB). Verification passed: no
  RPATH, `.comment` names only `GCC 14.3.1 (Red Hat)`, every library found,
  and pip 26.2.1. The smoke test passed in a throwaway venv.
- `build` again: it verified the existing prefix instead of rebuilding (18 s).
- `build --no-optimize` into a scratch prefix: about one minute, with every step
  passing. In the same scratch workdir: `build` again (reuse), then
  `--rebuild` (the old build set aside, the new one verified and smoke-tested,
  then the old one removed). `--rebuild` with the scraper missing kept the old
  build and said so. A foreign `--prefix` with `--rebuild` was refused before
  anything was built, and its files were untouched. A prefix holding 3.14.8 was
  refused for `--version 3.13`.
- `check` against the scraper's current `.venv` (system Python 3.12): every
  check passed, and it flagged psutil and requests as coming from the system
  site-packages.
- `venv` without `--replace` against the real `.venv`: refused, and the venv
  kept the same inode and mtime.
- `venv --venv DIR`, fresh and again (the "sync" path), and with
  `--constraints`: passed.
- `venv --replace` on two **copies** of the scraper. One succeeded, with the old
  venv kept beside the new one. In the other, a module was broken on purpose:
  the smoke test caught it and the **old venv was put back**.
- The busy check against the real crawler (63 `python3.12 main.py` processes):
  it found them, and named the main pid for `kill -INT`.

**Not done:** `venv --replace` on the real scraper. That is your call, and the
crawler was running. The crawler has not been run for real on 3.14: the smoke
test exercises its code paths without crawling. `--install-deps` has not been
run, because sudo needs your password.

## What has been tested (2026-10-09, the cloud container: Ubuntu 24.04, 4 CPUs)

As an ordinary user (`satuser`, scratch HOME, `--opt-root`/`--workdir` in scratch;
python.org is blocked there, so the CPython 3.13.7 tarball was taken from the Ubuntu
archive's `python3.13_3.13.7.orig.tar.xz` -- upstream's, byte for byte -- and put in
`<workdir>/downloads/`):

- `--help`, `doctor --json --for general` (pure JSON: `ok` true, libmpdec-dev and
  libzstd-dev wanted, tk-dev optional, `sudo apt-get install -y libmpdec-dev libzstd-dev`),
  `status --json` before and after, `plan --for general` with a stand-in toolchain
  (`<opt>/gcc-current` and `<opt>/binutils-current`, links to directories holding the
  system's gcc 13 and binutils 2.42 under their own paths).
- `build --for general --version 3.13.7 --cc "$(realpath <opt>/gcc-current)/bin/gcc"
  --binutils "$(realpath <opt>/binutils-current)" --no-optimize`: configure (with
  `AR=<binutils>/bin/ar`), make 1m23s, 112 modules (only `_tkinter` missing), staged
  install + one rename into `<opt>/cpython-3.13.7`, verify (the .sh's CORE, ssl context,
  pip, HTTPS to pypi.org, no RPATH, `.comment` naming the gcc asked for, ldd), the general
  smoke test, `cpython-current` linked; 2m23s in all. sysconfig records the `CC` and `AR`
  given. `build` again verified instead of rebuilding (4 s); `--rebuild` set the old prefix
  aside, reused the build tree, verified, smoke-tested, removed the old one, relinked.
- `verify` (text and `--json`), `toolchains`, `detect --json`, `status --for general`;
  `build --for general` with no `--version` stops with "Pass --version X.Y.Z" when
  python.org cannot be reached; `wizard` without a terminal exits 2.

Not tested here: a PGO + LTO build in general mode (the scraper-mode one was, on
2026-10-03), a real step-4 gcc (the gcc tool's output) and step-3 binutils, `--install-deps`,
zypper and pacman machines, and the remote path (satellite_remote has its own tests).

## Tests

```
/usr/bin/python3 -I tests/test_tool.py
```

114 stdlib `unittest` tests. No network, no root needed (they pass as root and as an
ordinary user; what refuses root is told it is uid 1000), no CPython tree: fake gcc,
binutils and python3 are shell scripts in a temp directory, with `XDG_CONFIG_HOME`
pointed there and `SATELLITE_TOOLKIT_WHERE=local`. One test makes a real venv from the
python running the tests (skipped where that python cannot). They cover the
version policy, the python.org API parser, the module report parser, the
environment scrub, the import scan and venv detection. They also cover:

- the busy-process check, against a fake `/proc`;
- checksummed downloads, and re-unpacking when the tarball changes;
- `make install` played by a fake Makefile: a foreign prefix is refused and an
  install without pip is refused;
- `cmd_build`'s rollback (verify fails, or the smoke test fails, so the
  previous build comes back);
- `clean`'s guards;
- the smoke test's process-group cleanup;
- console output to a dead terminal;
- the file itself: Python 3.6 syntax, pure ASCII, the `-I` shebang and the PyPy re-exec,
  the satellite_remote block equal to what `sync_remote.py` writes, `where_to_run` the
  first statement of `main()`;
- general mode: the prefix and the per-mode build tree / stage / record names, `auto` =
  latest, `--cc`/`--binutils` by real path (`AR=`, the binutils' `bin/` first on PATH,
  what `-print-prog-name` resolves to, a gcc pinned `--with-ld`), the `.comment` each
  compiler leaves and verify failing when the asked-for one is missing, the .sh's CORE/NICE
  lists, the `bin/python3` link, a new tarball forgetting both modes' builds; the C++
  partner by name beside `--cc`, `--cxx` by real path, a `--cc` without one refused in
  general mode (a required doctor entry), warned about in scraper mode, never silently the
  distro's `g++`;
- `cmd_build` in general mode: verified with its compiler, smoke-tested, linked; a failed
  smoke test or verify rolls back and links nothing; `--no-link-current`; a real directory
  named `cpython-current` left alone; the record's status / stage / error; a reused prefix
  proved against the compiler its record names (not what the same path answers now);
- the general smoke test (a real venv, and a pip that is not the venv's own);
- `cpython-current`: atomic link and relink;
- the doctor contract: the exact keys, CRB/PowerTools/RHEL repository ids, dnf / apt-get /
  zypper / pacman / yum package names and commands, root, no compiler, a broken one, the
  package manager that fits `/etc/os-release`; `doctor --json` through `main()` is pure
  JSON on stdout, creates nothing, answers as root, reports a missing `--cc` or `--cxx`;
- with `--json` an error goes to stderr and stdout stays empty;
- the status contract on a fake `<opt>`: ours, the scraper's, a hand-built one (the third
  state), one that does not run, leftovers, a killed / failed / done / legacy record;
- profiles (round trip, what is never stored, defaults < profile < command line), the
  wizard offering the toolkit's gcc/binutils pair and asking for the C++ compiler when
  none is beside the chosen one, `--cxx` on every build command and in a profile,
  `check --for general --python NAME` found on PATH (never in the current directory),
  every command taking `--opt-root`,
  `--yes` and `--for`, exit codes (usage 2, root 1, ^C 130, `venv --for general` 2),
  `build --dry-run` building nothing, `verify --json`, and detection (`<opt>/*-current`
  first, shown by both paths; binutils prefixes; pythons this tool built).

Two adversarial review passes ran over the code: four lenses, each with a
refuting verifier, then a second pass over the fixes.

- The first pass confirmed 29 defects. One was high-severity: `--rebuild` could
  delete a directory the tool had not made.
- The second pass confirmed 13 in the fixes, 12 of them distinct. Among them: a
  venv's marker file could pass for a build's, and `--rebuild` could change the
  Python series under a venv.
- A real `--rebuild` run found one more that both passes missed. ensurepip saw
  the old prefix's pip and skipped installing it.

All are fixed, and the transaction paths have tests.
