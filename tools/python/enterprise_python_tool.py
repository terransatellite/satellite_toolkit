#!/usr/bin/python3 -I
# -*- coding: utf-8 -*-
"""enterprise_python_tool.py -- build a CPython 3 for claude_scraper, and prove it runs it.

Successor to build_python_universal.sh, narrowed to what the scraper needs: one
CPython 3 (no PyPy), with its own pip, in a prefix of its own, and a venv for
the scraper made from it.  What it keeps from the .sh: the newest release as
python.org reports it, PGO + LTO, ensurepip, loadable SQLite extensions, a
versioned prefix that is never installed over, and an interpreter that is
verified instead of trusted.

What is different, and why:

  The environment is scrubbed.  The .sh hands configure whatever the shell
  exports.  On the machine it was written for that is CC/CXX = clang trunk,
  LDFLAGS=-fuse-ld=lld, a long LD_RUN_PATH (ld copies it into every binary's
  RPATH) and GCC 17 trunk first on PATH; its CPython build died at the first
  link (python_log.txt, 2026-08-29).  Here: the system compiler, PATH=/usr/bin
  first, no *FLAGS, no LD_*.

  "Done" means the SCRAPER runs, not that python starts.  `build` ends by making
  a throwaway venv, pip-installing the scraper's requirements.txt into it and
  running a smoke test in a throwaway copy of the scraper's code: every module
  its entry points reach is imported; its text extraction, SQLite schema, queue,
  resource monitor and worker processes are exercised; one HTTPS GET is made.
  Nothing in the scraper's directory is written.

  The scraper's .venv is replaced only when asked (`venv --replace`), never while
  the crawler runs, and the old one is kept beside it.  If the new one fails the
  smoke test, the old one is put back.

  Downloads are checked against the SHA-256 python.org publishes.

  `auto` skips a brand-new series until its first bugfix release, so 3.15.0 is
  not picked the week it ships.  `latest` does pick it.

One file, standard library only, syntax held to Python 3.6.  Never run it as
root: only --install-deps calls sudo.

Licence: public domain / CC0.  No warranty.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("enterprise_python_tool.py needs Python 3.6 or newer")

import argparse
import ast
import errno
import fcntl
import glob
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

TOOL = "enterprise_python_tool"
VERSION = "0.1.0"
PLATFORM_PYTHON = "/usr/bin/python3"
USER_AGENT = TOOL + "/" + VERSION

DEFAULT_SCRAPER = "/run/media/madness/purple10/claude_scraper"
DEFAULT_WORKDIR = "~/src/python_build_tool"
DEFAULT_OPT = "~/opt"
# The scraper's entry points, from its README.  Everything they import, directly
# or not, is "the scraper"; the other top-level .py files are an older version.
ENTRY_POINTS = ("main.py", "extract_existing.py", "fetch_repos.py", "reseed.py")
# Top-level directories of the scraper that hold data, not code.
DATA_DIRS = ("html", "text", "repos", "metadata", "logs", "screenshots")

API_RELEASES = "https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false"
API_FILES = "https://www.python.org/api/v2/downloads/release_file/?release=%s"
FTP_INDEX = "https://www.python.org/ftp/python/"
TARBALL_URL = FTP_INDEX + "%(v)s/Python-%(v)s.tar.xz"
CHECK_URL = "https://www.python.org/"
PIP_PROBE_URL = "https://pypi.org/simple/pip/"
MIN_SERIES = (3, 9)            # trafilatura, lxml 6 and the scraper's own code need at least this
MARKER = ".python_build_tool.json"             # in an interpreter prefix this tool built
VENV_MARKER = ".python_build_tool-venv.json"   # in a venv it made: a DIFFERENT name, never confused

# What CPython's modules need from the system, found by asking the compiler for
# the header -- so the check is the same on every distro; the package names are
# only for the install hint.  "pkg:" asks pkg-config instead: libxml2's headers
# only preprocess with its own -I, which is what lxml's build gets from it too.
#   required  the scraper or pip cannot work without it: the build will not start
#   wanted    without it something real is lost: said loudly, the build goes on
#   optional  interactive niceties
#   fallback  only if pip finds no wheel for this Python and has to compile lxml
DEPS = (
    # key        headers (any one)                 dnf               apt               level
    ("openssl", ("openssl/ssl.h",), "openssl-devel", "libssl-dev", "required",
     "ssl + hashlib: every HTTPS fetch the scraper makes, and pip itself"),
    ("sqlite", ("sqlite3.h",), "sqlite-devel", "libsqlite3-dev", "required",
     "sqlite3: crawler_data.db -- the queue, what was visited, robots"),
    ("zlib", ("zlib.h",), "zlib-devel", "zlib1g-dev", "required",
     "zlib: the html/*.html.gz pages, gzip'd responses, pip's wheels"),
    ("libffi", ("ffi.h",), "libffi-devel", "libffi-dev", "required", "ctypes"),
    ("bzip2", ("bzlib.h",), "bzip2-devel", "libbz2-dev", "required", "bz2"),
    ("xz", ("lzma.h",), "xz-devel", "liblzma-dev", "required", "lzma"),
    ("mpdecimal", ("mpdecimal.h",), "mpdecimal-devel", "libmpdec-dev", "wanted",
     "decimal against the system libmpdec (CPython's bundled copy is deprecated)"),
    ("zstd", ("zstd.h",), "libzstd-devel", "libzstd-dev", "wanted",
     "compression.zstd (3.14+): requests can then accept zstd-encoded pages"),
    ("libuuid", ("uuid/uuid.h",), "libuuid-devel", "uuid-dev", "optional", "_uuid: fast uuid1()"),
    ("readline", ("readline/readline.h",), "readline-devel", "libreadline-dev", "optional",
     "line editing at the >>> prompt"),
    ("ncurses", ("ncurses.h", "curses.h"), "ncurses-devel", "libncurses-dev", "optional", "curses"),
    ("gdbm", ("gdbm.h",), "gdbm-devel", "libgdbm-dev", "optional", "dbm.gnu"),
    ("libxml2", ("pkg:libxml-2.0",), "libxml2-devel", "libxml2-dev", "fallback",
     "only if pip has to compile lxml (no lxml wheel for this Python)"),
    ("libxslt", ("pkg:libxslt",), "libxslt-devel", "libxslt1-dev", "fallback",
     "only if pip has to compile lxml (no lxml wheel for this Python)"),
)
DEPS_BY_KEY = tuple((row[0], row[5]) for row in DEPS)
# name on PATH, dnf package, apt package
BUILD_TOOLS = (("make", "make", "make"), ("tar", "tar", "tar"), ("xz", "xz", "xz-utils"),
               ("pkg-config", "pkgconfig", "pkg-config"), ("readelf", "binutils", "binutils"),
               ("ldd", "glibc-common", "libc-bin"))
# Which system library each C module comes from: a module missing from CPython's
# own build report is turned back into "install this".
MODULE_DEP = {"_ssl": "openssl", "_hashlib": "openssl", "_sqlite3": "sqlite", "zlib": "zlib",
              "binascii": "zlib", "_bz2": "bzip2", "_lzma": "xz", "_ctypes": "libffi",
              "_decimal": "mpdecimal", "_zstd": "zstd", "_uuid": "libuuid", "readline": "readline",
              "_curses": "ncurses", "_curses_panel": "ncurses", "_gdbm": "gdbm"}

# Imported in the INSTALLED interpreter.  One of these not importing fails the
# build: a Python that "built" without _ssl is worse than a failed build, because
# you find out when the crawler starts.  Pure-Python modules are listed too,
# because they are what the scraper and pip actually import.
MODULES_REQUIRED = ("_ssl", "_hashlib", "_sqlite3", "zlib", "_bz2", "_lzma", "_ctypes", "_json",
                    "_multiprocessing", "_posixsubprocess", "_socket", "select", "_pickle", "_struct",
                    "binascii", "math", "pyexpat", "_elementtree", "_queue", "fcntl",
                    "ssl", "hashlib", "sqlite3", "gzip", "bz2", "lzma", "ctypes", "json",
                    "multiprocessing", "subprocess", "socket", "urllib.request", "ensurepip", "venv")
MODULES_WANTED = (("_decimal", (3, 0)), ("_zstd", (3, 14)))     # (module, from which version)
MODULES_OPTIONAL = ("_uuid", "readline", "_curses", "_gdbm")

# ------------------------------------------------------------------------ output


class UI(object):
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    tty = sys.stdout.isatty()


def _c(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if UI.color else text


def out(text=""):
    """Never raises: a closed pipe or a dead terminal must not turn into an
    exception in the middle of a rollback."""
    try:
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode("ascii"))
        sys.stdout.flush()
    except (OSError, IOError, ValueError):
        pass


def say(msg):
    out("\n" + _c("1", "==> " + msg))


def info(msg):
    out("  " + msg)


def ok(msg):
    out("  " + _c("32", "ok  ") + " " + msg)


def note(msg):
    out("  --   " + msg)


def bad(msg):
    out("  " + _c("31", "FAIL") + " " + msg)


def warn(msg):
    # stdout, not stderr: redirected to one log the two streams interleave out
    # of order, and a warning that lands three sections late is worse than none.
    out(_c("33", "warning:") + " " + msg)


def status_line(text):
    if UI.tty:
        try:
            sys.stdout.write("\r\033[K" + text)
            sys.stdout.flush()
        except (OSError, IOError, ValueError, UnicodeEncodeError):
            pass


def clear_status():
    status_line("")


class ToolError(Exception):
    """A failure with a message fit to show the user.  No traceback."""


# ----------------------------------------------------------------------- process

# An ALLOW-list.  The machine this was written on exports CC, CXX, LDFLAGS,
# LLVM, LD_RUN_PATH and LD_LIBRARY_PATH from ~/.bashrc, and configure reads all
# of them.  The proxy and CA variables are what an enterprise network needs.
ENV_KEEP = ("HOME", "USER", "LOGNAME", "TERM", "TMPDIR", "LANG",
            "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "all_proxy", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR")
# pip honours these; a company mirror, proxy CA or cache is configured through them.
PIP_KEEP = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CERT", "PIP_CLIENT_CERT",
            "PIP_PROXY", "PIP_FIND_LINKS", "PIP_NO_INDEX", "PIP_CONFIG_FILE", "PIP_CACHE_DIR",
            "PIP_TIMEOUT", "PIP_DEFAULT_TIMEOUT", "PIP_RETRIES", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
            "XDG_CACHE_HOME", "XDG_CONFIG_HOME")
# /usr/local/bin is left out on purpose: on the machine this was written on it
# holds a root-installed python3.14 and LLVM tools, and configure goes looking
# for both.
SYSTEM_PATH = ["/usr/bin", "/usr/sbin", "/bin", "/sbin"]
TRAP_VARS = ("CC", "CXX", "CPP", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LDSHARED", "AR", "RANLIB",
             "CPATH", "C_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH", "LD_RUN_PATH", "LD_PRELOAD",
             "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "CONFIG_SITE", "MAKEFLAGS", "MFLAGS", "LLVM",
             "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "DESTDIR")
# These the build never sees, but the scraper will: it runs from your shell.
RUNTIME_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "LD_PRELOAD",
                "LD_LIBRARY_PATH")


def scrubbed_env(extra_path=None, extra=None, keep=()):
    """PYTHONNOUSERSITE: a pip in ~/.local/lib/python3.X would make `make
    install`'s ensurepip say "already satisfied" and leave the prefix without
    one; nothing the build or pip runs should see the user site."""
    env = dict((k, os.environ[k]) for k in tuple(ENV_KEEP) + tuple(keep) if k in os.environ)
    env["PATH"] = ":".join(list(extra_path or []) + SYSTEM_PATH)
    env["LC_ALL"] = "C"
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra or {})
    return env


def env_traps(environ=None):
    environ = os.environ if environ is None else environ
    found = []
    for var in TRAP_VARS:
        if var in environ:
            val = environ[var]
            found.append("%s=%s" % (var, val if len(val) < 70 else val[:67] + "..."))
    return found


def which(name, path=None):
    return shutil.which(name, path=path if path is not None else ":".join(SYSTEM_PATH))


def run(cmd, cwd=None, env=None, check=True, stdin_data=None, interactive=False, timeout=None):
    """subprocess.run with 3.6-safe arguments; stdin is /dev/null unless asked for."""
    kw = dict(cwd=cwd, env=env, universal_newlines=True, encoding="utf-8", errors="replace", timeout=timeout)
    if not interactive:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.STDOUT
    if stdin_data is not None:
        kw["input"] = stdin_data
    elif not interactive:
        kw["stdin"] = subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, **kw)
    except FileNotFoundError:
        raise ToolError("command not found: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise ToolError("timed out after %ss: %s" % (timeout, " ".join(cmd)[:200]))
    except OSError as e:
        # ^C under a root-owned sudo child: killing it raises EPERM.  Any other
        # EPERM -- an exec policy such as fapolicyd -- is a real error.
        if e.errno == errno.EPERM and cmd and os.path.basename(cmd[0]) == "sudo":
            raise KeyboardInterrupt()
        raise ToolError("cannot run %s: %s" % (cmd[0], e))
    if check and p.returncode != 0:
        raise ToolError("command failed (exit %d): %s\n%s"
                        % (p.returncode, " ".join(cmd), (p.stdout or "").strip()[-2000:]))
    return p


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, IOError):
        return default


def read_json(path):
    try:
        data = json.loads(read_text(path) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    os.replace(tmp, path)


def human(nbytes):
    for unit in ("B", "K", "M", "G", "T"):
        if nbytes < 1024 or unit == "T":
            return "%.1f%s" % (nbytes, unit) if unit != "B" else "%dB" % nbytes
        nbytes /= 1024.0


def hms(seconds):
    seconds = int(seconds)
    return "%dh%02dm%02ds" % (seconds // 3600, seconds // 60 % 60, seconds % 60) \
        if seconds >= 3600 else "%dm%02ds" % (seconds // 60, seconds % 60)


def free_gb(path):
    """Of the nearest EXISTING parent: statvfs cannot measure a path that is not
    there yet, and on a fresh machine the work directory never is."""
    path = os.path.abspath(path)
    while not os.path.exists(path):
        path = os.path.dirname(path)
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / float(1 << 30)


def dir_size(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def tail(text, n=25):
    return "\n".join("    " + line for line in (text or "").rstrip().splitlines()[-n:])


def fit_line(text, cols=None):
    cols = cols or shutil.get_terminal_size((100, 24)).columns
    return text[:max(cols - 1, 20)]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def same_path(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


# ---------------------------------------------------------------------- versions

def numver(text):
    m = re.search(r"(\d+(?:\.\d+)+|\d+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def vstr(v):
    return ".".join(str(x) for x in v) if v else "?"


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise ToolError("cannot fetch %s: %s" % (url, e))


def url_exists(url, timeout=20):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.getcode() < 300
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


def parse_api_releases(raw):
    """python.org's release list -> [{version, id, date}]: CPython 3 final
    releases, newest first."""
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except ValueError:
        raise ToolError("python.org's release list is not JSON (a proxy's block page?)")
    rels = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        m = re.match(r"^Python (3\.\d+\.\d+)$", (r.get("name") or "").strip())
        if not m or r.get("pre_release") or r.get("is_published") is False:
            continue
        rid = (r.get("resource_uri") or "").rstrip("/").split("/")[-1]
        rels.append({"version": m.group(1), "id": rid if rid.isdigit() else "",
                     "date": (r.get("release_date") or "")[:10]})
    rels.sort(key=lambda r: numver(r["version"]), reverse=True)
    return rels


def parse_ftp_index(html):
    """The FTP directory listing -> candidate versions, newest first.  A directory
    exists for a release still in beta, so each one still needs its tarball
    confirmed before it counts as released."""
    return sorted(set(re.findall(r'href="(3\.\d+\.\d+)/"', html or "")), key=numver, reverse=True)


def releases_from_ftp(want="auto", limit=16):
    """Only the directories that can answer `want`: an X.Y asks about its own
    series, not about the 16 newest directories of every series."""
    html = http_get(FTP_INDEX, timeout=30).decode("utf-8", "replace")
    cands = parse_ftp_index(html)
    want = want or "auto"
    if re.match(r"^\d+\.\d+\.\d+$", want):
        cands = [v for v in cands if v == want]
    elif re.match(r"^\d+\.\d+$", want):
        cands = [v for v in cands if v.startswith(want + ".")]
    found = []
    for v in cands[:limit]:
        if url_exists(TARBALL_URL % {"v": v}):
            found.append({"version": v, "id": "", "date": ""})
    return found            # the listing answered (http_get raises when it does not)


def get_releases(want="auto"):
    """-> (releases, where from).  The API knows what is RELEASED; the FTP listing
    only what has a directory, so it is the fallback."""
    try:
        rels = parse_api_releases(http_get(API_RELEASES))
        if rels:
            return rels, "python.org API"
        note("python.org's API listed no releases -- reading the FTP listing instead")
    except ToolError as e:
        note("python.org's API did not answer (%s) -- reading the FTP listing instead"
             % str(e).splitlines()[0][:90])
    try:
        rels = releases_from_ftp(want)
    except ToolError as e:
        raise ToolError("cannot list CPython releases: python.org's API and its FTP listing both failed (%s).\n"
                        "  Pass --version X.Y.Z to build a release you know exists." % str(e).splitlines()[0][:120])
    if not rels:
        raise ToolError("python.org's API did not answer, and its FTP listing has no finished release%s."
                        % ("" if (want or "auto") in ("auto", "latest") else " for " + want))
    return rels, "python.org FTP listing"


def by_series(releases):
    groups = {}
    for r in releases:
        groups.setdefault(numver(r["version"])[:2], []).append(r)
    for rs in groups.values():
        rs.sort(key=lambda r: numver(r["version"]), reverse=True)
    return groups


def pick_version(releases, want):
    """want: auto | latest | X.Y | X.Y.Z  ->  (release, why)."""
    want = (want or "auto").strip()
    if not releases and not re.match(r"^\d+\.\d+\.\d+$", want):
        raise ToolError("no CPython releases to choose from")
    if re.match(r"^\d+\.\d+\.\d+$", want):
        for r in releases:
            if r["version"] == want:
                return r, "asked for"
        if releases:
            raise ToolError("python.org has no final release %s.  Newest: %s.  (Pre-releases are not "
                            "built: the scraper needs a finished Python.)" % (want, releases[0]["version"]))
        return {"version": want, "id": "", "date": ""}, "asked for (not confirmed: no release list)"
    if re.match(r"^\d+\.\d+$", want):
        rs = by_series(releases).get(numver(want))
        if not rs:
            raise ToolError("python.org has no %s.x release" % want)
        return rs[0], "newest %s.x" % want
    if want == "latest":
        return releases[0], "newest stable release"
    if want == "auto":
        groups = by_series(releases)
        skipped = []
        for series in sorted(groups, reverse=True):
            rs = groups[series]
            if any(numver(r["version"])[2:3] >= (1,) for r in rs):
                why = "newest release of the newest series that has had a bugfix release"
                if skipped:
                    why += " (%s skipped: only %s so far -- `--version latest` takes it)" % (
                        ", ".join(vstr(s) for s in skipped),
                        ", ".join(groups[s][0]["version"] for s in skipped))
                return rs[0], why
            skipped.append(series)
        return releases[0], "newest stable release (no series has a bugfix release yet)"
    raise ToolError("--version takes auto, latest, X.Y or X.Y.Z (got %r)" % want)


def tarball_info(release):
    """{url, sha256, size} of the release's .tar.xz; sha256 is empty when
    python.org did not say."""
    v = release["version"]
    url = TARBALL_URL % {"v": v}
    if release.get("id"):
        try:
            files = json.loads(http_get(API_FILES % release["id"]).decode("utf-8", "replace"))
            for f in files if isinstance(files, list) else []:
                if isinstance(f, dict) and (f.get("url") or "").endswith("/Python-%s.tar.xz" % v):
                    return {"url": f["url"], "sha256": (f.get("sha256_sum") or "").strip().lower(),
                            "size": int(f.get("filesize") or 0)}
        except (ToolError, ValueError, TypeError) as e:
            note("python.org's file list did not answer (%s)" % str(e).splitlines()[0][:90])
    return {"url": url, "sha256": "", "size": 0}


# ------------------------------------------------------------------------ fetching

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url, dest, sha256="", size=0):
    """To dest.part, checked, then renamed: a file with the final name is whole."""
    if os.path.isfile(dest) and (not sha256 or sha256_file(dest) == sha256):
        ok("already downloaded: %s" % dest)
        return
    if os.path.isfile(dest):
        warn("%s does not match python.org's checksum -- downloading it again" % dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started, got, shown, total = time.time(), 0, 0.0, 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                got += len(block)
                if UI.tty and time.time() - shown > 0.2:
                    shown = time.time()
                    status_line("  %s / %s" % (human(got), human(total) if total else "?"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise ToolError("download failed: %s\n  %s" % (url, e))
    finally:
        clear_status()
    if (total and got != total) or (size and got != size):
        raise ToolError("download was cut short: %s of %s from %s" % (human(got), human(total or size), url))
    if sha256:
        have = sha256_file(part)
        if have != sha256:
            os.unlink(part)
            raise ToolError("checksum mismatch for %s\n  python.org says %s\n  got            %s"
                            % (url, sha256, have))
        ok("sha256 matches python.org's")
    else:
        note("python.org gave no checksum for this file: its integrity rests on TLS alone")
    os.replace(part, dest)
    ok("downloaded %s in %s" % (human(got), hms(time.time() - started)))


def extract(tarball, dest_dir):
    """Into a temp dir beside the destination, then renamed: a tree with the
    final name is complete.  GNU tar, not tarfile: before Python 3.12 tarfile
    has no member filter."""
    parent = os.path.dirname(dest_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".extract-", dir=parent)
    try:
        run(["tar", "-xf", tarball, "-C", tmp, "--no-same-owner"], env=scrubbed_env(), timeout=1800)
        tops = os.listdir(tmp)
        if len(tops) != 1 or not os.path.isdir(os.path.join(tmp, tops[0])):
            raise ToolError("%s does not hold exactly one directory" % tarball)
        os.rename(os.path.join(tmp, tops[0]), dest_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- the machine

def parse_os_release(text):
    d = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def crb_command(osr):
    """mpdecimal-devel lives in CodeReady Builder on RHEL and its rebuilds; the
    command that enables it differs."""
    major = (osr.get("VERSION_ID") or "").split(".")[0]
    if osr.get("ID") == "rhel":
        return "sudo subscription-manager repos --enable codeready-builder-for-rhel-%s-$(arch)-rpms" % major
    if major == "8":
        return "sudo dnf config-manager --set-enabled powertools"
    return "sudo dnf config-manager --set-enabled crb"


def pkg_manager():
    """-> (command, family): ('dnf', 'dnf'), ('apt-get', 'apt'), or ('', '')."""
    for name, family in (("dnf", "dnf"), ("yum", "dnf"), ("apt-get", "apt")):
        if which(name):
            return name, family
    return "", ""


def install_command(packages, mgr, family):
    if not packages:
        return ""
    if family == "dnf":
        return "sudo %s install -y %s" % (mgr, " ".join(packages))
    if family == "apt":
        return "sudo apt-get install -y %s" % " ".join(packages)
    return ""


def packages_for(rows, family):
    return [r[2] if family != "apt" else r[3] for r in rows]


def pick_cc(forced=""):
    """The SYSTEM compiler unless told otherwise: extensions pip compiles later use
    the compiler recorded in sysconfig, and the system one is the one that is
    still there next year."""
    if forced:
        path = forced if os.path.isabs(forced) else which(forced)
        if not path or not os.access(path, os.X_OK):
            raise ToolError("--cc %s: not an executable" % forced)
        return path
    for name in ("gcc", "cc", "clang"):
        path = which(name)
        if path:
            return path
    return ""


def cxx_for(cc):
    d, base = os.path.split(cc)
    for c, cxx in (("clang", "clang++"), ("gcc", "g++"), ("cc", "c++")):
        if base == c or base.startswith(c + "-"):
            cand = os.path.join(d, cxx + base[len(c):])
            if os.access(cand, os.X_OK):
                return cand
            return which(cxx) or ""
    return ""


def compiler_banner(cc):
    p = run([cc, "--version"], env=scrubbed_env(), check=False, timeout=60)
    return ((p.stdout or "").strip().splitlines() or ["?"])[0]


def compiler_works(cc, scratch_parent=None):
    """Compile, link and RUN a hello-world, in the work directory: configure has
    to run its test programs there anyway, and with /tmp mounted noexec (CIS
    hardening) a probe in /tmp would blame a compiler that works."""
    try:
        if scratch_parent:
            os.makedirs(scratch_parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".ept-cc-", dir=scratch_parent)
    except OSError as e:
        return False, "cannot write a test program in %s: %s" % (scratch_parent or tempfile.gettempdir(), e)
    try:
        src = os.path.join(tmp, "t.c")
        with open(src, "w") as f:
            f.write("#include <stdio.h>\nint main(void) { puts(\"ok\"); return 0; }\n")
        exe = os.path.join(tmp, "t")
        p = run([cc, src, "-o", exe], env=scrubbed_env(), check=False, timeout=120)
        if p.returncode != 0:
            return False, (p.stdout or "").strip()[-600:]
        try:
            p = run([exe], env=scrubbed_env(), check=False, timeout=60)
        except ToolError as e:
            return False, ("%s\n  (it compiled, but %s will not run programs: mounted noexec, or blocked by an "
                           "exec policy such as fapolicyd?)" % (e, tmp))
        return p.returncode == 0 and "ok" in (p.stdout or ""), (p.stdout or "").strip()[-300:]
    except (ToolError, OSError) as e:
        return False, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def have_header(cc, header):
    p = run([cc, "-E", "-x", "c", "-o", os.devnull, "-"], env=scrubbed_env(), check=False,
            stdin_data="#include <%s>\n" % header, timeout=60)
    return p.returncode == 0


def have_dep(cc, probe):
    if probe.startswith("pkg:"):
        return bool(which("pkg-config")) and run(["pkg-config", "--exists", probe[4:]], env=scrubbed_env(),
                                                 check=False, timeout=60).returncode == 0
    return have_header(cc, probe)


def probe_label(probe):
    return "pkg-config %s" % probe[4:] if probe.startswith("pkg:") else probe


def check_deps(cc):
    """-> {key: True/False} for every DEPS row, by asking the compiler (or pkg-config)."""
    return dict((row[0], any(have_dep(cc, h) for h in row[1])) for row in DEPS)


def missing_rows(found, levels):
    return [row for row in DEPS if row[4] in levels and not found.get(row[0])]


def meminfo_gb():
    m = re.search(r"MemTotal:\s+(\d+)", read_text("/proc/meminfo"))
    return int(m.group(1)) / 1048576.0 if m else 0.0


# ------------------------------------------------------------------- bookkeeping

class State(object):
    """<workdir>/state.json, written whole and atomically on every change."""

    def __init__(self, path):
        self.path = path
        self.d = read_json(path)
        if self.d is None:
            if os.path.exists(path):
                warn("%s is unreadable -- starting a fresh record" % path)
            self.d = {}
        self.d.setdefault("builds", {})
        self.d.setdefault("checks", [])

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.d["tool"] = TOOL                   # what `clean` checks before it deletes anything
        write_atomic(self.path, json.dumps(self.d, indent=1, sort_keys=True) + "\n")

    def build(self, version):
        return self.d["builds"].setdefault(version, {})

    def set_build(self, version, **kv):
        self.build(version).update(kv)
        self.save()

    def add_check(self, entry):
        self.d["checks"] = (self.d["checks"] + [entry])[-30:]
        self.save()

    def last_seconds(self):
        secs = [b.get("make_seconds") for b in self.d["builds"].values() if b.get("make_seconds")]
        return secs[-1] if secs else 0


_LOCKS = []


def lock_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        f = open(os.path.join(path, ".lock"), "a")
    except OSError as e:
        raise ToolError("cannot use the work directory %s: %s" % (path, writable_parent(path)[1] or e))
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        f.close()
        raise ToolError("another %s is working in %s right now" % (TOOL, path))
    _LOCKS.append(f)


# --------------------------------------------------------------- running make

# CPython's Makefile (3.9-3.15).  "Running code to generate profile data" is NOT
# a phase marker: 3.14's profile-run-stamp recipe prints it BEFORE the
# instrumented build, so the training run is recognised by its own command line.
# Phases only move forward.
PHASES = (("Building with support for profile generation", "PGO 1/3: instrumented build"),
          ("-m test --pgo", "PGO 2/3: training run -- CPython's test suite, the long part"),
          ("Rebuilding with profile guided optimizations", "PGO 3/3: optimised rebuild + LTO link"))
PROGRESS = re.compile(r"\[\s*(\d+)/(\d+)\]")
ERRORISH = re.compile(r"(\berror:|\bfatal error\b|undefined reference|^make(\[\d+\])?: \*\*\*|"
                      r"\bKilled\b|Segmentation fault|Could not build the ssl module|^ERROR:)")


def run_logged(cmd, cwd, env, log_path, label, verbose=False, phases=False):
    """Run cmd; everything goes to log_path.  The console gets a one-line status
    on a terminal (a heartbeat every five minutes otherwise), each PGO phase as it
    starts, and the first error lines.  Errors during the PGO training run are
    test failures that CPython itself ignores, so they are not echoed."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if os.path.exists(log_path):
        os.replace(log_path, log_path + ".prev")
    started = time.time()
    phase, last, progress, shown_err, beat, drawn = "", "", "", 0, started, 0.0
    phase_i = 0
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8",
                            errors="replace", bufsize=1)

    def status():
        return fit_line("  [%s] %s%s%s  %s" % (hms(time.time() - started), label,
                                              " | " + phase if phase else "",
                                              " " + progress if progress else "", last))
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write("# %s\n# cwd: %s\n" % (" ".join(shlex.quote(c) for c in cmd), cwd))
            for line in proc.stdout:
                log.write(line)
                text = line.rstrip("\n")
                if verbose:
                    out(text)
                    continue
                for i, (needle, name) in enumerate(PHASES if phases else (), 1):
                    if i > phase_i and needle in text:
                        phase_i, phase, progress = i, name.split(":")[0], ""
                        clear_status()
                        info("%s  [%s]" % (name, hms(time.time() - started)))
                training = phase_i == 2
                m = PROGRESS.search(text)
                if m and training:
                    progress = "test %s/%s" % (m.group(1), m.group(2))
                if text.strip():
                    last = text.strip()
                if not training and ERRORISH.search(text) and "warning:" not in text:
                    shown_err += 1
                    if shown_err <= 40:
                        clear_status()
                        out("    " + text[:300])
                if UI.tty:
                    if time.time() - drawn >= 0.1:
                        drawn = time.time()
                        status_line(status())
                elif time.time() - beat >= 300:
                    beat = time.time()
                    out(status())
        rc = proc.wait()
        proc.stdout.close()
    except BaseException:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.communicate(timeout=60)
        except (subprocess.TimeoutExpired, ValueError):
            proc.kill()
            proc.wait()
        raise
    finally:
        clear_status()
    return rc, int(time.time() - started)


