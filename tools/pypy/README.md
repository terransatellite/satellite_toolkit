# enterprise_pypy_tool.py

Builds **PyPy from source**: pypy2 (step 6 of the satellite toolkit) and pypy3 (step 7), and the
Python 2 that translates them. It is the successor of the PyPy half of
`legacy/build_python_universal.sh` (the CPython half went to `tools/python`).

One file. Standard library only. Runs under `/usr/bin/python3`, syntax held to 3.6; started by a
`python3` that is PyPy, it re-runs itself under `/usr/bin/python3`. Never run it as root: only
`--install-deps` uses `sudo`.

```
./enterprise_pypy_tool.py doctor                       # what is missing, and the one command that installs it
./enterprise_pypy_tool.py plan --target pypy2           # release, host, compiler, rpython line, stages -- touches nothing
./enterprise_pypy_tool.py build --target pypy2          # step 6: <opt>/pypy2.7-v<rel>, then <opt>/pypy2-current
./enterprise_pypy_tool.py build --target pypy3          # step 7: <opt>/pypy3.N-v<rel>, translated by pypy2-current
./enterprise_pypy_tool.py build --target host           # only the translation host (CPython 2.7.18)
./enterprise_pypy_tool.py smoke [--smoke-jit]           # a tiny RPython translation (25 s; JIT ~7 min): host + compiler work
./enterprise_pypy_tool.py verify [PREFIX]               # modules, ssl context, JIT, pip, libraries
./enterprise_pypy_tool.py status [--json] | versions | detect [--json] | toolchains | profiles | clean [--all]
./enterprise_pypy_tool.py                               # on a terminal: the wizard (never with --yes)
```

The main file runs step 6 as `--local build --target pypy2 --opt-root <opt> --yes` and step 7 the
same with `--target pypy3`. Done is asked of the machine: `<opt>/pypy2-current/bin/pypy` (or
`pypy3-current/bin/pypy3`) runs `import ssl; ssl.create_default_context()` and exits 0. Step 6
depends on steps 3 and 4 (binutils, gcc), not on step 5: CPython 3 cannot run RPython.

## How a build goes

PyPy is not compiled, it is **translated**: RPython -- a Python 2 program -- reads PyPy's source
and writes C, which gcc then builds. Each stage is recorded in `<workdir>/state.json`; a failure
resumes at the stage that failed (`build` again), a finished stage is skipped while its product is
still there. A stage that runs again makes every stage after it run again (a new stage is verified
again; a new `pypy-c` gets its cffi table again) -- except `smoke`, which never undoes a
translation. The record is per release; built into another `--opt-root` / `--prefix`, packaging,
install and verification are done again for it (the translation and the cffi modules are kept).

| stage | what | product |
|---|---|---|
| fetch | the source tarball: downloads.python.org, else GitHub's archive of the tag, else `git clone --depth 1 --branch release-pypyX-vR` | `<workdir>/downloads/` |
| host | the Python 2 that runs RPython (below) | `<workdir>/host/...` |
| extract | a fresh source tree (translation writes into it) | `<workdir>/src/<stem>-src` |
| smoke | (`--smoke`) a tiny translation first | `<workdir>/smoke` |
| translate | `rpython --batch --opt=jit --cc=<gcc> --make-jobs=N targetpypystandalone.py` | `pypy/goal/pypy-c` + `libpypy-c.so` (`pypy3.N-c`) |
| setuptools | (pypy3.12+) ensurepip + the pinned setuptools wheel, offline | in the translated interpreter |
| cffi | `build_cffi_imports.py`, then every cffi module imported, in a table | `lib_pypy/*.so` |
| package | `package.py`, output kept in `logs/<stem>/package.log`, steered by env vars | `<workdir>/package/<stem>/<stem>` = the stage |
| verify-stage | the staged interpreter verified before anything is installed | |
| install | the stage renamed into the prefix in ONE rename (EXDEV: copy, then rename) | `<opt>/<stem>` |
| verify | the installed prefix verified; the marker says `verified` | `.pypy_build_tool.json`, `BUILD-INFO.txt` |
| link | `<opt>/pypyN-current` -> the prefix, relative, made beside and renamed over | |

