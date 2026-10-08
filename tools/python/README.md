# enterprise_python_tool.py

Builds a CPython 3 **for claude_scraper**. It gets a prefix of its own and its own
pip, is optimised with PGO + LTO, and is built by the system compiler. Before the
tool calls the job done, it **proves the interpreter runs the scraper**.
Successor to `build_python_universal.sh` for this purpose. That script also builds
PyPy, which the scraper does not use, and PyPy stays with the `.sh`.

One file. Standard library only. Runs under `/usr/bin/python3`, with syntax held
to 3.6. If it is started with a `python3` that is PyPy, it re-runs itself under
`/usr/bin/python3`. Never run it as root: only `--install-deps` uses `sudo`.

```
./enterprise_python_tool.py doctor           # what is missing, and the command that installs it
./enterprise_python_tool.py plan             # version, paths, configure line -- builds nothing
./enterprise_python_tool.py build            # download, build (PGO+LTO), install, verify, smoke-test the scraper
./enterprise_python_tool.py venv --replace   # give the scraper a .venv made from it (the old one is kept)
./enterprise_python_tool.py check            # smoke-test the scraper's .venv (or --venv DIR / --python EXE)
./enterprise_python_tool.py status | versions | clean
```

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
| `~/opt/cpython-<version>-claude_scraper/` | the interpreter, with its pip, `BUILD-INFO.txt` and `.python_build_tool.json` |
| `~/src/python_build_tool/` | downloads, source and build trees, `logs/<version>/{configure,make,install}.log`, smoke-test logs, `state.json` |
| `<scraper>/.venv/` | after `venv`: made from the prefix, plus `requirements.lock.txt` and `.python_build_tool-venv.json` |

`clean` removes source/build trees and throwaway copies (`--all`: downloads, logs
and the record too). It never touches an installed prefix or a venv. It works
only in a directory that is demonstrably its own: the default one, or one with
its `state.json`. Even there it deletes only names it gives things, such as
`Python-3.*`, `build-3.*` and `check-xxxxxxxx`. It refuses the scraper's
directory, your home directory, and anything that contains either.

Options worth knowing: `--version auto|latest|X.Y|X.Y.Z`, `--prefix`, `--cc`,
`--jobs`, `--no-optimize` (about one minute instead of ten, for trying the tool
out), `--constraints FILE`, `--install-deps`, `--offline`, `--verbose`,
`--scraper DIR` (default `/run/media/madness/purple10/claude_scraper`), and
`--workdir`.

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

## Tests

```
/usr/bin/python3 -I tests/test_tool.py
```

64 stdlib `unittest` tests. No network, no root, no compiler. They cover the
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
- console output to a dead terminal.

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