def error_summary(log_path, limit=14):
    """The FIRST real errors: the tail of a -j24 build is the other 23 jobs."""
    hits = []
    phase_i = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                for i, (needle, _) in enumerate(PHASES, 1):
                    if i > phase_i and needle in line:
                        phase_i = i
                if phase_i != 2 and ERRORISH.search(line) and "warning:" not in line:
                    hits.append("%7d: %s" % (n, line.rstrip()[:300]))
                    if len(hits) >= limit:
                        break
    except (OSError, IOError):
        pass
    return hits


CHECKED = re.compile(r"Checked (\d+) modules \((\d+) built-in, (\d+) shared, (\d+) n/a on \S+, "
                     r"(\d+) disabled, (\d+) missing, (\d+) failed on import\)")
REPORT_HEADERS = (("missing", "The necessary bits to build these optional modules were not found:"),
                  ("failed", "Failed to build these modules:"),
                  ("removed", "were removed because they could not be imported:"),
                  ("disabled", "*disabled* in configure script:"))


def module_report(text):
    """CPython's own end-of-build module summary -> {missing, failed, removed,
    disabled, summary}.  A PGO build prints one per pass; the LAST one is the
    build that gets installed."""
    lines = (text or "").splitlines()
    marks = [i for i, line in enumerate(lines) if CHECKED.search(line)]
    end = marks[-1] if marks else len(lines)
    start = marks[-2] + 1 if len(marks) > 1 else 0
    rep = {"missing": [], "failed": [], "removed": [], "disabled": [],
           "summary": lines[end].strip() if marks else ""}
    i = start
    while i < end:
        hit = next((k for k, h in REPORT_HEADERS if h in lines[i]), None)
        i += 1
        if hit:
            while i < end and lines[i].strip() and not lines[i].startswith("To find"):
                rep[hit].extend(lines[i].split())
                i += 1
    return rep