`<stem>` is upstream's own: `pypy2.7-v8.0.0`, `pypy3.12-v8.0.0` -- the names the `.sh` used and
`build_everything.sh` globs. A prefix is never installed over: an existing one is replaced only
with `--rebuild`, only if this tool made it, never while a process runs from it; it is set aside
as `<prefix>.old-<stamp>` and put back if the new build fails verification. After a verified
install the 4 GB usession tree and the package directory are removed; after a failure they are
kept (`--from compile`, below).

Work directory (`--workdir`, default `~/src/enterprise_pypy_tool`): `downloads/`, `cache/` (the
release list, checksums, release notes -- for `--offline`), `src/`, `host/`, `usession/<stem>/`
(`PYPY_USESSION_DIR`), `package/<stem>/`, `smoke/`, `tmp/` (`TMPDIR`), `logs/<stem>/<stage>.log`
(the previous one kept as `.prev`), `state.json`, `.lock` (one instance per work directory: two
translations at once would fight over the memory).

## The translation host (`--host`)

RPython is Python 2 code. A CPython 3 cannot run it -- the `.sh`'s `HOST_MODE=cpython` ran
`command -v python3` and so could never work. Here the working meaning keeps a clear name:

| `--host` | what | translation time |
|---|---|---|
| `auto` (default) | `cpython27` for pypy2; for pypy3 the **real path** of `<opt>/pypy2-current/bin/pypy` when it runs (pypy3 translated by step 6's pypy2), else the same as pypy2. With `--target both`: the pypy2 this same run just built and verified (its prefix, by real path -- not pypy2-current, so `--no-link-current` and an older pypy2-current change nothing); if that pypy2 FAILED, pypy3 is not started (say `--target pypy3 --host cpython27\|prebuilt\|PATH` to translate it anyway) | |
| `cpython27` | CPython 2.7.18 built in `<workdir>/host/cpython-2.7.18/` (not in `<opt>`: it is a tool of this tool) with `--cc`/`--binutils`, `-O2 -std=gnu17` (+ `-fpermissive` on GCC 14+), `--enable-unicode=ucs4 --with-system-ffi --with-ensurepip=install`, no PGO/LTO/shared; installed through a stage + one rename; then pycparser 2.21 + cffi 1.15.1 (pinned sha256, the sdist built with the same compiler; cffi's cp27mu wheel only if that fails). Reused while the compiler is the same | 3-6 min once, then translation 2-3x a PyPy host (1-3 h for pypy2) |
| `prebuilt` | the official portable PyPy2 for this architecture (the `.sh`'s default). 8.x binaries need glibc 2.28+; older glibc gets the newest 7.3.x | 20-60 min for pypy2 |
| `system` | a `pypy2`, `pypy`, `pypy2.7`, `python2.7` or `python2` on PATH or in `~/opt/*/bin`, and the `.sh`'s `~/opt/pypy2/pypy2/bin/pypy`. EL10, Debian 12 and Ubuntu 24.04 ship no Python 2 | |
| `built` | `<opt>/pypy2-current/bin/pypy`, insisted on | |
| `PATH` | the interpreter you name | |
| `cpython` | accepted, means `cpython27` (with a note) | |

Every host is asked, by running it, whether it is a Python 2 with pycparser (or PyPy's
`cffi._pycparser`) and with `_curses` or `_minimal_curses` -- every translation imports one of them
on the host (`pypy/module/_minimal_curses`, in every release's working modules), else it stops at
once with `Skipped: no _curses or _minimal_curses module`; a CPython 3 or a PyPy3 is refused with
the reason. The CPython 2.7 host is checked for `_curses` too, so one built before the ncurses
headers were installed is built again. `--low-memory` runs a PyPy
host with `PYPY_GC_MAX_DELTA=200MB --jit loop_longevity=300`. pypy3 takes about 1.3-1.5 times as
long as pypy2. RAM: a JIT translation peaks around 8 GB (the `.sh`'s warning, kept); the build
refuses to start under 6 GB available+swap with a PyPy host (8 GB with CPython or for pypy3)
unless `--ignore-ram`. `make -j` = one per CPU, at most one per 1.5 GB available.

## Which PyPy (`--pypy-version`, `--series`)

`--pypy-version auto` = the newest release that ships the needed source tarball (`.tar.gz` or
`.tar.bz2`; both are matched, `.tar.gz` preferred); `latest` = the same with release candidates and
`stable: false` entries; `X.Y.Z` = exactly that.

`--series` (pypy3 only): `auto` = the newest pypy3.N **upstream does not call beta**; `latest` = the
newest, beta or not; `3.N` = that one. Beta is read from the release notes' title
(`release-vX.Y.Z.rst`: "release of python 2.7, 3.11, and 3.12 beta"), then versions.json's
`stable` flag, then a built-in table (8.0.0 and 8.0.1 call 3.12 beta). `plan`, `build` and
`versions` print what was picked and why -- e.g. today: *pypy3.12 of 8.0.0 is skipped: the 8.0.0
release notes call it beta; pypy3.11: the newest series upstream does not call beta; --series 3.12
(or --series latest) builds the newer one anyway*. The `.sh`'s `sort -V | tail -1` silently took
the highest. 8.0.1 is the last release with 3.11; once a release's notes stop calling 3.12 beta,
auto takes it.

The release list comes from downloads.python.org's index (fetched gzip-encoded: the `.sh`'s
`--compressed` lesson). When that host is unreachable: versions.json (downloads.python.org or
raw.githubusercontent.com) for the binaries and flags, plus `git ls-remote` of
github.com/pypy/pypy for the release tags (every tag is a source). Offline: the cached list.

## What the 2026-08-29 run taught (legacy/python_log-2026-08-29.txt)

- **`--batch`** on every rpython call. Without it the ^C at 1261 became `[translation:ERROR]
  KeyboardInterrupt` and a `(Pdb+)` prompt waiting for a keyboard -- forever, under a session keeper.
- **RPython takes CC, CFLAGS and LDFLAGS from the environment** (`platform/posix.py`, `linux.py`):
  it used `CC=clang-current` from `~/.bashrc` and printed ~100 `-Wno-discarded-qualifiers` warnings.
  Every step runs in an allow-listed environment (no CC/CFLAGS/LD_LIBRARY_PATH/PYTHONPATH/
  PYPY_*), `/usr/bin` first, and this tool sets `CC` (the chosen gcc), `CFLAGS` (only
  `--extra-cflags`), `LDFLAGS=` and passes `--cc` too. `doctor`/`plan`/`build` name the shell
  variables that are NOT passed on.
- The script blamed "RAM or the host" for a ^C. Failures are classified: **interrupted** (exit 130,
  say how to resume), **out of memory** (RAM, swap, the peak so far; `--low-memory`, swap, fewer
  `--jobs`), **error** (the `[translation:ERROR]` block with the frames above it and the first C
  `error:` lines; C23-looking errors suggest `--extra-cflags=-std=gnu17`).
- package.py's output was sent to `/dev/null`, so failures fell silently through to a hand copy.

## package.py, cffi, pip

`package.py` is steered only by environment variables (its flag names differ between the 2.7 and
3.x copies): `PYPY_NO_EMBED_DEPENDENCIES=1` and `PYPY_NO_REBUILD_EXTENSIONS=1` (on x86_64 it would
otherwise wget OpenSSL, gdbm and xz and link them statically, and rebuild the modules just built --
the system's libraries are used), `PYPY_NO_MAKE_PORTABLE=1`, `PYPY_PACKAGE_WITHOUTTK=1` unless
`--with-tk`, `PYPY_PACKAGE_NOKEEPDEBUG=1` with `--strip`; `--embed-deps` turns embedding back on.
`PIP_NO_INDEX=1 PIP_FIND_LINKS=<downloads>`: pypy3.12's package.py runs `pip install setuptools`,
which the setuptools stage has already satisfied from the pinned wheel (setuptools 84.0.0) -- no
network; if the wheel cannot be had the build stops and says where to put it. The 2.7 package.py
is Python-2-only, so the host runs it; for pypy3, if the host fails before the tree exists, it is
run again with the built `pypy3.N-c`. Failed wanted cffi modules are passed as `--without-<key>`.

If package.py fails, the prefix is **assembled by hand, with a loud warning** in the layouts
package.py makes: pypy2.7 `bin/pypy` + `libpypy-c.so` + links, `lib-python/2.7`, `lib_pypy`,
`include`; pypy3.N `bin/pypy3.N` + `libpypy3.N-c.so` + links, `lib/pypy3.N` = `lib-python/3` +
`lib_pypy` without its `__init__.py`, `include/pypy3.N`. The record says `packaged_by`.

The cffi table (after `build_cffi_imports.py`, found wherever it is): required `_pypy_util_cffi_inner`
`_ssl` `sqlite3` (+ `_blake2` `_sha3` `lzma` for pypy3), wanted `curses` `gdbm`/`_gdbm` `syslog`
`grp` `resource` `_posixshmem` (3.x), optional `audioop`, `_tkinter` with `--with-tk` -- cut down to
the keys the release's own `build_cffi_imports.py` lists (package.py makes a `--without-KEY` of
each, and an unknown one would stop it). A failed module is mapped to its package and the install
command. With `--embed-deps` on a machine with curl and no wget, `USE_CURL=1` is set for
`build_cffi_imports.py` and for package.py (upstream fetches with wget otherwise).

`ensurepip --default-pip` runs in the stage (bundled wheels, no network) unless `--no-pip`; the
`#!<stage>/bin/pypy` lines it writes into `bin/pip*` are pointed at the prefix before the rename.

## verify

Run against the stage before installing, against the prefix after, and as `verify [PREFIX]`:
the interpreter found by the real layout (`bin/pypy` for 2.7; `bin/pypy3.N` -- the only file --
for 3.N, with `pypy3`/`pypy`/`python3` links; the `.sh`'s `find -type f -name pypy3` found nothing
in a good pypy3); the `.sh`'s CORE (fail) and NICE (warn) lists per flavour plus `_cffi_backend`,
`pyexpat`, `json`, `__pypy__`/`pypyjit` (2) and `_blake2`, `_sha3`, `ensurepip`, `venv`,
`pypyjit` (3); `ssl.create_default_context()` in its own process -- a death by signal is a
**CRASH (signal N)** (a cffi `_ssl` built against another OpenSSL: the HANDOFF's `~/opt/pypy2`
and `pypy3`), any other exit a failure; `sys.pypy_version_info` = the release built and the
Python level = the flavour; `translation.jit` True and a 2,000,000-iteration hot loop (dies where
SELinux denies execmem); `-m pip --version`; an HTTPS GET unless `--offline` (a warning); `ldd` of
`libpypy*-c.so` and every cffi `.so` (any `not found` fails); RUNPATH `$ORIGIN/` only and the
`.comment` naming the chosen gcc (warnings).

## Resume, smoke, `--from compile`

`build` again continues at the stage that failed. A translation is all-or-nothing, but the
download, the host, the source tree, cffi, package, install and verify are separate stages. When
what a translation depends on changes (release, source sha256, host, compiler, binutils,
`--extra-cflags`, `--lto`) the source tree and everything after it are made again, said with the
differences. `--restart` does that unasked; `--rebuild` too, and replaces the prefix. A build's
status is `done`, `partial` (stages done, more to do -- e.g. after `smoke`), `failed`, `interrupted`
or `running`; a record that says `running` while nobody holds the lock is reported `killed`.

`--smoke` (or the `smoke` command) translates `rpython/translator/goal/targetnopstandalone.py`
first -- 23 s with the CPython 2.7 host here -- and runs it (`debug: hello world`).
`--smoke-jit` translates RPython's toy register interpreter `rpython/jit/tl/targettlr.py` with
`--opt=jit` and runs its SQUARE program hot (50000 -> 2500000000): the JIT backend, proved (6m39s
here under the CPython 2.7 host).
(`targetjitstandalone.py` is not used: in 8.0.0 it no longer translates -- two jit drivers, neither
`is_recursive` -- which this tool's own smoke run found.)

When a translation fails while compiling its C, the usession directory keeps the Makefile:
`build --from compile` runs `make -jN` there again and copies `pypy-c` and `libpypy-c.so` into
`pypy/goal`, instead of translating for another hour.

## The toolkit's contracts

- `--opt-root DIR` (default `~/opt`); `--link-current` (default) / `--no-link-current`.
- `--cc PATH` and `--binutils DIR` (a prefix whose `bin/` holds `as` and `ld`) are taken as REAL
  paths -- `<opt>/gcc-current` and `<opt>/binutils-current` resolved the moment the step starts and
  recorded -- so a later rebuild of gcc-current does not change what was used. Defaults: those two
  links when they work, else `/usr/bin/gcc` and the compiler's own as/ld, said so. `--cxx` is
  accepted (and recorded); PyPy has no C++.
- `--yes` on every subcommand: ask nothing (Proceed is yes; an earlier run is resumed, never
  deleted). With `--yes` and no command there is no wizard (exit 2: give a command); `wizard
  --yes` asks nothing and builds from the flags / `--profile` / defaults, like `build`. Long
  options only. `--version` is `--pypy-version` here, so it still gets the "run where?" question
  (only `-h`, `--help`, `--tool-version` skip it).
- `doctor --json`, exactly: `{"tool", "ok", "package_manager": "dnf|apt-get|zypper|pacman|null",
  "missing": [{"what", "package", "level": "required|wanted|optional"}], "enable_repos": ["crb"],
  "install_command": "sudo dnf install -y ..."}`. Required: a C compiler that compiles, links and
  RUNS a program, make, tar, gzip, bzip2, xz, pkg-config, binutils; headers for libffi, zlib, bzip2,
  expat, OpenSSL, SQLite, ncurses (`curses.h` and `term.h`: every translation needs them) (+
  liblzma for pypy3). Wanted: gdbm, patch (+ dpkg-dev on
  Debian/Ubuntu with the 2.7 host: its setup.py finds `/usr/include/<multiarch>` with it when the
  compiler cannot say). Optional: tk (required with `--with-tk`), readline, liblzma for pypy2, git.
  Package names for dnf / apt-get / zypper / pacman (EL10: `zlib-ng-compat-devel`; `gdbm-devel` is
  in CRB). Running as root is a required problem.
- `status --json`, exactly: `{"tool", "opt_root", "current": {"pypy2-current": TARGET|null,
  "pypy3-current": ...}, "installed": [{"name": "pypy2|pypy3", "version": "<PyPy release>", "prefix",
  "runs", "built_by_tool", "compiler", "for": "python2.7|python3.N"}], "builds": [{"id": "<stem>",
  "status", "stage", "error"}]}`. **`runs` is the step's done-when**: the interpreter starts AND
  makes an ssl context -- so the HANDOFF's old `~/opt/pypy2` / `~/opt/pypy3` (found, also in their
  nested `pypy2/pypy2/bin` layout) show `runs: false, built_by_tool: false` and are never touched.
  Each `prefix` is a real path, as the `current` targets are, so the two match by string even when
  the opt root is reached through a symlink (`opt_root` is as given).
- With `--json` stdout is the JSON alone; messages go to stderr.
- Exit codes: 0 done, 1 failed, 2 usage, 130 interrupted (a ^C here, or one that reached RPython).
- Local or remote: `--local`, `--remote[=USER@HOST]`, `--attach[=SESSION]`, `--sessions` come from
  the `satellite_remote` block (written by `sync_remote.py`; `main()`'s first statement is
  `where_to_run(...)`); sessions are named `pypy-<date>-<time>`.

## Options

| option | default | what |
|---|---|---|
| `--target` | `both` | `pypy2`, `pypy3`, `both` (= `pypy2,pypy3`, in that order), `host` |
| `--pypy-version` (`--version`) | `auto` | `auto`, `latest`, `X.Y.Z` |
| `--series` | `auto` | pypy3: `auto`, `latest`, `3.N` |
| `--host` | `auto` | `auto`, `cpython27`, `prebuilt`, `system`, `built`, PATH |
| `--cc` / `--cxx` | `<opt>/gcc-current` (real path) | the C compiler for RPython's C and the 2.7 host |
| `--binutils` | `<opt>/binutils-current` (real path) | a prefix with `bin/as`, `bin/ld`; `system` |
| `--opt-root`, `--prefix`, `--workdir` | `~/opt`, `<opt>/<stem>`, `~/src/enterprise_pypy_tool` | |
| `--source` | `auto` | `auto`, `git`, a tarball or a tree (copied) |
| `--jobs` | CPUs, 1.5 GB each | `--make-jobs` and the host's `make -j` |
| `--extra-cflags`, `--lto` | | RPython's CFLAGS (appended); `rpython --lto` |
| `--low-memory`, `--ignore-ram`, `--ignore-disk` | | |
| `--with-tk`, `--strip`, `--embed-deps`, `--no-pip` | | package.py's choices |
| `--link-current` / `--no-link-current` | link | `<opt>/pypyN-current` |
| `--offline` | | cached list and downloads only; verify skips HTTPS |
| `--smoke`, `--smoke-jit` | | the tiny translation first |
| `--from compile` | | `make` again in the failed translation's C directory |
| `--rebuild`, `--rebuild-host`, `--restart`, `--resume` | resume | |
| `--install-deps`, `--dry-run`, `--yes`, `--verbose` | | |
| `--profile NAME`, `--save-profile NAME` | | `~/.config/enterprise_pypy_tool/profiles/NAME.ini` |
| `--json` | | `doctor`, `status`, `detect` |

## build_python_universal.sh -> flags (nothing dropped)

| the `.sh` | here | what changed, and why |
|---|---|---|
| `TARGETS=cpython,pypy2,pypy3` | `--target pypy2\|pypy3\|both\|host` | cpython is `tools/python` (step 5) |
| `SRC_ROOT=~/src/python-build` | `--workdir` (`~/src/enterprise_pypy_tool`) | its own directory, locked, with a record |
| `OPT_ROOT=~/opt` | `--opt-root` | |
| `JOBS=$(nproc)` | `--jobs` | the `.sh` never passed it to rpython; here `--make-jobs`, capped by memory |
| `INSTALL_DEPS=1` (default on) | `--install-deps` (opt-in) + `doctor` | no sudo unasked; the main file installs every step's packages once, up front. The lists lost CPython-3 leftovers (`gmp-devel`, `uuid-dev`) and gained the zypper/pacman names |
| `LINK_CURRENT=1` | `--link-current` / `--no-link-current` | atomic, relative, only after verify |
| `PYPY_VERSION=auto` | `--pypy-version auto\|latest\|X.Y.Z` | `latest` added (release candidates) |
| `HOST_MODE=prebuilt` (default) | `--host prebuilt` | still there; no longer the default (choice J) |
| `HOST_MODE=system` | `--host system` | also finds `python2.7`/`python2`; refuses what cannot run RPython |
| `HOST_MODE=cpython` | `--host cpython27` (`cpython` accepted) | was CPython 3 and never worked; now builds CPython 2.7.18 |
| `PYPY_HOST=` | `--host PATH` | probed: Python 2 + pycparser, else refused with the reason |
| `dl()`: cached, `.part`, `curl --retry 3` | `download()` / `fetch_any()` | `.part`, Content-Length and sha256 checked (pypy.org's checksums page; pinned sha256 for CPython 2.7.18, pycparser, cffi, setuptools), then a chain of mirrors instead of retries |
| index with `--compressed` | the same (gzip decoded) | plus versions.json + git tags when the index is unreachable, plus a cache for `--offline` |
| `PYPY_EXT` `.tar.bz2\|.tar.gz` | both matched | `.tar.gz` preferred when both exist |
| `latest_pypy_release`, `pypy_srcname` (`sort -V \| tail -1`) | `--pypy-version`, `--series auto\|latest\|3.N` | the series is chosen and explained, beta skipped by default |
| `pypy_hostname` per `uname -m` | the same tags (`linux64`, `aarch64`, `linux32`, `s390x`) | + the glibc 2.28 floor of 8.x binaries, with the 7.3.x fallback |
| preflight `curl tar make gcc bzip2` | `doctor` | the compiler is made to compile, link and RUN a program; headers asked of it |
| disk warning under 20G | warn under 20 GB, refuse under 10 GB per target (`--ignore-disk`), 1 GB at `<opt>` | |
| RAM warning under 8G | the same warning, plus a floor (`--ignore-ram`) and `--low-memory` | |
| `verify_interpreter` CORE/NICE, ssl context, crash vs failure | `verify` | lists extended (JIT, pip, `ldd`, RUNPATH, `.comment`, the release) |
| `acquire_host` prebuilt: extract, "host ok" | `get_prebuilt_host` | extracted via a temp dir, probed (Python 2 + pycparser) |
| `PYPY_USESSION_DIR=$SRC_ROOT/rpython-scratch` | `<workdir>/usession/<stem>` + `KEEP=1` + `BASENAME` | one 4 GB tree per target, not three; removed after success |
| `rpython --opt=jit targetpypystandalone.py` | + `--batch --cc --make-jobs [--lto]` | the 2026-08-29 Pdb trap and CC leak |
| `find pypy*-c` | the exact names (`pypy-c`, `pypy3.N-c`) | |
| `build_cffi_imports.py` found with `find` | found (new and old locations, then a walk) | run with `PYTHONPATH`, `CC`, `LDSHARED`; the table and packages for what failed |
| `package.py --archive-name pypy-local --targetdir stage >/dev/null` | `package.py --archive-name <stem> --builddir`, logged, env-steered | its tree is the stage; no OpenSSL/gdbm/xz downloads |
| the by-hand copy | kept, LOUD, with the right 3.x layout | the `.sh`'s copy was wrong for 3.x |
| exe by `find -type f` | `find_exe` by layout | the `.sh` missed `bin/pypy3.N` |
| `[ -e "$prefix" ]` -> skip | already built and verified -> skip (link only); not this tool's -> refuse; `--rebuild` | |
| `ln -sfn` pypy{2,3}-current | `link_current` | |
| BUILT / FAILED summary | the same | |
| the PATH caution | the same, sharper: never put `pypyN-current/bin` on PATH (it holds `python`/`python3`) | the HANDOFF's trap 1 |
| the ASCII self-check | a test (`test_pure_ascii`); Python itself refuses a stray U+00A0 | |
| `[ "$(id -u)" -ne 0 ]` | refused (`build`, `smoke`, `clean`, `--install-deps`, the wizard) | |
| `set -e` + `|| true` | stages, records, exit codes | the `.sh` carried on after a failed CPython |

## Open choices (his yes/no pending)

- **J: the pypy2 host.** Default `cpython27`: CPython 2.7.18 built with step 4's gcc, as PLAN says
  ("with that python/gcc/binutils" -- CPython 3 cannot run RPython). It is the slowest host (upstream:
  2-3x a PyPy host, so 1-3 hours for pypy2 here). The alternatives are one flag each: (B)
  `--host prebuilt`, the `.sh`'s default and fast; (C) cpython27 with prebuilt as a fallback is not
  implemented (a failed host stops the build and names `--host prebuilt`).
- **The pypy3 series.** Default `auto` = 3.11 while 8.0.0/8.0.1's notes call 3.12 beta; (A)
  `--series latest` (or `3.12`) = 3.12 now.
- **Prefix names** follow upstream's stems (`pypy2.7-v8.0.0`, `pypy3.12-v8.0.0`); `--prefix` changes it.

## Tested here, and not

Unit tests: `tests/test_tool.py`, 111 tests, stdlib unittest, no network, no root (they pass as
root and as a normal user), no real translation: a fake PyPy (a `/usr/bin/python3` wrapper that
answers like one, and can lose a module, crash in ssl by SIGSEGV, lack the JIT or pip), a fake
Python 2 host, a fake RPython that writes a fake `pypy-c`, a fake `package.py`; end-to-end builds of
pypy2 and pypy3.12 through every stage, resume after a failed translation, a compiler change that
starts over, an interrupted and an OOM translation, the by-hand fallback, a foreign prefix refused.
The real `legacy/python_log-2026-08-29.txt` is read for the phase parser and the classifier.

Real runs in this container (Ubuntu 24.04, GCC 13.3, as a non-root user; downloads.python.org,
www.python.org and pypy.org blocked here, github.com via git and PyPI reachable):

- `build --target host`: CPython 2.7.18 fetched by the git fallback (tag v2.7.18), configured with
  `-O2 -std=gnu17`, built, installed through the stage, pycparser 2.21 + cffi 1.15.1 (built from the
  sdist with that gcc) -- all CPY27_NEEDED modules import. Missing, as expected: `_hashlib`
  (OpenSSL 3), `_tkinter`, `bsddb185`, `dl`, `imageop`, `nis`, `sunaudiodev`.
- `smoke`: PyPy 8.0.0's source by the git fallback, `targetnopstandalone` translated by that CPython
  2.7 host with GCC 13 in 23 s, and it runs. `smoke --smoke-jit`: `targettlr` translated `--opt=jit`
  in 6 min 39 s (JIT compiler generation ~3 min under CPython 2.7), and SQUARE 50000 printed
  2500000000. The first attempt, with `targetjitstandalone.py`, failed inside RPython (see above):
  the error report showed the `[translation:ERROR]` block and its frames, as it should.
- `plan`, `versions`, `doctor [--json]`, `status --json`, `detect [--json]`, `toolchains`: run.

Not tested anywhere yet: a full pypy2 or pypy3 translation (hours, 6-8 GB), so real timings and peak
memory; GCC 14/15/16 compiling CPython 2.7.18 (`-fpermissive`) and RPython's C; package.py's real
behaviour (2.7 and 3.x, under a CPython 2.7 host); the OpenSSL 3.5 cffi `_ssl`; the prebuilt host
(its server is blocked here); SELinux and the JIT on EL10; `--install-deps` with sudo; remote
sessions on Mom's machine or the OptiPlex.

## The PATH caution

Never put `<opt>/pypy2-current/bin` or `pypy3-current/bin` on PATH: they hold `python` and
`python3` links, so `python3` would BE PyPy for every script and build system on the machine (the
HANDOFF's trap 1). Call `pypy` / `pypy3` by full path, or link only those two names into a directory
on PATH. PyPy does not ship the same third-party modules, so anything that shells out to `python3`
and imports yaml, psutil or setuptools may fail in a way that looks like a broken build rather than
a shadowed interpreter.