# ----------------------------------------------------------------- configuration

def scraper_tag(scraper):
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(os.path.normpath(scraper))) or "app"


def default_prefix(version, scraper):
    return os.path.join(expand(DEFAULT_OPT), "cpython-%s-%s" % (version, scraper_tag(scraper)))


def configure_args(prefix, cc, cxx, optimize=True, system_mpdec=True):
    """--enable-shared is deliberately NOT used (as in the .sh): a libpython3.so
    has to be found at run time, and nothing the scraper does needs it."""
    a = ["--prefix=" + prefix]
    if optimize:
        a += ["--enable-optimizations", "--with-lto"]
    a += ["--with-computed-gotos", "--with-ensurepip=install", "--enable-loadable-sqlite-extensions"]
    if system_mpdec:
        a.append("--with-system-libmpdec")
    a.append("CC=" + cc)
    if cxx:
        a.append("CXX=" + cxx)
    return a


class Ctx(object):
    """Everything one build needs, resolved once."""

    def __init__(self, args, release=None, why="", source=""):
        self.args = args
        self.scraper = expand(args.scraper)
        self.workdir = expand(args.workdir)
        self.release = release or {}
        self.version = self.release.get("version", "")
        self.why, self.source = why, source
        self.prefix = expand(args.prefix) if getattr(args, "prefix", None) else \
            (default_prefix(self.version, self.scraper) if self.version else "")
        self.downloads = os.path.join(self.workdir, "downloads")
        self.tarball = os.path.join(self.downloads, "Python-%s.tar.xz" % self.version)
        self.src = os.path.join(self.workdir, "Python-" + self.version)
        self.build = os.path.join(self.workdir, "build-" + self.version)
        self.stage = os.path.join(self.workdir, "stage-" + self.version)
        self.check_venv = os.path.join(self.workdir, "check-venv-" + self.version)
        self.logs = os.path.join(self.workdir, "logs", self.version or "misc")
        self.cc = pick_cc(getattr(args, "cc", "") or "")
        self.cxx = cxx_for(self.cc) if self.cc else ""
        self.jobs = getattr(args, "jobs", None) or os.cpu_count() or 2
        self.optimize = not getattr(args, "no_optimize", False)
        self.tar = {}

    def python(self):
        return os.path.join(self.prefix, "bin", "python3")

    def configure(self, system_mpdec):
        return configure_args(self.prefix, self.cc, self.cxx, self.optimize, system_mpdec)


def resolve_release(args):
    want = args.version or "auto"
    if re.match(r"^\d+\.\d+\.\d+$", want):
        try:
            rels, source = get_releases(want)
        except ToolError as e:
            warn(str(e).splitlines()[0])
            rels, source = [], "the command line"
        if source != "python.org API" and not any(r["version"] == want for r in rels):
            rels = []           # the FTP fallback cannot prove a NEGATIVE: build it unconfirmed
    else:
        rels, source = get_releases(want)
    rel, why = pick_version(rels, args.version or "auto")
    if numver(rel["version"])[:2] < MIN_SERIES:
        raise ToolError("CPython %s is older than the scraper supports (%s+)" % (rel["version"], vstr(MIN_SERIES)))
    return rel, why, source


# ------------------------------------------------------------------- doctor

def writable_parent(path):
    """-> (nearest existing ancestor of path, problem or "").  Where the prefix (or
    work directory) is to be created must be writable NOW, not after the build."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        if os.path.lexists(p):
            return p, "%s is a dangling symlink" % p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if not os.path.isdir(p):
        return p, "%s is not a directory" % p
    if not os.access(p, os.W_OK | os.X_OK):
        return p, "%s is not writable by you" % p
    return p, ""


def preflight(ctx, install_deps=False):
    """Everything that can be known before ten minutes of compiling.  Raises on
    what would make the build fail or the scraper not run."""
    problems = []

    def line(kind, msg):
        {"ok": ok, "note": note, "bad": bad, "warn": warn, "info": info}[kind](msg)

    say("Machine")
    osr = parse_os_release(read_text("/etc/os-release"))
    rhel_like = "rhel" in (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).lower()
    line("ok", "%s, %s, %d CPUs, %.0f GB RAM" % (osr.get("PRETTY_NAME", "unknown Linux"), os.uname().machine,
                                                 os.cpu_count() or 0, meminfo_gb()))
    line("ok", "this tool runs under %s %s" % (sys.executable, vstr(sys.version_info[:3])))
    _, why = writable_parent(ctx.workdir)
    if not why:
        try:
            os.makedirs(ctx.workdir, exist_ok=True)
        except OSError as e:
            why = str(e)
    if why:
        problems.append("the work directory %s cannot be made: %s" % (ctx.workdir, why))
        line("bad", problems[-1])

    def check_cc():
        if not ctx.cc:
            return "no C compiler (gcc) found in %s" % ":".join(SYSTEM_PATH)
        works, detail = compiler_works(ctx.cc, ctx.workdir if not why else None)
        if works:
            line("ok", "C compiler %s -- %s" % (ctx.cc, compiler_banner(ctx.cc)))
            return ""
        return "the C compiler %s cannot build and run a hello-world:\n%s" % (ctx.cc, detail)
    cc_problem = check_cc()
    if cc_problem:
        line("bad", cc_problem)
    mgr, family = pkg_manager()
    tool_rows = []
    for name, dnf_pkg, apt_pkg in BUILD_TOOLS:
        if which(name):
            continue
        tool_rows.append((name, (), dnf_pkg, apt_pkg, "required", name))
        line("bad" if name in ("make", "tar", "xz") else "warn", "%s not found" % name)
    if not tool_rows:
        line("ok", "make, tar, xz, pkg-config, readelf, ldd")
    needed_tools = [r for r in tool_rows if r[0] in ("make", "tar", "xz")]
    cc_rows = [] if ctx.cc else [("gcc", (), "gcc", "build-essential", "required", "a C compiler")]

    found = check_deps(ctx.cc) if ctx.cc else {}
    say("Libraries CPython's modules need (headers, as the compiler sees them)")
    if not ctx.cc:
        line("note", "(cannot ask without a compiler)")
    for row in DEPS if ctx.cc else ():
        key, headers, dnf_pkg, apt_pkg, level, what = row
        pkg = apt_pkg if family == "apt" else dnf_pkg
        text = "%-26s %-16s %s" % (probe_label(headers[0]), pkg, what)
        if found.get(key):
            line("ok", text)
        elif level == "required":
            line("bad", text)
        elif level == "wanted":
            line("warn", "missing: " + text)
        else:
            line("note", "%s, %s: %s" % ("missing", level, text))

    def missing_now():
        rows = (cc_rows if not ctx.cc else []) + [r for r in needed_tools if not which(r[0])]
        if ctx.cc:
            rows += missing_rows(found, ("required",))
        return rows, (missing_rows(found, ("wanted",)) if ctx.cc else [])
    req, want = missing_now()
    if (req or want or cc_problem) and install_deps:
        if not family:
            raise ToolError("--install-deps: no dnf, yum or apt-get here; install what is listed above yourself")
        say("Installing build dependencies (sudo)")
        if family == "apt":
            run(["sudo", "apt-get", "update"], interactive=True, check=False)
        for attempt in (1, 2):
            had_cc = bool(ctx.cc)
            if req:
                cmd = install_command(packages_for(req, family), mgr, family)
                info(cmd)
                run(shlex.split(cmd), interactive=True, check=False)
            # The wanted ones one at a time: on RHEL/Alma/Rocky mpdecimal-devel lives in
            # CRB, and in one strict transaction its absence would block everything.
            for row in want:
                cmd = install_command(packages_for([row], family), mgr, family)
                info(cmd)
                run(shlex.split(cmd), interactive=True, check=False)
            # The package manager's exit status is not trusted (an unrelated broken
            # repo makes dnf exit 1); what is now on disk decides.
            if not ctx.cc:
                ctx.cc = pick_cc(getattr(ctx.args, "cc", "") or "")
                ctx.cxx = cxx_for(ctx.cc) if ctx.cc else ""
            cc_problem = check_cc()
            found = check_deps(ctx.cc) if ctx.cc else {}
            req, want = missing_now()
            # A second pass only when this one brought the compiler: without it the
            # headers could not even be asked about.
            if had_cc or not ctx.cc or not (req or want):
                break
    if cc_problem:
        problems.append(cc_problem)
    if req:
        cmd = install_command(packages_for(req, family), mgr, family)
        problems.append("missing what the scraper's Python cannot do without: %s.%s" % (
            ", ".join(r[0] for r in req),
            ("\n  Install with:  " + cmd + "\n  (or re-run with --install-deps)") if cmd else
            "\n  Install the development packages that provide: " + ", ".join((r[1][0] if r[1] else r[0])
                                                                                  for r in req)))
    if want:
        cmd = install_command(packages_for(want, family), mgr, family)
        warn("building without %s.  To have them: %s%s" % (
            ", ".join(r[0] for r in want), cmd or "install their headers",
            "\n  (mpdecimal-devel is in the CodeReady Builder repository: %s)" % crb_command(osr)
            if rhel_like and family == "dnf" and any(r[0] == "mpdecimal" for r in want) else ""))
    fallback = missing_rows(found, ("fallback",)) if ctx.cc else []
    if fallback:
        cmd = install_command(packages_for(fallback, family), mgr, family)
        note("not needed while lxml publishes a wheel for this Python; if pip ever has to compile it: %s" % cmd)

    traps = env_traps()
    if traps:
        say("Your shell's build variables (the build does not see them)")
        for t in traps:
            note(t)
        py3 = shutil.which("python3")
        if py3:
            note("python3 on your PATH is %s -- this tool never uses it" % py3)

    say("Disk")
    prefix = ctx.prefix or expand(DEFAULT_OPT)
    where, why_prefix = writable_parent(os.path.dirname(prefix) if ctx.prefix else prefix)
    if why_prefix:
        problems.append("the prefix %s cannot be installed: %s" % (prefix, why_prefix))
        line("bad", problems[-1])
    for path, need in ((ctx.workdir, 3.0), (prefix, 1.0)):
        have = free_gb(path)
        if have < need:
            problems.append("only %.1f GB free at %s; %.0f GB is needed" % (have, path, need))
            line("bad", problems[-1])
        else:
            line("ok", "%.0f GB free at %s" % (have, path))
    if problems:
        raise ToolError("\n\n".join(problems))
    return found


def describe_venv(venv):
    vi = venv_info(venv)
    if not vi:
        return "none" if not os.path.exists(venv) else "not a venv (no pyvenv.cfg)"
    who = "made by this tool" if vi["marker"] else "not from this tool"
    return "%s -> %s (Python %s)%s, %s" % (venv, vi["home"] or "?", vi["version"] or "?",
                                          ", system site-packages ON" if vi["system_site"] else "", who)


def show_scraper(scraper):
    say("Scraper")
    if not os.path.isdir(scraper):
        warn("%s is not there (drive not mounted?) -- the interpreter can still be built; "
             "the scraper check needs it" % scraper)
        return None
    scan = scan_scraper(scraper)
    ok("%s: %d modules reachable from %s" % (scraper, len(scan["modules"]), ", ".join(scan["entries"])))
    if scan["missing_entries"]:
        note("entry points not found: %s" % ", ".join(scan["missing_entries"]))
    for rel, err in sorted(scan["errors"].items()):
        warn("%s does not parse: %s" % (rel, err))
    reqs = requirement_names(read_text(os.path.join(scraper, "requirements.txt")))
    if reqs:
        ok("requirements.txt: %s" % " ".join(reqs))
    else:
        warn("no requirements.txt in %s: the venv will hold no packages" % scraper)
    std = set(getattr(sys, "stdlib_module_names", ()))
    if std:
        third = [n for n in scan["externals"] if n not in std and n != "__future__"]
        norm = set(r.lower().replace("-", "_") for r in reqs)
        unlisted = [n for n in third if n.lower() not in norm]
        info("third-party imports in the live code: %s" % (" ".join(third) or "none"))
        if unlisted:
            warn("imported but not in requirements.txt (or listed under another name): %s" % " ".join(unlisted))
    info(".venv: %s" % describe_venv(os.path.join(scraper, ".venv")))
    return scan


def cmd_doctor(args):
    ctx = Ctx(args)
    try:
        rel, why, source = resolve_release(args)
        ctx = Ctx(args, rel, why, source)
    except ToolError as e:
        warn("cannot resolve a CPython version: %s" % str(e).splitlines()[0])
    try:
        preflight(ctx, install_deps=args.install_deps)
        verdict = None
    except ToolError as e:
        verdict = e
    say("Network")
    if ctx.version:
        ok("%s: CPython %s (%s)" % (ctx.source, ctx.version, ctx.why))
    for url in (CHECK_URL, PIP_PROBE_URL):
        try:
            http_get(url, timeout=20)
            ok("reachable: %s" % url)
        except ToolError as e:
            warn(str(e).splitlines()[0])
    show_scraper(ctx.scraper)
    say("Verdict")
    if verdict:
        bad("not ready:")
        out(str(verdict))
        return 1
    ok("ready.  `%s build` builds %s into %s" % (sys.argv[0], ctx.version or "CPython", ctx.prefix or "~/opt"))
    return 0


# ------------------------------------------------------------- build pipeline

def show_plan(ctx, system_mpdec):
    say("Plan")
    info("CPython      %s  (%s; from the %s)" % (ctx.version, ctx.why, ctx.source))
    sha = ctx.tar.get("sha256")
    info("source       %s" % ctx.tar.get("url", TARBALL_URL % {"v": ctx.version}))
    info("             %s" % ("sha256 %s (python.org)" % sha if sha else "no published checksum"))
    info("compiler     %s -- %s" % (ctx.cc, compiler_banner(ctx.cc) if ctx.cc else "?"))
    info("configure    %s" % " ".join(ctx.configure(system_mpdec)))
    info("jobs         %d" % ctx.jobs)
    info("work dir     %s  (logs: %s)" % (ctx.workdir, ctx.logs))
    info("prefix       %s%s" % (ctx.prefix, "  (exists)" if os.path.exists(ctx.prefix) else ""))
    if os.path.isdir(ctx.scraper):
        info("then         throwaway venv + pip install -r requirements.txt + the scraper smoke test")
        info(".venv        left alone: %s" % describe_venv(os.path.join(ctx.scraper, ".venv")))
    else:
        info("then         (no scraper check: %s is not there)" % ctx.scraper)


def is_venv_dir(path):
    return os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def prefix_record(prefix):
    """The record of an interpreter prefix this tool built, or None.  A build
    record always carries its configure line; a venv is never a prefix."""
    rec = read_json(os.path.join(prefix, MARKER))
    if not rec or "configure" not in rec or is_venv_dir(prefix):
        return None
    return rec


def prefix_version(prefix):
    rec = prefix_record(prefix) or {}
    if rec.get("version"):
        return rec["version"]
    try:
        p = run([os.path.join(prefix, "bin", "python3"), "-I", "-c",
                 "import sys; print('%d.%d.%d' % sys.version_info[:3])"], env=scrubbed_env(), check=False, timeout=60)
        return (p.stdout or "").strip()
    except ToolError:
        return ""


def same_series(a, b):
    return bool(numver(a)[:2]) and numver(a)[:2] == numver(b)[:2]


PYTHONISH = re.compile(r"^(python|pypy)[0-9.]*$")


def busy_processes(paths, cwd_roots=(), proc_root="/proc"):
    """[(pid, ppid, command line)] of processes that use one of `paths` -- in
    argv, as their executable, or as VIRTUAL_ENV -- or that run a python with
    their working directory inside one of `cwd_roots`.  The crawler gets started
    as `.venv/bin/python main.py` (a RELATIVE path, resolved here against each
    process's cwd without following the venv's symlink) and also, as seen on
    2026-10-03, as plain `python3.12 main.py` from the scraper directory."""
    def forms(p):
        return set([os.path.normpath(os.path.abspath(p)), os.path.realpath(p)])
    targets, roots = set(), set()
    for p in paths:
        targets |= forms(p)
    for p in cwd_roots:
        roots |= forms(p)

    def under(c, bases):
        return any(c == b or c.startswith(b + os.sep) for b in bases)
    me = os.getpid()
    hits = []
    try:
        pids = [d for d in os.listdir(proc_root) if d.isdigit()]
    except OSError:
        return hits
    for pid in pids:
        if int(pid) == me:
            continue
        base = os.path.join(proc_root, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except (OSError, IOError):
            continue
        if not argv:
            continue
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            cwd = ""
        try:
            exe = os.path.realpath(os.readlink(os.path.join(base, "exe")))
        except OSError:
            exe = ""
        cands = [exe] if exe else []
        for a in argv[:3]:
            if not a.startswith("-"):
                cands.append(os.path.normpath(a if os.path.isabs(a) or not cwd else os.path.join(cwd, a)))
        venv = ""
        try:
            with open(os.path.join(base, "environ"), "rb") as f:
                for kv in f.read().split(b"\0"):
                    if kv.startswith(b"VIRTUAL_ENV="):
                        venv = os.path.normpath(kv[12:].decode("utf-8", "replace"))
        except (OSError, IOError):
            pass
        pythonish = bool(PYTHONISH.match(os.path.basename(exe)) or PYTHONISH.match(os.path.basename(argv[0])))
        if any(under(c, targets) for c in cands) or (venv and under(venv, targets)) or \
                (pythonish and cwd and under(os.path.normpath(cwd), roots)):
            ppid = 0
            try:
                with open(os.path.join(base, "stat")) as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, IOError, IndexError, ValueError):
                pass
            hits.append((int(pid), ppid, " ".join(argv)[:160]))
    return hits


def busy_message(what, hits):
    """Who is using it, and how to stop it: SIGINT to the top of each process
    tree, which for the crawler means a clean shutdown it resumes from."""
    pids = set(h[0] for h in hits)
    tops = sorted(h[0] for h in hits if h[1] not in pids)
    return ("%s is in use:\n%s%s\n  Stop it first (the crawler resumes where it left off):\n    kill -INT %s"
            % (what, "\n".join("    pid %d: %s" % (h[0], h[2]) for h in sorted(hits)[:8]),
               "\n    ... %d more" % (len(hits) - 8) if len(hits) > 8 else "", " ".join(str(p) for p in tops)))


def fetch_and_unpack(ctx, state):
    say("Source")
    download(ctx.tar["url"], ctx.tarball, ctx.tar.get("sha256", ""), ctx.tar.get("size", 0))
    digest = sha256_file(ctx.tarball)
    stamp_file = os.path.join(ctx.src, ".unpacked-from-sha256")
    if os.path.isfile(os.path.join(ctx.src, "configure")) and read_text(stamp_file).strip() == digest:
        ok("already unpacked: %s" % ctx.src)
        return
    # A tree from ANOTHER tarball -- one replaced after a checksum mismatch, a
    # re-rolled release -- is not built: unpack again, and forget its build.
    for path in (ctx.src, ctx.build):
        if os.path.exists(path):
            shutil.rmtree(path)
    state.build(ctx.version).pop("make_done", None)
    state.save()
    extract(ctx.tarball, ctx.src)
    write_atomic(stamp_file, digest + "\n")
    ok("unpacked: %s" % ctx.src)


def do_configure(ctx, state, args_list, found):
    """Configured again when the options OR the libraries on the machine changed:
    `dnf install libzstd-devel` and build --rebuild has to rebuild WITH it."""
    say("Configure")
    record = state.build(ctx.version)
    stamp_file = os.path.join(ctx.build, ".configured-by-" + TOOL)
    stamp_text = json.dumps([args_list, sorted(k for k, v in found.items() if v)])
    if read_text(stamp_file) == stamp_text and os.path.isfile(os.path.join(ctx.build, "Makefile")):
        ok("already configured with these options and libraries: %s" % ctx.build)
        return
    if os.path.exists(ctx.build):
        shutil.rmtree(ctx.build)
    os.makedirs(ctx.build)
    log = os.path.join(ctx.logs, "configure.log")
    rc, secs = run_logged([os.path.join(ctx.src, "configure")] + args_list, ctx.build, scrubbed_env(),
                          log, "configure", verbose=ctx.args.verbose)
    if rc != 0:
        raise ToolError("configure failed (exit %d).  Its log: %s\n  and the details: %s\n%s"
                        % (rc, log, os.path.join(ctx.build, "config.log"), tail(read_text(log), 20)))
    write_atomic(stamp_file, stamp_text)
    record.pop("make_done", None)
    state.set_build(ctx.version, configure=args_list, configured=now())
    ok("configured in %s" % hms(secs))
    for line in read_text(log).splitlines():
        if "WARNING" in line:
            warn(line.replace("configure: WARNING: ", "configure says: "))


def do_make(ctx, state):
    say("Build (PGO + LTO)" if ctx.optimize else "Build (no PGO/LTO: --no-optimize)")
    record = state.build(ctx.version)
    exe = os.path.join(ctx.build, "python")
    if record.get("make_done") and os.access(exe, os.X_OK):
        ok("already built: %s" % exe)
        return module_report(read_text(os.path.join(ctx.logs, "make.log")))
    last = state.last_seconds()
    info("%d jobs.  %s" % (ctx.jobs, ("The last build here took %s." % hms(last)) if last else
                         ("Expect 10-25 minutes; most of it is the PGO training run, which is mostly one core."
                          if ctx.optimize else "A few minutes.")))
    log = os.path.join(ctx.logs, "make.log")
    rc, secs = run_logged(["make", "-j%d" % ctx.jobs], ctx.build, scrubbed_env(), log, "make",
                          verbose=ctx.args.verbose, phases=ctx.optimize)
    if rc != 0 or not os.access(exe, os.X_OK):
        errs = error_summary(log)
        raise ToolError("make failed (exit %d) after %s.  Log: %s\n  The first errors:\n%s"
                        % (rc, hms(secs), log, "\n".join(errs) or tail(read_text(log), 25)))
    rep = module_report(read_text(log))
    state.set_build(ctx.version, make_done=now(), make_seconds=secs, modules=rep)
    ok("built in %s" % hms(secs))
    return rep


def judge_modules(rep, found, family, mgr):
    """CPython's own report of modules it could not build -> raise if the
    scraper needs one of them, warn otherwise."""
    if rep.get("summary"):
        info(rep["summary"])
    lost = rep["missing"] + rep["failed"] + rep["removed"]
    needed = [m for m in lost if m in MODULES_REQUIRED or m in ("_ssl", "_hashlib", "_sqlite3", "zlib")]
    wanted = [m for m in lost if m in dict(MODULES_WANTED)]
    other = [m for m in lost if m not in needed and m not in wanted]
    rows = [r for r in DEPS if r[0] in set(MODULE_DEP.get(m) for m in needed + wanted)]
    cmd = install_command(packages_for(rows, family), mgr, family)
    if needed:
        raise ToolError("CPython built WITHOUT %s, which the scraper needs.%s"
                        % (", ".join(needed), ("\n  Install:  %s\n  then build again." % cmd) if cmd else ""))
    if wanted:
        warn("built without %s%s" % (", ".join(wanted), ("; to have them: %s and build --rebuild" % cmd) if cmd else ""))
    if other:
        note("not built (the scraper does not use them): %s" % " ".join(sorted(other)))


def do_install(ctx, state, rebuild, args_list, old=""):
    """make install into a DESTDIR stage, then ONE rename into the prefix: a prefix
    with the final name only ever holds a whole install.  -> where the previous
    build was set aside ("" if there was none); cmd_build removes it once the new
    one has proved itself, or puts it back."""
    say("Install")
    old = ""
    if os.path.lexists(ctx.prefix):
        # Only ever a tree this tool made -- cmd_build refuses the rest before
        # building; this is the same rule, checked again at the moment it matters.
        if not rebuild or not prefix_record(ctx.prefix):
            raise ToolError("%s exists and is not a build of this tool's to replace; not touching it" % ctx.prefix)
        busy = busy_processes([ctx.prefix])
        if busy:
            raise ToolError("cannot replace it: " + busy_message(ctx.prefix, busy))
        # Set aside BEFORE `make install`: its ensurepip runs the build tree's
        # python, which looks in <prefix>/lib/python3.X/site-packages, finds the
        # old build's pip "already satisfied", and stages a prefix with no pip.
        old = old or "%s.old-%s" % (ctx.prefix, stamp())
        os.rename(ctx.prefix, old)
    else:
        old = ""
    try:
        if os.path.exists(ctx.stage):
            shutil.rmtree(ctx.stage)
        log = os.path.join(ctx.logs, "install.log")
        rc, secs = run_logged(["make", "install", "DESTDIR=" + ctx.stage], ctx.build, scrubbed_env(), log,
                              "make install", verbose=ctx.args.verbose)
        staged = os.path.join(ctx.stage, ctx.prefix.lstrip(os.sep))
        if rc != 0 or not os.access(os.path.join(staged, "bin", "python3"), os.X_OK):
            raise ToolError("make install failed (exit %d).  Log: %s\n%s" % (rc, log, tail(read_text(log), 25)))
        if not glob.glob(os.path.join(glob.escape(staged), "lib", "python3*", "site-packages", "pip")):
            said = [line for line in read_text(log).splitlines() if "already satisfied" in line or "ensurepip" in line]
            raise ToolError("make install put no pip in the prefix: the interpreter would have no pip of its own.\n"
                            "  Log: %s\n%s" % (log, tail("\n".join(said) or read_text(log), 8)))
        # A provisional record, so that whatever stops the run between here and the
        # verification, the tree at the prefix is still recognisably this tool's.
        write_atomic(os.path.join(staged, MARKER), json.dumps(
            {"tool": "%s %s" % (TOOL, VERSION), "version": ctx.version, "configure": args_list,
             "state": "unverified", "built": now()}, indent=1, sort_keys=True) + "\n")
        size = dir_size(staged)
        os.makedirs(os.path.dirname(ctx.prefix), exist_ok=True)
        try:
            os.rename(staged, ctx.prefix)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            tmp = "%s.partial-%d" % (ctx.prefix, os.getpid())       # another filesystem: copy, then rename
            try:
                shutil.copytree(staged, tmp, symlinks=True)
                os.rename(tmp, ctx.prefix)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
    except BaseException as e:
        if old and not os.path.lexists(ctx.prefix):
            try:
                os.rename(old, ctx.prefix)
                note("the previous build is back in %s" % ctx.prefix)
            except OSError as e2:
                raise ToolError("could not put the previous build back (%s).  It is intact at %s:\n    mv %s %s"
                                % (e2, old, shlex.quote(old), shlex.quote(ctx.prefix)))
        if isinstance(e, OSError):
            raise ToolError("cannot install into %s: %s" % (ctx.prefix, e))
        raise
    ok("installed %s (%s)" % (ctx.prefix, human(size)))
    return old


VERIFY_SCRIPT = r'''
import importlib, json, sys, sysconfig
groups = json.loads(sys.argv[1])
res = {"version": sys.version.split()[0], "executable": sys.executable, "prefix": sys.prefix,
       "missing": {}, "errors": {}}
for group in sorted(groups):
    res["missing"][group] = []
    for name in groups[group]:
        try:
            importlib.import_module(name)
        except Exception as e:
            res["missing"][group].append(name)
            res["errors"][name] = "%s: %s" % (type(e).__name__, e)
try:
    import sqlite3
    res["sqlite"] = sqlite3.sqlite_version
    res["sqlite_extensions"] = hasattr(sqlite3.Connection, "enable_load_extension")
except Exception:
    pass
try:
    import ssl
    res["openssl"] = ssl.OPENSSL_VERSION
    paths = ssl.get_default_verify_paths()
    res["cafile"] = paths.cafile or paths.openssl_cafile or ""
except Exception:
    pass
res["cc"] = sysconfig.get_config_var("CC") or ""
res["config_args"] = sysconfig.get_config_var("CONFIG_ARGS") or ""
print(json.dumps(res))
'''

SYSTEM_LIB_DIRS = ("/lib64/", "/usr/lib64/", "/lib/", "/usr/lib/")


def ldd_map(path, env):
    p = run(["ldd", path], env=env, check=False, timeout=60)
    libs = {}
    for line in (p.stdout or "").splitlines():
        m = re.match(r"\s*(\S+)\s+=>\s+(not found|\S+)", line)
        if m:
            libs[m.group(1)] = m.group(2)
    return libs


def elf_facts(path):
    facts = {"runpath": [], "needed": [], "comment": []}
    if not which("readelf"):
        return facts
    p = run(["readelf", "-d", path], env=scrubbed_env(), check=False, timeout=60)
    for line in (p.stdout or "").splitlines():
        m = re.search(r"\((?:RPATH|RUNPATH)\)\s+Library r(?:un)?path: \[(.*)\]", line)
        if m:
            facts["runpath"].append(m.group(1))
        m = re.search(r"\(NEEDED\)\s+Shared library: \[(.*)\]", line)
        if m:
            facts["needed"].append(m.group(1))
    p = run(["readelf", "-p", ".comment", path], env=scrubbed_env(), check=False, timeout=60)
    for line in (p.stdout or "").splitlines():
        m = re.match(r"\s*\[\s*[0-9a-f]+\]\s+(.*)", line)
        if m and m.group(1).strip() not in facts["comment"]:
            facts["comment"].append(m.group(1).strip())
    return facts


def verify_interpreter(prefix, offline=False, quiet=False):
    """One place that decides whether an installed interpreter is usable.
    -> (good, summary dict).  Prints as it goes unless quiet."""
    show = not quiet
    py = os.path.join(prefix, "bin", "python3")
    env = scrubbed_env()
    good = True
    summary = {"when": now(), "prefix": prefix}

    def fail(msg):
        if show:
            bad(msg)
        summary.setdefault("failures", []).append(msg)
        return False

    if not os.access(py, os.X_OK):
        fail("no executable %s" % py)
        return False, summary
    p = run([py, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"], env=env, check=False,
            timeout=60)
    ver = numver(p.stdout or "")
    groups = {"required": list(MODULES_REQUIRED),
              "wanted": [m for m, since in MODULES_WANTED if ver[:2] >= since],
              "optional": list(MODULES_OPTIONAL)}
    p = run([py, "-I", "-c", VERIFY_SCRIPT, json.dumps(groups)], env=env, check=False, timeout=300)
    try:
        res = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        fail("the interpreter cannot run its own module check (exit %d):\n%s" % (p.returncode, tail(p.stdout)))
        return False, summary
    summary.update(version=res["version"], sqlite=res.get("sqlite"), openssl=res.get("openssl"),
                   cc=res.get("cc"), config_args=res.get("config_args"), missing=res["missing"])
    if show:
        ok("Python %s at %s" % (res["version"], py))
    if res["missing"].get("required"):
        good = fail("REQUIRED modules missing: %s" % "; ".join(
            "%s (%s)" % (m, res["errors"].get(m, "")) for m in res["missing"]["required"]))
    elif show:
        ok("%d required modules import (ssl, hashlib, sqlite3, zlib, bz2, lzma, ctypes, multiprocessing, ...)"
           % len(groups["required"]))
    for m in res["missing"].get("wanted", []):
        if show:
            warn("%s missing: %s" % (m, dict(DEPS_BY_KEY).get(MODULE_DEP.get(m, ""), "")))
    if show and res["missing"].get("optional"):
        note("optional modules missing: %s" % " ".join(res["missing"]["optional"]))
    if show and res.get("sqlite"):
        ok("SQLite %s%s" % (res["sqlite"], ", loadable extensions on" if res.get("sqlite_extensions") else ""))

    # ssl must not merely import: built against one OpenSSL and loading another,
    # it imports fine and then SEGFAULTs here (the .sh's lesson, from PyPy).
    p = run([py, "-I", "-c", "import ssl; ssl.create_default_context()"], env=env, check=False, timeout=60)
    if p.returncode < 0 or p.returncode > 128:
        sig = -p.returncode if p.returncode < 0 else p.returncode - 128
        good = fail("ssl CRASHED (signal %d) building a default context: its OpenSSL bindings are broken" % sig)
    elif p.returncode != 0:
        good = fail("ssl imports but cannot create a default context:\n%s" % tail(p.stdout))
    elif show:
        ok("%s, default context ok, CA file %s" % (res.get("openssl", "OpenSSL"), res.get("cafile") or "?"))
    if res.get("cafile") and not os.path.exists(res["cafile"]) and show:
        note("the CA file OpenSSL names does not exist; requests uses certifi's bundle, so the scraper is unaffected")

    p = run([py, "-I", "-m", "pip", "--version"], env=env, check=False, timeout=120)
    if p.returncode != 0:
        good = fail("pip does not run:\n%s" % tail(p.stdout))
    else:
        summary["pip"] = (p.stdout or "").split(" from ")[0].strip()
        if show:
            ok("its own pip: %s" % summary["pip"])

    if not offline:
        try:
            p = run([py, "-I", "-c", "import urllib.request; r = urllib.request.urlopen(%r, timeout=30); "
                     "print(r.getcode())" % PIP_PROBE_URL], env=env, check=False, timeout=150)
            answer = (p.stdout or "") if p.returncode == 0 else "exit %d\n%s" % (p.returncode, p.stdout or "")
        except ToolError as e:                  # a timeout: the network, not the interpreter
            answer = str(e)
        if answer.strip() == "200":
            if show:
                ok("HTTPS with the system CA store: %s answered" % PIP_PROBE_URL)
        elif show:
            warn("HTTPS to %s failed (network? proxy?):\n%s" % (PIP_PROBE_URL, tail(answer, 6)))

    exe = os.path.realpath(py)
    facts = elf_facts(exe)
    summary["comment"] = facts["comment"]
    if facts["runpath"]:
        if show:
            warn("%s carries RPATH/RUNPATH %s -- something leaked into the link" % (exe, facts["runpath"]))
    elif show and which("readelf"):
        ok("no RPATH/RUNPATH in the binary (nothing leaked from LD_RUN_PATH)")
    if show and facts["comment"]:
        info("     built by: %s" % "; ".join(facts["comment"]))
    if which("ldd"):
        libdir = glob.glob(os.path.join(glob.escape(prefix), "lib", "python3.*", "lib-dynload"))
        targets = [exe]
        for mod in ("_ssl", "_hashlib", "_sqlite3", "_ctypes", "zlib", "_bz2", "_lzma", "_decimal", "_zstd"):
            targets += glob.glob(os.path.join(glob.escape(libdir[0]), mod + ".*.so")) if libdir else []
        missing_libs, foreign, shell_diff = [], [], []
        for t in targets:
            clean = ldd_map(t, env)
            for lib, where in sorted(clean.items()):
                if where == "not found":
                    missing_libs.append("%s needs %s" % (os.path.basename(t), lib))
                elif not where.startswith(SYSTEM_LIB_DIRS):
                    foreign.append("%s -> %s" % (lib, where))
            mine = ldd_map(t, dict(os.environ))
            for lib, where in sorted(mine.items()):
                if clean.get(lib) and where != clean[lib]:
                    shell_diff.append("%s: %s (instead of %s)" % (lib, where, clean[lib]))
        if missing_libs:
            good = fail("shared libraries not found: %s" % "; ".join(sorted(set(missing_libs))))
        elif show:
            ok("every library the interpreter and its C modules load is found (%d checked)" % len(targets))
        if foreign and show:
            warn("libraries from outside the system directories: %s" % "; ".join(sorted(set(foreign))))
        if shell_diff and show:
            note("from YOUR shell (LD_LIBRARY_PATH) these resolve elsewhere: %s"
                 % "; ".join(sorted(set(shell_diff))))
    summary["good"] = good
    return good, summary



def write_build_info(ctx, args_list, verify_summary, rep):
    """<prefix>/.python_build_tool.json (what `build` and `status` read back) and
    BUILD-INFO.txt (for people)."""
    rec = {"tool": "%s %s" % (TOOL, VERSION), "for": ctx.scraper, "version": ctx.version,
           "source": ctx.tar.get("url"), "sha256": ctx.tar.get("sha256") or "",
           "configure": args_list, "built": now(), "compiler": "%s (%s)" % (ctx.cc, compiler_banner(ctx.cc)),
           "optimized": ctx.optimize, "modules": rep, "verify": verify_summary, "state": "verified"}
    write_atomic(os.path.join(ctx.prefix, MARKER), json.dumps(rec, indent=1, sort_keys=True) + "\n")
    lines = ["Built by %s for %s" % (rec["tool"], ctx.scraper),
             "CPython   %s" % ctx.version,
             "source    %s" % rec["source"],
             "sha256    %s" % (rec["sha256"] or "(python.org published none)"),
             "compiler  %s" % rec["compiler"],
             "configure %s" % " ".join(args_list),
             "built     %s" % rec["built"],
             "modules   %s" % (rep.get("summary") or "?"),
             "missing   %s" % (" ".join(rep.get("missing", [])) or "none"),
             "verify    %s" % ("ok" if verify_summary.get("good") else "FAILED"),
             ""]
    write_atomic(os.path.join(ctx.prefix, "BUILD-INFO.txt"), "\n".join(lines))


def cmd_plan(args):
    rel, why, source = resolve_release(args)
    ctx = Ctx(args, rel, why, source)
    ctx.tar = tarball_info(rel)
    found = preflight(ctx)
    show_plan(ctx, found.get("mpdecimal", False))
    show_scraper(ctx.scraper)
    return 0


def own_flags(ctx):
    """The options a follow-up command needs to find exactly what this one made
    (`venv` alone would take the NEWEST build, which need not be this one)."""
    if ctx.prefix != default_prefix(ctx.version, ctx.scraper):
        flags = ["--prefix", ctx.prefix]
    else:
        flags = ["--version", ctx.version]
    if ctx.workdir != expand(DEFAULT_WORKDIR):
        flags += ["--workdir", ctx.workdir]
    if ctx.scraper != expand(DEFAULT_SCRAPER):
        flags += ["--scraper", ctx.scraper]
    return flags


def verified(rec):
    return bool(rec) and rec.get("state") != "unverified" and bool((rec.get("verify") or {}).get("good"))


def cmd_build(args):
    started = time.time()
    rel, why, source = resolve_release(args)
    ctx = Ctx(args, rel, why, source)
    lock_dir(ctx.workdir)
    state = State(os.path.join(ctx.workdir, "state.json"))
    state.save()                                # stamped as this tool's from the first moment (see clean)
    ctx.tar = tarball_info(rel)
    found = preflight(ctx, install_deps=args.install_deps)
    mgr, family = pkg_manager()
    system_mpdec = bool(found.get("mpdecimal"))
    args_list = ctx.configure(system_mpdec)
    show_plan(ctx, system_mpdec)

    # What is at the prefix decides what may happen to it.  A directory this tool
    # did not make is never moved or deleted -- not even with --rebuild.
    have = prefix_record(ctx.prefix) if os.path.lexists(ctx.prefix) else None
    reuse = False
    venv = os.path.join(ctx.scraper, ".venv")
    vi = venv_info(venv)
    venv_on_prefix = bool(vi) and same_path(vi["home"], os.path.join(ctx.prefix, "bin"))
    if os.path.lexists(ctx.prefix):
        if is_venv_dir(ctx.prefix):
            raise ToolError("%s is a venv, not an interpreter prefix: `build` never replaces it.  Choose "
                            "another --prefix." % ctx.prefix)
        if not have:
            raise ToolError("%s exists and was not made by this tool.  It is never moved or deleted: remove "
                            "it yourself if it should go, or choose another --prefix." % ctx.prefix)
        if args.rebuild:
            if venv_on_prefix and not same_series(vi["version"], ctx.version):
                raise ToolError("the scraper's .venv is made from %s (Python %s).  Rebuilding that prefix as %s "
                                "would leave the venv without its packages: a venv belongs to ONE X.Y series.\n"
                                "  Build %s into another --prefix (the default names carry the version), then "
                                "`venv --replace`." % (ctx.prefix, vi["version"], ctx.version, ctx.version))
            tried = not args.no_check and os.path.isdir(ctx.scraper)
            note("--rebuild: %s is replaced once the new build has verified%s" % (
                ctx.prefix, " and passed the smoke test" if tried else ""))
        elif have.get("state") == "unverified":
            raise ToolError("%s holds an install by this tool that was never verified (a run that stopped "
                            "half-way).  Pass --rebuild to replace it." % ctx.prefix)
        elif have.get("version") != ctx.version:
            raise ToolError("%s holds CPython %s, not %s.  Pass --rebuild to replace it, or choose another "
                            "--prefix." % (ctx.prefix, have.get("version", "?"), ctx.version))
        elif have.get("configure") != args_list:
            raise ToolError("%s was built with other options:\n    %s\n  Pass --rebuild to replace it."
                            % (ctx.prefix, " ".join(have.get("configure") or [])))
        else:
            say("Already built")
            ok("%s (%s) -- verifying it instead of rebuilding" % (ctx.prefix, have.get("built", "?")))
            reuse = True
        if args.rebuild and venv_on_prefix:
            note("the scraper's .venv is made from this prefix: after the rebuild it runs on the new build")

    # cmd_build names the set-aside path itself, so that whatever interrupts the
    # install, the rollback below knows where the previous build is.
    old = "%s.old-%s" % (ctx.prefix, stamp()) if not reuse and os.path.lexists(ctx.prefix) else ""
    if reuse:
        rep = have.get("modules") or {}
    else:
        fetch_and_unpack(ctx, state)
        do_configure(ctx, state, args_list, found)
        rep = do_make(ctx, state)
        judge_modules(rep, found, family, mgr)

    def roll_back():
        """The new tree to <prefix>.failed-<date>, the previous build (if any)
        back to the prefix.  -> what happened, for the error message."""
        if reuse or (old and not os.path.lexists(old)):
            return ""           # nothing of the previous build was moved: nothing to undo
        failed = "%s.failed-%s" % (ctx.prefix, stamp())
        try:
            if os.path.lexists(ctx.prefix):
                os.rename(ctx.prefix, failed)
            if old:
                os.rename(old, ctx.prefix)
        except OSError as e:
            return ("\n  Putting things back failed (%s): the new build is at %s, the previous one at %s"
                    % (e, failed if os.path.lexists(failed) else ctx.prefix, old or "(none)"))
        return "\n  The new build is kept for inspection at %s%s." % (
            failed, "; the previous build is back in %s" % ctx.prefix if old else "")

    # Phase 1: from the install until the interpreter has verified, ANY failure
    # (a timeout, ^C, a full disk) puts things back the way they were.
    try:
        if not reuse:
            do_install(ctx, state, args.rebuild, args_list, old)
        say("Verify the interpreter")
        good, vsum = verify_interpreter(ctx.prefix, offline=args.offline)
        if reuse:
            have["verify"] = vsum               # a failure is recorded too: `venv` will not use it
            write_atomic(os.path.join(ctx.prefix, MARKER), json.dumps(have, indent=1, sort_keys=True) + "\n")
        if not good:
            raise ToolError("%s does not verify (see above)%s" % (
                "the new interpreter" if not reuse else ctx.prefix,
                "" if not reuse else ".  Rebuild it: build --rebuild"))
        if not reuse:
            write_build_info(ctx, args_list, vsum, rep)
        state.set_build(ctx.version, installed=ctx.prefix, verified=vsum.get("when"), configure=args_list)
    except BaseException as e:
        extra = roll_back()
        if isinstance(e, ToolError):
            raise ToolError(str(e) + extra)
        out(extra)
        raise
    shutil.rmtree(ctx.stage, ignore_errors=True)

    # Phase 2: the scraper.  A failure here says nothing against a FRESH build
    # (pip may have lost the network), so that stays; a --rebuild's previous
    # build, which was working, comes back.
    result = None
    try:
        if args.no_check:
            note("--no-check: the scraper was not tried")
        elif not os.path.isdir(ctx.scraper):
            warn("%s is not there (drive not mounted?): the scraper was not tried.  Once it is, run `build` "
                 "again WITHOUT --rebuild -- it re-verifies this interpreter instead of rebuilding it, then tries "
                 "the scraper." % ctx.scraper)
        else:
            say("Can it run the scraper?  (throwaway venv: %s)" % ctx.check_venv)
            if os.path.exists(ctx.check_venv):
                shutil.rmtree(ctx.check_venv)
            vpy = create_venv(ctx.python(), ctx.check_venv)
            pip_sync(vpy, ctx.scraper, os.path.join(ctx.logs, "pip-check-venv.log"), args.constraints)
            result = run_check(vpy, ctx.scraper, ctx.workdir, offline=args.offline, keep=args.keep,
                               label="build-check")
            state.add_check({"when": now(), "python": vpy, "version": ctx.version, "status": result["status"]})
            if result["status"] == "fail" and old:
                raise ToolError("the scraper's smoke test failed with the new build")
            if result["status"] != "fail":
                shutil.rmtree(ctx.check_venv, ignore_errors=True)
    except BaseException as e:
        if old:
            extra = roll_back()
            if isinstance(e, ToolError):
                raise ToolError(str(e) + extra)
            out(extra)
        raise
    if old and os.path.lexists(old):
        if result is not None or args.no_check:
            shutil.rmtree(old, ignore_errors=True)
            note("removed the previous build")
        else:
            warn("the previous build is kept at %s until the scraper's smoke test has passed with the new one: "
                 "mount the drive, run `build` (without --rebuild), then:  rm -rf %s" % (old, shlex.quote(old)))

    say("Done in %s" % hms(time.time() - started))
    info("interpreter  %s  (%s)" % (ctx.python(), vsum.get("version", ctx.version)))
    info("its pip      %s -m pip   (or %s)" % (ctx.python(), os.path.join(ctx.prefix, "bin", "pip3")))
    info("build notes  %s" % os.path.join(ctx.prefix, "BUILD-INFO.txt"))
    if result and result["status"] == "fail":
        bad("the scraper smoke test FAILED with this interpreter -- see above; the venv is kept at %s"
            % ctx.check_venv)
        return 1
    if result:
        ok("the scraper's smoke test passed with it")
        vi = venv_info(venv)
        if vi and same_path(vi["home"], os.path.join(ctx.prefix, "bin")) and same_series(vi["version"],
                                                                                          vsum.get("version", "")):
            info("the scraper's .venv already uses it.")
        else:
            info("next: give the scraper a .venv made from it%s:" % (
                " (the current one is kept as .venv.replaced-<date>)" if vi else ""))
            info("    " + " ".join(shlex.quote(a) for a in [sys.argv[0], "venv"] + (["--replace"] if vi else [])
                                    + own_flags(ctx)))
            info("then run the scraper with that venv's python (not a bare python3 / python3.12):")
            info("    cd %s && .venv/bin/python main.py" % ctx.scraper)
    return 0


# ------------------------------------------------------------------------ venvs

def venv_info(venv):
    cfg_path = os.path.join(venv, "pyvenv.cfg")
    if not os.path.isfile(cfg_path):
        return None
    cfg = {}
    for line in read_text(cfg_path).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip().lower()] = v.strip()
    return {"home": cfg.get("home", ""), "version": cfg.get("version") or cfg.get("version_info", ""),
            "system_site": cfg.get("include-system-site-packages", "false").lower() == "true",
            "marker": read_json(os.path.join(venv, VENV_MARKER)), "python": os.path.join(venv, "bin", "python")}


def create_venv(python, target):
    p = run([python, "-I", "-m", "venv", target], env=scrubbed_env(), check=False, timeout=900)
    vpy = os.path.join(target, "bin", "python")
    if p.returncode != 0 or not os.access(vpy, os.X_OK):
        raise ToolError("`python -m venv %s` failed (exit %d):\n%s" % (target, p.returncode, tail(p.stdout)))
    ok("venv created: %s" % target)
    return vpy


def requirement_names(text):
    names = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            names.append(m.group(1))
    return names


def pip_hints(text):
    hints = []
    if re.search(r"lxml", text) and re.search(r"(libxml2|libxslt|xslt-config|xml2-config)", text):
        hints.append("lxml had to be compiled (no wheel for this Python) and needs its headers: "
                     "sudo dnf install libxml2-devel libxslt-devel")
    if "Could not find a version that satisfies" in text or "No matching distribution" in text:
        hints.append("a package has no release for this Python, or PyPI was not reachable")
    if re.search(r"SSL|CERTIFICATE_VERIFY_FAILED|certificate verify failed", text):
        hints.append("pip could not verify PyPI's certificate: a proxy? set PIP_CERT or SSL_CERT_FILE")
    if "Python.h" in text:
        hints.append("a C extension could not find Python.h -- is the venv made from this tool's interpreter?")
    return "".join("\n  hint: " + h for h in hints)


def pip_sync(vpy, scraper, log_path, constraints=""):
    """pip install -r requirements.txt, wheels preferred; `constraints` pins
    versions (pip -c), e.g. to the ones the old environment had.  The environment
    is scrubbed like the build's: if something has to be compiled, it is compiled
    by the compiler the interpreter recorded, not by $CC from your shell."""
    req = os.path.join(scraper, "requirements.txt")
    env = scrubbed_env(extra_path=[os.path.dirname(vpy)], keep=PIP_KEEP, extra={"LC_ALL": "C.UTF-8"})
    if not os.path.isfile(req):
        warn("no %s -- nothing to install" % req)
        return {}
    cmd = [vpy, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary", "-r", req]
    if constraints:
        constraints = expand(constraints)
        if not os.path.isfile(constraints):
            raise ToolError("--constraints %s: no such file" % constraints)
        cmd += ["-c", constraints]
    info("pip install -r %s%s" % (req, "  -c " + constraints if constraints else ""))
    rc, secs = run_logged(cmd, scraper, env, log_path, "pip")
    text = read_text(log_path)
    if rc != 0:
        raise ToolError("pip install failed (exit %d).  Log: %s\n%s%s" % (rc, log_path, tail(text, 25), pip_hints(text)))
    for line in text.splitlines():
        if line.startswith("Successfully installed"):
            words = line.split()
            info(" ".join(words[:2]))
            for i in range(2, len(words), 6):
                info("    " + " ".join(words[i:i + 6]))
    built = sorted(set(re.findall(r"Building wheel for (\S+)", text)))
    if built:
        note("compiled from source (no wheel for this Python): %s" % " ".join(built))
    p = run([vpy, "-m", "pip", "check", "--disable-pip-version-check"], env=env, check=False, timeout=300)
    if p.returncode == 0:
        ok("pip check: no broken requirements")
    else:
        warn("pip check:\n%s" % tail(p.stdout, 15))
    freeze = run([vpy, "-m", "pip", "freeze", "--disable-pip-version-check", "--all"], env=env, check=False,
                 timeout=300).stdout or ""
    lock = os.path.join(os.path.dirname(os.path.dirname(vpy)), "requirements.lock.txt")
    write_atomic(lock, "# pip freeze after `%s` installed %s, %s\n%s" % (TOOL, req, now(), freeze))
    ok("packages in %s; exact versions recorded in %s" % (hms(secs), lock))
    return {"seconds": secs, "lock": lock}


def restore_venv(target, aside):
    """Put the venv that was set aside back; the new one goes to .failed-<date>."""
    failed = "%s.failed-%s" % (target, stamp())
    try:
        if os.path.lexists(target):
            os.rename(target, failed)
        os.rename(aside, target)
    except OSError as e:
        raise ToolError("could not put the old venv back (%s).  It is intact at %s -- move it back with:\n"
                        "    mv %s %s" % (e, aside, shlex.quote(aside), shlex.quote(target)))
    warn("put the old venv back at %s%s" % (target, (" (the new one is at %s)" % failed)
                                               if os.path.lexists(failed) else ""))


def newest_built_prefix(scraper):
    found = []
    for path in glob.glob(os.path.join(glob.escape(expand(DEFAULT_OPT)), "cpython-*-%s" % scraper_tag(scraper))):
        rec = prefix_record(path)
        if rec and os.access(os.path.join(path, "bin", "python3"), os.X_OK):
            found.append((numver(rec.get("version", "")), path, rec))
    found.sort(reverse=True)
    return found


def pick_prefix(args, scraper):
    want = getattr(args, "version", None)
    if getattr(args, "prefix", None):
        prefix = expand(args.prefix)
    elif want and re.match(r"^\d+\.\d+\.\d+$", want):
        prefix = default_prefix(want, scraper)
    elif want and not re.match(r"^\d+\.\d+$", want):
        raise ToolError("--version takes X.Y or X.Y.Z of a BUILT interpreter here (got %r)" % want)
    else:
        built = [b for b in newest_built_prefix(scraper) if verified(b[2])]
        if want:
            built = [b for b in built if b[0][:2] == numver(want)]
        if not built:
            raise ToolError("no verified %sinterpreter built for %s under %s.  Run `build%s` first."
                            % ("%s.x " % want if want else "", scraper_tag(scraper), expand(DEFAULT_OPT),
                               " --version " + want if want else ""))
        prefix = built[0][1]
    if is_venv_dir(prefix):
        raise ToolError("%s is a venv, not an interpreter this tool built: --prefix wants e.g. %s"
                        % (prefix, default_prefix("X.Y.Z", scraper)))
    if not os.access(os.path.join(prefix, "bin", "python3"), os.X_OK):
        raise ToolError("%s has no bin/python3 -- build it first" % prefix)
    rec = prefix_record(prefix)
    if not rec:
        warn("%s was not built by this tool; using it anyway" % prefix)
    elif not verified(rec):
        raise ToolError("%s is an install this tool never verified.  `build --rebuild` it first." % prefix)
    return prefix


def cmd_venv(args):
    scraper = expand(args.scraper)
    if not os.path.isdir(scraper):
        raise ToolError("%s is not there (drive not mounted?)" % scraper)
    workdir = expand(args.workdir)
    lock_dir(workdir)
    state = State(os.path.join(workdir, "state.json"))
    state.save()
    prefix = pick_prefix(args, scraper)
    py = os.path.join(prefix, "bin", "python3")
    target = expand(args.venv) if args.venv else os.path.join(scraper, ".venv")
    logs = os.path.join(workdir, "logs", "venv")
    say("The scraper's venv")
    info("interpreter  %s" % py)
    info("venv         %s" % describe_venv(target))
    vi = venv_info(target)
    if os.path.exists(target) and not vi:
        raise ToolError("%s exists but is not a venv (no pyvenv.cfg); not touching it" % target)
    ours = bool(vi) and same_path(vi["home"], os.path.join(prefix, "bin")) and \
        same_series(vi["version"], prefix_version(prefix))
    if vi and not ours and not args.replace:
        raise ToolError("%s is a different Python's venv (%s, Python %s%s).\n"
                        "  Re-run with --replace: it is renamed to %s.replaced-<date> (kept, not deleted),\n"
                        "  a new one is made from %s, and if the smoke test fails the old one is put back."
                        % (target, vi["home"], vi["version"], ", system site-packages" if vi["system_site"] else "",
                           os.path.basename(target), prefix))
    if vi:
        # Replacing a venv, or changing its packages, under a running crawler is
        # not done -- however the crawler was started.
        inside = target.startswith(scraper + os.sep)
        busy = busy_processes([target], cwd_roots=[scraper] if inside else [])
        if busy:
            raise ToolError(busy_message("the scraper (or its venv)", busy))
    fresh = not os.path.exists(target)
    aside = ""
    if vi and not ours:
        aside = "%s.replaced-%s" % (target, stamp())
        os.rename(target, aside)
    try:
        if aside:
            ok("old venv kept as %s" % aside)
        if ours:
            ok("already made from %s: bringing its packages in line with requirements.txt" % prefix)
            vpy = vi["python"]
        else:
            vpy = create_venv(py, target)
        pip_sync(vpy, scraper, os.path.join(logs, "pip-%s.log" % stamp()), args.constraints)
        rec = {"tool": "%s %s" % (TOOL, VERSION), "prefix": prefix, "made": now(),
               "replaced": aside}
        if not ours:
            write_atomic(os.path.join(target, VENV_MARKER), json.dumps(rec, indent=1, sort_keys=True) + "\n")
        say("Smoke test")
        result = run_check(vpy, scraper, workdir, offline=args.offline, keep=args.keep, label="venv-check")
    except BaseException:
        # Put things back FIRST; printing comes after, and cannot stop it.
        if aside:
            restore_venv(target, aside)
        elif fresh and os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
            note("removed the half-made %s" % target)
        raise
    if result["status"] == "fail" and aside:
        restore_venv(target, aside)
    try:
        state.add_check({"when": now(), "python": vpy, "venv": target, "status": result["status"]})
    except (OSError, IOError) as e:
        warn("could not record the smoke test in %s: %s" % (state.path, e))
    if result["status"] == "fail":
        bad("the smoke test failed with the new venv (see above)")
        return 1
    say("Done")
    ok("%s runs the scraper with %s" % (target, py))
    run_with = ".venv/bin/python" if target == os.path.join(scraper, ".venv") else vpy
    info("run the scraper with the venv's python:   cd %s && %s main.py" % (scraper, run_with))
    info("(a bare python3 / python3.12 is NOT this venv unless it is activated: `source %s/bin/activate`)"
         % (os.path.relpath(target, scraper) if target.startswith(scraper + os.sep) else target))
    if aside:
        info("the old venv is at %s -- delete it when you are happy:  rm -rf %s" % (aside, shlex.quote(aside)))
    return 0


# --------------------------------------------------------------- scraper scan

def skip_dir(name, top):
    if name in ("__pycache__", ".git", ".pytest_cache", ".mypy_cache"):
        return True
    return top and (name in DATA_DIRS or name.startswith(".venv") or name.startswith("old_crawl_"))


def py_files(root):
    """{relative path: absolute path} of the .py files that are code, not data."""
    found = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        top = dirpath == root
        dirnames[:] = sorted(d for d in dirnames
                             if not skip_dir(d, top) and not os.path.islink(os.path.join(dirpath, d)))
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if fn.endswith(".py") and os.path.isfile(p):      # not a dangling link, a pipe, an editor lock
                found[os.path.relpath(p, root)] = p
    return found


def module_name(rel):
    m = rel[:-3].replace(os.sep, ".")
    return m[:-len(".__init__")] if m.endswith(".__init__") else m


def imports_of(path, rel):
    """[(absolute module name, line)] imported ANYWHERE in the file -- function
    bodies too, since a lazy import still has to work -- with relative imports
    resolved against the file's package."""
    with open(path, "rb") as f:
        tree = ast.parse(f.read(), path)
    pkg = module_name(rel).split(".")
    if not rel.endswith("__init__.py"):
        pkg = pkg[:-1]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.append((a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                cut = node.level - 1
                if cut > len(pkg):
                    continue
                base = pkg[:len(pkg) - cut]
                if node.module:
                    found.append((".".join(base + [node.module]), node.lineno))
                else:
                    for a in node.names:
                        found.append((".".join(base + [a.name]), node.lineno))
            elif node.module:
                found.append((node.module, node.lineno))
                for a in node.names:
                    if a.name != "*":
                        found.append((node.module + "." + a.name, node.lineno))
    return found


def local_file(name, files):
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        cand = os.path.join(*parts[:i])
        if cand + ".py" in files:
            return cand + ".py"
        if os.path.join(cand, "__init__.py") in files:
            return os.path.join(cand, "__init__.py")
    return None


def scan_scraper(root, entries=ENTRY_POINTS):
    """What the scraper IS: the files its entry points reach, and every module
    from outside it that they import."""
    files = py_files(root)
    tops = set()
    for rel in files:
        first = rel.split(os.sep)[0]
        tops.add(first[:-3] if first.endswith(".py") else first)
    present = [e for e in entries if e in files]
    imports, errors = {}, {}

    def imps(rel):
        if rel not in imports:
            try:
                imports[rel] = imports_of(files[rel], rel)
            except (SyntaxError, ValueError, OSError, IOError) as e:
                errors[rel] = str(e)
                imports[rel] = []
        return imports[rel]

    live, todo = set(), list(present)
    while todo:
        rel = todo.pop()
        if rel in live:
            continue
        live.add(rel)
        parts = rel.split(os.sep)
        for i in range(1, len(parts)):
            init = os.path.join(*(parts[:i] + ["__init__.py"]))
            if init in files and init not in live:
                todo.append(init)
        for name, _ in imps(rel):
            if name.split(".")[0] in tops:
                lf = local_file(name, files)
                if lf and lf not in live:
                    todo.append(lf)
    externals = sorted(set(name.split(".")[0] for rel in live for name, _ in imps(rel)) - tops)
    return {"entries": present, "missing_entries": [e for e in entries if e not in files],
            "modules": sorted(module_name(rel) for rel in live), "externals": externals,
            "errors": dict((r, e) for r, e in errors.items() if r in live), "files": len(files)}


# ------------------------------------------------------------- the smoke test

CHECK_NAME = "_python_build_tool_check.py"
CHECK_MARK = "@@PYTHON_BUILD_TOOL_CHECK@@"
CHECK_SCRIPT = r'''# Written by enterprise_python_tool.py into a THROWAWAY COPY of the scraper's code
# and run by the interpreter under test: can this interpreter run the scraper?
# The real scraper directory is only READ (a few saved pages from html/, and its
# disk space); its database is never opened.
import json
import os
import sys
import time
import traceback

RESULTS = []


def rec(name, status, detail=""):
    RESULTS.append({"check": name, "status": status, "detail": str(detail)})


def last_line():
    lines = traceback.format_exc().strip().splitlines()
    return lines[-1][:400] if lines else "?"


SAMPLE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Move semantics in modern C++</title></head>
<body>
<nav><a href="/">Home</a> <a href="/notes">Notes</a> <a href="/about">About</a></nav>
<article>
<h1>Move semantics in modern C++</h1>
<p>Copying a large object is expensive: every element of a vector has to be duplicated, and the
original is usually thrown away a moment later. Move semantics let the compiler hand over the
resources of a temporary object instead of duplicating them, which turns an expensive copy into a
couple of pointer assignments.</p>
<p>An rvalue reference, written with two ampersands, binds to temporaries. A class that wants to be
moved provides a move constructor and a move assignment operator that take an rvalue reference,
steal the internal buffer of their argument and leave it in a valid but unspecified state.</p>
<pre><code class="language-cpp">#include &lt;string&gt;
#include &lt;utility&gt;
#include &lt;vector&gt;

std::vector&lt;std::string&gt; make_names() {
    std::vector&lt;std::string&gt; names{"ada", "grace", "linus"};
    return names;                       // moved, or elided entirely
}

int main() {
    auto names = make_names();
    std::string first = std::move(names.front());
    return first.empty() ? 1 : 0;
}
</code></pre>
<p>The standard library function std::move does not move anything by itself. It is a cast that turns
its argument into an rvalue, so that overload resolution picks the move constructor. After the move
the source object must not be relied upon for its value, although it may still be assigned to or
destroyed, and well written classes document exactly what state they leave behind.</p>
<p>Return values are a special case. When a function returns a local variable by value, the compiler
first tries to elide the copy entirely, and if it cannot, it treats the variable as an rvalue and
moves it. Writing std::move in a return statement is therefore usually a pessimisation, because it
prevents the elision that would otherwise have happened.</p>
<p>Containers benefit the most. When a vector grows it moves its elements into the new buffer, but
only if their move constructor is declared noexcept; otherwise it falls back to copying so that it
can keep its strong exception guarantee. Marking move operations noexcept is one of the cheapest
performance wins available in everyday C++ code.</p>
</article>
<footer><p>Copyright 2026. All rights reserved.</p></footer>
</body></html>
"""
SAMPLE_URL = "https://example.com/notes/move-semantics"


def worker_probe(db_path, storage_dir, text_dir, q):
    """In a process started the way the scraper starts its workers: build a real
    Worker and let it look at its (empty) queue once."""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        import multiprocessing
        from workers.worker import Worker
        w = Worker(db_path, storage_dir, text_dir=text_dir, progress=None)
        more = w.process_url()
        q.put(("ok", os.getpid(), multiprocessing.current_process().name, bool(more)))
    except BaseException:
        q.put(("error", os.getpid(), traceback.format_exc()[-1500:], False))


def classify(names):
    known = getattr(sys, "stdlib_module_names", None)
    std, third = [], []
    for n in names:
        if n == "__future__" or n in sys.builtin_module_names or (known is not None and n in known):
            std.append(n)
        elif known is not None:
            third.append(n)
        else:
            import importlib.util
            import sysconfig
            try:
                spec = importlib.util.find_spec(n)
            except Exception:
                spec = None
            origin = (getattr(spec, "origin", "") or "") if spec else ""
            stdlib_dir = sysconfig.get_paths()["stdlib"]
            if spec and (origin in ("built-in", "frozen") or
                         (origin.startswith(stdlib_dir) and "site-packages" not in origin)):
                std.append(n)
            else:
                third.append(n)
    return std, third


def checks(cfg):
    import platform
    from pathlib import Path
    here = Path(os.getcwd())
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rec("interpreter", "ok", "%s %s, %s%s" % (
        platform.python_implementation(), platform.python_version(), sys.executable,
        " (a venv of %s)" % sys.base_prefix if in_venv else ""))

    std, third = classify(cfg["externals"])
    missing = []
    for n in std + ["_ssl", "_hashlib", "_sqlite3", "zlib", "_bz2", "_lzma", "_json", "_multiprocessing",
                    "_socket", "select", "pyexpat"]:
        try:
            __import__(n)
        except Exception:
            missing.append("%s (%s)" % (n, last_line()))
    rec("stdlib", "fail" if missing else "ok",
        "cannot import: " + "; ".join(missing) if missing else
        "the %d standard modules the scraper imports, and the C modules behind them" % len(std))
    try:
        import _decimal  # noqa: F401
    except ImportError:
        rec("decimal", "warn", "no C _decimal: decimal falls back to the slow pure-Python one")

    vers, bad, outside = [], [], []
    for n in third:
        try:
            m = __import__(n)
            v = getattr(m, "__version__", None) or "?"
            vers.append("%s %s" % (n, v))
            f = os.path.abspath(getattr(m, "__file__", "") or "")
            if f and not f.startswith(os.path.abspath(sys.prefix) + os.sep):
                outside.append("%s from %s" % (n, os.path.dirname(f)))
        except Exception:
            bad.append("%s (%s)" % (n, last_line()))
    rec("packages", "fail" if bad else "ok", "cannot import: " + "; ".join(bad) if bad else ", ".join(vers))
    if outside:
        rec("package-origin", "warn", "imported from OUTSIDE this environment (system site-packages): "
            + "; ".join(outside))
    try:
        from importlib import metadata
        extra = []
        for d in ("urllib3", "certifi", "charset-normalizer", "idna", "htmldate", "jusText", "courlan",
                  "dateparser", "regex"):
            try:
                extra.append("%s %s" % (d, metadata.version(d)))
            except Exception:
                pass
        if extra:
            rec("dependencies", "info", ", ".join(extra))
    except ImportError:
        pass

    failed = []
    t = time.time()
    for name in cfg["modules"]:
        try:
            __import__(name)
        except BaseException:
            failed.append("%s: %s" % (name, last_line()))
    rec("scraper-modules", "fail" if failed else "ok",
        "\n".join(failed) if failed else "all %d modules reachable from %s import (%.1fs)" % (
            len(cfg["modules"]), ", ".join(cfg["entries"]), time.time() - t))

    try:
        import ssl
        ssl.create_default_context()
        paths = ssl.get_default_verify_paths()
        rec("ssl", "ok", "%s; system CA file %s" % (ssl.OPENSSL_VERSION, paths.cafile or paths.openssl_cafile))
    except Exception:
        rec("ssl", "fail", last_line())
    if cfg.get("offline"):
        rec("https", "skip", "--offline")
    else:
        try:
            import requests
            t = time.time()
            r = requests.get(cfg["url"], timeout=30, headers={"User-Agent": cfg["ua"]})
            good = r.status_code == 200 and len(r.content) > 0
            rec("https", "ok" if good else "fail", "requests GET %s -> HTTP %d, %d bytes in %.1fs; "
                "Accept-Encoding sent: %s" % (cfg["url"], r.status_code, len(r.content), time.time() - t,
                                              r.request.headers.get("Accept-Encoding", "")))
        except Exception:
            rec("https", "fail", last_line())

    try:
        from content.text_extractor import html_to_text
        text = html_to_text(SAMPLE, SAMPLE_URL)
        lacking = [w for w in ("std::move", "```", "rvalue") if w not in text]
        rec("text-extraction", "fail" if lacking else "ok",
            "html_to_text gave %d chars without %s: %r" % (len(text), lacking, text[:300]) if lacking else
            "html_to_text (lxml + trafilatura): %d chars of Markdown, code block kept" % len(text))
    except Exception:
        rec("text-extraction", "fail", last_line())
    try:
        from content.extractor import extract_document
        doc = extract_document(SAMPLE.encode("utf-8"), "text/html; charset=utf-8", SAMPLE_URL)
        good = doc.kind == "html" and bool(doc.text)
        rec("extract_document", "ok" if good else "fail", "kind=%s, %d chars of text, %d links" % (
            doc.kind, len(doc.text), len(getattr(doc, "links", None) or [])))
    except Exception:
        rec("extract_document", "fail", last_line())

    samples = []
    html_dir, text_dir = cfg.get("html_dir") or "", cfg.get("text_dir") or ""
    try:
        for entry in sorted(os.listdir(html_dir))[:300]:
            d = os.path.join(html_dir, entry)
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".html.gz"):
                        fp = fn[:-len(".html.gz")]
                        samples.append((os.path.join(d, fn), os.path.join(text_dir, entry, fp + ".txt"),
                                        os.path.join(text_dir, entry, fp + ".json"), entry))
                        break
            if len(samples) >= cfg.get("samples", 3):
                break
    except OSError:
        pass
    if not samples:
        rec("saved-pages", "skip", "no html/<site>/*.html.gz to try")
    else:
        try:
            import gzip
            from content.extractor import extract_document
            sizes, empty, shrunk = [], 0, 0
            for page, txt, meta, site in samples:
                url = "https://%s/" % site
                try:
                    with open(meta) as f:
                        url = json.load(f).get("url") or url
                except (OSError, ValueError):
                    pass
                with gzip.open(page, "rb") as f:
                    body = f.read()
                new = len(extract_document(body, "text/html", url).text)     # the crawler's own path
                try:
                    with open(txt, encoding="utf-8", errors="replace") as f:
                        saved = len(f.read())
                except OSError:
                    saved = None
                empty += new == 0
                shrunk += bool(saved) and new < saved // 2
                sizes.append("%s %d chars%s" % (site, new, " (saved: %d)" % saved if saved is not None else ""))
            status = "fail" if empty == len(samples) else ("warn" if empty or shrunk else "ok")
            rec("saved-pages", status, "re-extracted %d of your saved pages: %s%s" % (
                len(samples), "; ".join(sizes),
                "" if status == "ok" else " -- less text than the crawler saved from the same page"))
        except Exception:
            rec("saved-pages", "fail", last_line())

    try:
        import sqlite3
        from database.schema import init_database
        from database.connection_pool import get_pool
        from database.queue_db import add_batch_to_queue, get_queue_size, pop_from_queue
        db = here / "_check.db"
        init_database(db)
        pool = get_pool(db)
        added = add_batch_to_queue(pool, [("https://example.com/a", 5, None), ("https://example.org/b", 3, None)])
        size = get_queue_size(pool)
        popped = pop_from_queue(pool)
        mode = pool.get_connection().execute("PRAGMA journal_mode").fetchone()[0]
        pool.close_all()
        good = (added > 0 and size == added and popped is not None and str(mode).lower() == "wal"
                and popped[0] in ("https://example.com/a", "https://example.org/b"))
        rec("database", "ok" if good else "fail",
            "SQLite %s, a new database with the scraper's schema: queued %d, queue size %d, popped %s, "
            "journal_mode=%s" % (sqlite3.sqlite_version, added, size, popped[0] if popped else None, mode))
    except Exception:
        rec("database", "fail", last_line())

    try:
        import psutil
        vm = psutil.virtual_memory()
        detail = "psutil %s: memory %.0f%% used, CPU %.0f%%" % (
            psutil.__version__, vm.percent, psutil.cpu_percent(interval=0.1))
        try:
            from resilience.resource_monitor import ResourceMonitor
            pause, why = ResourceMonitor(path=Path(cfg["scraper"])).should_pause_crawling()
            detail += "; the scraper's ResourceMonitor: pause=%s%s" % (pause, " (%s)" % why if why else "")
        except Exception:
            detail += "; ResourceMonitor not exercised (%s)" % last_line()
        rec("psutil", "ok", detail)
    except Exception:
        rec("psutil", "fail", last_line())

    import multiprocessing as mp
    method = mp.get_start_method()
    try:
        from database.schema import init_database
        from workers.worker_pool import WorkerPool
        db = here / "_workers.db"
        init_database(db)                          # main.py does this before it starts workers
        q = mp.Queue()
        wp = WorkerPool(db_path=db, storage_dir=here / "html", num_workers=2, progress=q)
        t = time.time()
        wp.spawn_workers()
        time.sleep(6)
        alive = [w.is_alive() for w in wp.workers]
        codes = [w.exitcode for w in wp.workers]
        wp.shutdown()
        good = len(alive) == 2 and all(alive)
        rec("worker-pool", "ok" if good else "fail",
            "the scraper's WorkerPool started 2 workers (start method: %s); %s after %.0fs; its shutdown() "
            "stopped them" % (method, "both still running" if good else "exit codes %s" % codes, time.time() - t))
    except Exception:
        rec("worker-pool", "fail", last_line())
    try:
        q = mp.Queue()
        p = mp.Process(target=worker_probe, args=(here / "_probe.db", here / "html", here / "text", q),
                       name="Worker-check", daemon=True)
        p.start()
        try:
            msg = q.get(timeout=180)
        except Exception:
            msg = None
        p.join(20)
        if p.is_alive():
            p.terminate()
            p.join(5)
        if msg and msg[0] == "ok":
            rec("worker", "ok", "a Worker made in a %s-started process read its (empty) queue" % method)
        else:
            rec("worker", "fail", msg[2] if msg else "no answer from the worker process (exit code %s)" % p.exitcode)
    except Exception:
        rec("worker", "fail", last_line())
    if method != "fork":
        rec("start-method", "info", "workers start by %s, not fork (Python 3.14 changed the Linux default). "
            "main.py's comment assumes fork; the workers above started fine without it." % method)

    import shutil
    git = shutil.which("git")
    rec("git", "ok" if git else "warn", git or "no git on PATH: fetch_repos.py cannot clone")
    env = ["%s=%s" % (k, os.environ[k][:90]) for k in cfg.get("runtime_vars", []) if os.environ.get(k)]
    if env:
        rec("environment", "info", "your shell sets %s -- the scraper runs with it too" % "; ".join(env))


def main():
    cfg = json.loads(sys.argv[1])
    try:
        checks(cfg)
    except BaseException:
        rec("internal", "fail", traceback.format_exc()[-1500:])
    import multiprocessing
    sys.stdout.flush()
    print("\n" + MARK + json.dumps({"results": RESULTS, "start_method": multiprocessing.get_start_method()}))
    sys.stdout.flush()


MARK = "@@PYTHON_BUILD_TOOL_CHECK@@"

if __name__ == "__main__":
    main()
'''


def copy_code(scraper, dest):
    n = 0
    for rel, src in py_files(scraper).items():
        dst = os.path.join(dest, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        except (OSError, IOError) as e:
            raise ToolError("cannot copy the scraper's %s for the smoke test: %s" % (src, e))
        n += 1
    return n


def run_group(cmd, cwd, log_path, timeout):
    """Run cmd in a session of its own with its output in log_path; afterwards
    kill whatever of its process group is left.  The smoke test starts worker
    processes, and one that outlived it would otherwise hold a pipe open and hang
    the tool.  -> exit status, or None on timeout."""
    with open(log_path, "w") as logf:
        p = subprocess.Popen(cmd, cwd=cwd, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=logf,
                             stderr=subprocess.STDOUT, start_new_session=True)
        rc = None
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        finally:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    break
                time.sleep(0.5)
            if rc is None:
                p.wait()
    return rc


def parse_check_output(text):
    for line in reversed((text or "").splitlines()):
        if line.startswith(CHECK_MARK):
            try:
                return json.loads(line[len(CHECK_MARK):])
            except ValueError:
                return None
    return None


def run_check(python, scraper, workdir, offline=False, keep=False, label="check"):
    """The scraper smoke test, in a throwaway copy of its code, in YOUR
    environment: the scraper will be started from your shell, so the test is too."""
    scan = scan_scraper(scraper)
    if not scan["entries"]:
        raise ToolError("%s has none of %s: is it the scraper?" % (scraper, ", ".join(ENTRY_POINTS)))
    log = os.path.join(workdir, "logs", "%s-%s.log" % (label, stamp()))
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="check-", dir=workdir)
    except OSError as e:
        raise ToolError("cannot make the smoke test's throwaway copy in %s: %s" % (workdir, e))
    result = {"status": "fail", "results": []}
    try:
        n = copy_code(scraper, tmp)
        with open(os.path.join(tmp, CHECK_NAME), "w") as f:
            f.write(CHECK_SCRIPT)
        cfg = {"modules": scan["modules"], "externals": scan["externals"], "entries": scan["entries"],
               "html_dir": os.path.join(scraper, "html"), "text_dir": os.path.join(scraper, "text"),
               "scraper": scraper, "offline": offline,
               "url": CHECK_URL, "ua": "%s (smoke test of a Python build)" % USER_AGENT, "samples": 3,
               "runtime_vars": list(RUNTIME_VARS)}
        info("%d files of the scraper's code copied to %s; running it with %s" % (n, tmp, python))
        started = time.time()
        rc = run_group([python, "-B", "-X", "faulthandler", CHECK_NAME, json.dumps(cfg)], tmp, log, 1200)
        text = read_text(log)
        parsed = parse_check_output(text)
        if not parsed:
            bad("the smoke test did not finish (%s).  Output: %s\n%s" % (
                "timed out" if rc is None else "exit %d" % rc, log, tail(text, 30)))
            return result
        # The scraper logs problems at ERROR to the console; a clean run logs none.
        logged = [line.strip() for line in text.splitlines() if " - ERROR - " in line]
        if logged:
            parsed["results"].append({"check": "scraper-errors", "status": "fail",
                                      "detail": "the scraper logged errors during the test:\n" +
                                      "\n".join(line[:240] for line in logged[:8])})
        if rc != 0:
            parsed["results"].append({"check": "exit", "status": "fail", "detail": (
                "timed out after printing its results" if rc is None else
                "exit status %d after printing its results" % rc) + "\n" + tail(text, 12)})
        result["results"] = parsed["results"]
        for r in parsed["results"]:
            text = "%-16s %s" % (r["check"], r["detail"].replace("\n", "\n" + " " * 24))
            {"ok": ok, "fail": bad, "warn": lambda m: out("  " + _c("33", "warn") + " " + m),
             "skip": note, "info": note}.get(r["status"], note)(text)
        fails = [r["check"] for r in parsed["results"] if r["status"] == "fail"]
        result["status"] = "fail" if fails else "ok"
        if fails:
            bad("smoke test FAILED (%s) in %s.  Full output: %s" % (", ".join(fails), hms(time.time() - started), log))
        else:
            ok("smoke test passed in %s.  Full output: %s" % (hms(time.time() - started), log))
        return result
    finally:
        if keep or result["status"] == "fail":
            note("the throwaway copy is kept for a look: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_check(args):
    scraper = expand(args.scraper)
    if not os.path.isdir(scraper):
        raise ToolError("%s is not there (drive not mounted?)" % scraper)
    if args.python:
        python = expand(args.python) if os.sep in args.python else (shutil.which(args.python) or args.python)
    else:
        venv = expand(args.venv) if args.venv else os.path.join(scraper, ".venv")
        if not venv_info(venv):
            raise ToolError("%s is not a venv.  Name one with --venv, or an interpreter with --python" % venv)
        python = os.path.join(venv, "bin", "python")
        say("Smoke test of %s" % describe_venv(venv))
    if not os.access(python, os.X_OK):
        raise ToolError("%s is not executable" % python)
    workdir = expand(args.workdir)
    result = run_check(python, scraper, workdir, offline=args.offline, keep=args.keep)
    state = State(os.path.join(workdir, "state.json"))
    state.add_check({"when": now(), "python": python, "status": result["status"]})
    return 0 if result["status"] == "ok" else 1


# ------------------------------------------------------------- other commands

def cmd_versions(args):
    rels, source = get_releases()
    groups = by_series(rels)
    auto, why = pick_version(rels, "auto")
    latest, _ = pick_version(rels, "latest")
    say("CPython releases (%s)" % source)
    for series in sorted(groups, reverse=True)[:args.count]:
        r = groups[series][0]
        tags = []
        if r is auto:
            tags.append("<- auto")
        if r is latest and r is not auto:
            tags.append("<- latest")
        info("%-8s %-10s %-11s %d releases %s" % (vstr(series), r["version"], r["date"], len(groups[series]),
                                                 " ".join(tags)))
    info("")
    info("auto: %s -- %s" % (auto["version"], why))
    return 0


def cmd_status(args):
    scraper = expand(args.scraper)
    workdir = expand(args.workdir)
    say("Interpreters built for %s" % scraper_tag(scraper))
    built = newest_built_prefix(scraper)
    if not built:
        note("none under %s" % expand(DEFAULT_OPT))
    for _, path, rec in built:
        v = rec.get("verify") or {}
        info("%-52s %s  built %s  %s  verify %s" % (path, rec.get("version", "?"), (rec.get("built") or "?")[:16],
                                                 "PGO+LTO" if rec.get("optimized") else "no PGO/LTO",
                                                 "ok" if v.get("good") else "FAILED"))
    leftovers = sorted(glob.glob(os.path.join(glob.escape(expand(DEFAULT_OPT)), "cpython-*-%s.old-*"
                                              % scraper_tag(scraper))) +
                       glob.glob(os.path.join(glob.escape(expand(DEFAULT_OPT)), "cpython-*-%s.failed-*"
                                              % scraper_tag(scraper))))
    for path in leftovers:
        note("left over, yours to delete once you do not need it: %s" % path)
    say("The scraper's venv")
    info(describe_venv(os.path.join(scraper, ".venv")) if os.path.isdir(scraper) else
         "%s is not there (drive not mounted?)" % scraper)
    state = State(os.path.join(workdir, "state.json"))
    if state.d["checks"]:
        say("Recent smoke tests")
        for c in state.d["checks"][-6:]:
            info("%s  %-4s  %s" % (c.get("when", "?")[:16], c.get("status", "?"), c.get("venv") or c.get("python")))
    say("Work directory %s" % workdir)
    if os.path.isdir(workdir):
        for pattern, what in (("Python-*", "source trees"), ("build-*", "build trees"), ("downloads", "downloads"),
                              ("check-*", "kept smoke-test copies and venvs"), ("logs", "logs")):
            paths = [p for p in glob.glob(os.path.join(glob.escape(workdir), pattern)) if os.path.isdir(p)]
            if paths:
                info("%-34s %s" % (what, human(sum(dir_size(p) for p in paths))))
    else:
        note("not created yet")
    return 0


CLEAN_PATTERNS = ("Python-3.*", "build-3.*", "stage-3.*", "check-venv-3.*", "check-????????", ".extract-*",
                  ".ept-cc-*")


def cmd_clean(args):
    """Only in a work directory that is demonstrably this tool's, and only the
    names this tool gives things: a mistyped --workdir must not cost anyone a
    directory."""
    workdir = expand(args.workdir)
    if not os.path.isdir(workdir):
        note("nothing to clean: %s does not exist" % workdir)
        return 0
    real, scraper, home = (os.path.realpath(p) for p in (workdir, expand(args.scraper), expand("~")))

    def inside(a, b):                           # a is b, or somewhere below it
        return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    if real == "/" or inside(scraper, real) or inside(home, real) or inside(real, scraper):
        raise ToolError("refusing to clean %s: it is, holds, or is inside the scraper or your home directory"
                        % workdir)
    record = read_json(os.path.join(workdir, "state.json")) or {}
    legacy = isinstance(record.get("builds"), dict) and isinstance(record.get("checks"), list)  # before "tool"
    if record.get("tool") != TOOL and not legacy and not same_path(workdir, expand(DEFAULT_WORKDIR)):
        raise ToolError("%s has no state.json stamped by this tool: it is not a work directory of %s.  Not "
                        "touching it." % (workdir, TOOL))
    lock_dir(workdir)
    patterns = list(CLEAN_PATTERNS)
    if args.all:
        patterns += ["downloads", "logs", "state.json"]
    freed = 0
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(glob.escape(workdir), pattern))):
            size = dir_size(path) if os.path.isdir(path) else os.path.getsize(path)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            freed += size
            info("removed %s" % path)
    ok("freed %s.  Installed interpreters and venvs are never touched by clean." % human(freed))
    return 0


# ------------------------------------------------------------------------- main

def add_common(p):
    p.add_argument("--scraper", default=DEFAULT_SCRAPER, help="the scraper directory (default %(default)s)")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR, help="downloads, build trees, logs (default %(default)s)")


def add_build_options(p):
    add_common(p)
    p.add_argument("--version", default=None,
                   help="auto (default): newest release of the newest series that has had a bugfix release; "
                        "latest; X.Y; or X.Y.Z")
    p.add_argument("--prefix", help="install here (default ~/opt/cpython-<version>-<scraper dir name>)")
    p.add_argument("--cc", default="", help="C compiler (default: the system gcc, /usr/bin/gcc)")
    p.add_argument("--jobs", type=int, default=0, help="make -j (default: every CPU)")
    p.add_argument("--no-optimize", dest="no_optimize", action="store_true",
                   help="skip PGO + LTO: a build in minutes instead of ~15, for trying the tool out")
    p.add_argument("--install-deps", dest="install_deps", action="store_true",
                   help="sudo dnf/apt install the missing development packages first")
    p.add_argument("--offline", action="store_true", help="skip the HTTPS checks")
    p.add_argument("--constraints", default="", help="pip constraints file for the scraper's packages "
                   "(e.g. `pip freeze` of the old environment, to keep its versions)")
    p.add_argument("--verbose", action="store_true", help="stream configure/make output")


def build_parser():
    p = argparse.ArgumentParser(prog=TOOL + ".py", description="Build a CPython 3 for claude_scraper -- its "
                                "own prefix, its own pip -- and prove it runs the scraper.")
    p.add_argument("--tool-version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")
    sp = sub.add_parser("doctor", help="preflight: what is missing, and the command that installs it")
    add_build_options(sp)
    sp = sub.add_parser("plan", help="resolve the version, paths and configure line; build nothing")
    add_build_options(sp)
    sp = sub.add_parser("build", help="download, configure, make (PGO+LTO), install, verify, smoke-test the scraper")
    add_build_options(sp)
    sp.add_argument("--rebuild", action="store_true", help="replace an existing prefix (the old one is kept until "
                                                           "the new one verifies)")
    sp.add_argument("--no-check", dest="no_check", action="store_true", help="do not try the scraper afterwards")
    sp.add_argument("--keep", action="store_true", help="keep the smoke test's throwaway copy")
    sp = sub.add_parser("venv", help="(re)make the scraper's .venv from the built interpreter, install its "
                                     "requirements, smoke-test it")
    add_common(sp)
    sp.add_argument("--version", default=None, help="X.Y.Z of a built interpreter (default: the newest built)")
    sp.add_argument("--prefix", help="the built interpreter's prefix (default: the newest built)")
    sp.add_argument("--venv", help="make it here instead of <scraper>/.venv")
    sp.add_argument("--replace", action="store_true", help="replace an existing venv made from another Python "
                                                           "(it is kept as .venv.replaced-<date>)")
    sp.add_argument("--constraints", default="", help="pip constraints file (pin the packages' versions)")
    sp.add_argument("--offline", action="store_true", help="skip the HTTPS check")
    sp.add_argument("--keep", action="store_true", help="keep the smoke test's throwaway copy")
    sp = sub.add_parser("check", help="smoke-test a venv or interpreter against the scraper (read-only for it)")
    add_common(sp)
    sp.add_argument("--venv", help="the venv to test (default <scraper>/.venv)")
    sp.add_argument("--python", help="test this interpreter instead of a venv (it needs the packages installed)")
    sp.add_argument("--offline", action="store_true", help="skip the HTTPS check")
    sp.add_argument("--keep", action="store_true", help="keep the throwaway copy")
    sp = sub.add_parser("status", help="what is built, what the scraper's .venv uses, recent smoke tests")
    add_common(sp)
    sp = sub.add_parser("versions", help="CPython releases on python.org, and what auto picks")
    sp.add_argument("--count", type=int, default=6)
    sp = sub.add_parser("clean", help="remove source/build trees and throwaway copies (never a prefix or venv)")
    add_common(sp)
    sp.add_argument("--all", action="store_true", help="downloads, logs and the record too")
    return p


def _as_interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if os.geteuid() == 0:
        out(_c("31", "ERROR:") + " do not run this as root: it builds, installs and makes venvs as you, and "
            "root-owned files would be left in your home.  Only --install-deps uses sudo, by itself.")
        return 1
    if not args.command:
        parser.print_help()
        out("\nStart with:  %s doctor" % (sys.argv[0] or TOOL + ".py"))
        return 2
    # SIGTERM / SIGHUP take the ^C path so a finally runs and the record is right;
    # a SIGHUP somebody chose to ignore (`nohup ... &`) stays ignored.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, _as_interrupt)
    try:
        return {"doctor": cmd_doctor, "plan": cmd_plan, "build": cmd_build, "venv": cmd_venv,
                "check": cmd_check, "status": cmd_status, "versions": cmd_versions,
                "clean": cmd_clean}[args.command](args)
    except ToolError as e:
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 1
    except (OSError, IOError) as e:             # a full disk, a vanished mount: a message, not a traceback
        out("\n" + _c("31", "ERROR:") + " %s" % e)
        return 1
    except KeyboardInterrupt:
        out("\ninterrupted.  `build` again continues: finished steps are not redone.")
        return 130


def _ensure_platform_python():
    """Started as `python3 tool.py` where python3 is PyPy (on the machine this was
    written on it is, and its ssl segfaults) or a private build?  Re-run under the
    platform python, which is what the tool is tested with."""
    impl = getattr(sys.implementation, "name", "cpython")
    if impl != "cpython" and os.access(PLATFORM_PYTHON, os.X_OK) and not os.environ.get("EPT_NO_REEXEC"):
        os.environ["EPT_NO_REEXEC"] = "1"
        os.execv(PLATFORM_PYTHON, [PLATFORM_PYTHON, "-I", os.path.abspath(__file__)] + sys.argv[1:])


if __name__ == "__main__":
    _ensure_platform_python()
    sys.exit(main())
