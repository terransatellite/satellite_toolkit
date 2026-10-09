#!/usr/bin/python3 -I
# -*- coding: utf-8 -*-
"""enterprise_pypy_tool.py -- build PyPy from source: pypy2 and pypy3, and the Python 2 that
translates them.  Steps 6 and 7 of the satellite toolkit; the successor of the PyPy half of
build_python_universal.sh.

    enterprise_pypy_tool.py build --target pypy2         step 6: <opt>/pypy2.7-v<rel>, pypy2-current
    enterprise_pypy_tool.py build --target pypy3         step 7: <opt>/pypy3.N-v<rel>, pypy3-current
    enterprise_pypy_tool.py build --target host          only the translation host (CPython 2.7.18)
    enterprise_pypy_tool.py smoke                        a tiny RPython translation (25 s; --smoke-jit ~7 min)
    enterprise_pypy_tool.py plan | doctor | status | verify PREFIX | versions | detect | toolchains

PyPy is not compiled, it is TRANSLATED: RPython -- a Python 2 program -- reads PyPy's source and
writes C, which a C compiler then builds.  So a PyPy needs a Python 2 to exist.  Which one is
--host:

  cpython27 (default for pypy2, the toolkit's choice J)  CPython 2.7.18 built here, in the work
            directory, with the toolkit's gcc (step 4) and binutils (step 3); then pycparser 2.21
            and cffi 1.15.1 put into it.  The slowest host (2-3x a PyPy host) but made from source.
  prebuilt  the official portable PyPy2 for this architecture (the .sh's default; fastest)
  system    a pypy / pypy2 / python2.7 already on this machine
  built     <opt>/pypy2-current -- the default for pypy3 when it runs ("pypy3 with that pypy2")
  PATH      an interpreter you name.  Never a CPython 3: RPython cannot run on it.

What the 2026-08-29 run taught (legacy/python_log-2026-08-29.txt): RPython takes CC from the
shell (it used clang-current and printed 100 unknown-warning lines); without --batch an error
or a ^C opens an interactive (Pdb+) prompt that would wait forever under a session keeper; and
package.py's output must be kept, not thrown away.  So: a scrubbed environment with CC, CFLAGS
and LDFLAGS set by this tool, rpython --batch, every log kept, the first real errors shown.

One file, standard library only, syntax held to Python 3.6.  Never run it as root: only
--install-deps calls sudo.  Licence: public domain / CC0.  No warranty.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("enterprise_pypy_tool.py needs Python 3.6 or newer")

import argparse
import configparser
import errno
import fcntl
import glob
import gzip
import hashlib
import http.client
import json
import os
import re
import resource
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

TOOL = "enterprise_pypy_tool"
VERSION = "0.1.0"
PLATFORM_PYTHON = "/usr/bin/python3"
USER_AGENT = TOOL + "/" + VERSION
MARKER = ".pypy_build_tool.json"            # in every prefix this tool installed
REEXEC_GUARD = "EPPT_NO_REEXEC"

# Where PyPy comes from.  The index lists every file of every release (sources AND binaries);
# versions.json lists binaries only, with the stable / latest flags and the dates.
PYPY_BASE = "https://downloads.python.org/pypy/"
PYPY_VERSIONS_URLS = (PYPY_BASE + "versions.json",
                      "https://raw.githubusercontent.com/pypy/pypy/main/pypy/tool/release/versions.json")
PYPY_CHECKSUMS_URL = "https://www.pypy.org/checksums.html"
PYPY_GIT = "https://github.com/pypy/pypy"
PYPY_ARCHIVE_URL = "https://github.com/pypy/pypy/archive/refs/tags/%s.tar.gz"
PYPY_NOTES_URL = "https://raw.githubusercontent.com/pypy/pypy/main/pypy/doc/release-v%s.rst"
# The release notes' title line says which series is beta ("... 3.11, and 3.12 beta").  These
# are what the notes said when this was written, for when they cannot be fetched.
KNOWN_BETA = {"8.0.0": ("3.12",), "8.0.1": ("3.12",)}
# PyPy 8.0 binaries are built on manylinux_2_28 (AlmaLinux 8, gcc 14): glibc 2.28 or newer.
GLIBC_FLOOR_V8 = (2, 28)
ARCH_TAGS = {"x86_64": "linux64", "amd64": "linux64", "aarch64": "aarch64", "arm64": "aarch64",
             "i686": "linux32", "i586": "linux32", "i386": "linux32", "s390x": "s390x"}

# The CPython 2.7 translation host.  sha256 as published with the release (python.org) -- the
# same values pyenv's python-build carries for 2.7.18.
CPY27_VERSION = "2.7.18"
CPY27_SOURCES = (
    ("https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tar.xz", "Python-2.7.18.tar.xz",
     "b62c0e7937551d0cc02b8fd5cb0f544f9405bafc9a54d3808ed4594812edef43"),
    ("https://www.python.org/ftp/python/2.7.18/Python-2.7.18.tgz", "Python-2.7.18.tgz",
     "da3080e3b488f648a3d7a4560ddee895284c3380b11d6de75edb986526b9a814"),
    ("https://github.com/python/cpython/archive/refs/tags/v2.7.18.tar.gz", "cpython-v2.7.18.tar.gz", ""),
)
CPY27_GIT = ("https://github.com/python/cpython", "v2.7.18")
# What RPython needs in a CPython host: pycparser (rpython/tool/cparser/cparser.py) and cffi.
# The last releases that support Python 2.7.  sha256 from PyPI's JSON, 2026-10-09.
PYPI_FILES = "https://files.pythonhosted.org/packages/"
HOST_FILES = {
    "pycparser": ("pycparser-2.21-py2.py3-none-any.whl",
                  PYPI_FILES + "62/d5/5f610ebe421e85889f2e55e33b7f9a6795bd982198517d912eb1c76e1a53/"
                  "pycparser-2.21-py2.py3-none-any.whl",
                  "8ee45429555515e1f6b185e78100aea234072576aa43ab53aefcae078162fca9"),
    "cffi": ("cffi-1.15.1.tar.gz",
             PYPI_FILES + "2b/a8/050ab4f0c3d4c1b8aaa805f70e26e84d0e27004907c5b8ecc1d31815f92a/cffi-1.15.1.tar.gz",
             "d400bfb9a37b1351253cb402671cea7e89bdecc294e8016a707f6d1d8ac934f9"),
    # Used only when the sdist will not build (x86_64; needs a UCS4 build, which this host is).
    "cffi-wheel-x86_64": ("cffi-1.15.1-cp27-cp27mu-manylinux1_x86_64.whl",
                          PYPI_FILES + "a4/42/54bdf22cf6c8f95113af645d0bd7be7f9358ea5c2d57d634bb11c6b4d0b2/"
                          "cffi-1.15.1-cp27-cp27mu-manylinux1_x86_64.whl",
                          "ed9cb427ba5504c1dc15ede7d516b84757c3e3d7868ccc85121d9310d27eed0b"),
}
HOST_PINS = ("pycparser==2.21", "cffi==1.15.1")
# pypy3.12 has no distutils, and its package.py runs `pip install setuptools` (twice): with this
# wheel in the downloads directory and PIP_NO_INDEX/PIP_FIND_LINKS set, that needs no network.
SETUPTOOLS_WHEEL = ("setuptools-84.0.0-py3-none-any.whl",
                    PYPI_FILES + "95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/"
                    "setuptools-84.0.0-py3-none-any.whl",
                    "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670", "84.0.0")
HTTPS_PROBE_URL = "https://pypi.org/simple/pip/"

# --------------------------------------------------------------- local or remote
# --local, --remote[=USER@HOST], --attach, --sessions: satellite_remote.py, written here
# by sync_remote.py.  main() hands its argv to where_to_run() before anything else.
# >>> satellite_remote >>> (generated by sync_remote.py from satellite_remote.py -- edit that file, not this block)
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import tty

_SR_VERSION = "1"
_SR_HOME = "~/.satellite_toolkit"                    # on the machine that runs the tool
_SR_CONFIG = "~/.config/satellite_toolkit/remote.json"  # on the machine that sends it: the last target
_SR_SATELLITE_SSH = ("~/code/py/satellite_ssh",)    # where satellite_ssh.py keeps its passwords
_SR_DETACH = b"\x1d"                                 # Ctrl-]  -- telnet's escape, nothing else uses it
# The keeper's last word to an attached client: an OSC sequence a terminal ignores, carrying the
# program's exit code, so the attach on the far side can exit with it (and ssh with that).
_SR_EXIT = b"\x1b]777;satellite-remote-exit;"
_SR_SUDO = re.compile(br"\[sudo\] password for ([^:\r\n]+): *$")
_SR_TAIL = 65536                                     # what a re-attach shows of the past
# A session name as _sr_session_name makes it (and the keeper's -2, -3 for a second one started
# in the same second): what `--attach NAME`, with a space, takes for a name.
_SR_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_]{1,20}-[0-9]{8}-[0-9]{6}(-[0-9]{1,3})?$")
# Exit codes of a remote run that did not see the program end (the module's docstring).
_SR_DETACHED = 75                                    # EX_TEMPFAIL: Ctrl-], still running there
_SR_DROPPED = 255                                    # ssh's own code for a lost connection


class RemoteError(Exception):
    """Said in one paragraph and the tool exits 1 -- never a traceback."""


def _sr_say(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _sr_tty():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------- the options

def _sr_take(argv):
    """-> (rest, opts): this block's options taken out of argv, everything else kept in order.
    Nothing after a bare `--` is looked at: it belongs to whatever comes after it."""
    o = {"where": None, "target": None, "attach": None, "sessions": False,
         "keeper": None, "keeper_attach": None, "keeper_list": False, "size": None}
    rest, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i:])
            break
        if a == "--local":
            o["where"] = "local"
        elif a == "--remote":
            o["where"] = "remote"
            # `--remote build` must not take `build` for a machine: only USER@HOST is a target.
            if i + 1 < len(argv) and "@" in argv[i + 1] and not argv[i + 1].startswith("-"):
                o["target"] = argv[i + 1]
                i += 1
        elif a.startswith("--remote="):
            o["where"], o["target"] = "remote", a.split("=", 1)[1] or None
        elif a == "--attach":
            o["attach"] = ""
            # `--attach NAME` as well as --attach=NAME -- but only a word shaped like a session
            # name, so `--attach status` still leaves `status` to the tool.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-") and _SR_NAME_SHAPE.match(argv[i + 1]):
                o["attach"] = argv[i + 1]
                i += 1
        elif a.startswith("--attach="):
            o["attach"] = a.split("=", 1)[1]
        elif a == "--sessions":
            o["sessions"] = True
        elif a.startswith("--satellite-keeper="):          # internal: we ARE the keeper, there
            o["keeper"] = a.split("=", 1)[1]
        elif a.startswith("--satellite-keeper-attach="):   # internal: the attach, there
            o["keeper_attach"] = a.split("=", 1)[1]
        elif a == "--satellite-keeper-list":
            o["keeper_list"] = True
        elif a.startswith("--satellite-size="):
            o["size"] = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return rest, o


def _sr_wants_question(rest):
    if os.environ.get("SATELLITE_TOOLKIT_WHERE") == "local" or not _sr_tty():
        return False
    # --version is a real option in several tools (an LLVM / CPython / GCC release),
    # so it still asks; only help and the tool's own version are quiet
    return not any(a in ("-h", "--help", "--tool-version") for a in rest)


def _sr_ask(prompt, default=""):
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        answer = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        sys.stdout.write("\n")
        raise SystemExit(130)
    if not answer:                      # the end of the input is the default, not a loop
        return default
    return answer.strip() or default


def _sr_ask_where():
    sys.stdout.write("\nRun where?   l = this machine   r = another machine, over ssh\n")
    while True:
        a = _sr_ask("[l]: ", "l").lower()
        if a in ("l", "local", "here", "this"):
            return "local"
        if a in ("r", "remote", "there", "ssh"):
            return "remote"
        sys.stdout.write("  l or r\n")


# ---------------------------------------------------------------- the last target

def _sr_config():
    try:
        with open(os.path.expanduser(_SR_CONFIG)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sr_remember(target, tool_name):
    path = os.path.expanduser(_SR_CONFIG)
    data = _sr_config()
    data["last"] = target
    data.setdefault("by_tool", {})[tool_name] = target
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".writing"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass                            # remembering is a convenience, never a reason to stop


def _sr_split_target(target):
    if not target or "@" not in target:
        raise RemoteError("a remote machine is USER@HOST, not %r" % (target or ""))
    user, host = target.split("@", 1)
    host = host.strip("[]")             # an IPv6 address may come bracketed; ssh wants it bare
    if not user or not host or any(c.isspace() for c in target):
        raise RemoteError("a remote machine is USER@HOST, not %r" % target)
    return user, host


def _sr_target(o, tool_name):
    if o["target"]:
        return o["target"]
    cfg = _sr_config()
    last = cfg.get("by_tool", {}).get(tool_name) or cfg.get("last") or ""
    if not _sr_tty():
        if last and (o["attach"] is not None or o["sessions"]):
            return last
        raise RemoteError("--remote needs USER@HOST when nobody is at a terminal to ask"
                          + (" (the last one was %s)" % last if last else ""))
    while True:
        t = _sr_ask("ssh to (USER@HOST)%s: " % (" [%s]" % last if last else ""), last)
        if "@" in t:
            return t
        sys.stdout.write("  USER@HOST, e.g. claude_code@192.168.1.20\n")


# ---------------------------------------------------------------- the login

def _sr_satellite_ssh():
    """satellite_ssh, from where it keeps its passwords -- or None.  Its folder is where the
    kept password files live, so it is imported from there, never copied."""
    dirs = [os.environ.get("SATELLITE_SSH_DIR")] + [os.path.expanduser(d) for d in _SR_SATELLITE_SSH]
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, "satellite_ssh.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import satellite_ssh
                return satellite_ssh
            except Exception:           # a broken copy must not stop a plain-ssh login
                return None
    return None


def _sr_ssh_words(text):
    """What ssh's own error means, in a sentence -- the two that come up after a reinstall."""
    low = text.lower()
    if "remote host identification has changed" in low or "host key verification failed" in low:
        return ("the machine answered with a NEW host key -- it was reinstalled, or it is not the "
                "machine you think.  If it was reinstalled: ssh-keygen -R <host>, then try again")
    if "permission denied" in low:
        return "the login was refused (the password or the key)"
    if "connection refused" in low:
        return "nothing listens for ssh there (is sshd running? the firewall?)"
    if "timed out" in low or "no route to host" in low or "network is unreachable" in low:
        return "the machine cannot be reached (is it on? the address?)"
    return ""


class RemoteLink(object):
    """One ssh login kept open on a control socket; every command, upload and attach goes over
    it, so a password is typed (by satellite_ssh, or by a person) once."""

    def __init__(self, target):
        self.target = target
        self.user, self.host = _sr_split_target(target)
        self.kept = None                # satellite_ssh, when it keeps a password for the target
        self.session = None             # satellite_ssh's Session
        self.dir = None                 # our own control socket's folder (plain ssh)
        self.sock = None
        self.python = None              # the python3 there
        # A port, a key, a known_hosts file: SATELLITE_SSH_OPTIONS="-p 2222 -i KEY", passed to
        # every ssh -- the one way to reach a machine ~/.ssh/config does not describe.
        self.options = shlex.split(os.environ.get("SATELLITE_SSH_OPTIONS", ""))

    def open(self):
        mod = _sr_satellite_ssh()
        if mod is not None and mod.has_password(self.user, self.host):
            try:
                self.session = mod.connect(self.user, self.host, options=self.options)
            except mod.Refused as e:
                raise RemoteError("%s: %s%s" % (self.target, e, ("\n  " + _sr_ssh_words(str(e)))
                                                if _sr_ssh_words(str(e)) else ""))
            self.kept = mod
            return self
        # Plain ssh: a key or an agent, or the password typed here.  ControlPersist keeps the
        # master after -f puts it in the background; close() ends it -- and 10 minutes with no
        # ssh using it end it too, so a tool killed before close() leaves no master for good.
        self.dir = tempfile.mkdtemp(prefix="satellite-remote-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.sock = os.path.join(self.dir, "ssh")     # short: a socket path stops at 107 bytes
        argv = (["ssh", "-M", "-S", self.sock] + self.options
                + ["-o", "ControlPersist=10m", "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-fN", self.target])
        if not _sr_tty():
            argv[1:1] = ["-o", "BatchMode=yes"]       # nobody to type a password: a key, or nothing
        # stderr to a file, not a pipe: the master that -f leaves behind must never be what we
        # wait on for an end of file.
        with tempfile.TemporaryFile() as errf:
            rc = subprocess.call(argv, stdin=None if _sr_tty() else subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=errf)
            errf.seek(0)
            text = errf.read().decode("utf-8", "replace").strip()
        if rc != 0:
            hint = _sr_ssh_words(text)
            self.close()
            raise RemoteError("cannot log in to %s%s\n  %s%s" % (
                self.target, (": " + hint) if hint else "", text.replace("\n", "\n  "),
                "" if mod is None else "\n  (satellite_ssh keeps no password for it: "
                "python3 ~/code/py/satellite_ssh/satellite_ssh.py %s setpassword)" % self.target))
        return self

    def ssh(self, *flags):
        """argv for one ssh over the open login, flags before the target."""
        if self.session is not None:
            return self.session.ssh(*flags)
        return ["ssh", "-S", self.sock] + self.options + list(flags) + [self.target]

    def run(self, command, data=None, timeout=None):
        """One shell command there: (exit code, stdout, stderr) as text."""
        p = subprocess.Popen(self.ssh("-o", "BatchMode=yes", "-T") + [command],
                             stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            return 124, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def password(self):
        """The kept password, when satellite_ssh keeps one -- read when it is needed, because
        the 6-hour rotation may have changed it since the login."""
        if self.kept is None:
            return None
        return self.kept.read_kept(self.user, self.host)

    def close(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
        if self.sock:
            if os.path.exists(self.sock):
                subprocess.call(["ssh", "-S", self.sock] + self.options + ["-O", "exit", self.target],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.sock = None
        if self.dir:
            try:
                os.rmdir(self.dir)
            except OSError:
                pass
            self.dir = None


# The python3 on the far side, found once: the platform python first, as the tools want.
_SR_FIND_PYTHON = r'''for p in /usr/bin/python3 /usr/libexec/platform-python /usr/bin/python3.[0-9]* python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
    command -v "$p"; exit 0
  fi
done
exit 1'''


def _sr_python(link):
    if link.python:
        return link.python
    rc, out, err = link.run("sh -c %s" % shlex.quote(_SR_FIND_PYTHON), timeout=60)
    if rc == 0 and out.strip():
        link.python = out.strip().splitlines()[-1]
        return link.python
    if rc == 255:
        raise RemoteError("ssh to %s failed: %s" % (link.target, err.strip()))
    raise RemoteError("%s has no python3 (3.6 or newer), which every tool here runs under.  On it:\n"
                      "  sudo dnf install python3      (Enterprise Linux, Fedora)\n"
                      "  sudo apt-get install python3  (Debian, Ubuntu)" % link.target)


# ---------------------------------------------------------------- the upload

def _sr_pack(files):
    """files = [(path here, path there)] -> (sha256 of names + contents, tar.gz bytes).  The
    digest names the folder there, so a tool that did not change is not sent again."""
    h = hashlib.sha256()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for here, there in sorted(files, key=lambda f: f[1]):
            with open(here, "rb") as f:
                data = f.read()
            h.update(there.encode() + b"\0" + data + b"\0")
            info = tarfile.TarInfo(there)
            info.size = len(data)
            info.mode = os.stat(here).st_mode & 0o755
            info.mtime = int(os.stat(here).st_mtime)
            tar.addfile(info, io.BytesIO(data))
    return h.hexdigest(), buf.getvalue()


def _sr_upload(link, files, name):
    """-> the folder there that holds files, sent only when it is not there already."""
    digest, blob = _sr_pack(files)
    folder = "%s/code/%s-%s" % (_SR_HOME, name, digest[:12])
    q = '"$HOME"' + shlex.quote(folder[1:])          # ~/... -> "$HOME"'/...': quoted, and still home
    rc, out, err = link.run('test -f %s/.complete && echo have || echo send' % q, timeout=60)
    if rc != 0:
        raise RemoteError("ssh to %s failed: %s" % (link.target, err.strip() or out.strip()))
    if out.strip() != "have":
        _sr_say("  sending %s (%d KB) to %s" % (name, max(1, len(blob) // 1024), link.target))
        # Two runs sending the same tool at once each unpack into a .part of their own; the
        # first rename wins (mv -T: never INTO a folder that is there), and the other one --
        # the same files, by the digest -- is dropped.  Only a folder left half-made before is
        # replaced.
        script = ('set -e; umask 077; d=%s; p="$d.part.$$"; rm -rf "$p"; mkdir -p "$p"; '
                  'tar -xzf - -C "$p"; touch "$p/.complete"; mv -T "$p" "$d" 2>/dev/null && exit 0; '
                  'if [ -f "$d/.complete" ]; then rm -rf "$p"; exit 0; fi; rm -rf "$d"; mv -T "$p" "$d"') % q
        rc, out, err = link.run("sh -c %s" % shlex.quote(script), data=blob, timeout=600)
        if rc != 0:
            raise RemoteError("could not unpack %s on %s: %s" % (name, link.target, (err or out).strip()))
    return folder


# ---------------------------------------------------------------- the keeper (runs THERE)

def _sr_sessions():
    d = os.path.join(os.path.expanduser(_SR_HOME), "sessions")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _sr_socket_path(sdir):
    """A UNIX socket path stops at 107 bytes; a long home or name gets a short one in /tmp,
    named in the session folder."""
    path = os.path.join(sdir, "socket")
    if len(path.encode()) < 100:
        return path
    short = os.path.join(tempfile.gettempdir(), "satellite-remote-%d" % os.getuid())
    os.makedirs(short, mode=0o700, exist_ok=True)
    return os.path.join(short, hashlib.sha256(sdir.encode()).hexdigest()[:16])


def _sr_set_size(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, ValueError):
        pass


def _sr_size(text, default=(40, 120)):
    try:
        rows, cols = (int(x) for x in text.lower().split("x"))
        if 2 <= rows <= 1000 and 10 <= cols <= 1000:
            return rows, cols
    except (AttributeError, ValueError):
        pass
    return default


def _sr_exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _sr_write_file(path, text):
    tmp = path + ".writing"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _sr_keeper_main(name, argv, size=None):
    """`<tool> --satellite-keeper=NAME -- COMMAND...`: start COMMAND on a pseudo-terminal in a
    session that outlives this ssh login, say it is listening, and return.  The keeper itself
    is a daemon: its own session (setsid), no terminal, stdio on /dev/null."""
    cmd = argv[1:] if argv[:1] == ["--"] else list(argv)
    if not cmd or not re.match(r"^[A-Za-z0-9._-]{1,48}$", name):
        _sr_say("satellite_remote: a session needs a plain name and a command")
        return 2
    # Two runs started in the same second ask for the same name: the second is NAME-2, then
    # NAME-3 ...  mkdir is the claim, so two starters can never share a folder.  The name that
    # was taken is the one in the "ok NAME PID" answer.
    root = _sr_sessions()
    for n in range(1, 100):
        sdir = os.path.join(root, name if n == 1 else "%s-%d" % (name, n))
        try:
            os.mkdir(sdir, 0o700)
            break
        except OSError as e:
            if e.errno != errno.EEXIST or n == 99:
                _sr_say("satellite_remote: session %s: %s" % (os.path.basename(sdir), e))
                return 2
    _sr_write_file(os.path.join(sdir, "cmd.json"), json.dumps(
        {"argv": cmd, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "cwd": os.getcwd(),
         "version": _SR_VERSION}, indent=1))
    ready_r, ready_w = os.pipe()
    if os.fork() > 0:                            # the starter: wait for the keeper's word
        os.close(ready_w)
        word = b""
        deadline = time.time() + 20
        while time.time() < deadline:
            r, _, _ = select.select([ready_r], [], [], 0.5)
            if r:
                chunk = os.read(ready_r, 256)
                if not chunk:
                    break
                word += chunk
                if b"\n" in word:
                    break
        os.close(ready_r)
        word = word.decode("utf-8", "replace").strip()
        if not word.startswith("ok"):
            _sr_say("satellite_remote: the session did not start: %s" % (word or "no answer"))
            return 1
        sys.stdout.write("%s\n" % word)
        sys.stdout.flush()
        return 0
    os.close(ready_r)
    os.setsid()
    if os.fork() > 0:                            # the second fork: never a session leader again
        os._exit(0)
    try:
        _sr_keeper_loop(sdir, cmd, ready_w, size)
    except BaseException as e:                   # a keeper that dies says so, in its folder
        try:
            _sr_write_file(os.path.join(sdir, "keeper-error"), "%s: %s\n" % (type(e).__name__, e))
            os.write(ready_w, ("error %s\n" % e).encode())
        except OSError:
            pass
    os._exit(0)


def _sr_keeper_loop(sdir, cmd, ready_w, size):
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.chdir(os.path.expanduser("~"))
    log = os.open(os.path.join(sdir, "output.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    path = _sr_socket_path(sdir)
    if path != os.path.join(sdir, "socket"):
        _sr_write_file(os.path.join(sdir, "socket.path"), path)
    try:
        os.unlink(path)
    except OSError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o077)
    listener.bind(path)
    os.umask(old)
    listener.listen(4)
    listener.setblocking(False)
    import pty
    env = dict(os.environ)
    env["SATELLITE_TOOLKIT_WHERE"] = "local"     # the program there never asks "where?" again
    env.setdefault("TERM", "xterm-256color")
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(cmd[0], cmd, env)
        except OSError as e:
            sys.stderr.write("satellite_remote: cannot start %s: %s\n" % (cmd[0], e))
        os._exit(127)
    _sr_set_size(master, *_sr_size(size))
    _sr_write_file(os.path.join(sdir, "pids"), "%d %d\n" % (os.getpid(), pid))
    os.write(ready_w, ("ok %s %d\n" % (os.path.basename(sdir), pid)).encode())
    os.close(ready_w)
    clients = {}                                 # socket -> [bytes waiting to go, hello seen, input, since]
    ended = None
    status = None                                # the program's wait status, once it has one
    drain_until = 0.0

    def finish(st):
        code = _sr_exit_code(st)
        _sr_write_file(os.path.join(sdir, "exit_code"), "%d\n" % code)
        _sr_write_file(os.path.join(sdir, "ended"), time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        for cst in clients.values():
            if cst[1]:
                cst[0] += _SR_EXIT + str(code).encode() + b"\x07"
        os.close(master)
        return code

    while True:
        rd = [listener, master] if ended is None else [listener]
        wr = []
        for c, st in clients.items():
            rd.append(c)
            if st[0]:
                wr.append(c)
        # Ended, and every client has had all of it.  One that connected but has not said its
        # hello yet is waited for (10 s at most): quitting under it would show it neither the
        # output nor the exit code -- only a reset connection.
        if ended is not None and not wr and all(st[1] or time.time() - st[3] > 10 for st in clients.values()):
            break
        r, w, _ = select.select(rd, wr, [], 1.0)
        if master in r:
            try:
                data = os.read(master, 65536)
            except OSError as e:                 # EIO: the program and everything it started are gone
                if e.errno != errno.EIO:
                    raise
                data = b""
            if data:
                os.write(log, data)
                for st in clients.values():
                    if st[1]:
                        st[0] += data
            else:
                if status is None:
                    _, status = os.waitpid(pid, 0)
                ended = finish(status)
        if ended is None and status is None:
            done, st_now = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                # The program is gone but something it started still holds the terminal (a
                # daemon it left behind): what is already written is read for a moment more,
                # then the session ends with the program's own exit code.
                status, drain_until = st_now, time.time() + 2.0
        if ended is None and status is not None and time.time() > drain_until and master not in r:
            ended = finish(status)
        if listener in r:
            try:
                c, _ = listener.accept()
                c.setblocking(False)
                clients[c] = [b"", False, b"", time.time()]
            except OSError:
                pass
        for c in list(clients):
            st = clients[c]
            if c in r:
                try:
                    data = c.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    c.close()
                    del clients[c]
                    continue
                if not st[1]:                    # the hello: "ATTACH <tail bytes> <rows> <cols>\n"
                    st[2] += data
                    if b"\n" not in st[2]:
                        if len(st[2]) > 256:
                            c.close()
                            del clients[c]
                        continue
                    hello, _, extra = st[2].partition(b"\n")
                    words = hello.decode("ascii", "replace").split()
                    tail = int(words[1]) if len(words) > 1 and words[1].isdigit() else _SR_TAIL
                    if len(words) > 3 and words[2].isdigit() and words[3].isdigit() and ended is None:
                        _sr_set_size(master, int(words[2]), int(words[3]))
                    size_now = os.fstat(log).st_size
                    with open(os.path.join(sdir, "output.log"), "rb") as f:
                        if tail and size_now > tail:
                            f.seek(size_now - tail)
                        st[0] = f.read(size_now if not tail else min(tail, size_now))
                    st[1], st[2] = True, b""
                    if ended is not None:
                        st[0] += _SR_EXIT + str(ended).encode() + b"\x07"
                    data = extra
                if data and ended is None:
                    try:
                        os.write(master, data)
                    except OSError:
                        pass
            if c in w and c in clients and st[0]:
                try:
                    sent = c.send(st[0][:262144])
                    st[0] = st[0][sent:]
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    c.close()
                    del clients[c]
                    continue
            # A client that stopped reading is dropped, never waited for: the program's output
            # must keep flowing into the log, or the build itself would stop on a full pty.
            if c in clients and len(st[0]) > (8 << 20):
                c.close()
                del clients[c]
        if ended is not None and not clients:
            break
    listener.close()
    try:
        os.unlink(path)
    except OSError:
        pass
    os.close(log)


def _sr_session_state(sdir):
    """-> dict: name, state (running | ended | stopped), exit code, times, command."""
    info = {"name": os.path.basename(sdir), "state": "running", "exit": None}
    try:
        with open(os.path.join(sdir, "cmd.json")) as f:
            c = json.load(f)
        info["argv"], info["started"] = c.get("argv", []), c.get("started", "")
    except (OSError, ValueError):
        info["argv"], info["started"] = [], ""
    try:
        with open(os.path.join(sdir, "exit_code")) as f:
            info["exit"] = int(f.read().strip())
        info["state"] = "ended"
        with open(os.path.join(sdir, "ended")) as f:
            info["ended"] = f.read().strip()
    except (OSError, ValueError):
        pass
    if info["state"] == "running":
        try:
            with open(os.path.join(sdir, "pids")) as f:
                keeper = int(f.read().split()[0])
            os.kill(keeper, 0)
        except (OSError, ValueError, IndexError):
            info["state"] = "stopped"            # no keeper and no exit code: a reboot, a kill -9
    return info


def _sr_list_here():
    root = _sr_sessions()
    rows = [_sr_session_state(os.path.join(root, n)) for n in sorted(os.listdir(root))]
    sys.stdout.write(json.dumps(rows) + "\n")
    return 0


def _sr_attach_here(name, size=None):
    """`<tool> --satellite-keeper-attach=NAME`, THERE, inside `ssh -tt`: this terminal joins the
    session.  Exits with the program's exit code once it ends."""
    sdir = os.path.join(_sr_sessions(), name)
    if not os.path.isdir(sdir):
        _sr_say("satellite_remote: there is no session %s here" % name)
        return 2
    path = os.path.join(sdir, "socket")
    if os.path.exists(os.path.join(sdir, "socket.path")):
        with open(os.path.join(sdir, "socket.path")) as f:
            path = f.read().strip()
    st = _sr_session_state(sdir)
    rows, cols = _sr_size(size, (0, 0))
    if not rows:
        try:
            cols, rows = os.get_terminal_size(0)
        except OSError:
            rows, cols = 40, 120
    saved = None
    if os.isatty(0):
        saved = termios.tcgetattr(0)
        tty.setraw(0)
    try:
        return _sr_attach_socket(sdir, name, path, st, rows, cols)
    finally:
        if saved is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)


def _sr_attach_socket(sdir, name, path, st, rows, cols):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(path)
    except OSError:
        c.close()
        tail = b""
        try:
            with open(os.path.join(sdir, "output.log"), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - _SR_TAIL))
                tail = f.read()
        except OSError:
            pass
        os.write(1, tail)
        if st["state"] == "ended":
            _sr_say("\r\n[%s ended: exit %d]" % (name, st["exit"]))
            return st["exit"]
        _sr_say("\r\n[%s stopped without an exit code -- the machine restarted, or it was killed]" % name)
        return 1
    code, held, inputs = 1, b"", [0]
    try:
        c.sendall(("ATTACH %d %d %d\n" % (_SR_TAIL, rows, cols)).encode())
        while True:
            r, _, _ = select.select([c] + inputs, [], [])
            if c in r:
                data = c.recv(65536)
                if not data:
                    if held:
                        os.write(1, held)
                    break
                data = held + data
                at = data.find(_SR_EXIT)
                if at >= 0:
                    end = data.find(b"\x07", at)
                    if end < 0:
                        held = data
                        continue
                    os.write(1, data[:at])
                    try:
                        code = int(data[at + len(_SR_EXIT):end])
                    except ValueError:
                        code = 1
                    return code
                # Hold back a tail that could be the start of the exit mark, split across reads.
                keep = 0
                for n in range(1, len(_SR_EXIT)):
                    if data.endswith(_SR_EXIT[:n]):
                        keep = n
                held = data[len(data) - keep:] if keep else b""
                os.write(1, data[:len(data) - keep] if keep else data)
            if 0 in r:
                try:
                    data = os.read(0, 4096)
                except OSError:              # ssh went away: the session stays, this attach goes
                    break
                if not data:
                    # The end of the input is not the end of the session: a terminal that hangs
                    # up sends SIGHUP, which ends this attach anyway; a plain end of input (a
                    # pipe, /dev/null) only means nothing more will be typed.
                    if os.isatty(0):
                        break
                    inputs = []
                    continue
                c.sendall(data)
    except OSError:                          # the keeper went away under us (killed, the machine
        pass                                 # stopping): the session's own record says the rest
    finally:
        c.close()
    st = _sr_session_state(sdir)
    return st["exit"] if st["exit"] is not None else code


# ---------------------------------------------------------------- the drive (runs HERE)

def _sr_window_size():
    try:
        cols, rows = os.get_terminal_size(1)
        return rows, cols
    except OSError:
        return 40, 120


def _sr_drive(argv, link, detach_hint=""):
    """Run argv (an `ssh -tt ... attach`) on a pseudo-terminal of our own: what it prints goes to
    this terminal, what is typed goes to it, Ctrl-] leaves.  -> exit code, or None when detached.
    A ^C that reaches THIS process (no terminal: a piped run) leaves too, and is raised again as
    KeyboardInterrupt once the ssh is gone -- the session there keeps running either way.
    A sudo question there is answered with the kept password -- once per question."""
    import pty
    interactive = _sr_tty()
    rows, cols = _sr_window_size()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    _sr_set_size(fd, rows, cols)
    saved = None
    if interactive:
        saved = termios.tcgetattr(0)
        tty.setraw(0)

    def resized(signum, frame):
        _sr_set_size(fd, *_sr_window_size())
    old_winch = signal.signal(signal.SIGWINCH, resized) if interactive else None
    window, last_out, answered, refused, detached = b"", time.time(), 0, False, False
    interrupted = False
    try:
        while True:
            try:
                r, _, _ = select.select([fd] + ([0] if interactive else []), [], [], 0.25)
            except InterruptedError:
                continue
            if fd in r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(1, data)
                window = (window + data)[-2048:]
                last_out = time.time()
                if b"Sorry, try again" in data or b"incorrect password" in data:
                    refused = answered > 0
            if interactive and 0 in r:
                data = os.read(0, 4096)
                if _SR_DETACH in data:
                    detached = True
                    break
                if data:
                    os.write(fd, data)
                    window = b""
            # sudo asks, nothing has come for a moment (something is waiting), we have the password
            m = _SR_SUDO.search(window) if window else None
            if m and time.time() - last_out > 0.3 and not refused and answered < 20:
                user = m.group(1).decode("utf-8", "replace").strip()
                pw = link.password() if user == link.user else None
                window = b""
                if pw:
                    os.write(fd, pw.encode() + b"\n")
                    answered += 1
    except KeyboardInterrupt:                   # not a terminal: ^C leaves, the session stays
        interrupted = True
    finally:
        if saved is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        if old_winch is not None:
            signal.signal(signal.SIGWINCH, old_winch)
    if detached or interrupted:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        os.waitpid(pid, 0)
        try:
            os.close(fd)
        except OSError:
            pass
        _sr_say("\r\n%s -- it keeps running there.%s" % ("interrupted here" if interrupted else "detached",
                                                         detach_hint))
        if interrupted:
            raise KeyboardInterrupt()
        return None
    _, status = os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass
    return _sr_exit_code(status)


# ---------------------------------------------------------------- one machine, there

class RemoteMachine(object):
    """What the toolkit's main file and where_to_run() use: a login, the code there, quick
    commands, and sessions."""

    def __init__(self, target):
        self.link = RemoteLink(target)
        self.target = target

    def open(self):
        self.link.open()
        _sr_python(self.link)
        return self

    def close(self):
        self.link.close()

    def code(self, files, name):
        """Send files ([(here, there)]) unless they are there already -> the folder there."""
        return _sr_upload(self.link, files, name)

    def command(self, folder, entry, args):
        """The shell words that run entry there, with --local so it never asks again."""
        words = [self.link.python, "-I", "%s/%s" % (folder, entry), "--local"] + list(args)
        return " ".join(_sr_home_quote(w) for w in words)

    def capture(self, folder, entry, args, timeout=600):
        """Run it there, quickly, and take what it prints: (exit code, stdout, stderr)."""
        return self.link.run("cd && SATELLITE_TOOLKIT_WHERE=local " + self.command(folder, entry, args),
                             timeout=timeout)

    def sessions(self, folder, entry):
        rc, out, err = self.link.run("cd && " + " ".join(_sr_home_quote(w) for w in
                                     [self.link.python, "-I", "%s/%s" % (folder, entry), "--satellite-keeper-list"]),
                                     timeout=60)
        if rc != 0:
            raise RemoteError("could not list the sessions on %s: %s" % (self.target, (err or out).strip()))
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return []

    def start(self, folder, entry, args, name):
        """-> the session's name: name, or name-2, name-3 ... when one of that name is there."""
        rows, cols = _sr_window_size()
        words = ([self.link.python, "-I", "%s/%s" % (folder, entry), "--satellite-keeper=" + name,
                  "--satellite-size=%dx%d" % (rows, cols), "--", self.link.python, "-I", "%s/%s" % (folder, entry),
                  "--local"] + list(args))
        rc, out, err = self.link.run("cd && " + " ".join(_sr_home_quote(w) for w in words), timeout=60)
        word = (out.strip().splitlines() or [""])[-1].split()     # "ok NAME PID"
        if rc != 0 or len(word) < 2 or word[0] != "ok":
            raise RemoteError("could not start the session on %s: %s" % (self.target, (err or out).strip()))
        return word[1]

    def attach(self, folder, entry, name, hint=""):
        rows, cols = _sr_window_size()
        words = [self.link.python, "-I", "%s/%s" % (folder, entry), "--satellite-keeper-attach=" + name,
                 "--satellite-size=%dx%d" % (rows, cols)]
        argv = self.link.ssh("-tt", "-q", "-o", "ServerAliveInterval=30") + ["cd && " + " ".join(_sr_home_quote(w) for w in words)]
        return _sr_drive(argv, self.link, hint)


def _sr_home_quote(word):
    """Quote for the remote shell -- except a leading ~/, which stays $HOME."""
    if word.startswith("~/"):
        return '"$HOME"' + shlex.quote(word[1:])
    return shlex.quote(word)


def _sr_short(tool_name):
    """The first word of a tool's session names -- clang, python, linux_kernel ...  --attach
    alone looks for it."""
    short = re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"_tool$", "", tool_name.replace("enterprise_", "")))[:20]
    return short.strip("_") or "tool"


def _sr_session_name(tool_name):
    # Second resolution only: a second run started in the same second is made NAME-2 by the
    # keeper (_sr_keeper_main), which is where the name is claimed.
    return "%s-%s" % (_sr_short(tool_name), time.strftime("%Y%m%d-%H%M%S"))


def _sr_after_attach(machine, folder, entry, name, code, hint):
    """The attach ended with code.  That is the program's exit code only when the session ENDED
    there: a dropped connection ends ssh too (255, or 128+N when ssh was killed) while the build
    goes on.  So the machine is asked which it was."""
    try:
        found = [s for s in machine.sessions(folder, entry) if s.get("name") == name]
    except RemoteError:
        _sr_say("\n  the connection to %s dropped and it cannot be reached again -- session %s may "
                "still be running there.%s" % (machine.target, name, hint))
        return _SR_DROPPED
    if not found:                                # no such session there: the attach said so
        return code
    s = found[0]
    if s["state"] == "running":
        _sr_say("\n  the connection dropped -- it keeps running there.%s" % hint)
        return _SR_DROPPED
    if s["state"] == "ended" and s.get("exit") is not None:
        _sr_say("  session %s on %s ended: exit %d" % (name, machine.target, s["exit"]))
        return s["exit"]
    _sr_say("  session %s on %s stopped without an exit code" % (name, machine.target))
    return code or 1


def _sr_remote(tool_file, tool_name, rest, o, upload, entry, before=None):
    target = _sr_target(o, tool_name)
    _sr_split_target(target)
    machine = RemoteMachine(target)
    try:
        # RemoteMachine.open() in two halves, so the target is remembered once it let us in:
        # a typo is never offered again; a machine that let us in but lacks python3 still is.
        machine.link.open()
        _sr_remember(target, tool_name)
        _sr_python(machine.link)
        files = upload() if upload else [(os.path.abspath(tool_file), os.path.basename(tool_file))]
        entry = entry or os.path.basename(tool_file)
        folder = machine.code(files, tool_name)
        me = os.path.basename(tool_file)
        if o["sessions"]:
            rows = machine.sessions(folder, entry)
            sys.stdout.write("sessions on %s:\n" % target)
            for s in rows or []:
                state = s["state"] + (" (exit %s)" % s["exit"] if s.get("exit") is not None else "")
                words = s.get("argv", [])[3:]
                if words[:1] == ["--local"]:
                    words = words[1:]
                sys.stdout.write("  %-34s %-18s %s  %s\n" % (s["name"], state, s.get("started", ""),
                                 " ".join(words)[:80]))
            if not rows:
                sys.stdout.write("  none\n")
            return 0
        name = o["attach"]
        if name == "":                           # --attach alone: this tool's newest one there
            mine = [s for s in machine.sessions(folder, entry)
                    if s["name"].startswith(_sr_short(tool_name) + "-")]
            running = [s for s in mine if s["state"] == "running"]
            pick = (running or mine or [None])[-1]
            if pick is None:
                raise RemoteError("no session of %s on %s to attach to" % (tool_name, target))
            name = pick["name"]
        if name is None:
            if before is not None:
                # The caller's last word before a NEW session starts (never for --attach or
                # --sessions): the login, the python there and the upload are done.  It may send
                # more, and may change the arguments; a RemoteError from it starts nothing.
                changed = before(machine, folder, list(rest))
                if changed is not None:
                    rest = list(changed)
            name = machine.start(folder, entry, rest, _sr_session_name(tool_name))
            _sr_say("  %s runs there as session %s -- Ctrl-] leaves it running" % (entry, name))
        hint = "\n  back to it:  %s --remote=%s --attach=%s" % (me, target, name)
        code = machine.attach(folder, entry, name, hint)
        if code is None:                         # Ctrl-]: still running there -- not "done"
            return _SR_DETACHED
        return _sr_after_attach(machine, folder, entry, name, code, hint)
    except RemoteError as e:
        _sr_say("ERROR: %s" % e)
        return 1
    finally:
        machine.close()


def where_to_run(argv, tool_file, tool_name, upload=None, entry=None, before=None):
    """The first line of every tool's main(): -> the argv to run HERE, or it runs the tool on
    another machine and exits with what it exited with there (or 75 / 255 / 130 / 1: the
    module's docstring).  argv None is sys.argv[1:].  upload() -> [(path here, path there)]
    when more than the tool file goes; entry = which of them runs there; before: the docstring."""
    rest, o = _sr_take(list(sys.argv[1:] if argv is None else argv))
    if o["keeper"] is not None:
        sys.exit(_sr_keeper_main(o["keeper"], rest, o["size"]))
    if o["keeper_attach"] is not None:
        sys.exit(_sr_attach_here(o["keeper_attach"], o["size"]))
    if o["keeper_list"]:
        sys.exit(_sr_list_here())
    where = o["where"]
    if where is None and (o["attach"] is not None or o["sessions"]):
        where = "remote"
    if where is None and _sr_wants_question(rest):
        where = _sr_ask_where()
    if where != "remote":
        return rest
    try:
        code = _sr_remote(tool_file, tool_name, rest, o, upload, entry, before)
    except RemoteError as e:
        _sr_say("ERROR: %s" % e)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
# <<< satellite_remote <<<


# ------------------------------------------------------------------------ output

class UI(object):
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    tty = sys.stdout.isatty()
    stream = None             # None: stdout.  With --json, stderr: stdout is the JSON alone.


def _c(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if UI.color else text


def out(text=""):
    """Never raises: a closed pipe or a dead terminal must not turn into an exception in
    the middle of a rollback."""
    s = UI.stream or sys.stdout
    try:
        try:
            s.write(text + "\n")
        except UnicodeEncodeError:
            s.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        s.flush()
    except (OSError, IOError, ValueError):
        pass


def emit_json(data):
    sys.stdout.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
    sys.stdout.flush()


def json_mode():
    """--json: every message to stderr, the JSON alone on stdout."""
    UI.stream = sys.stderr
    UI.tty = False
    UI.color = sys.stderr.isatty() and "NO_COLOR" not in os.environ


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
    # The same stream as the rest: redirected to one log, two streams interleave out of
    # order, and a warning that lands three sections late is worse than none.
    out(_c("33", "warning:") + " " + msg)


def loud(msg):
    """A warning nobody can scroll past."""
    bar = "!" * 78
    out(_c("1;33", bar))
    for line in msg.splitlines():
        out(_c("1;33", "!! ") + line)
    out(_c("1;33", bar))


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


class UsageError(ToolError):
    """A wrong option: exit 2, like argparse's own."""


class Interrupted(ToolError):
    """The translation was stopped by a signal from outside (a ^C that reached RPython):
    exit 130, and say how to resume."""


# ----------------------------------------------------------------------- process

# An ALLOW-list.  RPython reads CC, CFLAGS and LDFLAGS from the environment
# (rpython/translator/platform/posix.py and linux.py), and on 2026-08-29 it picked up
# CC=clang-current from ~/.bashrc.  The proxy and CA variables are what an enterprise network
# needs for downloads, git and pip.
ENV_KEEP = ("HOME", "USER", "LOGNAME", "TERM", "LANG",
            "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "all_proxy", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO",
            "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "PIP_INDEX_URL", "PIP_TRUSTED_HOST")
SYSTEM_PATH = ["/usr/bin", "/usr/sbin", "/bin", "/sbin"]
# What a shell commonly exports that a translation or a CPython build would silently obey.
TRAP_VARS = ("CC", "CXX", "CPP", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LDSHARED", "LIBS",
             "AR", "AS", "LD", "RANLIB", "CPATH", "C_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
             "LD_RUN_PATH", "LD_PRELOAD", "PKG_CONFIG_PATH", "MAKEFLAGS", "PYTHONPATH", "PYTHONHOME",
             "PYTHONSTARTUP", "PYTHONUSERBASE", "PYPY_USESSION_DIR", "PYPY_USESSION_BASENAME",
             "PYPY_USESSION_KEEP", "PYPY_GC_MAX_DELTA", "PYPY_EMBED_DEPENDENCIES", "PYPY_REBUILD_EXTENSIONS",
             "PYPY_MAKE_PORTABLE", "LLVM", "DESTDIR")


def scrubbed_env(extra_path=None, extra=None):
    """The allow-list, PATH = [extra] + the system's directories (/usr/bin first), LC_ALL=C.
    No CC/CFLAGS/LDFLAGS/LD_LIBRARY_PATH/PYTHONPATH: whoever needs one sets it explicitly."""
    env = dict((k, os.environ[k]) for k in ENV_KEEP if k in os.environ)
    env["PATH"] = ":".join(list(extra_path or []) + SYSTEM_PATH)
    env["LC_ALL"] = "C"
    env.update(extra or {})
    return env


def env_traps(environ=None):
    environ = os.environ if environ is None else environ
    found = []
    for var in sorted(environ):
        if var in TRAP_VARS:
            val = environ[var]
            found.append("%s=%s" % (var, val if len(val) < 60 else val[:57] + "..."))
    return found


def which(name, path=None):
    return shutil.which(name, path=path if path is not None else ":".join(SYSTEM_PATH))


def run(cmd, cwd=None, env=None, check=True, capture=True, stdin_data=None,
        interactive=False, timeout=None):
    """subprocess.run with 3.6-safe arguments; stdin is /dev/null unless asked for."""
    kw = dict(cwd=cwd, env=env, universal_newlines=True, errors="replace", timeout=timeout)
    if capture and not interactive:
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
        raise ToolError("timed out: %s" % " ".join(cmd))
    except OSError as e:
        # ^C under a root-owned sudo child: the kill is refused with EPERM, and that must
        # stay a ^C.  An exec refused with EPERM (fapolicyd) is not one.
        if e.errno == errno.EPERM and isinstance(getattr(e, "__context__", None), KeyboardInterrupt):
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
        d = json.loads(read_text(path) or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def write_atomic(path, text):
    """Whole or absent: written beside, synced, renamed over."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
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
    """Of the nearest EXISTING parent: on a fresh machine the directory is not there yet."""
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


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def tail(text, n=25):
    return "\n".join("    " + l for l in (text or "").rstrip().splitlines()[-n:])


def fit_line(text, cols=None):
    cols = cols or shutil.get_terminal_size((100, 24)).columns
    return text[:max(cols - 1, 20)]


def expand(path):
    return os.path.abspath(os.path.expanduser(path)) if path else ""


def rmtree(path):
    """A tree, a file or a link -- whichever it is; gone afterwards either way."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------- versions

def numver(text):
    m = re.search(r"(\d+(?:\.\d+)+|\d+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def ver_ge(have, need):
    n = max(len(have), len(need))
    return tuple(have) + (0,) * (n - len(have)) >= tuple(need) + (0,) * (n - len(need))


def vstr(v):
    return ".".join(str(x) for x in v) if v else "?"


def release_key(rel):
    """8.0.0 > 8.0.0rc2 > 8.0.0rc1 > 7.3.23.  Anything else (nightly) sorts first."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$", rel or "")
    if not m:
        return (-1,)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1, int(m.group(4) or 0))


def is_rc(rel):
    return "rc" in (rel or "")


def series_key(series):
    return numver(series)


# --------------------------------------------------------------------- detection

def parse_os_release(text):
    d = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


PKG_MANAGER_OF = (  # (manager, the ID / ID_LIKE words of /etc/os-release that mean it)
    ("apt-get", ("debian", "ubuntu")), ("zypper", ("suse", "opensuse", "sles", "sled")),
    ("pacman", ("arch", "manjaro")),
    ("dnf", ("fedora", "rhel", "centos", "almalinux", "rocky", "amzn", "ol")))
PKG_MANAGERS = ("dnf", "apt-get", "zypper", "pacman")


def preferred_pkg_managers(osr):
    ids = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).lower().split()
    return [pm for pm, names in PKG_MANAGER_OF if any(i == n or i.startswith(n + "-") for i in ids for n in names)]


def find_pkg_manager(osr=None, have=None):
    """dnf, apt-get, zypper, pacman or ''.  The one that fits the distribution first: a dnf
    installed on Ubuntu for rpm work is not Ubuntu's package manager."""
    osr = parse_os_release(read_text("/etc/os-release")) if osr is None else osr
    have = have or which
    for pm in preferred_pkg_managers(osr) + list(PKG_MANAGERS):
        if have(pm):
            return pm
    return ""


def meminfo_gb(text=None):
    """-> (MemTotal, MemAvailable, SwapFree) in GB."""
    text = read_text("/proc/meminfo") if text is None else text
    vals = dict((m.group(1), int(m.group(2))) for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M))
    total = vals.get("MemTotal", 8 << 20) / float(1 << 20)
    avail = vals.get("MemAvailable", vals.get("MemFree", 4 << 20)) / float(1 << 20)
    swap = vals.get("SwapFree", 0) / float(1 << 20)
    return total, avail, swap


def cpu_count():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def glibc_version():
    """(2, 39) -- or () where this is not glibc (musl), which no PyPy binary runs on."""
    try:
        v = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError, AttributeError):
        v = ""
    return numver(v) if v.startswith("glibc") else ()


def arch_tag(machine=None):
    """The word PyPy's binary tarballs use for this architecture, or ''."""
    return ARCH_TAGS.get(machine or os.uname().machine, "")


def selinux_mode():
    v = read_text("/sys/fs/selinux/enforce").strip()
    if v == "1":
        return "enforcing"
    if v == "0":
        return "permissive"
    return "disabled" if not os.path.isdir("/sys/fs/selinux") else "unknown"


def crb_hint(machine):
    """How the repository with the extra -devel packages is enabled on Enterprise Linux: crb
    (EL 9+), powertools (EL 8) or, on RHEL itself, a subscription-manager channel."""
    osid, like = machine.get("os_id", ""), machine.get("os_like", "")
    major = (re.match(r"\d+", machine.get("os_version", "") or "") or re.match(r"", "")).group(0)
    if osid == "rhel":
        return "sudo subscription-manager repos --enable codeready-builder-for-rhel-%s-%s-rpms" % (
            major or "9", machine.get("arch", "x86_64"))
    if osid in ("almalinux", "rocky", "centos") or (osid != "fedora" and "rhel" in like.split()):
        return "sudo dnf config-manager --set-enabled %s" % ("powertools" if major == "8" else "crb")
    return ""


def el_major(machine):
    """10 on AlmaLinux 10.2, 0 where this is not Enterprise Linux."""
    osid, like = machine.get("os_id", ""), machine.get("os_like", "")
    if osid in ("rhel", "almalinux", "rocky", "centos", "ol") or (osid != "fedora" and "rhel" in like.split()):
        m = re.match(r"\d+", machine.get("os_version", "") or "")
        return int(m.group(0)) if m else 0
    return 0


# ------------------------------------------------------------- compilers, binutils

def compiler_info(cc):
    """-> {path, kind, version, banner} or None.  The compiler is ASKED; a name like `gcc`
    says nothing (on some systems it is clang)."""
    if not cc or not os.path.isfile(cc) or not os.access(cc, os.X_OK):
        return None
    try:
        o = run([cc, "--version"], check=False, env=scrubbed_env(), timeout=30).stdout or ""
    except ToolError:
        return None
    banner = (o.strip().splitlines() or [""])[0]
    if "clang version" in o:
        kind, ver = "clang", numver(o.split("clang version", 1)[1])
    elif "Free Software Foundation" in o or re.search(r"\bg(cc|\+\+)\b", banner, re.I):
        kind = "gcc"
        try:
            d = run([cc, "-dumpfullversion", "-dumpversion"], check=False, env=scrubbed_env(), timeout=30).stdout
        except ToolError:
            d = ""
        ver = numver(d) or numver(banner)
    else:
        return None
    return {"path": cc, "kind": kind, "version": ver, "banner": banner}


C_HELLO = "#include <stdio.h>\nint main(void){puts(\"ok-c\");return 0;}\n"


def c_compiler_works(cc, extra_path=None, scratch=None, flags=()):
    """Compile, link and RUN a C program in the scrubbed environment the build will have --
    in the work directory, because /tmp may be mounted noexec.  -> (works, why)."""
    if scratch:
        os.makedirs(scratch, exist_ok=True)
    d = tempfile.mkdtemp(prefix="eppt-cc-", dir=scratch)
    env = scrubbed_env(extra_path)
    try:
        src, exe = os.path.join(d, "t.c"), os.path.join(d, "t")
        with open(src, "w") as f:
            f.write(C_HELLO)
        p = run([cc] + list(flags) + [src, "-o", exe], check=False, env=env, timeout=120)
        if p.returncode != 0:
            lines = [l for l in (p.stdout or "").splitlines() if "rror" in l] or \
                (p.stdout or "").strip().splitlines() or ["compile failed"]
            return False, lines[0].strip()[:160]
        try:
            r = run([exe], check=False, env=env, timeout=30)
        except ToolError as e:
            return False, ("a program cannot be run from %s (%s): is that filesystem noexec?"
                           % (d, str(e).strip().splitlines()[0][-60:]))
        if r.returncode != 0 or "ok-c" not in (r.stdout or ""):
            return False, "its programs do not RUN in a clean environment: " + \
                ((r.stdout or "").strip().splitlines() or ["?"])[-1][:120]
        return True, "compiles, links and runs a C program"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def have_header(header, cc="", extra_path=None):
    """Asked of the compiler when there is one (it knows its own search path, multiarch
    directories included); otherwise the usual directories."""
    if cc:
        try:
            p = run([cc, "-E", "-x", "c", "-"], check=False, env=scrubbed_env(extra_path),
                    stdin_data="#include <%s>\n" % header, timeout=60)
            return p.returncode == 0
        except ToolError:
            pass
    for base in ["/usr/include", "/usr/local/include"] + glob.glob("/usr/include/*-linux-gnu*"):
        if os.path.exists(os.path.join(base, header)):
            return True
    return False


def resolve_cc(asked, opt_root):
    """-> {path, kind, version, banner, why} or None.  A REAL path (the toolkit's contract):
    <opt>/gcc-current is resolved the moment the step starts, so a later rebuild of
    gcc-current does not silently change what this build used."""
    if asked:
        p = os.path.realpath(expand(asked))
        ci = compiler_info(p)
        if not ci:
            raise UsageError("--cc %s: not a C compiler that answers --version" % asked)
        ci["why"] = "--cc"
        return ci
    cur = os.path.join(opt_root, "gcc-current", "bin", "gcc")
    if os.path.lexists(cur):
        ci = compiler_info(os.path.realpath(cur))
        if ci:
            ci["path"] = os.path.realpath(cur)
            ci["why"] = "%s (step 4's gcc), as its real path" % cur
            return ci
        warn("%s is there but does not answer --version: using the system's gcc" % cur)
    for c in ("/usr/bin/gcc", which("gcc") or "", "/usr/bin/cc"):
        ci = compiler_info(c)
        if ci:
            ci["path"] = os.path.realpath(c)
            ci["why"] = "the system's %s (no working %s; step 4 makes one)" % (c, cur)
            return ci
    return None


def resolve_binutils(asked, opt_root):
    """-> (prefix or '', why).  --binutils DIR is a prefix whose bin/ holds as and ld (a bin
    directory itself is accepted too); 'system' or nothing found: whatever the compiler finds."""
    def has_pair(d):
        return all(os.access(os.path.join(d, t), os.X_OK) for t in ("as", "ld"))
    if asked and asked not in ("auto", "system"):
        d = os.path.realpath(expand(asked))
        if has_pair(os.path.join(d, "bin")):
            return d, "--binutils"
        if has_pair(d):
            return os.path.dirname(d), "--binutils (its bin directory)"
        raise UsageError("--binutils %s: no bin/as and bin/ld there" % asked)
    if asked == "system":
        return "", "--binutils system: the as/ld the compiler finds"
    cur = os.path.join(opt_root, "binutils-current")
    if has_pair(os.path.join(cur, "bin")):
        return os.path.realpath(cur), "%s (step 3's binutils), as its real path" % cur
    return "", "the as/ld the compiler finds (no %s yet)" % cur


def gcc_major(ci):
    return ci["version"][0] if ci and ci.get("kind") == "gcc" and ci.get("version") else 0


TOOL_GLOBS = ["~/opt/*/bin", "~/.local/bin", "/usr/local/bin", "/opt/*/bin", "/opt/rh/*/root/usr/bin"]
COMPILER_NAME = re.compile(r"^(gcc|clang)(-\d+(\.\d+)*)?$")
PYTHON2_NAME = re.compile(r"^(pypy|pypy2|pypy2\.7|python2|python2\.7)$")
PYPY3_NAME = re.compile(r"^pypy3(\.\d+)?$")


def tool_search_dirs(home=None, path_env=None):
    home = home or os.path.expanduser("~")
    dirs = list(SYSTEM_PATH)
    for d in (path_env if path_env is not None else os.environ.get("PATH", "")).split(":"):
        if d and os.path.isabs(d) and d not in dirs:
            dirs.append(d)
    for g in TOOL_GLOBS:
        for d in sorted(glob.glob(g.replace("~", home, 1))):
            if d not in dirs:
                dirs.append(d)
    return [d for d in dirs if os.path.isdir(d)]


def scan_names(dirs, pattern):
    """Executables whose name matches, de-duplicated by what they really are."""
    found, seen = [], set()
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if pattern.match(n) and os.path.isfile(p) and os.access(p, os.X_OK):
                rp = os.path.realpath(p)
                if rp not in seen:
                    seen.add(rp)
                    found.append(p)
    return found


def find_compilers(dirs=None):
    out_ = []
    for p in scan_names(tool_search_dirs() if dirs is None else dirs, COMPILER_NAME):
        ci = compiler_info(p)
        if ci:
            out_.append(ci)
    return out_


def find_binutils(dirs=None):
    """Every GNU binutils set: a directory holding both `as` and `ld`."""
    sets, seen = [], set()
    for d in (tool_search_dirs() if dirs is None else dirs):
        a_, l_ = os.path.join(d, "as"), os.path.join(d, "ld")
        if not (os.access(a_, os.X_OK) and os.access(l_, os.X_OK)) or os.path.realpath(a_) in seen:
            continue
        seen.add(os.path.realpath(a_))
        try:
            line = ((run([a_, "--version"], check=False, env=scrubbed_env(), timeout=20).stdout or "").splitlines() or [""])[0]
        except ToolError:
            continue
        if "GNU assembler" in line:
            sets.append({"dir": d, "version": numver(re.sub(r"^.*?(\d+\.\d+)", r"\1", line)),
                         "banner": line.strip(), "system": os.path.realpath(d) == "/usr/bin"})
    return sets


# ----------------------------------------------------------- the PyPy release list

FILE_RE = re.compile(r"pypy(2\.7|3\.\d+)-v(\d+\.\d+\.\d+(?:rc\d+)?)-(src|linux64|aarch64|linux32|s390x)"
                     r"\.(tar\.bz2|tar\.gz)(?![\w.])")
TAG_RE = re.compile(r"refs/tags/release-pypy(2\.7|3\.\d+)-v(\d+\.\d+\.\d+(?:rc\d+)?)$", re.M)
CHECKSUM_RE = re.compile(r"([0-9a-f]{64})\s+(pypy[\w.+-]+?\.(?:tar\.bz2|tar\.gz|zip))\b")


def _prefer(old, new):
    """Both a .tar.bz2 and a .tar.gz of one file: the .tar.gz (what 7.3.23 and 8.x ship)."""
    return new if not old or new.endswith(".tar.gz") else old


def parse_index(html):
    """downloads.python.org/pypy/ -> {release: {"src": {series: file}, "bin": {series: {tag: file}}}}.
    The listing carries every release for every Python level at once (pypy2.7-v8.0.0-src...,
    pypy3.12-v8.0.0-linux64...), as .tar.bz2 or .tar.gz -- which one has changed over time."""
    rels = {}
    for m in FILE_RE.finditer(html or ""):
        series, rel, kind = m.group(1), m.group(2), m.group(3)
        r = rels.setdefault(rel, {"src": {}, "bin": {}})
        if kind == "src":
            r["src"][series] = _prefer(r["src"].get(series), m.group(0))
        else:
            b = r["bin"].setdefault(series, {})
            b[kind] = _prefer(b.get(kind), m.group(0))
    return rels


def parse_versions_json(raw):
    """-> ({(release, series): {stable, latest, date, python}}, binaries as parse_index's).
    versions.json lists BINARIES only -- never a source tarball (the .sh's lesson)."""
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except ValueError:
        raise ToolError("versions.json is not JSON (a proxy's block page?)")
    flags, rels = {}, {}
    for e in data if isinstance(data, list) else []:
        rel, pv = str(e.get("pypy_version") or ""), str(e.get("python_version") or "")
        if release_key(rel) == (-1,) or not re.match(r"^\d+\.\d+", pv):
            continue                                            # nightly
        series = ".".join(pv.split(".")[:2])
        flags[(rel, series)] = {"stable": bool(e.get("stable")), "latest": bool(e.get("latest_pypy")),
                                "date": str(e.get("date") or ""), "python": pv}
        for f in e.get("files") or []:
            m = FILE_RE.match(str(f.get("filename") or ""))
            if m and m.group(3) != "src":
                b = rels.setdefault(rel, {"src": {}, "bin": {}})["bin"].setdefault(m.group(1), {})
                b[m.group(3)] = _prefer(b.get(m.group(3)), m.group(0))
    return flags, rels


def parse_git_tags(text):
    """`git ls-remote --tags` of the PyPy repository -> {release: set(series)}: every release
    tag is a source tree, downloadable from GitHub when downloads.python.org is not reachable."""
    out_ = {}
    for m in TAG_RE.finditer(text or ""):
        out_.setdefault(m.group(2), set()).add(m.group(1))
    return out_


def parse_checksums(html):
    return dict((m.group(2), m.group(1)) for m in CHECKSUM_RE.finditer(html or ""))


def parse_notes_beta(text):
    """The release notes' title: 'PyPy v8.0.1: release of python 2.7, 3.11, and 3.12 beta ...'
    -> ({'3.12'}, the title).  Only the title: the body talks about older betas too."""
    for line in (text or "").splitlines()[:8]:
        if "release of python" in line.lower():
            return set(re.findall(r"(\d\.\d+)\s+beta", line)), line.strip()
    return set(), ""


class Catalog(object):
    """What upstream has: releases, which flavours have a source tarball, which have a binary
    for which architecture, the stable flags and dates, and which series it calls beta."""

    def __init__(self, releases, flags=None, origin="", notes=None, checksums=None):
        self.releases = releases or {}
        self.flags = flags or {}
        self.origin = origin
        self.notes = notes                    # release -> the notes' text (may raise ToolError)
        self.checksums = checksums            # {file: sha256} or None (not fetched yet)
        self._beta = {}

    def unstable(self, rel):
        if is_rc(rel):
            return True
        marks = [v["stable"] for (r, _), v in self.flags.items() if r == rel]
        return bool(marks) and not any(marks)

    def all_releases(self, include_rc=False):
        rs = [r for r in self.releases if release_key(r) != (-1,) and (include_rc or not self.unstable(r))]
        return sorted(rs, key=release_key, reverse=True)

    def src_series(self, rel):
        return sorted(self.releases.get(rel, {}).get("src", {}), key=series_key, reverse=True)

    def has_src(self, rel, series):
        return series in self.releases.get(rel, {}).get("src", {})

    def src_name(self, rel, series):
        """The file name, or '' when the list came from git tags (the name is then guessed)."""
        return self.releases.get(rel, {}).get("src", {}).get(series) or ""

    def bin_name(self, rel, series, tag):
        return self.releases.get(rel, {}).get("bin", {}).get(series, {}).get(tag, "")

    def date(self, rel):
        return next((v["date"] for (r, _), v in sorted(self.flags.items()) if r == rel and v.get("date")), "")

    def beta(self, rel, series):
        """-> (is beta, why).  Release notes first (their title says it), then versions.json's
        stable flag, then what the notes said when this tool was written."""
        k = (rel, series)
        if k in self._beta:
            return self._beta[k]
        res = None
        if self.notes:
            try:
                betas, title = parse_notes_beta(self.notes(rel))
                if title:
                    res = (True, "the %s release notes call it beta (\"%s\")" % (rel, title[:90])) \
                        if series in betas else (False, "the %s release notes do not call it beta" % rel)
            except ToolError:
                res = None
        if res is None:
            fl = self.flags.get(k)
            if fl is not None and not fl["stable"]:
                res = (True, "versions.json marks %s of %s stable: false" % (series, rel))
            elif series in KNOWN_BETA.get(rel, ()):
                res = (True, "the %s release notes called it beta when this tool was written "
                             "(they could not be read now)" % rel)
            else:
                res = (False, "nothing upstream calls it beta")
        self._beta[k] = res
        return res


def pick_release(cat, series, want):
    """-> (release, why).  auto: the newest release that ships this flavour's source;
    latest: the same, release candidates and stable:false entries included; X.Y.Z: exactly it."""
    flavour = "pypy" + series
    if want not in ("auto", "latest"):
        if not re.match(r"^\d+\.\d+\.\d+(rc\d+)?$", want or ""):
            raise UsageError("--pypy-version is auto, latest or X.Y.Z (like 8.0.0), not %r" % want)
        if cat.releases and not cat.has_src(want, series):
            have = [r for r in cat.all_releases(True) if cat.has_src(r, series)][:8]
            raise ToolError("PyPy %s has no %s source upstream (%s).  Releases with one: %s"
                            % (want, flavour, cat.origin or "the release list", ", ".join(have) or "none"))
        return want, "asked for (--pypy-version %s)" % want
    for rel in cat.all_releases(include_rc=(want == "latest")):
        if cat.has_src(rel, series):
            return rel, "%s: the newest release that ships the %s source (%s)" % (want, flavour, cat.origin)
    raise ToolError("no release in %s ships a %s source tarball" % (cat.origin or "the release list", flavour))


def pick_pypy3(cat, want_version, want_series):
    """-> (release, series, [why lines]).  --series auto = the newest pypy3.N upstream does
    NOT call beta; latest = the newest even if beta; 3.N = that series."""
    why = []
    if want_series not in ("auto", "latest"):
        if not re.match(r"^3\.\d+$", want_series or ""):
            raise UsageError("--series is auto, latest or 3.N (like 3.11), not %r" % want_series)
        rel, w = pick_release(cat, want_series, want_version)
        is_beta, bwhy = cat.beta(rel, want_series)
        why.append("pypy%s: asked for (--series %s)%s" % (want_series, want_series,
                                                          "; NOTE: %s" % bwhy if is_beta else ""))
        why.append("release %s: %s" % (rel, w))
        return rel, want_series, why
    if want_version not in ("auto", "latest"):
        rels = [want_version]
        if cat.releases and not any(s.startswith("3.") for s in cat.src_series(want_version)):
            raise ToolError("PyPy %s ships no pypy3 source (%s)" % (want_version, cat.origin))
    else:
        rels = cat.all_releases(include_rc=(want_version == "latest"))
    first = None
    for rel in rels:
        cands = [s for s in cat.src_series(rel) if s.startswith("3.")] if cat.releases else []
        for s in cands:
            if first is None:
                first = (rel, s)
            if want_series == "latest":
                why.append("pypy%s: --series latest takes the newest series, beta or not" % s)
                why.append("release %s: the newest that ships it (%s)" % (rel, cat.origin))
                return rel, s, why
            is_beta, bwhy = cat.beta(rel, s)
            if is_beta:
                why.append("pypy%s of %s is skipped: %s" % (s, rel, bwhy))
                continue
            why.append("pypy%s: the newest series upstream does not call beta (%s)" % (s, bwhy))
            why.append("release %s: the newest that ships it (%s)" % (rel, cat.origin))
            if any("skipped" in w for w in why):
                why.append("--series %s (or --series latest) builds the newer one anyway" % first[1])
            return rel, s, why
    if first:
        why.append("every pypy3 series is called beta: taking the newest, pypy%s of %s" % (first[1], first[0]))
        return first[0], first[1], why
    raise ToolError("no pypy3 source in %s" % (cat.origin or "the release list"))


def prebuilt_host_choice(cat, tag, glibc):
    """-> (release, file) of the official PyPy2 binary for this architecture: the newest
    stable one, but 7.3.x where glibc is older than 2.28 (8.x binaries need 2.28)."""
    if not tag:
        raise ToolError("PyPy publishes no Linux binary for %s: use --host cpython27" % os.uname().machine)
    old_glibc = bool(glibc) and not ver_ge(glibc, GLIBC_FLOOR_V8)
    for rel in cat.all_releases():
        name = cat.bin_name(rel, "2.7", tag)
        if not name:
            continue
        if old_glibc and release_key(rel) >= release_key("8.0.0"):
            continue
        return rel, name
    raise ToolError("no prebuilt PyPy2 for %s in %s%s" % (tag, cat.origin or "the release list",
                                                          " that runs on glibc %s" % vstr(glibc) if old_glibc else ""))


# ------------------------------------------------------------------- downloading

def decode_body(body, encoding=""):
    """downloads.python.org serves its listing gzip-encoded (the .sh's --compressed lesson):
    without decoding, every pattern matches nothing and a healthy mirror looks empty."""
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError):
            return body
    return body


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return decode_body(r.read(), r.headers.get("Content-Encoding", "") or "")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise ToolError("cannot fetch %s: %s" % (url, e))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url, dest, sha256="", quiet=False):
    """To dest.part, checked, then renamed: a file with the final name is whole."""
    if os.path.isfile(dest) and (not sha256 or sha256_file(dest) == sha256):
        if not quiet:
            ok("already here: %s" % dest)
        return
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
    if total and got != total:
        raise ToolError("download was cut short: %s of %s from %s" % (human(got), human(total), url))
    if sha256:
        have = sha256_file(part)
        if have != sha256:
            os.unlink(part)
            raise ToolError("checksum mismatch for %s\n  expected %s\n  got      %s" % (url, sha256, have))
        if not quiet:
            ok("sha256 matches")
    elif not quiet:
        note("no published checksum for this file: its integrity rests on TLS alone")
    os.replace(part, dest)
    if not quiet:
        ok("downloaded %s in %s" % (human(got), hms(time.time() - started)))


def fetch_any(cands, ddir, offline=False, fetch=None):
    """cands: [(url, file name, sha256)] in order.  The first already in ddir (with a matching
    sha256 when one is known) is used; otherwise each is tried in turn.  -> (path, url, sha256)."""
    fetch = fetch or download
    for url, name, sha in cands:
        p = os.path.join(ddir, name)
        if os.path.isfile(p) and (not sha or sha256_file(p) == sha):
            ok("already here: %s" % p)
            return p, url, sha or sha256_file(p)
    if offline:
        raise ToolError("--offline and none of these is in %s: %s" % (ddir, ", ".join(c[1] for c in cands)))
    errors = []
    for url, name, sha in cands:
        info("fetching %s" % url)
        try:
            fetch(url, os.path.join(ddir, name), sha)
            return os.path.join(ddir, name), url, sha or sha256_file(os.path.join(ddir, name))
        except ToolError as e:
            errors.append(str(e).splitlines()[0])
            note("that did not work: %s" % errors[-1][:150])
    raise ToolError("could not download any of:\n    " + "\n    ".join(errors))


def git_archive(url, ref, dest_tar, top, scratch):
    """`git clone --depth 1 --branch REF` -> a tarball of it (top directory `top`): the same
    thing a release tarball is, from github.com (which answers where downloads.python.org
    is blocked).  -> the commit id."""
    if not which("git"):
        raise ToolError("git is not installed: the git fallback cannot be used")
    os.makedirs(scratch, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="git-", dir=scratch)
    try:
        info("git clone --depth 1 --branch %s %s" % (ref, url))
        p = run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url, os.path.join(tmp, "t")],
                check=False, env=scrubbed_env(extra={"GIT_TERMINAL_PROMPT": "0"}), timeout=3600)
        if p.returncode != 0:
            raise ToolError("git clone of %s at %s failed:\n%s" % (url, ref, tail(p.stdout, 6)))
        commit = (run(["git", "-C", os.path.join(tmp, "t"), "rev-parse", "HEAD"], env=scrubbed_env()).stdout or "").strip()
        os.makedirs(os.path.dirname(dest_tar), exist_ok=True)
        run(["git", "-C", os.path.join(tmp, "t"), "archive", "--format=tar", "--prefix=%s/" % top,
             "-o", dest_tar + ".part", "HEAD"], env=scrubbed_env(), timeout=3600)
        os.replace(dest_tar + ".part", dest_tar)
        return commit
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract(tarball, dest_dir):
    """Into a temp dir beside the destination, then renamed: a tree with the final name is
    complete.  GNU tar, not tarfile: before Python 3.12 tarfile has no member filter."""
    parent = os.path.dirname(dest_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".extract-", dir=parent)
    try:
        run(["tar", "-xf", tarball, "-C", tmp, "--no-same-owner"], env=scrubbed_env(), timeout=3600)
        tops = os.listdir(tmp)
        if len(tops) != 1 or not os.path.isdir(os.path.join(tmp, tops[0])):
            raise ToolError("%s does not hold exactly one directory" % tarball)
        os.rename(os.path.join(tmp, tops[0]), dest_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ state, locks

class State(object):
    """<workdir>/state.json, stamped with the tool's name (what `clean` checks before it
    deletes anything), written whole and atomically on every change."""

    def __init__(self, path):
        self.path = path
        self.d = read_json(path)
        if self.d is None:
            if os.path.exists(path):
                warn("%s is unreadable -- starting a fresh record" % path)
            self.d = {}
        self.d.setdefault("builds", {})
        self.d.setdefault("hosts", {})

    def save(self):
        self.d["tool"] = TOOL
        write_atomic(self.path, json.dumps(self.d, indent=1, sort_keys=True) + "\n")

    def build(self, key):
        b = self.d["builds"].setdefault(key, {})
        b.setdefault("stages", {})
        return b

    def set_build(self, key, **kv):
        self.build(key).update(kv)
        self.save()

    def stage(self, key, name):
        return self.build(key)["stages"].get(name, {})

    def set_stage(self, key, name, **kv):
        self.build(key)["stages"].setdefault(name, {}).update(kv)
        self.save()

    def done(self, key, name):
        return self.stage(key, name).get("status") == "done"

    def reset(self, key, names):
        st = self.build(key)["stages"]
        for n in names:
            st.pop(n, None)
        self.save()


_LOCKS = []


def lock_dir(path):
    """One instance per work directory: two translations at once would fight over the RAM,
    and two runs fetching the same file would write it twice."""
    try:
        os.makedirs(path, exist_ok=True)
        f = open(os.path.join(path, ".lock"), "a")
    except OSError as e:
        raise ToolError("cannot use the work directory %s: %s" % (path, e))
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as e:
        f.close()
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            raise ToolError("another %s is working in %s right now" % (TOOL, path))
        raise ToolError("cannot lock %s: %s  (a filesystem without locking -- NFS without lockd?)" % (path, e))
    try:
        f.seek(0)
        f.truncate()
        f.write("%d\n" % os.getpid())
        f.flush()
    except (OSError, IOError):
        pass
    _LOCKS.append(f)


def unlock_all():
    while _LOCKS:
        f = _LOCKS.pop()
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()
        except (OSError, IOError, ValueError):
            pass


def lock_is_held(path):
    """Whether some process holds the directory's lock -- a record that says `running` while
    nobody does belongs to a run that was killed."""
    try:
        f = open(os.path.join(path, ".lock"), "r")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        return True
    finally:
        f.close()
    return False


# --------------------------------------------------------------- running things

ERRORISH = re.compile(r"(\berror:|\bfatal error\b|undefined reference|^make(\[\d+\])?: \*\*\*|\bKilled\b|"
                      r"Segmentation fault|^ERROR:|\[translation:ERROR\]|MemoryError|^configure: error)")
OOMISH = re.compile(r"(Killed signal|^Killed\b|\bKilled$|signal 9|[Oo]ut of memory|MemoryError|std::bad_alloc|"
                    r"Cannot allocate memory|virtual memory exhausted)", re.M)
DOTS = re.compile(r"^[.+*%#\s]+$")       # RPython's mandelbrot: progress, not output


class LineWatcher(object):
    """The plain watcher: echoes the first error lines.  The translation's is below."""

    def __init__(self, label):
        self.label = label
        self.shown = 0
        self.last = ""

    def feed(self, text, elapsed):
        if text.strip() and not DOTS.match(text):
            self.last = text.strip()
        if ERRORISH.search(text) and "warning:" not in text:
            self.shown += 1
            if self.shown <= 40:
                return ["    " + text[:300]]
        return []

    def status(self, elapsed):
        return "  [%s] %s  %s" % (hms(elapsed), self.label, self.last)


def run_logged(cmd, cwd, env, log_path, watcher=None, verbose=False, label=""):
    """Run cmd; everything goes to log_path.  The console gets a one-line status on a terminal
    (a heartbeat every five minutes otherwise) and what the watcher picks out: phases and the
    first errors.  stdin is /dev/null -- nothing can wait for a keyboard.  The child runs in its
    own session: a SIGHUP from a closing terminal reaches the tool, not the build; a ^C reaches
    the build through the killpg below.  -> (exit code, seconds)."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if os.path.exists(log_path):
        os.replace(log_path, log_path + ".prev")
    watcher = watcher or LineWatcher(label or os.path.basename(cmd[0]))
    started = time.time()
    beat, drawn = started, 0.0
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8",
                                errors="replace", bufsize=1, start_new_session=True)
    except OSError as e:
        raise ToolError("cannot run %s: %s" % (cmd[0], e))
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write("# %s\n# cwd: %s\n" % (" ".join(shlex.quote(c) for c in cmd), cwd))
            for line in proc.stdout:
                log.write(line)
                text = line.rstrip("\n")
                el = time.time() - started
                if verbose:
                    out(text)
                    continue
                said = watcher.feed(text, el)
                if said:
                    clear_status()
                    for s in said:
                        out(s)
                if UI.tty:
                    if said or time.time() - drawn >= 0.2:
                        drawn = time.time()
                        status_line(fit_line(watcher.status(el)))
                elif time.time() - beat >= 300:
                    beat = time.time()
                    out(watcher.status(el))
        rc = proc.wait()
        proc.stdout.close()
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except OSError:
            pass
        try:
            proc.communicate(timeout=60)
        except (subprocess.TimeoutExpired, ValueError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        raise
    finally:
        clear_status()
    return rc, int(time.time() - started)


def error_summary(log_path, limit=14):
    """The FIRST real errors: the tail of a -j8 build is the seven other jobs."""
    hits = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if ERRORISH.search(line) and "warning:" not in line and not line.startswith("#"):
                    hits.append("%7d: %s" % (n, line.rstrip()[:300]))
                    if len(hits) >= limit:
                        break
    except OSError:
        pass
    return hits


# ------------------------------------------------------- watching a translation

# rpython/translator/driver.py's task titles, in the order a JIT translation runs them; the
# last one is targetpypystandalone's own (it runs build_cffi_imports.py with the new binary).
PHASES = ("Annotating&simplifying", "RTyping", "JIT compiler generation", "lltype back-end optimisations",
          "inserting stack checks", "Creating database for generating c source", "Generating c source",
          "Compiling c source", "Create cffi bindings for modules")
PHASE_RE = re.compile(r"^\[translation:info\] (.+?)\.\.\.\s*$")
USESSION_RE = re.compile(r"usession directory: (\S+)")
WRITTEN_RE = re.compile(r"\[translation:info\] written: (\S+)")
CREATED_RE = re.compile(r"\[translation:info\] created: (\S+)")
TIMER_RE = re.compile(r"^\[Timer\] (\S.*?)\s+---\s+([\d.]+) s")
C_ERROR_RE = re.compile(r"(\berror:|undefined reference|^make(\[\d+\])?: \*\*\*|internal compiler error)")
NCURSES_RE = re.compile(r"no _curses or _minimal_curses module|failed to guess where ncurses is installed")
C23_RE = re.compile(r"(too many arguments to function|expected identifier or '\(' before '(true|false|bool)'|"
                    r"'bool' cannot be defined|two or more data types in declaration specifiers|"
                    r"conflicting types for '(bool|true|false)')")


class TranslationWatcher(object):
    """Picks out of RPython's output: the phase (a line on the console and the status line),
    the usession directory, the C file and the executable it wrote, the Timings table, and
    the error lines.  The mandelbrot dots are progress, not errors."""

    def __init__(self, label):
        self.label = label
        self.phase_i, self.phase = 0, ""
        self.usession = self.c_file = self.created = ""
        self.timings = {}
        self.shown = 0

    def feed(self, text, elapsed):
        m = PHASE_RE.match(text)
        if m and m.group(1) in PHASES:
            i = PHASES.index(m.group(1)) + 1
            if i > self.phase_i:
                self.phase_i, self.phase = i, m.group(1)
                return ["  phase %d/%d: %s  [%s]" % (i, len(PHASES), self.phase, hms(elapsed))]
            return []
        for rx, attr in ((USESSION_RE, "usession"), (WRITTEN_RE, "c_file"), (CREATED_RE, "created")):
            m = rx.search(text)
            if m:
                setattr(self, attr, m.group(1))
                return []
        m = TIMER_RE.match(text)
        if m:
            self.timings[m.group(1).strip()] = float(m.group(2))
            return []
        if DOTS.match(text) or "warning:" in text or text.startswith("[platform:WARNING]"):
            return []
        if "[translation:ERROR]" in text or C_ERROR_RE.search(text) or OOMISH.search(text):
            self.shown += 1
            if self.shown <= 40:
                return ["    " + text[:300]]
        return []

    def status(self, elapsed):
        where = "phase %d/%d: %s" % (self.phase_i, len(PHASES), self.phase) if self.phase else "starting"
        return "  [%s] %s | %s" % (hms(elapsed), self.label, where)


def classify_translation(rc, text):
    """-> ok | interrupted | oom | error.  A ^C that reached RPython ends as
    '[translation:ERROR] KeyboardInterrupt' (and exit 1 under --batch)."""
    if rc == 0:
        return "ok"
    if rc in (130, -signal.SIGINT) or re.search(r"\[translation:ERROR\]\s+KeyboardInterrupt", text or ""):
        return "interrupted"
    if rc in (137, -signal.SIGKILL) or OOMISH.search(text or ""):
        return "oom"
    return "error"


def translation_errors(text, limit=24):
    """The [translation:ERROR] block (with the last frames above it), then the first C errors."""
    lines = (text or "").splitlines()
    found = []
    for i, line in enumerate(lines):
        if "[translation:ERROR]" in line:
            ctx_, j = [], i - 1
            while j >= 0 and len(ctx_) < 4 and "[translation:info] Error:" not in lines[j]:
                if lines[j].strip():
                    ctx_.insert(0, lines[j].rstrip())
                j -= 1
            found.extend(ctx_)
            found.append(line.rstrip())
            k = i + 1
            while k < len(lines) and k <= i + 6 and lines[k].startswith((" ", "\t")):
                found.append(lines[k].rstrip())
                k += 1
            break
    for line in lines:
        if C_ERROR_RE.search(line) and "warning:" not in line and line.rstrip() not in found:
            found.append(line.rstrip())
            if len(found) >= limit:
                break
    return [l[:300] for l in found[:limit]]


def failed_phase(text):
    """The last phase that started before the end: where a failure happened."""
    last = ""
    for line in (text or "").splitlines():
        m = PHASE_RE.match(line)
        if m and m.group(1) in PHASES:
            last = m.group(1)
    return last


# --------------------------------------------------------------- the host Python

HOST_PROBE = (
    "import sys, json\n"
    "r = {'major': sys.version_info[0], 'version': '%d.%d.%d' % tuple(sys.version_info[:3]),\n"
    "     'impl': hasattr(sys, 'pypy_version_info') and 'pypy' or 'cpython', 'pypy': '',\n"
    "     'pycparser': '', 'cffi': '', 'executable': sys.executable}\n"
    "if hasattr(sys, 'pypy_version_info'):\n"
    "    r['pypy'] = '%d.%d.%d' % tuple(sys.pypy_version_info[:3])\n"
    "try:\n"
    "    from cffi import _pycparser\n"
    "    r['pycparser'] = 'cffi._pycparser'\n"
    "except Exception:\n"
    "    try:\n"
    "        import pycparser\n"
    "        r['pycparser'] = getattr(pycparser, '__version__', 'yes')\n"
    "    except Exception:\n"
    "        pass\n"
    "for m in ('_curses', '_minimal_curses'):\n"
    "    try:\n"
    "        __import__(m)\n"
    "        r['curses'] = m\n"
    "        break\n"
    "    except Exception:\n"
    "        r['curses'] = ''\n"
    "try:\n"
    "    import cffi\n"
    "    r['cffi'] = cffi.__version__\n"
    "except Exception:\n"
    "    pass\n"
    "print(json.dumps(r))\n")


def probe_interpreter(path):
    """-> {major, version, impl, pypy, pycparser, cffi, executable, path} or None.  Written to
    run under Python 2 and 3 alike, so a CPython 3 is recognised (and refused) by name."""
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    try:
        p = run([path, "-c", HOST_PROBE], check=False, env=scrubbed_env(extra={"PYTHONNOUSERSITE": "1"}), timeout=120)
    except ToolError:
        return None
    try:
        d = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if p.returncode != 0 or not isinstance(d, dict):
        return None
    d["path"] = path
    return d


def host_problem(pr, path):
    """'' when RPython can run on it; otherwise the sentence saying why not."""
    if pr is None:
        return "%s does not run (or is not a Python)" % path
    if pr.get("major") != 2:
        what = "PyPy3" if pr.get("impl") == "pypy" else "CPython %s" % pr.get("version")
        return ("%s is %s.  RPython -- what translates PyPy -- is Python 2 code, and a Python 3 cannot "
                "run it.  (build_python_universal.sh's HOST_MODE=cpython ran `python3` and so never "
                "worked; here that choice is --host cpython27, which builds CPython 2.7.18 for the "
                "purpose.)  Use --host cpython27, --host prebuilt, or the path of a Python 2.7 / PyPy2."
                % (path, what))
    if not pr.get("pycparser"):
        return ("%s is Python %s but has no pycparser, which RPython's C parser needs: install "
                "pycparser==2.21 and cffi==1.15.1 into it, or use --host cpython27 (it brings its own)"
                % (path, pr.get("version")))
    if not pr.get("curses"):
        return ("%s is Python %s but has neither _curses nor _minimal_curses.  Every PyPy translation "
                "imports one of them ON THE HOST (pypy/module/_minimal_curses, in every release's working "
                "modules) and stops with \"Skipped: no _curses or _minimal_curses module\".  Use a Python "
                "built with the ncurses headers, or --host cpython27 (built here, with _curses checked)"
                % (path, pr.get("version")))
    return ""


def host_argv(host, low_memory=False):
    """How the host is started.  -E -s: no PYTHON* variables, no user site directory.  A PyPy
    host with --low-memory runs its JIT more frugally (upstream's advice for small machines)."""
    argv = [host["path"], "-E", "-s"]
    if low_memory and host.get("impl") == "pypy":
        argv += ["--jit", "loop_longevity=300"]
    return argv


def host_env_extra(host, low_memory=False):
    return {"PYPY_GC_MAX_DELTA": "200MB"} if low_memory and host.get("impl") == "pypy" else {}


def find_system_hosts(opt_root, dirs=None):
    cands = scan_names(tool_search_dirs() if dirs is None else dirs, PYTHON2_NAME)
    for p in (os.path.join(opt_root, "pypy2", "pypy2", "bin", "pypy"), os.path.join(opt_root, "pypy2", "bin", "pypy")):
        if os.path.isfile(p) and os.access(p, os.X_OK) and p not in cands:
            cands.append(p)
    return cands


def cpy27_dir(workdir):
    return os.path.join(workdir, "host", "cpython-" + CPY27_VERSION)


def cpy27_python(workdir):
    return os.path.join(cpy27_dir(workdir), "prefix", "bin", "python2.7")


def plan_host(opts, target, opt_root, workdir, probe=None, system_hosts=None, pypy2_exe=None):
    """-> {mode, path, why, impl, ...}: which Python 2 translates.  Nothing is built or
    downloaded here; a cpython27 or prebuilt host's path is where it WILL be.  pypy2_exe: in a
    `--target both` run, the interpreter of the pypy2 this run just built and verified -- with
    --host auto, pypy3 is translated by THAT one (PLAN step 7), never by whatever pypy2-current
    happens to point at (an older release, or nothing with --no-link-current)."""
    probe = probe or probe_interpreter
    asked = (opts.get("host") or "auto").strip()
    if asked == "cpython":
        note("--host cpython means CPython 2.7.18 built here (cpython27): a CPython 3 cannot run RPython")
        asked = "cpython27"
    cpy = {"mode": "cpython27", "path": cpy27_python(workdir), "impl": "cpython", "version": CPY27_VERSION}
    cur = os.path.join(opt_root, "pypy2-current", "bin", "pypy")
    if asked == "auto":
        if target == "pypy3" and pypy2_exe:
            real = os.path.realpath(pypy2_exe)
            pr = probe(real)
            problem = host_problem(pr, real)
            if problem:
                raise ToolError("auto for pypy3: the pypy2 this run built cannot translate it: %s" % problem)
            return dict(pr, mode="built", path=real,
                        why="auto for pypy3: %s, the pypy2 this run just built and verified -- pypy3 "
                            "translated by that pypy2" % real)
        if target == "pypy3":
            if os.path.lexists(cur):
                real = os.path.realpath(cur)
                pr = probe(real)
                if not host_problem(pr, real):
                    return dict(pr, mode="built", path=real,
                                why="auto for pypy3: %s (step 6's pypy2) as its real path -- pypy3 translated "
                                    "by that pypy2" % cur)
                why_not = "%s does not run as a Python 2 with pycparser" % cur
            else:
                why_not = "no %s yet (step 6 makes it)" % cur
            return dict(cpy, why="auto for pypy3: %s, so the same host as pypy2: CPython %s built here "
                                 "with the toolkit's gcc (PLAN choice J)" % (why_not, CPY27_VERSION))
        return dict(cpy, why="auto: CPython %s built here with the toolkit's gcc and binutils (PLAN choice J; "
                             "--host prebuilt is 2-3x faster)" % CPY27_VERSION)
    if asked == "cpython27":
        return dict(cpy, why="--host cpython27: CPython %s built here with the toolkit's gcc" % CPY27_VERSION)
    if asked == "prebuilt":
        return {"mode": "prebuilt", "path": "", "impl": "pypy", "version": "2.7.18",
                "why": "--host prebuilt: the official portable PyPy2 for %s (downloaded)" % os.uname().machine}
    if asked == "built":
        real = os.path.realpath(cur)
        pr = probe(real) if os.path.lexists(cur) else None
        problem = host_problem(pr, cur) if os.path.lexists(cur) else "there is no %s" % cur
        if problem:
            raise ToolError("--host built: %s.\n  Build it first:  %s.py build --target pypy2\n"
                            "  or translate with another host: --host cpython27 | prebuilt | PATH" % (problem, TOOL))
        return dict(pr, mode="built", path=real, why="--host built: %s as its real path" % cur)
    if asked == "system":
        tried = []
        for c in (find_system_hosts(opt_root) if system_hosts is None else system_hosts):
            pr = probe(c)
            problem = host_problem(pr, c)
            if not problem:
                return dict(pr, mode="system", path=os.path.realpath(c), why="--host system: %s" % c)
            tried.append(problem.split(".  ")[0])
        raise ToolError("--host system: no Python 2 with pycparser here%s.  Enterprise Linux 10, Debian 12 "
                        "and Ubuntu 24.04 ship no Python 2 at all: use --host cpython27 (the default) or "
                        "--host prebuilt." % ((" (tried: %s)" % "; ".join(tried)) if tried else ""))
    path = expand(asked)
    if not os.path.exists(path):
        raise UsageError("--host is auto, cpython27, prebuilt, system, built or the path of an interpreter; "
                         "%s does not exist" % asked)
    pr = probe(path)
    problem = host_problem(pr, path)
    if problem:
        raise ToolError(problem)
    return dict(pr, mode="path", path=os.path.realpath(path), why="--host %s" % path)


# ------------------------------------------------------------ CPython 2.7.18 host

def cpy27_cflags(ci):
    """GCC 15+ defaults to C23 (bool is a keyword, () means (void)); GCC 14 made implicit
    declarations, int conversions and incompatible pointers hard errors.  CPython 2.7.18 (2020)
    predates both: -std=gnu17 always, -fpermissive (which turns those back into warnings for C
    from GCC 14 on) where it is accepted.  -O2, no PGO, no LTO: it is a tool, not the product."""
    kind, v = ci.get("kind"), ci.get("version") or ()
    new_std = (kind == "gcc" and ver_ge(v, (8,))) or (kind == "clang" and ver_ge(v, (6,)))
    flags = ["-O2", "-std=gnu17" if new_std else "-std=gnu11"]
    if kind == "gcc" and gcc_major(ci) >= 14:
        flags.append("-fpermissive")
    elif kind == "clang" and ver_ge(v, (16,)):
        flags += ["-Wno-error=implicit-function-declaration", "-Wno-error=int-conversion",
                  "-Wno-error=incompatible-pointer-types"]
    return flags


def cpy27_configure_args(src, prefix, ci):
    """UCS4 (what cffi's cp27mu wheels and most 2.7 builds use), the system's libffi (never
    the bundled copy in Modules/_ctypes/libffi, which modern compilers reject), pip installed
    by ensurepip.  Not shared, not optimised: --enable-optimizations is PGO, hours for nothing."""
    return [os.path.join(src, "configure"), "--prefix=" + prefix, "--enable-unicode=ucs4",
            "--with-system-ffi", "--with-ensurepip=install",
            "CC=" + ci["path"], "CFLAGS=" + " ".join(cpy27_cflags(ci))]


def cpy27_module_report(text):
    """setup.py's two lists at the end of 2.7's make: not found / failed to build."""
    rep = {"missing": [], "failed": []}
    for key, head in (("missing", "necessary bits to build these modules were not found"),
                      ("failed", "Failed to build these modules")):
        i = (text or "").rfind(head)
        if i < 0:
            continue
        for line in text[i:].splitlines()[1:]:
            if not line.strip() or line.startswith("To find the necessary bits"):
                break
            rep[key].extend(line.split())
    return rep


# What the host must have; _hashlib and _ssl are NOT here: against OpenSSL 3, 2.7.18's
# setup.py cannot read the version and skips _hashlib (the built-in md5/sha modules stand in),
# and translating needs neither.
# _curses: every translation imports it on the host (pypy/module/_minimal_curses/moduledef.py); a host
# built before the ncurses headers were installed lacks it, fails this check, and is built again.
CPY27_NEEDED = ("zlib", "bz2", "ctypes", "select", "socket", "subprocess", "multiprocessing", "hashlib",
                "binascii", "struct", "_curses", "pycparser", "cffi", "_cffi_backend")
CPY27_CHECK = ("import sys\nbad = []\nfor m in sys.argv[1:]:\n    try:\n        __import__(m)\n"
               "    except Exception:\n        bad.append(m)\nimport hashlib\nhashlib.md5(b'x').hexdigest()\n"
               "print('VERSION %d.%d.%d' % tuple(sys.version_info[:3]))\nprint('MISSING ' + ' '.join(bad))\n")


def cpy27_check(py):
    """-> (version, [missing modules]) or (None, [why])."""
    try:
        p = run([py, "-E", "-s", "-c", CPY27_CHECK] + list(CPY27_NEEDED), check=False, env=scrubbed_env(), timeout=120)
    except ToolError as e:
        return None, [str(e)]
    m = re.search(r"^VERSION (\S+)", p.stdout or "", re.M)
    miss = re.search(r"^MISSING ?(.*)$", p.stdout or "", re.M)
    if p.returncode != 0 or not m or not miss:
        return None, [tail(p.stdout, 6) or "exit %d" % p.returncode]
    return m.group(1), miss.group(1).split()


def cpy27_fingerprint(ci, binutils):
    return {"version": CPY27_VERSION, "cc": ci["path"], "cc_banner": ci["banner"],
            "cflags": cpy27_cflags(ci), "binutils": binutils}


def build_cpython27(workdir, ci, binutils, jobs, state, offline=False, verbose=False, force=False):
    """CPython 2.7.18 in <workdir>/host/cpython-2.7.18/prefix (not in <opt>: it is a tool of
    this tool), built with the toolkit's gcc, installed through a staging directory and one
    rename, then pycparser + cffi from pinned files.  Reused while the compiler is the same."""
    base, py = cpy27_dir(workdir), cpy27_python(workdir)
    prefix = os.path.join(base, "prefix")
    fp = cpy27_fingerprint(ci, binutils)
    rec = state.d["hosts"].get("cpython27") or {}
    if not force and rec.get("fingerprint") == fp:
        v, missing = cpy27_check(py)
        if v == CPY27_VERSION and not missing:
            ok("CPython %s host: %s (built %s with this compiler)" % (v, py, rec.get("built", "?")))
            return py
        if v == CPY27_VERSION:
            note("the CPython %s host has no %s%s: building it again" % (
                CPY27_VERSION, " ".join(missing), "  (its headers were missing when it was built)"
                if "_curses" in missing else ""))
    if rec.get("fingerprint") and rec.get("fingerprint") != fp:
        note("the host was built with %s; the compiler is now %s: building it again"
             % (rec["fingerprint"].get("cc"), ci["path"]))
    say("Translation host: CPython %s, built with %s" % (CPY27_VERSION, ci["banner"]))
    ddir, logs = os.path.join(workdir, "downloads"), os.path.join(workdir, "logs", "host")
    try:
        tb, url, sha = fetch_any(list(CPY27_SOURCES), ddir, offline)
    except ToolError as e:
        if offline:
            raise
        note("no tarball could be downloaded: trying git (%s)" % str(e).splitlines()[-1].strip()[:100])
        tb = os.path.join(ddir, "cpython-%s.git.tar" % CPY27_VERSION)
        if not os.path.isfile(tb):
            git_archive(CPY27_GIT[0], CPY27_GIT[1], tb, "Python-" + CPY27_VERSION, os.path.join(workdir, "tmp"))
        url, sha = "%s@%s" % CPY27_GIT, sha256_file(tb)
    src, build, stage = os.path.join(base, "src"), os.path.join(base, "build"), os.path.join(base, "stage")
    for d in (src, build, stage, prefix):
        rmtree(d)
    extract(tb, src)
    os.makedirs(build)
    tmp = os.path.join(workdir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = scrubbed_env([os.path.join(binutils, "bin")] if binutils else None, {"TMPDIR": tmp})
    info("CFLAGS %s" % " ".join(cpy27_cflags(ci)))
    for what, cmd, log in (("configure", cpy27_configure_args(src, prefix, ci), "configure.log"),
                           ("make", ["make", "-j%d" % max(1, jobs)], "make.log"),
                           ("make install", ["make", "install", "DESTDIR=" + stage], "install.log")):
        info("%s  (log: %s)" % (what, os.path.join(logs, log)))
        rc, secs = run_logged(cmd, build, env, os.path.join(logs, log), verbose=verbose, label="cpython 2.7 " + what)
        if rc != 0:
            hint = "\n  config.log: %s" % os.path.join(build, "config.log") if what == "configure" else ""
            raise ToolError("CPython %s: %s failed (exit %d).  Log: %s%s\n%s"
                            % (CPY27_VERSION, what, rc, os.path.join(logs, log), hint,
                               "\n".join(error_summary(os.path.join(logs, log))) or tail(read_text(os.path.join(logs, log)), 20)))
        if what == "make":
            rep = cpy27_module_report(read_text(os.path.join(logs, log)))
            if rep["failed"]:
                note("modules that failed to build: %s" % " ".join(rep["failed"]))
            if rep["missing"]:
                note("modules not built (fine unless listed as needed below): %s" % " ".join(rep["missing"]))
    staged = os.path.join(stage, prefix.lstrip(os.sep))
    if not os.access(os.path.join(staged, "bin", "python2.7"), os.X_OK):
        raise ToolError("make install put no bin/python2.7 in %s" % staged)
    os.rename(staged, prefix)
    rmtree(stage)
    host_packages(py, ci, binutils, ddir, logs, offline, verbose)
    v, missing = cpy27_check(py)
    if v != CPY27_VERSION or missing:
        raise ToolError("the CPython %s host is incomplete: %s%s" % (
            CPY27_VERSION, (" ".join(missing) or "it does not run"),
            ".  _curses needs the ncurses headers (curses.h, term.h): `doctor --install-deps`, then `build` again"
            if "_curses" in missing else ""))
    ok("CPython %s host ready: %s (%s)" % (v, py, " ".join(CPY27_NEEDED)))
    state.d["hosts"]["cpython27"] = {"path": py, "version": v, "built": now(), "fingerprint": fp,
                                     "source": url, "sha256": sha}
    state.save()
    return py


def host_packages(py, ci, binutils, ddir, logs, offline=False, verbose=False):
    """pycparser 2.21 + cffi 1.15.1 into the host, from pinned files: the sdist built with the
    same compiler and flags, the manylinux wheel only if that fails (x86_64)."""
    files = [HOST_FILES["pycparser"], HOST_FILES["cffi"]]
    for name, url, sha in files:
        fetch_any([(url, name, sha)], ddir, offline)
    wheel = HOST_FILES["cffi-wheel-x86_64"]
    have_wheel = False
    if os.uname().machine == "x86_64":
        try:
            fetch_any([(wheel[1], wheel[0], wheel[2])], ddir, offline)
            have_wheel = True
        except ToolError:
            pass
    env = scrubbed_env([os.path.join(binutils, "bin")] if binutils else None,
                       {"CC": ci["path"], "CFLAGS": " ".join(cpy27_cflags(ci)), "PIP_NO_INDEX": "1",
                        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1"})
    if run([py, "-E", "-s", "-m", "pip", "--version"], check=False, env=env, timeout=120).returncode != 0:
        run_logged([py, "-E", "-s", "-m", "ensurepip"], os.path.dirname(py), env,
                   os.path.join(logs, "ensurepip.log"), verbose=verbose)
    base = [py, "-E", "-s", "-m", "pip", "install", "--no-index", "--find-links", ddir,
            "--disable-pip-version-check", "--no-cache-dir"]
    log = os.path.join(logs, "pip.log")
    info("pip install %s  (log: %s)" % (" ".join(HOST_PINS), log))
    rc, _ = run_logged(base + ["--no-binary", "cffi"] + list(HOST_PINS), os.path.dirname(py), env, log, verbose=verbose)
    if rc != 0 and have_wheel:
        warn("cffi 1.15.1 did not build from source here (log: %s) -- using PyPI's cp27mu wheel" % log)
        rc, _ = run_logged(base + ["--only-binary", "cffi"] + list(HOST_PINS), os.path.dirname(py), env,
                           os.path.join(logs, "pip-wheel.log"), verbose=verbose)
    if rc != 0:
        raise ToolError("pycparser/cffi could not be installed into the host.  Log: %s\n%s"
                        % (log, "\n".join(error_summary(log)) or tail(read_text(log), 15)))


def get_prebuilt_host(cat, workdir, offline=False):
    """The official PyPy2 for this architecture, unpacked into <workdir>/host/<stem>/ and asked
    whether it runs on this C library -> (probe, release)."""
    tag, g = arch_tag(), glibc_version()
    rel, name = prebuilt_host_choice(cat, tag, g)
    if g and not ver_ge(g, GLIBC_FLOOR_V8):
        note("glibc %s: PyPy 8.x binaries need 2.28, so the host is PyPy2 %s" % (vstr(g), rel))
    stem = re.sub(r"\.tar\.(gz|bz2)$", "", name)
    hdir = os.path.join(workdir, "host", stem)
    exe = os.path.join(hdir, "bin", "pypy")
    pr = probe_interpreter(exe)
    if pr and not host_problem(pr, exe):
        ok("prebuilt host: %s (PyPy %s)" % (exe, pr.get("pypy")))
        return dict(pr, mode="prebuilt"), rel
    say("Translation host: the prebuilt %s" % name)
    sums = cat.checksums or {}
    path, _, _ = fetch_any([(PYPY_BASE + name, name, sums.get(name, ""))], os.path.join(workdir, "downloads"), offline)
    rmtree(hdir)
    extract(path, hdir)
    pr = probe_interpreter(exe)
    if pr is None:
        raise ToolError("the downloaded PyPy2 does not run on this system (%s).  Use --host cpython27." % exe)
    problem = host_problem(pr, exe)
    if problem:
        raise ToolError(problem)
    ok("prebuilt host: %s (PyPy %s, Python %s)" % (exe, pr.get("pypy"), pr.get("version")))
    return dict(pr, mode="prebuilt"), rel


# ------------------------------------------------------------- one build: context

CFFI_KEYS = {"2.7": ("_pypy_util_cffi_inner", "_ssl", "sqlite3", "audioop", "_tkinter", "curses", "syslog",
                     "gdbm", "grp", "resource"),
             "3": ("_pypy_util_cffi_inner", "_blake2", "_sha3", "_ssl", "sqlite3", "audioop", "_tkinter",
                   "curses", "syslog", "_gdbm", "grp", "resource", "lzma")}
CFFI_PACKAGE = {"_ssl": "openssl", "sqlite3": "sqlite", "curses": "ncurses", "gdbm": "gdbm", "_gdbm": "gdbm",
                "lzma": "xz", "_tkinter": "tk"}


def release_cffi_keys(src_dir):
    """The keys the release's own build_cffi_imports.py lists in cffi_build_scripts (package.py
    makes a --without-KEY option of each) -> set, or None when it cannot be read."""
    script = find_cffi_script(src_dir) if src_dir and os.path.isdir(src_dir) else ""
    text = read_text(script) if script else ""
    i = text.find("cffi_build_scripts =")
    if i < 0:
        return None
    end = re.search(r"\n\s*[\]}]", text[i:])
    block = re.sub(r"#[^\n]*", "", text[i:i + end.start()] if end else text[i:])     # not the commented-out ones
    keys = set(re.findall(r"""\(\s*["']([\w.]+)["']\s*,""", block)) | \
        set(re.findall(r"""["']([\w.]+)["']\s*:""", block))
    return keys or None


def cffi_keys(series, src_dir=None):
    """What build_cffi_imports.py builds per flavour (_posixshmem: every 3.x -- the 3.11 and 3.12
    scripts both list it), cut down to what THIS release's script lists when its tree is given:
    a --without-KEY its package.py does not know would stop it."""
    keys = list(CFFI_KEYS["2.7"]) if series == "2.7" else list(CFFI_KEYS["3"]) + ["_posixshmem"]
    have = release_cffi_keys(src_dir) if src_dir else None
    return [k for k in keys if have is None or k in have]


def cffi_level(key, series, with_tk=False):
    if key in ("_pypy_util_cffi_inner", "_ssl", "sqlite3") or (series != "2.7" and key in ("_blake2", "_sha3", "lzma")):
        return "required"
    if key == "_tkinter":
        return "required" if with_tk else "skipped"
    if key == "audioop":
        return "optional"
    return "wanted"


# What verify asks of the interpreter (build_python_universal.sh's verify_interpreter lists,
# per flavour -- lzma arrived in 3.3, gdbm is dbm.gnu in 3 -- plus what PyPy itself must have).
CORE = {"2": ("zlib", "bz2", "ctypes", "sqlite3", "hashlib", "decimal", "select", "socket", "ssl",
              "_cffi_backend", "pyexpat", "json", "binascii", "_multiprocessing", "__pypy__", "pypyjit"),
        "3": ("zlib", "bz2", "lzma", "ctypes", "sqlite3", "hashlib", "decimal", "select", "socket", "ssl",
              "_cffi_backend", "pyexpat", "json", "_blake2", "_sha3", "ensurepip", "venv", "pypyjit")}
NICE = {"2": ("curses", "readline", "gdbm", "uuid"), "3": ("curses", "readline", "dbm.gnu", "uuid")}


class Ctx(object):
    """One target's build: what, from where, with what, to where."""

    def __init__(self, opts, target, workdir, opt_root, machine=None):
        self.opts, self.target, self.workdir, self.opt_root = opts, target, workdir, opt_root
        self.machine = machine or {}
        self.release = self.series = ""
        self.why = []
        self.cc = None
        self.binutils, self.binutils_why = "", ""
        self.host = {}
        self.jobs = 1
        self.src_cands, self.src_git, self.src_local = [], None, ""
        self.src = {}

    @property
    def stem(self):
        return "pypy%s-v%s" % (self.series, self.release)

    @property
    def key(self):
        return self.stem

    @property
    def is3(self):
        return self.series.startswith("3")

    @property
    def major(self):
        return "3" if self.is3 else "2"

    @property
    def exe_c(self):
        return "pypy%s-c" % self.series if self.is3 else "pypy-c"

    @property
    def lib_c(self):
        return "lib%s.so" % self.exe_c

    @property
    def final_exe(self):
        return "pypy" + self.series if self.is3 else "pypy"

    @property
    def current_name(self):
        return "pypy%s-current" % self.major

    @property
    def needs_setuptools(self):
        return self.is3 and ver_ge(numver(self.series), (3, 12))

    @property
    def src_dir(self):
        return os.path.join(self.workdir, "src", self.stem + "-src")

    @property
    def goal(self):
        return os.path.join(self.src_dir, "pypy", "goal")

    @property
    def usession(self):
        return os.path.join(self.workdir, "usession", self.key)

    @property
    def pkgdir(self):
        return os.path.join(self.workdir, "package", self.key)

    @property
    def stage(self):
        return os.path.join(self.pkgdir, self.stem)

    @property
    def logs(self):
        return os.path.join(self.workdir, "logs", self.key)

    @property
    def ddir(self):
        return os.path.join(self.workdir, "downloads")

    @property
    def tmp(self):
        return os.path.join(self.workdir, "tmp")

    @property
    def prefix(self):
        return expand(self.opts.get("prefix")) if self.opts.get("prefix") else os.path.join(self.opt_root, self.stem)

    @property
    def link(self):
        return self.opts.get("link_current") is not False


def source_candidates(cat, rel, series):
    """-> ([(url, file, sha256)], (git url, tag)).  downloads.python.org (the index's name, or
    both spellings when the list came from git tags), then GitHub's archive of the tag; git
    itself is the last resort (fetch_source)."""
    flavour = "pypy" + series
    name = cat.src_name(rel, series)
    names = [name] if name else ["%s-v%s-src.tar.gz" % (flavour, rel), "%s-v%s-src.tar.bz2" % (flavour, rel)]
    sums = cat.checksums or {}
    tag = "release-%s-v%s" % (flavour, rel)
    cands = [(PYPY_BASE + n, n, sums.get(n, "")) for n in names]
    cands.append((PYPY_ARCHIVE_URL % tag, "%s-v%s-src.github.tar.gz" % (flavour, rel), ""))
    return cands, (PYPY_GIT, tag)


def default_jobs(machine):
    """make -j for RPython's C and the host: one per CPU, but no more than 1.5 GB of available
    memory each (gcc on RPython's huge C files)."""
    return max(1, min(machine.get("cpus", 1), int(machine.get("mem_avail_gb", 4) / 1.5) or 1))


def translate_env(ctx, usession=None):
    """The scrubbed environment plus what RPython reads, set by this tool: CC (the chosen gcc,
    never the shell's), CFLAGS (only --extra-cflags; RPython appends it), LDFLAGS (empty), the
    usession directory inside the work directory (PYPY_USESSION_KEEP=1: one 4 GB tree, not
    three), TMPDIR inside the work directory (/tmp may be a small tmpfs)."""
    path = [os.path.join(ctx.binutils, "bin")] if ctx.binutils else []
    env = scrubbed_env(path, {
        "CC": ctx.cc["path"], "CFLAGS": ctx.opts.get("extra_cflags") or "", "LDFLAGS": "",
        "PYPY_USESSION_DIR": usession or ctx.usession, "PYPY_USESSION_BASENAME": ctx.key,
        "PYPY_USESSION_KEEP": "1", "TMPDIR": ctx.tmp, "PYTHONNOUSERSITE": "1"})
    env.update(host_env_extra(ctx.host, ctx.opts.get("low_memory")))
    return env


def rpython_argv(ctx):
    """--batch: an error or a ^C never opens the (Pdb+) prompt (it waited forever on
    2026-08-29); --opt=jit is the point of PyPy; the C compiler named twice (--cc and CC)."""
    argv = host_argv(ctx.host, ctx.opts.get("low_memory")) + [
        os.path.join(ctx.src_dir, "rpython", "bin", "rpython"), "--batch", "--opt=jit",
        "--cc=" + ctx.cc["path"], "--make-jobs=%d" % ctx.jobs]
    if ctx.opts.get("lto"):
        argv.append("--lto")
    argv.append("targetpypystandalone.py")
    return argv


def offline_pip_env(ctx):
    return {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": ctx.ddir, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def cffi_env(ctx):
    """For build_cffi_imports.py: the translation's environment, the source tree on
    PYTHONPATH (the script imports rpython.tool.runsubprocess), LDSHARED for the same gcc."""
    env = translate_env(ctx)
    env.update(offline_pip_env(ctx))
    env.update({"PYTHONPATH": ctx.src_dir, "LDSHARED": "%s -pthread -shared" % ctx.cc["path"]})
    env.update(fetch_env(ctx))
    return env


def fetch_env(ctx):
    """--embed-deps: upstream's _build_dependency fetches OpenSSL/gdbm/xz with wget unless USE_CURL
    is set -- and a missing wget is an OSError outside its try (package.py crashes).  doctor
    accepts wget OR curl, so where there is only curl, USE_CURL=1: for build_cffi_imports.py
    AND for package.py (which rebuilds every module under PYPY_REBUILD_EXTENSIONS=1)."""
    if ctx.opts.get("embed_deps") and not which("wget") and which("curl"):
        return {"USE_CURL": "1"}
    return {}


def package_env(ctx):
    """package.py is steered ONLY by environment variables: every version reads these, while
    its flag names differ between the 2.7 and 3.x copies.  No embedding (no OpenSSL/gdbm/xz
    downloaded and linked in statically: the system's libraries), no rebuilding the cffi
    modules this tool just built, not 'portable', no tk unless asked, debug info unless --strip.
    pip offline: pypy3.12's package.py runs `pip install setuptools`."""
    env = translate_env(ctx)
    env.update(offline_pip_env(ctx))
    env["PYPY_NO_MAKE_PORTABLE"] = "1"
    if ctx.opts.get("embed_deps"):
        env.update({"PYPY_EMBED_DEPENDENCIES": "1", "PYPY_REBUILD_EXTENSIONS": "1"})
        env.update(fetch_env(ctx))
    else:
        env.update({"PYPY_NO_EMBED_DEPENDENCIES": "1", "PYPY_NO_REBUILD_EXTENSIONS": "1"})
    if not ctx.opts.get("with_tk"):
        env["PYPY_PACKAGE_WITHOUTTK"] = "1"
    if ctx.opts.get("strip"):
        env["PYPY_PACKAGE_NOKEEPDEBUG"] = "1"
    return env


def package_argv(ctx, runner, without=()):
    return list(runner) + [os.path.join(ctx.src_dir, "pypy", "tool", "release", "package.py"),
                           "--archive-name", ctx.stem, "--builddir", ctx.pkgdir] + \
        ["--without-" + k for k in without]


def find_cffi_script(src):
    """build_cffi_imports.py has moved between releases: lib_pypy/pypy_tools/ now,
    pypy/tool/ before.  Found, not assumed (the .sh used find)."""
    for rel in (("lib_pypy", "pypy_tools", "build_cffi_imports.py"), ("pypy", "tool", "build_cffi_imports.py")):
        p = os.path.join(src, *rel)
        if os.path.isfile(p):
            return p
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "lib-python")]
        if "build_cffi_imports.py" in files:
            return os.path.join(dirpath, "build_cffi_imports.py")
    return ""


# ------------------------------------------------------ the interpreter, verified

VERIFY_SCRIPT = r'''from __future__ import print_function
import json, sys
groups = json.loads(sys.argv[1])
res = {"version": "%d.%d.%d" % tuple(sys.version_info[:3]), "executable": sys.executable,
       "missing": {}, "errors": {}, "pypy": "", "jit": False, "openssl": ""}
pv = getattr(sys, "pypy_version_info", None)
if pv:
    res["pypy"] = "%d.%d.%d" % tuple(pv[:3])
ti = getattr(sys, "pypy_translation_info", None) or {}
try:
    res["jit"] = bool(ti["translation.jit"])
except Exception:
    pass
for group in sorted(groups):
    res["missing"][group] = []
    for name in groups[group]:
        try:
            __import__(name)
        except Exception as e:
            res["missing"][group].append(name)
            res["errors"][name] = "%s: %s" % (type(e).__name__, e)
try:
    import ssl
    res["openssl"] = ssl.OPENSSL_VERSION
except Exception:
    pass
print(json.dumps(res))
'''
SSL_SCRIPT = "import ssl; ssl.create_default_context()"
# A hot loop: the JIT compiles it after ~1000 iterations; a JIT denied executable memory
# (SELinux deny_execmem, a confined service) dies here, not in a user's program.
JIT_SCRIPT = ("import sys\ni = 0\nt = 0\nwhile i < 2000000:\n    t += i % 7\n    i += 1\n"
              "sys.exit(0 if t == 5999995 else 3)\n")
IMPORT_TABLE = ("import json, sys\nres = {}\nfor m in sys.argv[1:]:\n    try:\n        __import__(m)\n"
                "        res[m] = 'ok'\n    except Exception as e:\n        res[m] = '%s: %s' % (type(e).__name__, e)\n"
                "print(json.dumps(res))\n")


def verify_env():
    """No .pyc written by the checks: a stage is renamed into place afterwards."""
    return scrubbed_env(extra={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})


def find_exe(root, series=None):
    """The interpreter by the real layout -> (path, series or '').  pypy2.7: bin/pypy, a file.
    pypy3.N: bin/pypy3.N is the only file and bin/pypy3, bin/pypy, bin/python3 are links to
    it -- so the .sh's `find -type f -name pypy3` found nothing in a good pypy3."""
    b = os.path.join(root, "bin")

    def is_file(p):
        return os.path.isfile(p) and not os.path.islink(p) and os.access(p, os.X_OK)
    if series in (None, "", "2.7") and is_file(os.path.join(b, "pypy")):
        return os.path.join(b, "pypy"), "2.7"
    if series and series != "2.7" and is_file(os.path.join(b, "pypy" + series)):
        return os.path.join(b, "pypy" + series), series
    try:
        names = os.listdir(b)
    except OSError:
        return "", ""
    threes = sorted((n for n in names if re.match(r"^pypy3\.\d+$", n) and is_file(os.path.join(b, n))),
                    key=series_key, reverse=True)
    if threes and (not series or series.startswith("3")):
        return os.path.join(b, threes[0]), threes[0][4:]
    for n in ("pypy3", "pypy"):
        p = os.path.join(b, n)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p, ""
    return "", ""


def ssl_outcome(rc):
    """-> ('ok' | 'crash' | 'fail', signal number).  Above 128 (a shell's way) or negative
    (Python's way) is death by signal: an ssl module whose cffi bindings were built against
    another OpenSSL imports fine and SEGFAULTs here (the HANDOFF's ~/opt/pypy2 and pypy3)."""
    if rc == 0:
        return "ok", 0
    if rc < 0:
        return "crash", -rc
    if rc > 128:
        return "crash", rc - 128
    return "fail", 0


def ldd_map(path, env):
    try:
        p = run(["ldd", path], env=env, check=False, timeout=60)
    except ToolError:
        return {}
    libs = {}
    for line in (p.stdout or "").splitlines():
        m = re.match(r"\s*(\S+)\s+=>\s+(not found|\S+)", line)
        if m:
            libs[m.group(1)] = m.group(2)
    return libs


def elf_facts(path):
    facts = {"runpath": [], "comment": []}
    if not which("readelf"):
        return facts
    p = run(["readelf", "-d", path], env=scrubbed_env(), check=False, timeout=60)
    for line in (p.stdout or "").splitlines():
        m = re.search(r"\((?:RPATH|RUNPATH)\)\s+Library r(?:un)?path: \[(.*)\]", line)
        if m:
            facts["runpath"].append(m.group(1))
    p = run(["readelf", "-p", ".comment", path], env=scrubbed_env(), check=False, timeout=60)
    for line in (p.stdout or "").splitlines():
        m = re.match(r"\s*\[\s*[0-9a-f]+\]\s+(.*)", line)
        if m and m.group(1).strip() not in facts["comment"]:
            facts["comment"].append(m.group(1).strip())
    return facts


def shared_objects(root):
    """libpypy*-c.so and the cffi modules, for ldd."""
    found = glob.glob(os.path.join(glob.escape(root), "bin", "libpypy*-c.so"))
    for pat in (("lib_pypy", "*.so"), ("lib", "pypy3.*", "*.so")):
        found += sorted(glob.glob(os.path.join(glob.escape(root), *pat)))
    return found


def verify_interpreter(root, series=None, release=None, offline=True, with_tk=False, expect_pip=True,
                       cc_version="", quiet=False):
    """One place that decides whether an interpreter is usable -> (good, summary).  A build
    that 'succeeded' while quietly dropping ssl or ctypes is worse than a failed one."""
    show = not quiet
    summary = {"when": now(), "root": root, "failures": [], "warnings": []}

    def fail(msg):
        if show:
            bad(msg)
        summary["failures"].append(msg)

    def warned(msg):
        if show:
            warn(msg)
        summary["warnings"].append(msg)

    exe, found_series = find_exe(root, series)
    summary["exe"] = exe
    if not exe:
        fail("no interpreter under %s/bin" % root)
        summary["good"] = False
        return False, summary
    env = verify_env()
    try:
        p = run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], check=False, env=env, timeout=120)
    except ToolError as e:
        p = None
        fail("%s does not run: %s" % (exe, e))
    lang = (p.stdout or "").strip().splitlines()[-1] if p is not None and p.returncode == 0 and (p.stdout or "").strip() else ""
    if not lang:
        if p is not None:
            fail("%s does not run (exit %d):\n%s" % (exe, p.returncode, tail(p.stdout, 8)))
        summary["good"] = False
        return False, summary
    major = "2" if lang.startswith("2") else "3"
    groups = {"core": list(CORE[major]), "nice": list(NICE[major]) + (["_tkinter"] if with_tk else [])}
    p = run([exe, "-c", VERIFY_SCRIPT, json.dumps(groups)], check=False, env=env, timeout=600)
    try:
        res = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        fail("the interpreter cannot run its own module check (exit %d):\n%s" % (p.returncode, tail(p.stdout)))
        summary["good"] = False
        return False, summary
    summary.update(version=res.get("version"), pypy=res.get("pypy"), jit=res.get("jit"),
                   openssl=res.get("openssl"), missing=res.get("missing"))
    if show:
        ok("%s: Python %s, PyPy %s" % (exe, res.get("version"), res.get("pypy") or "?"))
    want_lang = series or found_series
    if want_lang and lang != want_lang:
        fail("it is Python %s, not %s" % (lang, want_lang))
    if not res.get("pypy"):
        fail("it is not a PyPy (no sys.pypy_version_info)")
    elif release and not is_rc(release) and res["pypy"] != release:
        fail("it says PyPy %s, the release built was %s" % (res["pypy"], release))
    core = res.get("missing", {}).get("core", [])
    if core:
        fail("REQUIRED modules missing: %s" % "; ".join("%s (%s)" % (m, res["errors"].get(m, "")[:80]) for m in core))
    elif show:
        ok("core modules import: %s" % " ".join(groups["core"]))
    for m in res.get("missing", {}).get("nice", []):
        warned("optional module %s missing (%s): install the matching -devel package and rebuild if you need it"
               % (m, res["errors"].get(m, "")[:60]))
    p = run([exe, "-c", SSL_SCRIPT], check=False, env=env, timeout=120)
    kind, sig = ssl_outcome(p.returncode)
    summary["ssl_context"] = kind
    if kind == "crash":
        fail("ssl CRASHED (signal %d) building a default context: the ssl module imports but its OpenSSL "
             "bindings are broken -- usually a cffi module built against another OpenSSL than the one now "
             "loaded.  HTTPS, pip and anything network-facing will not work." % sig)
    elif kind == "fail":
        fail("ssl imports but cannot create a default context:\n%s" % tail(p.stdout, 6))
    elif show:
        ok("ssl.create_default_context() works (%s)" % (res.get("openssl") or "OpenSSL"))
    if not res.get("jit"):
        fail("this PyPy was translated without the JIT (sys.pypy_translation_info['translation.jit'] is not True)")
    else:
        p = run([exe, "-c", JIT_SCRIPT], check=False, env=env, timeout=300)
        jk, jsig = ssl_outcome(p.returncode)
        if jk == "crash":
            fail("the JIT hot loop died (signal %d): is executable memory denied (SELinux deny_execmem, a "
                 "confined service)?" % jsig)
        elif jk == "fail":
            fail("the JIT hot loop gave a wrong answer (exit %d)" % p.returncode)
        elif show:
            ok("the JIT is on and a 2,000,000-iteration hot loop runs")
    p = run([exe, "-m", "pip", "--version"], check=False, env=env, timeout=300)
    if p.returncode == 0:
        said = [l for l in (p.stdout or "").splitlines() if l.startswith("pip ")]
        summary["pip"] = (said[0] if said else (p.stdout or "").strip()).split(" from ")[0].strip()
        if show:
            ok("its own pip: %s" % summary["pip"])
    elif expect_pip:
        fail("pip does not run:\n%s" % tail(p.stdout, 4))
    elif show:
        note("no pip in it (--no-pip)")
    if not offline:
        try:
            p = run([exe, "-c", "import sys\ntry:\n    from urllib.request import urlopen\nexcept ImportError:\n"
                     "    from urllib2 import urlopen\nprint(urlopen(%r, timeout=30).getcode())" % HTTPS_PROBE_URL],
                    check=False, env=env, timeout=150)
            answer = (p.stdout or "") if p.returncode == 0 else "exit %d\n%s" % (p.returncode, p.stdout or "")
        except ToolError as e:
            answer = str(e)
        if answer.strip() == "200":
            if show:
                ok("HTTPS with the system's CA store: %s answered" % HTTPS_PROBE_URL)
        else:
            warned("HTTPS to %s failed (network? proxy?):\n%s" % (HTTPS_PROBE_URL, tail(answer, 4)))
    libs = shared_objects(root)
    if which("ldd") and libs:
        missing_libs = []
        for so in libs:
            for lib, where in sorted(ldd_map(so, scrubbed_env()).items()):
                if where == "not found":
                    missing_libs.append("%s needs %s" % (os.path.basename(so), lib))
        if missing_libs:
            fail("shared libraries not found: %s" % "; ".join(sorted(set(missing_libs))[:12]))
        elif show:
            ok("every library the interpreter and its cffi modules load is found (%d checked)" % len(libs))
        main_so = [s for s in libs if os.path.basename(s).startswith("libpypy")]
        if main_so:
            facts = elf_facts(main_so[0])
            summary["comment"] = facts["comment"]
            if facts["runpath"] and any(r.rstrip("/") != "$ORIGIN" for r in facts["runpath"]):
                warned("%s carries RUNPATH %s (expected $ORIGIN/ only)" % (main_so[0], facts["runpath"]))
            if show and facts["comment"]:
                info("     built by: %s" % "; ".join(facts["comment"]))
            if numver(cc_version) and facts["comment"] and not any(cc_version in c for c in facts["comment"]):
                warned("the .comment section does not name the chosen compiler (version %s)" % cc_version)
    good = not summary["failures"]
    summary["good"] = good
    return good, summary


# ------------------------------------------------------------ install, -current

def busy_processes(paths, proc_root="/proc"):
    """[(pid, command line)] of processes that run from one of `paths` (executable or argv)."""
    targets = set()
    for p in paths:
        targets |= set([os.path.normpath(os.path.abspath(p)), os.path.realpath(p)])

    def under(c):
        return any(c == b or c.startswith(b + os.sep) for b in targets)
    hits = []
    try:
        pids = [d for d in os.listdir(proc_root) if d.isdigit()]
    except OSError:
        return hits
    for pid in pids:
        if int(pid) == os.getpid():
            continue
        base = os.path.join(proc_root, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            exe = os.path.realpath(os.readlink(os.path.join(base, "exe")))
        except (OSError, IOError):
            continue
        if under(exe) or any(under(os.path.normpath(a)) for a in argv[:2] if os.path.isabs(a)):
            hits.append((int(pid), " ".join(argv)[:160]))
    return hits


def relocate_scripts(bindir, old_root, new_root):
    """ensurepip in the stage wrote '#!<stage>/bin/pypy' into bin/pip*: after the rename that
    would point nowhere, so the scripts are pointed at the prefix first.  -> files changed."""
    old, new = ("#!" + old_root.rstrip("/") + "/").encode(), ("#!" + new_root.rstrip("/") + "/").encode()
    changed = []
    try:
        names = sorted(os.listdir(bindir))
    except OSError:
        return changed
    for n in names:
        p = os.path.join(bindir, n)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            data = f.read(1 << 16)
            if not data.startswith(old):
                continue
            data += f.read()
        mode = stat.S_IMODE(os.stat(p).st_mode)
        with open(p + ".reloc", "wb") as f:
            f.write(new + data[len(old):])
        os.chmod(p + ".reloc", mode)
        os.replace(p + ".reloc", p)
        changed.append(n)
    return changed


def install_tree(stage, prefix, rebuild=False, marker=None, busy=None):
    """The stage becomes the prefix in ONE rename: a prefix with the final name only ever
    holds a whole install, and is never installed over.  An existing prefix is replaced only
    with rebuild, only if it is this tool's, never while something runs from it; it is set
    aside and put back if anything fails.  -> where the old one was set aside ('' if none)."""
    busy = busy or busy_processes
    old = ""
    if os.path.lexists(prefix):
        rec = read_json(os.path.join(prefix, MARKER))
        if not rebuild:
            raise ToolError("%s already exists: not installing over it (--rebuild replaces a build of this "
                            "tool's; --prefix puts this one elsewhere)" % prefix)
        if not rec or rec.get("tool") != TOOL:
            raise ToolError("%s exists and is not a build of this tool's: not touching it (choose another --prefix)"
                            % prefix)
        hits = busy([prefix])
        if hits:
            raise ToolError("%s is in use:\n%s\n  Stop those first." % (prefix, "\n".join(
                "    pid %d: %s" % h for h in hits[:8])))
        old = "%s.old-%s" % (prefix, stamp())
        os.rename(prefix, old)
    try:
        if marker is not None:
            write_atomic(os.path.join(stage, MARKER), json.dumps(marker, indent=1, sort_keys=True) + "\n")
        relocate_scripts(os.path.join(stage, "bin"), stage, prefix)
        os.makedirs(os.path.dirname(prefix), exist_ok=True)
        try:
            os.rename(stage, prefix)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            tmp = "%s.partial-%d" % (prefix, os.getpid())       # another filesystem: copy, then rename
            try:
                shutil.copytree(stage, tmp, symlinks=True)
                os.rename(tmp, prefix)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            shutil.rmtree(stage, ignore_errors=True)
    except BaseException as e:
        if old and not os.path.lexists(prefix):
            try:
                os.rename(old, prefix)
                note("the previous build is back in %s" % prefix)
            except OSError as e2:
                raise ToolError("could not put the previous build back (%s).  It is intact at %s:\n    mv %s %s"
                                % (e2, old, shlex.quote(old), shlex.quote(prefix)))
        if isinstance(e, OSError):
            raise ToolError("cannot install into %s: %s" % (prefix, e))
        raise
    return old


def link_current(opt_root, name, prefix):
    """<opt>/<name> -> prefix: relative when the prefix is in <opt>, made beside and renamed
    over (atomic: there is never a moment without the link).  A real directory there is left
    alone."""
    link = os.path.join(opt_root, name)
    if os.path.lexists(link) and not os.path.islink(link):
        raise ToolError("%s is a real directory, not a link: not touching it" % link)
    os.makedirs(opt_root, exist_ok=True)
    target = os.path.basename(prefix) if os.path.dirname(os.path.abspath(prefix)) == os.path.abspath(opt_root) else prefix
    tmp = "%s.new-%d" % (link, os.getpid())
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return link


def copy_tree(src, dst, ignore=()):
    """copytree that merges into an existing directory (3.6 has no dirs_exist_ok) and keeps links."""
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(src):
        if any(re.match(fnpat, n) for fnpat in ignore):
            continue
        s, d = os.path.join(src, n), os.path.join(dst, n)
        if os.path.islink(s):
            if os.path.lexists(d):
                os.unlink(d)
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            copy_tree(s, d, ignore)
        else:
            shutil.copy2(s, d)


def make_readable(root):
    """chmod -R a+rX, as package.py's fix_permissions does."""
    for dirpath, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                continue
            m = os.stat(p).st_mode
            add = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if stat.S_ISDIR(m) or m & stat.S_IXUSR:
                add |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            os.chmod(p, (stat.S_IMODE(m) | add) & ~stat.S_IWGRP)
    os.chmod(root, stat.S_IMODE(os.stat(root).st_mode) | 0o755)


BYHAND_IGNORE = (r".*\.pyc$", r"^__pycache__$", r".*~$", r".*_cffi\.c$", r".*\.o$", r"^\.svn$")


def assemble_by_hand(src, goal, tree, series):
    """What package.py does, done here -- only when package.py failed.  pypy2.7: bin/pypy +
    libpypy-c.so, lib-python/2.7, lib_pypy, include; pypy3.N: bin/pypy3.N + libpypy3.N-c.so,
    lib/pypy3.N = lib-python/3 + lib_pypy (without its __init__.py), include/pypy3.N.  The
    libpypy .so sits next to the exe: RPython links with rpath $ORIGIN/."""
    is3 = series != "2.7"
    exe_c = "pypy%s-c" % series if is3 else "pypy-c"
    exe = "pypy" + series if is3 else "pypy"
    bindir = os.path.join(tree, "bin")
    os.makedirs(bindir, exist_ok=True)
    shutil.copy2(os.path.join(goal, exe_c), os.path.join(bindir, exe))
    so = os.path.join(goal, "lib%s.so" % exe_c)
    if os.path.isfile(so):
        shutil.copy2(so, os.path.join(bindir, os.path.basename(so)))
    links = ("pypy", "pypy3", "python", "python3", "python" + series) if is3 else \
        ("pypy2.7", "pypy2", "python", "python2.7", "python2")
    for l in links:
        if not os.path.lexists(os.path.join(bindir, l)):
            os.symlink(exe, os.path.join(bindir, l))
    if is3:
        lib = os.path.join(tree, "lib", "pypy" + series)
        copy_tree(os.path.join(src, "lib-python", "3"), lib, BYHAND_IGNORE)
        copy_tree(os.path.join(src, "lib_pypy"), lib, BYHAND_IGNORE)
        if os.path.isfile(os.path.join(lib, "__init__.py")):
            os.unlink(os.path.join(lib, "__init__.py"))
        sp = os.path.join(lib, "site-packages")
        readme = os.path.join(src, "lib-python", "3", "site-packages", "README.txt")
        inc_src = os.path.join(src, "include", "pypy" + series)
        inc_dst = os.path.join(tree, "include", "pypy" + series)
    else:
        copy_tree(os.path.join(src, "lib-python", "2.7"), os.path.join(tree, "lib-python", "2.7"), BYHAND_IGNORE)
        copy_tree(os.path.join(src, "lib_pypy"), os.path.join(tree, "lib_pypy"), BYHAND_IGNORE)
        sp = os.path.join(tree, "site-packages")
        readme = os.path.join(src, "site-packages", "README")
        inc_src, inc_dst = os.path.join(src, "include"), os.path.join(tree, "include")
    os.makedirs(sp, exist_ok=True)
    if os.path.isfile(readme):
        shutil.copy2(readme, sp)
    if not os.path.isdir(inc_src):
        inc_src = os.path.join(src, "include")
    if os.path.isdir(inc_src):
        copy_tree(inc_src, inc_dst, BYHAND_IGNORE)
    for f in ("LICENSE", "README.rst"):
        if os.path.isfile(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(tree, f))
    make_readable(tree)
    return os.path.join(bindir, exe)


# ------------------------------------------------------------- the release list

def cache_write(path, text):
    """A cache is a convenience: a directory that cannot be written is not an error."""
    try:
        write_atomic(path, text)
    except OSError:
        pass


def git_ls_remote_tags():
    if not which("git"):
        raise ToolError("git is not installed")
    p = run(["git", "ls-remote", "--tags", "--refs", PYPY_GIT, "release-pypy*"], check=False,
            env=scrubbed_env(extra={"GIT_TERMINAL_PROMPT": "0"}), timeout=90)
    if p.returncode != 0:
        raise ToolError("git ls-remote %s failed: %s" % (PYPY_GIT, tail(p.stdout, 3)))
    return p.stdout or ""


def notes_getter(get, cache_dir, offline=False):
    """release -> its release notes (raw.githubusercontent.com), remembered for this run and
    kept in <workdir>/cache/notes when a cache directory is given."""
    memo = {}

    def notes(rel):
        if rel not in memo:
            path = os.path.join(cache_dir, "notes", "release-v%s.rst" % rel) if cache_dir else ""
            text = read_text(path) if path else ""
            err = None
            if not text and not offline:
                try:
                    text = get(PYPY_NOTES_URL % rel).decode("utf-8", "replace")
                    if path and text:
                        cache_write(path, text)
                except ToolError as e:
                    err = e
            memo[rel] = text or (err or ToolError("no release notes for %s" % rel))
        if isinstance(memo[rel], ToolError):
            raise memo[rel]
        return memo[rel]
    return notes


def load_catalog(opts, workdir, get=None, ls_remote=None, write_cache=True):
    """downloads.python.org's index (everything); when that is unreachable, versions.json
    (binaries, flags) + the git tags (sources); offline or when all fail, the cached index."""
    get = get or (lambda u: http_get(u, timeout=30))
    ls_remote = ls_remote or git_ls_remote_tags
    cache = os.path.join(workdir, "cache")
    offline = bool(opts.get("offline"))
    notes = notes_getter(get, cache if write_cache else None, offline)
    errors = []

    def versions_json():
        for u in PYPY_VERSIONS_URLS:
            try:
                raw = get(u)
                flags, binrels = parse_versions_json(raw)
                if write_cache:
                    cache_write(os.path.join(cache, "versions.json"), raw.decode("utf-8", "replace"))
                return flags, binrels
            except ToolError as e:
                errors.append(str(e).splitlines()[0])
        cached = read_text(os.path.join(cache, "versions.json"))
        if cached:
            try:
                return parse_versions_json(cached)
            except ToolError:
                pass
        return {}, {}
    if not offline:
        try:
            html = get(PYPY_BASE).decode("utf-8", "replace")
            rels = parse_index(html)
            if rels:
                if write_cache:
                    cache_write(os.path.join(cache, "index.html"), html)
                flags, _ = versions_json()
                return Catalog(rels, flags, "the downloads.python.org index", notes)
            errors.append("%s lists no PyPy files (a proxy's page?)" % PYPY_BASE)
        except ToolError as e:
            errors.append(str(e).splitlines()[0])
        flags, binrels = versions_json()
        try:
            tags = parse_git_tags(ls_remote())
        except ToolError as e:
            errors.append(str(e).splitlines()[0])
            tags = {}
        if tags:
            rels = {}
            for rel, ss in tags.items():
                r = rels.setdefault(rel, {"src": {}, "bin": {}})
                for s in ss:
                    r["src"][s] = ""
            for rel, r in binrels.items():
                rels.setdefault(rel, {"src": {}, "bin": {}})["bin"] = r["bin"]
            note("the downloads.python.org index is not reachable (%s): the release list comes from "
                 "versions.json and the release tags of %s" % (errors[0][:160] if errors else "?", PYPY_GIT))
            return Catalog(rels, flags, "versions.json + the git tags of github.com/pypy/pypy", notes)
    html = read_text(os.path.join(cache, "index.html"))
    if html and parse_index(html):
        flags, _ = parse_versions_json(read_text(os.path.join(cache, "versions.json")) or "[]")
        when = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(os.path.join(cache, "index.html"))))
        if not offline:
            note("upstream is not reachable: using the release list cached on %s" % when)
        return Catalog(parse_index(html), flags, "the index cached on %s" % when, notes)
    raise ToolError("cannot get the list of PyPy releases%s:\n    %s\n  Name the release (--pypy-version X.Y.Z, "
                    "and --series 3.N for pypy3) and, if need be, the source (--source PATH)."
                    % (" (--offline, and nothing cached)" if offline else "", "\n    ".join(errors) or "-"))


def get_catalog(opts, workdir, write_cache=True, **kw):
    """load_catalog, or -- when it fails but the release is named -- an empty list that
    lets the named release through."""
    try:
        return load_catalog(opts, workdir, write_cache=write_cache, **kw)
    except ToolError as e:
        named = opts.get("pypy_version") not in ("auto", "latest", "", None)
        if named and (opts.get("target") in ("pypy2", "host") or opts.get("series") not in ("auto", "latest")):
            warn(str(e).splitlines()[0] + " -- going ahead with the release you named")
            return Catalog({}, {}, "(no release list: --pypy-version %s as given)" % opts["pypy_version"])
        raise


def load_checksums(cat, workdir, offline=False, get=None):
    """pypy.org/checksums.html -> {file: sha256}; cached.  Unreachable or changed: TLS alone."""
    if cat.checksums is not None:
        return cat.checksums
    path = os.path.join(workdir, "cache", "checksums.html")
    html = ""
    if not offline:
        try:
            html = (get or (lambda u: http_get(u, timeout=30)))(PYPY_CHECKSUMS_URL).decode("utf-8", "replace")
            if parse_checksums(html):
                cache_write(path, html)
        except ToolError as e:
            note("pypy.org's checksums page cannot be read (%s)" % str(e).splitlines()[0][:160])
    if not parse_checksums(html):
        html = read_text(path)
    cat.checksums = parse_checksums(html)
    if not cat.checksums:
        note("no published checksums: the PyPy downloads' integrity rests on TLS alone")
    return cat.checksums


# -------------------------------------------------------------------- doctor

PACKAGES = {
    "gcc": {"dnf": "gcc", "apt-get": "gcc", "zypper": "gcc", "pacman": "gcc"},
    "make": {"dnf": "make", "apt-get": "make", "zypper": "make", "pacman": "make"},
    "patch": {"dnf": "patch", "apt-get": "patch", "zypper": "patch", "pacman": "patch"},
    "tar": {"dnf": "tar", "apt-get": "tar", "zypper": "tar", "pacman": "tar"},
    "gzip": {"dnf": "gzip", "apt-get": "gzip", "zypper": "gzip", "pacman": "gzip"},
    "bzip2": {"dnf": "bzip2", "apt-get": "bzip2", "zypper": "bzip2", "pacman": "bzip2"},
    "xz": {"dnf": "xz", "apt-get": "xz-utils", "zypper": "xz", "pacman": "xz"},
    "pkgconf": {"dnf": "pkgconf-pkg-config", "apt-get": "pkg-config", "zypper": "pkg-config", "pacman": "pkgconf"},
    "binutils": {"dnf": "binutils", "apt-get": "binutils", "zypper": "binutils", "pacman": "binutils"},
    "libffi": {"dnf": "libffi-devel", "apt-get": "libffi-dev", "zypper": "libffi-devel", "pacman": "libffi"},
    "zlib": {"dnf": "zlib-devel", "apt-get": "zlib1g-dev", "zypper": "zlib-devel", "pacman": "zlib"},
    "libbz2": {"dnf": "bzip2-devel", "apt-get": "libbz2-dev", "zypper": "libbz2-devel", "pacman": "bzip2"},
    "expat": {"dnf": "expat-devel", "apt-get": "libexpat1-dev", "zypper": "libexpat-devel", "pacman": "expat"},
    "openssl": {"dnf": "openssl-devel", "apt-get": "libssl-dev", "zypper": "libopenssl-devel", "pacman": "openssl"},
    "sqlite": {"dnf": "sqlite-devel", "apt-get": "libsqlite3-dev", "zypper": "sqlite3-devel", "pacman": "sqlite"},
    "liblzma": {"dnf": "xz-devel", "apt-get": "liblzma-dev", "zypper": "xz-devel", "pacman": "xz"},
    "ncurses": {"dnf": "ncurses-devel", "apt-get": "libncurses-dev", "zypper": "ncurses-devel", "pacman": "ncurses"},
    "gdbm": {"dnf": "gdbm-devel", "apt-get": "libgdbm-dev", "zypper": "gdbm-devel", "pacman": "gdbm"},
    "tk": {"dnf": "tk-devel", "apt-get": "tk-dev", "zypper": "tk-devel", "pacman": "tk"},
    "readline": {"dnf": "readline-devel", "apt-get": "libreadline-dev", "zypper": "readline-devel", "pacman": "readline"},
    "dpkg-dev": {"apt-get": "dpkg-dev"},
    "wget": {"dnf": "wget", "apt-get": "wget", "zypper": "wget", "pacman": "wget"},
    "git": {"dnf": "git", "apt-get": "git", "zypper": "git", "pacman": "git"},
}
# On Enterprise Linux these live in CRB (EL 9/10; PowerTools on EL 8), which is off by default.
CRB_PACKAGES = ("gdbm-devel",)
HEADERS = {"libffi": "ffi.h", "zlib": "zlib.h", "libbz2": "bzlib.h", "expat": "expat.h", "openssl": "openssl/ssl.h",
           "sqlite": "sqlite3.h", "liblzma": "lzma.h", "ncurses": "curses.h", "ncurses_term": "term.h",
           "gdbm": "gdbm.h", "tk": "tk.h",
           "readline": "readline/readline.h"}
TOOLS = ("make", "tar", "gzip", "bzip2", "xz", "patch", "pkg-config", "as", "ld", "strip", "objcopy", "readelf",
         "wget", "curl", "git", "dpkg-architecture", "ldd")


def package_name(key, pm, machine):
    name = PACKAGES.get(key, {}).get(pm, "")
    if key == "zlib" and pm == "dnf" and el_major(machine) >= 10:
        return "zlib-ng-compat-devel"                  # EL10 replaced zlib with zlib-ng's compat build
    return name


def detect_machine(cc=None, binutils=""):
    """What this machine has.  Headers are asked of the compiler that will be used."""
    osr = parse_os_release(read_text("/etc/os-release"))
    total, avail, swap = meminfo_gb()
    extra = [os.path.join(binutils, "bin")] if binutils else None
    ccpath = (cc or {}).get("path", "")
    return {"os": "%s %s" % (osr.get("NAME", "Linux"), osr.get("VERSION_ID", "")),
            "os_id": osr.get("ID", ""), "os_like": osr.get("ID_LIKE", ""), "os_version": osr.get("VERSION_ID", ""),
            "arch": os.uname().machine, "arch_tag": arch_tag(), "glibc": vstr(glibc_version()) if glibc_version() else "",
            "cpus": cpu_count(), "mem_total_gb": round(total, 1), "mem_avail_gb": round(avail, 1),
            "swap_free_gb": round(swap, 1), "pkg_manager": find_pkg_manager(osr), "selinux": selinux_mode(),
            "tools": dict((t, which(t, ":".join((extra or []) + SYSTEM_PATH)) or "") for t in TOOLS),
            "headers": dict((k, have_header(h, ccpath, extra)) for k, h in sorted(HEADERS.items()))}


def requirements(opts, machine, targets, cc=None, cc_works=None, host_mode="cpython27"):
    """-> [(package key, what, present, level)] for THIS selection."""
    t, h = machine["tools"], machine["headers"]
    pypy3 = "pypy3" in targets
    why_not = "" if cc_works in (None, True) else ": " + str(cc_works)
    rows = [
        ("gcc", "a C compiler that builds and runs a program%s" % why_not, bool(cc) and cc_works in (None, True), "required"),
        ("make", "make", bool(t.get("make")), "required"),
        ("tar", "tar", bool(t.get("tar")), "required"),
        ("gzip", "gzip", bool(t.get("gzip")), "required"),
        ("bzip2", "bzip2", bool(t.get("bzip2")), "required"),
        ("xz", "xz", bool(t.get("xz")), "required"),
        ("pkgconf", "pkg-config: RPython finds libffi with it", bool(t.get("pkg-config")), "required"),
        ("binutils", "binutils: as, ld, strip, objcopy, readelf",
         all(t.get(x) for x in ("as", "ld", "strip", "objcopy", "readelf")), "required"),
        ("libffi", "libffi headers (ffi.h)", h.get("libffi"), "required"),
        ("zlib", "zlib headers (zlib.h)", h.get("zlib"), "required"),
        ("libbz2", "bzip2 headers (bzlib.h)", h.get("libbz2"), "required"),
        ("expat", "expat headers (expat.h)", h.get("expat"), "required"),
        ("openssl", "OpenSSL headers (openssl/ssl.h): the ssl module", h.get("openssl"), "required"),
        ("sqlite", "SQLite headers (sqlite3.h): sqlite3", h.get("sqlite"), "required"),
        ("liblzma", "liblzma headers (lzma.h): pypy3's lzma", h.get("liblzma"), "required" if pypy3 else "optional"),
        ("ncurses", "ncurses headers (curses.h, term.h): EVERY translation needs them (_minimal_curses), and the "
                    "2.7 host's _curses", bool(h.get("ncurses") and h.get("ncurses_term")), "required"),
        ("gdbm", "gdbm headers (gdbm.h): gdbm / dbm.gnu", h.get("gdbm"), "wanted"),
        ("patch", "patch", bool(t.get("patch")), "wanted"),
        ("tk", "Tk headers (tk.h): _tkinter", h.get("tk"), "required" if opts.get("with_tk") else "optional"),
    ]
    if host_mode == "cpython27":
        rows.append(("readline", "readline headers: the 2.7 host's readline (translating does not need it)",
                     h.get("readline"), "optional"))
        if machine.get("pkg_manager") == "apt-get":
            rows.append(("dpkg-dev", "dpkg-architecture: 2.7's setup.py finds /usr/include/<multiarch> (ffi.h) "
                         "with it when the compiler cannot say", bool(t.get("dpkg-architecture")), "wanted"))
    if opts.get("embed_deps"):
        rows.append(("wget", "wget or curl: --embed-deps downloads OpenSSL, gdbm and xz",
                     bool(t.get("wget") or t.get("curl")), "required"))
    rows.append(("git", "git: the fallback when downloads.python.org is unreachable", bool(t.get("git")), "optional"))
    return rows


def install_head(pm):
    """The install command without its packages: what one package at a time is put after."""
    if pm == "pacman":
        return ["sudo", "pacman", "-S", "--needed", "--noconfirm"]
    return ["sudo", pm, "install", "-y"]


def install_command(packages, pm):
    if not packages or not pm:
        return ""
    return " ".join(install_head(pm) + list(packages))


def doctor_report(rows, machine, euid=None):
    """The contract's shape, exactly: {tool, ok, package_manager, missing: [{what, package,
    level}], enable_repos, install_command}.  ok is false when anything required is missing
    or this runs as root."""
    pm = machine.get("pkg_manager") or ""
    missing = []
    euid = os.geteuid() if euid is None else euid
    if euid == 0:
        missing.append({"what": "not root: this runs as root -- build as yourself (only --install-deps uses sudo)",
                        "package": "", "level": "required"})
    for key, what, present, level in rows:
        if not present:
            missing.append({"what": what, "package": package_name(key, pm, machine), "level": level})
    pk = []
    for m in missing:
        if m["level"] in ("required", "wanted"):
            for p in m["package"].split():
                if p not in pk:
                    pk.append(p)
    enable = ["crb"] if pm == "dnf" and crb_hint(machine) and any(p in CRB_PACKAGES for p in pk) else []
    return {"tool": TOOL, "ok": not any(m["level"] == "required" for m in missing),
            "package_manager": pm or None, "missing": missing, "enable_repos": enable,
            "install_command": install_command(pk, pm)}


def refuse_root(what, euid=None):
    if (os.geteuid() if euid is None else euid) == 0:
        raise ToolError("do not %s as root: it builds and installs into your home, and root-owned files would "
                        "be left there.  Run it as yourself; only --install-deps uses sudo, by itself." % what)


def preflight(opts, targets, opt_root, workdir, quiet=False):
    """-> (report, machine, cc).  The compiler is TESTED: compile, link and run, in the
    environment the build will have."""
    try:
        cc = resolve_cc(opts.get("cc"), opt_root)
    except UsageError:
        raise
    try:
        binutils, _ = resolve_binutils(opts.get("binutils"), opt_root)
    except UsageError:
        binutils = ""
    machine = detect_machine(cc, binutils)
    works = None
    if cc:
        good, why = c_compiler_works(cc["path"], [os.path.join(binutils, "bin")] if binutils else None,
                                     scratch_dir(workdir))
        works = True if good else why
    mode = "cpython27"
    asked = opts.get("host") or "auto"
    if asked not in ("auto", "cpython27", "cpython"):
        mode = asked
    rows = requirements(opts, machine, targets, cc, works, mode)
    return doctor_report(rows, machine), machine, cc


def show_doctor(report, machine, cc):
    say("Preflight")
    traps = env_traps()
    if traps:
        note("set in your shell, and NOT passed on to the build: " + "  ".join(traps))
    if cc:
        info("C compiler : %s  (%s)" % (cc["banner"], cc["path"]))
        if cc["kind"] == "clang":
            warn("a clang: RPython's Makefile passes gcc-only flags (-Wno-discarded-qualifiers: about 100 "
                 "warnings in the 2026-08-29 log).  The toolkit translates with step 4's gcc.")
    for m in report["missing"]:
        line = "%s%s" % (m["what"], "   [%s]" % m["package"] if m["package"] else "")
        if m["level"] == "required":
            bad(line)
        else:
            note("%s (%s)" % (line, m["level"]))
    if report["ok"]:
        ok("everything required is here")
    if report["install_command"]:
        info("to install it:")
        hint = crb_hint(machine) if report["enable_repos"] else ""
        if hint:
            info("    %s" % hint)
        info("    %s" % report["install_command"])
    elif not report["ok"] and not report["package_manager"]:
        info("no dnf, apt-get, zypper or pacman here: install those by hand")
    return sum(1 for m in report["missing"] if m["level"] == "required")


def install_deps(report):
    """The one command; if the package manager refuses it as a whole, one package at a time,
    so one name this distribution does not have does not stop the rest."""
    pm = report["package_manager"]
    if not pm or not report["install_command"]:
        note("nothing to install")
        return
    refuse_root("run --install-deps")
    say("Installing with %s (sudo)" % pm)
    if report["enable_repos"] and pm == "dnf":
        run(["sudo", "dnf", "config-manager", "--set-enabled"] + report["enable_repos"], capture=False,
            interactive=True, check=False)
    if pm == "apt-get":
        run(["sudo", "apt-get", "update"], capture=False, interactive=True, check=False)
    cmd = shlex.split(report["install_command"])
    if run(cmd, capture=False, interactive=True, check=False).returncode == 0:
        return
    warn("the package manager refused the list as a whole -- one at a time")
    head = install_head(pm)
    if cmd[:len(head)] != head:
        raise ToolError("cannot split %r into a command and its packages: run it by hand" % report["install_command"])
    pkgs = cmd[len(head):]
    failed = [p for p in pkgs if run(head + [p], capture=False, interactive=True, check=False).returncode != 0]
    if failed:
        warn("not installed: %s" % " ".join(failed))


def url_answers(url, timeout=8):
    """-> (answers, why).  Any HTTP answer but a refusal (403/407: a proxy's block) counts:
    the host is there."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, "HTTP %d" % r.getcode()
    except urllib.error.HTTPError as e:
        return e.code not in (403, 407), "HTTP %d" % e.code
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return False, str(getattr(e, "reason", e))[:80]


def reachability(probe=None, git=None):
    """Which of the places this tool downloads from answer (text doctor only)."""
    probe = probe or url_answers
    rows = []
    for what, url in (("PyPy sources and binaries", PYPY_BASE), ("CPython 2.7.18", "https://www.python.org/ftp/python/"),
                      ("pycparser, cffi, setuptools", "https://files.pythonhosted.org/"),
                      ("release notes, versions.json", PYPY_VERSIONS_URLS[1])):
        good, why = probe(url)
        rows.append((what, url, good, why))
    try:
        (git or git_ls_remote_tags)()
        rows.append(("the git fallback", PYPY_GIT, True, "git ls-remote answers"))
    except ToolError as e:
        rows.append(("the git fallback", PYPY_GIT, False, str(e).splitlines()[-1].strip()[:80]))
    return rows


# ---------------------------------------------------------- status, the record

INSTALL_NAME = re.compile(r"^pypy(2\.7|3\.\d+)-v(\d+\.\d+\.\d+(?:rc\d+)?)$")


def prefix_entry(path, run_checks=True):
    """One interpreter prefix -> the status contract's installed entry, or None."""
    exe, series = find_exe(path)
    root = path
    if not exe:
        for sub in sorted(glob.glob(os.path.join(glob.escape(path), "*", "bin"))):  # ~/opt/pypy2/pypy2/bin/pypy
            exe, series = find_exe(os.path.dirname(sub))
            if exe:
                root = os.path.dirname(sub)
                break
    if not exe:
        return None
    rec = read_json(os.path.join(path, MARKER)) or {}
    mine = rec.get("tool") == TOOL
    version, lang = rec.get("release") if mine else None, rec.get("series") if mine else None
    runs_ = False
    if run_checks:
        env = verify_env()
        try:
            p = run([exe, "-c", "import sys; print('%d.%d.%d %d.%d' % (tuple(getattr(sys, 'pypy_version_info', "
                     "(0, 0, 0))[:3]) + tuple(sys.version_info[:2])))"], check=False, env=env, timeout=60)
            parts = (p.stdout or "").split()
            if p.returncode == 0 and len(parts) >= 2:
                version = version or (parts[-2] if parts[-2] != "0.0.0" else None)
                lang = lang or parts[-1]
            runs_ = p.returncode == 0 and ssl_outcome(run([exe, "-c", SSL_SCRIPT], check=False, env=env,
                                                          timeout=60).returncode)[0] == "ok"
        except ToolError:
            runs_ = False
    m = INSTALL_NAME.match(os.path.basename(path))
    lang = lang or (m.group(1) if m else (series or None))
    name = "pypy3" if (lang or "").startswith("3") or os.path.basename(path).startswith("pypy3") else "pypy2"
    return {"name": name, "version": version or (m.group(2) if m else None), "prefix": path, "runs": bool(runs_),
            "built_by_tool": bool(mine and rec.get("state") == "verified"),
            "compiler": rec.get("compiler") if mine else None,
            "for": ("python" + lang) if lang else None, "_root": root}


def status_data(opt_root, workdir, run_checks=True):
    """The toolkit's status contract, exactly: {tool, opt_root, current, installed, builds}.
    runs = the step's done-when: the interpreter starts AND makes an ssl context."""
    data = {"tool": TOOL, "opt_root": opt_root, "current": {}, "installed": [], "builds": []}
    for name in ("pypy2-current", "pypy3-current"):
        link = os.path.join(opt_root, name)
        data["current"][name] = os.path.realpath(link) if os.path.lexists(link) else None
    try:
        names = sorted(os.listdir(opt_root))
    except OSError:
        names = []
    for n in names:
        p = os.path.join(opt_root, n)
        if not n.startswith("pypy") or n.endswith("-current") or os.path.islink(p) or not os.path.isdir(p) \
                or re.search(r"\.(old|partial|new|failed)-", n):
            continue
        e = prefix_entry(p, run_checks)
        if e:
            e.pop("_root", None)
            # a real path, as "current" holds: the main file matches pypyN-current to its entry by
            # string, even when the opt root is reached through a symlink
            e["prefix"] = os.path.realpath(p)
            data["installed"].append(e)
    rec = read_json(os.path.join(workdir, "state.json")) or {}
    held = lock_is_held(workdir)
    for key, b in sorted((rec.get("builds") or {}).items()):
        st = b.get("status") or "unknown"
        if st == "running" and not held:
            st = "killed"
        data["builds"].append({"id": key, "status": st, "stage": b.get("stage"), "error": b.get("error")})
    return data


# ------------------------------------------------------------------ the stages

def run_stage(ctx, state, name, fn, valid=None, always=False):
    """One stage, recorded: running -> done | failed | interrupted.  A stage recorded done is
    skipped while its product is still there; `always` stages (fetch, host: cheap when their
    product is cached, and they fill in what the later stages read) run every time.
    -> True when it ran."""
    key = ctx.key
    if not always and state.done(key, name) and (valid is None or valid()):
        info("%-13s done earlier (%s)" % (name, state.stage(key, name).get("finished", "")))
        return False
    if not always and name in AFTER_HOST and name != "smoke":
        # What follows was made from what this stage made before: it is made again too (a new
        # stage is verified again; a new pypy-c gets its cffi modules tried again).  The smoke
        # test proves only the host and the compiler, so it never undoes a translation.
        state.reset(key, [n for n in AFTER_HOST[AFTER_HOST.index(name) + 1:] if n != "smoke"])
    state.set_stage(key, name, status="running", started=now(), error=None, finished=None)
    state.set_build(key, status="running", stage=name, error=None)
    t0 = time.time()
    try:
        fn()
    except (KeyboardInterrupt, Interrupted):
        state.set_stage(key, name, status="interrupted", seconds=int(time.time() - t0))
        state.set_build(key, status="interrupted", stage=name, error="interrupted")
        raise
    except Exception as e:
        msg = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
        state.set_stage(key, name, status="failed", seconds=int(time.time() - t0), error=msg)
        state.set_build(key, status="failed", stage=name, error=msg)
        raise
    state.set_stage(key, name, status="done", finished=now(), seconds=int(time.time() - t0))
    state.set_build(key, status="partial")          # build_one says done at the very end
    return True


def do_fetch(ctx, state, cat):
    """The source tarball into <workdir>/downloads, checked; or the tree you named."""
    src = ctx.opts.get("source") or "auto"
    if src not in ("auto", "git"):
        p = expand(src)
        if os.path.isdir(p):
            ctx.src = {"kind": "tree", "path": p, "url": p, "sha256": ""}
            ok("source: your tree %s (copied: translation writes into it)" % p)
        elif os.path.isfile(p):
            ctx.src = {"kind": "tarball", "path": p, "url": p, "sha256": sha256_file(p)}
            ok("source: your tarball %s" % p)
        else:
            raise UsageError("--source %s: no such file or directory" % src)
        state.set_build(ctx.key, source=ctx.src)
        return
    say("Source: %s" % ctx.stem)
    gtar = os.path.join(ctx.ddir, "%s-src.git.tar" % ctx.stem)
    if src == "auto":
        load_checksums(cat, ctx.workdir, ctx.opts.get("offline"))
        cands, _ = source_candidates(cat, ctx.release, ctx.series)
        if os.path.isfile(gtar) and not any(os.path.isfile(os.path.join(ctx.ddir, c[1])) for c in cands):
            ctx.src = {"kind": "tarball", "path": gtar, "url": "%s@%s" % ctx.src_git, "sha256": sha256_file(gtar)}
            ok("already here: %s" % gtar)
            state.set_build(ctx.key, source=ctx.src)
            return
        try:
            path, url, sha = fetch_any(cands, ctx.ddir, ctx.opts.get("offline"))
            ctx.src = {"kind": "tarball", "path": path, "url": url, "sha256": sha}
            state.set_build(ctx.key, source=ctx.src)
            return
        except ToolError as e:
            if ctx.opts.get("offline"):
                raise
            note("no tarball could be downloaded: trying git (%s)" % str(e).splitlines()[-1].strip()[:100])
    if not os.path.isfile(gtar):
        commit = git_archive(ctx.src_git[0], ctx.src_git[1], gtar, ctx.stem + "-src", ctx.tmp)
        ok("%s at %s (commit %s): integrity rests on TLS and git" % (ctx.src_git[0], ctx.src_git[1], commit[:12]))
    ctx.src = {"kind": "tarball", "path": gtar, "url": "%s@%s" % ctx.src_git, "sha256": sha256_file(gtar)}
    state.set_build(ctx.key, source=ctx.src)


def do_host(ctx, state, cat):
    """The translation host: built, downloaded or found -- and asked whether RPython can run on it."""
    mode = ctx.host.get("mode")
    why = ctx.host.get("why", "")
    if mode == "cpython27":
        py = build_cpython27(ctx.workdir, ctx.cc, ctx.binutils, ctx.jobs, state, ctx.opts.get("offline"),
                             ctx.opts.get("verbose"), force=bool(ctx.opts.get("rebuild_host")))
        pr = probe_interpreter(py)
    elif mode == "prebuilt":
        pr, rel = get_prebuilt_host(cat, ctx.workdir, ctx.opts.get("offline"))
        why = "%s (PyPy2 %s)" % (why, rel)
    else:
        pr = probe_interpreter(ctx.host.get("path"))
    problem = host_problem(pr, ctx.host.get("path") or mode)
    if problem:
        raise ToolError(problem)
    ctx.host = dict(pr, mode=mode, why=why)
    info("host: %s  (%s %s%s)" % (ctx.host["path"], "PyPy" if pr["impl"] == "pypy" else "CPython",
                                   pr.get("pypy") or pr["version"], ", pycparser " + pr["pycparser"]))
    state.set_build(ctx.key, host=dict((k, ctx.host.get(k)) for k in ("mode", "path", "version", "pypy", "impl")))


def fingerprint(ctx):
    """What the translation is a function of.  A change since the recorded run means the
    source tree and everything after it are made again."""
    return {"release": ctx.release, "series": ctx.series, "source_sha256": ctx.src.get("sha256", ""),
            "host": ctx.host.get("path", ""), "host_version": ctx.host.get("pypy") or ctx.host.get("version", ""),
            "cc": ctx.cc["path"], "cc_banner": ctx.cc["banner"], "binutils": ctx.binutils,
            "extra_cflags": ctx.opts.get("extra_cflags") or "", "lto": bool(ctx.opts.get("lto"))}


def fingerprint_diff(then, now_):
    return ["%s: %s -> %s" % (k, then.get(k), now_.get(k)) for k in sorted(set(then) | set(now_))
            if then.get(k) != now_.get(k)]


AFTER_HOST = ("extract", "smoke", "translate", "setuptools", "cffi", "package", "verify-stage", "install",
              "verify", "link")
PER_PREFIX = ("package", "verify-stage", "install", "verify", "link")


def do_extract(ctx):
    rmtree(ctx.src_dir)
    if ctx.src["kind"] == "tree":
        info("copying %s -> %s" % (ctx.src["path"], ctx.src_dir))
        shutil.copytree(ctx.src["path"], ctx.src_dir, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "*.pyc", "__pycache__"))
    else:
        info("extracting %s" % os.path.basename(ctx.src["path"]))
        extract(ctx.src["path"], ctx.src_dir)
    for need in (("pypy", "goal", "targetpypystandalone.py"), ("rpython", "bin", "rpython")):
        if not os.path.isfile(os.path.join(ctx.src_dir, *need)):
            raise ToolError("unexpected source layout: no %s under %s" % (os.path.join(*need), ctx.src_dir))
    ok("source tree: %s" % ctx.src_dir)


# The JIT smoke target: TLR, RPython's toy register language with a JIT driver, and its SQUARE
# program (a loop: x*x by repeated addition).  rpython/translator/goal/targetjitstandalone.py
# is NOT used: in 8.0.0 it has two jit drivers and neither is_recursive, so it no longer
# translates ("if you have more than one jitdriver ..." -- found by this tool's smoke run).
JIT_SMOKE = (("rpython", "jit", "tl", "targettlr.py"), "50000", "2500000000")
SQUARE_WRITER = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom rpython.jit.tl.tlr import SQUARE\n"
                 "f = open(sys.argv[2], 'wb')\nf.write(SQUARE)\nf.close()\n")


def smoke_test(host, src_dir, cc, binutils, workdir, jit=False, low_memory=False, verbose=False, logs=None):
    """A real RPython translation of a tiny target (25 s; the JIT one ~7 min): proves the host, the C
    compiler, make and the usession directory before hours go into the real one.  jit: a toy
    interpreter WITH a JIT, translated --opt=jit, then run hot enough to compile machine code --
    the JIT backend and executable memory, proved."""
    if jit:
        tpath = os.path.join(src_dir, *JIT_SMOKE[0])
    else:
        tpath = os.path.join(src_dir, "rpython", "translator", "goal", "targetnopstandalone.py")
    target = os.path.basename(tpath)
    if not os.path.isfile(tpath):
        raise ToolError("no %s in %s" % (target, src_dir))
    sdir = os.path.join(workdir, "smoke")
    rmtree(sdir)
    os.makedirs(os.path.join(sdir, "usession"))
    path = [os.path.join(binutils, "bin")] if binutils else []
    env = scrubbed_env(path, {"CC": cc["path"], "CFLAGS": "", "LDFLAGS": "",
                              "PYPY_USESSION_DIR": os.path.join(sdir, "usession"), "PYPY_USESSION_BASENAME": "smoke",
                              "PYPY_USESSION_KEEP": "1", "TMPDIR": os.path.join(workdir, "tmp"),
                              "PYTHONNOUSERSITE": "1"})
    env.update(host_env_extra(host, low_memory))
    os.makedirs(os.path.join(workdir, "tmp"), exist_ok=True)
    argv = host_argv(host, low_memory) + [os.path.join(src_dir, "rpython", "bin", "rpython"), "--batch",
                                          "--opt=jit" if jit else "--opt=2", "--cc=" + cc["path"], tpath]
    log = os.path.join(logs or os.path.join(workdir, "logs"), "smoke.log")
    say("Smoke test: translating %s%s" % (target, " with the JIT (about 7 minutes with a CPython host)" if jit
                                          else " (under a minute)"))
    info("log: %s" % log)
    w = TranslationWatcher("smoke")
    rc, secs = run_logged(argv, sdir, env, log, w, verbose)
    text = read_text(log)
    exe = os.path.join(sdir, target[:-3] + "-c")
    if rc != 0 or not os.access(exe, os.X_OK):
        kind = classify_translation(rc, text)
        if kind == "interrupted":
            raise Interrupted("the smoke translation was interrupted")
        raise ToolError("the smoke translation FAILED (exit %d, %s) -- the host or the compiler cannot translate.\n"
                        "  Log: %s\n%s" % (rc, kind, log, "\n".join("    " + l for l in translation_errors(text))))
    if jit:
        prog = os.path.join(sdir, "square.tlr")
        p = run(host_argv(host) + ["-c", SQUARE_WRITER, src_dir, prog], check=False, env=scrubbed_env(), timeout=120)
        if p.returncode != 0 or not os.path.isfile(prog):
            raise ToolError("cannot write the TLR test program with the host:\n%s" % tail(p.stdout, 5))
        cmd, want = [exe, prog, JIT_SMOKE[1]], JIT_SMOKE[2]
    else:
        cmd, want = [exe], "debug: hello world"
    p = run(cmd, check=False, env=scrubbed_env(), timeout=120)
    if p.returncode != 0 or want not in (p.stdout or ""):
        kind, sig = ssl_outcome(p.returncode)
        raise ToolError("the smoke binary %s did not say %r (exit %d%s):\n%s" % (
            exe, want, p.returncode, ", signal %d: is executable memory denied (SELinux)?" % sig if kind == "crash" else "",
            tail(p.stdout, 5)))
    ok("smoke test passed in %s: %s runs%s (host %s, cc %s)" % (hms(secs), os.path.basename(exe),
                                                              " and its JIT works" if jit else "", host["path"], cc["path"]))
    return secs


def translation_products(ctx):
    return [os.path.join(ctx.goal, ctx.exe_c), os.path.join(ctx.goal, ctx.lib_c)]


def compile_resumable(ctx, state):
    """-> the C directory when the last translation failed while compiling its C (the
    Makefile is there): `make` again there saves the hour of re-translating."""
    b = state.build(ctx.key)
    cfile = b.get("c_file") or ""
    d = os.path.dirname(cfile)
    if cfile and b.get("failed_phase") == "Compiling c source" and os.path.isfile(os.path.join(d, "Makefile")):
        return d
    return ""


def do_translate(ctx, state):
    if ctx.opts.get("from_compile"):
        d = compile_resumable(ctx, state)
        if not d:
            raise ToolError("--from compile: the last translation did not fail while compiling its C, or its "
                            "usession directory is gone -- run without --from compile")
        return resume_compile(ctx, state, d)
    for p in translation_products(ctx):
        rmtree(p)
    rmtree(ctx.usession)
    os.makedirs(ctx.usession)
    os.makedirs(ctx.tmp, exist_ok=True)
    argv, env = rpython_argv(ctx), translate_env(ctx)
    log = os.path.join(ctx.logs, "translate.log")
    say("Translating %s -- the long part" % ctx.stem)
    info("RPython is mostly one busy core until it writes C; that is normal, not a hang.")
    info("%s" % " ".join(shlex.quote(a) for a in argv))
    info("cwd %s   log %s" % (ctx.goal, log))
    w = TranslationWatcher(ctx.target + " translate")
    rc, secs = run_logged(argv, ctx.goal, env, log, w, ctx.opts.get("verbose"))
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    text = read_text(log)
    kind = classify_translation(rc, text)
    state.set_build(ctx.key, peak_rss_kb=peak, timings=w.timings, usession_dir=w.usession, c_file=w.c_file,
                    translate_seconds=secs, failed_phase=failed_phase(text) if kind != "ok" else "")
    if kind == "ok":
        missing = [p for p in translation_products(ctx) if not os.path.isfile(p)]
        if missing:
            raise ToolError("rpython exited 0 but did not leave %s" % ", ".join(missing))
        check_jit(ctx)
        ok("translated in %s (peak memory of the translation: %s)" % (hms(secs), human(peak * 1024)))
        return
    errs = "\n".join("    " + l for l in translation_errors(text))
    if kind == "interrupted":
        raise Interrupted("the translation was interrupted (exit %d).  A translation is all-or-nothing: `build` "
                          "again starts it over; the host, the download and the source are kept." % rc)
    total, avail, swap = meminfo_gb()
    if kind == "oom":
        raise ToolError("the translation ran OUT OF MEMORY (exit %d; this machine: %.1f GB RAM, %.1f GB swap free; "
                        "the translation's peak so far: %s).\n%s\n  Try: --low-memory (a PyPy host), more swap, or "
                        "fewer --jobs." % (rc, total, swap, human(peak * 1024), errs))
    hint = ""
    if NCURSES_RE.search(text):
        pkg = package_name("ncurses", ctx.machine.get("pkg_manager", ""), ctx.machine) or "the ncurses headers"
        hint += ("\n  ncurses: every translation needs its headers (curses.h, term.h) and a host with _curses.  "
                 "Install %s (`doctor --install-deps`), then `build` again%s" % (
                     pkg, " -- the CPython 2.7 host is checked and built again with _curses"
                     if ctx.host.get("mode") == "cpython27" else ""))
    if C23_RE.search(text):
        hint += "\n  These look like C23 errors (GCC 15+ defaults to C23): try --extra-cflags=-std=gnu17"
    if failed_phase(text) == "Compiling c source" and w.c_file:
        hint += ("\n  It failed while compiling the C: once fixed, `build --from compile` runs make again in %s "
                 "instead of translating again" % os.path.dirname(w.c_file))
    raise ToolError("the translation FAILED (exit %d) in phase: %s.  Log: %s\n%s%s"
                    % (rc, failed_phase(text) or "?", log, errs, hint))


def check_jit(ctx):
    exe = os.path.join(ctx.goal, ctx.exe_c)
    p = run([exe, "-c", "import sys; print(sys.pypy_translation_info['translation.jit'])"], check=False,
            env=verify_env(), timeout=120)
    if p.returncode != 0 or (p.stdout or "").strip().splitlines()[-1:] != ["True"]:
        raise ToolError("%s is not a JIT build (or does not run):\n%s" % (exe, tail(p.stdout, 6)))


def resume_compile(ctx, state, cdir):
    log = os.path.join(ctx.logs, "compile.log")
    say("Compiling the C the last translation wrote (%s)" % cdir)
    rc, secs = run_logged(["make", "-j%d" % ctx.jobs], cdir, translate_env(ctx), log,
                          verbose=ctx.opts.get("verbose"), label="make")
    if rc != 0:
        raise ToolError("make failed again (exit %d).  Log: %s\n%s" % (rc, log, "\n".join(error_summary(log))))
    for name in (ctx.exe_c, ctx.lib_c):
        s = os.path.join(cdir, name)
        if not os.path.isfile(s):
            raise ToolError("make finished but %s is not there" % s)
        shutil.copy2(s, os.path.join(ctx.goal, name))
    state.set_build(ctx.key, failed_phase="")
    check_jit(ctx)
    ok("compiled in %s" % hms(secs))


def do_setuptools(ctx):
    """pypy3.12: no distutils, and package.py runs `pip install setuptools`.  pip from
    ensurepip (bundled), setuptools from the pinned wheel: no network after the download."""
    exe = os.path.join(ctx.goal, ctx.exe_c)
    name, url, sha, ver = SETUPTOOLS_WHEEL
    try:
        fetch_any([(url, name, sha)], ctx.ddir, ctx.opts.get("offline"))
    except ToolError as e:
        raise ToolError("pypy%s's package.py runs `pip install setuptools`, which needs %s -- it could not be "
                        "fetched (%s).  Put it in %s, or allow network to files.pythonhosted.org."
                        % (ctx.series, name, str(e).splitlines()[0][:100], ctx.ddir))
    env = cffi_env(ctx)
    run_logged([exe, "-m", "ensurepip"], ctx.goal, env, os.path.join(ctx.logs, "ensurepip.log"),
               verbose=ctx.opts.get("verbose"), label="ensurepip")
    log = os.path.join(ctx.logs, "setuptools.log")
    rc, _ = run_logged([exe, "-m", "pip", "install", "--no-index", "--find-links", ctx.ddir, "setuptools==" + ver],
                       ctx.goal, env, log, verbose=ctx.opts.get("verbose"), label="pip install setuptools")
    if rc != 0:
        raise ToolError("setuptools %s could not be installed into %s (offline, from %s).  Log: %s\n%s"
                        % (ver, exe, ctx.ddir, log, tail(read_text(log), 10)))
    ok("setuptools %s in the translated interpreter (package.py's `pip install setuptools` needs no network)" % ver)


def cffi_table(ctx, exe=None):
    exe = exe or os.path.join(ctx.goal, ctx.exe_c)
    keys = cffi_keys(ctx.series, ctx.src_dir)
    p = run([exe, "-c", IMPORT_TABLE] + keys, check=False, env=verify_env(), timeout=300)
    try:
        return json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ToolError("%s cannot import its cffi modules (exit %d):\n%s" % (exe, p.returncode, tail(p.stdout, 8)))


def judge_cffi(table, series, with_tk=False, keys=None):
    """-> (rows [(key, level, result)], failed required, failed others)."""
    rows, req, other = [], [], []
    for k in (cffi_keys(series) if keys is None else keys):
        level = cffi_level(k, series, with_tk)
        res = table.get(k, "not tried")
        rows.append((k, level, res))
        if res != "ok":
            (req if level == "required" else other).append(k)
    return rows, req, other


def do_cffi(ctx, state, machine):
    """build_cffi_imports.py with the new binary (the translation ran it too and ignored its
    errors), then every cffi module imported, in a table, each failure mapped to its package."""
    script = find_cffi_script(ctx.src_dir)
    if not script:
        raise ToolError("build_cffi_imports.py is not in %s" % ctx.src_dir)
    exe = os.path.join(ctx.goal, ctx.exe_c)
    argv = [exe, script] + (["--embed-dependencies"] if ctx.opts.get("embed_deps") else [])
    log = os.path.join(ctx.logs, "cffi.log")
    say("Building the cffi-backed stdlib modules (ssl, sqlite3, curses, ...)")
    info("log: %s" % log)
    rc, secs = run_logged(argv, ctx.goal, cffi_env(ctx), log, verbose=ctx.opts.get("verbose"), label="cffi")
    table = cffi_table(ctx, exe)
    rows, req, other = judge_cffi(table, ctx.series, ctx.opts.get("with_tk"), cffi_keys(ctx.series, ctx.src_dir))
    pm = machine.get("pkg_manager", "")
    for k, level, res in rows:
        if res == "ok":
            ok("%-22s %s" % (k, level))
        elif level == "skipped":
            note("%-22s skipped (--with-tk builds it)" % k)
        else:
            pkg = package_name(CFFI_PACKAGE.get(k, ""), pm, machine) if CFFI_PACKAGE.get(k) else ""
            (bad if level == "required" else warn)("%-22s %s: %s%s" % (k, level, res[:90], "   [%s]" % pkg if pkg else ""))
    without = [k for k in other if k != "_tkinter"]
    state.set_build(ctx.key, cffi=table, without=without)
    if req:
        pk = sorted(set(package_name(CFFI_PACKAGE[k], pm, machine) for k in req if CFFI_PACKAGE.get(k)) - set([""]))
        raise ToolError("required cffi modules did not build: %s.  Log: %s%s"
                        % (", ".join(req), log, ("\n  Install: %s  -- then `build` again (it resumes here)"
                                                 % install_command(pk, pm)) if pk and pm else ""))


def do_package(ctx, state):
    """package.py (output KEPT in the log -- the .sh sent it to /dev/null), steered by
    environment variables; its output tree is the staging directory.  If it fails: assembled
    by hand, LOUDLY."""
    rmtree(ctx.pkgdir)
    os.makedirs(ctx.pkgdir)
    without = state.build(ctx.key).get("without") or []
    env = package_env(ctx)
    log = os.path.join(ctx.logs, "package.log")
    say("Packaging with package.py")
    info("log: %s" % log)
    # The 2.7 package.py is Python 2 only (os.environ.has_key, 0755): the host runs it.
    rc, _ = run_logged(package_argv(ctx, host_argv(ctx.host), without), ctx.src_dir, env, log,
                       verbose=ctx.opts.get("verbose"), label="package.py")
    if rc != 0 and ctx.is3 and not os.path.isdir(ctx.stage):
        note("package.py under the host stopped before making the tree: again with the built %s" % ctx.exe_c)
        log = os.path.join(ctx.logs, "package-retry.log")
        rc, _ = run_logged(package_argv(ctx, [os.path.join(ctx.goal, ctx.exe_c)], without), ctx.src_dir, env, log,
                           verbose=ctx.opts.get("verbose"), label="package.py")
    exe, _ = find_exe(ctx.stage, ctx.series)
    if rc == 0 and exe:
        state.set_build(ctx.key, packaged_by="package.py")
        ok("package.py made %s" % ctx.stage)
    else:
        errs = error_summary(log) or tail(read_text(log), 12).splitlines()
        loud("package.py FAILED (exit %d; log %s):\n%s\nThe prefix is assembled BY HAND instead.  It is verified like any "
             "other, but package.py knows this release's layout and this tool only copies what it did "
             "when written." % (rc, log, "\n".join(errs[:10])))
        rmtree(ctx.stage)
        assemble_by_hand(ctx.src_dir, ctx.goal, ctx.stage, ctx.series)
        state.set_build(ctx.key, packaged_by="by hand (package.py failed)")
    for f in glob.glob(os.path.join(glob.escape(ctx.pkgdir), ctx.stem + ".*")):
        if os.path.isfile(f):
            os.unlink(f)                                      # the archive package.py also wrote
    if ctx.opts.get("pip", True):
        exe, _ = find_exe(ctx.stage, ctx.series)
        log = os.path.join(ctx.logs, "stage-ensurepip.log")
        rc, _ = run_logged([exe, "-m", "ensurepip", "--default-pip"], ctx.stage,
                           dict(verify_env(), **offline_pip_env(ctx)), log, verbose=ctx.opts.get("verbose"),
                           label="ensurepip")
        if rc != 0:
            raise ToolError("ensurepip failed in the stage (exit %d).  Log: %s\n%s  (--no-pip skips it)"
                            % (rc, log, tail(read_text(log), 8)))
        ok("pip, from ensurepip's bundled wheels (no network)")


def marker_record(ctx, state, summary=None, verified=False):
    b = state.build(ctx.key)
    return {"tool": TOOL, "tool_version": VERSION, "target": ctx.target, "release": ctx.release,
            "series": ctx.series, "python": (summary or {}).get("version"), "prefix": ctx.prefix,
            "source": {"url": ctx.src.get("url"), "sha256": ctx.src.get("sha256")},
            "host": dict((k, ctx.host.get(k)) for k in ("mode", "path", "version", "pypy", "impl")),
            "cc": ctx.cc["path"], "compiler": ctx.cc["banner"], "cc_version": vstr(ctx.cc["version"]),
            "cxx": os.path.realpath(expand(ctx.opts["cxx"])) if ctx.opts.get("cxx") else None,
            "binutils": ctx.binutils or None,
            "rpython": rpython_argv(ctx), "cffi": b.get("cffi"), "packaged_by": b.get("packaged_by"),
            "verify": summary, "timings": b.get("timings"), "peak_rss_kb": b.get("peak_rss_kb"),
            "seconds": dict((k, v.get("seconds")) for k, v in sorted(b.get("stages", {}).items())),
            "state": "verified" if verified else "unverified", "built": now()}


def write_build_info(prefix, rec):
    lines = ["Built by %s %s" % (TOOL, VERSION),
             "PyPy      %s (Python %s)" % (rec["release"], rec.get("python") or rec["series"]),
             "source    %s" % rec["source"]["url"],
             "sha256    %s" % (rec["source"]["sha256"] or "-"),
             "host      %s %s (%s)" % (rec["host"].get("mode"), rec["host"].get("path"),
                                       rec["host"].get("pypy") or rec["host"].get("version")),
             "compiler  %s  (%s)" % (rec["compiler"], rec["cc"]),
             "binutils  %s" % (rec["binutils"] or "the compiler's own"),
             "rpython   %s" % " ".join(rec["rpython"]),
             "packaged  %s" % rec.get("packaged_by"),
             "built     %s" % rec["built"]]
    write_atomic(os.path.join(prefix, "BUILD-INFO.txt"), "\n".join(lines) + "\n")


def do_verify_stage(ctx, state):
    say("Verifying the staged interpreter (%s)" % ctx.stage)
    good, summary = verify_interpreter(ctx.stage, ctx.series, ctx.release, offline=True,
                                       with_tk=ctx.opts.get("with_tk"), expect_pip=ctx.opts.get("pip", True),
                                       cc_version=vstr(ctx.cc["version"]))
    state.set_build(ctx.key, verify_stage=summary)
    if not good:
        raise ToolError("the staged interpreter failed verification: %s\n  It is kept at %s; nothing was installed."
                        % ("; ".join(f.splitlines()[0] for f in summary["failures"]), ctx.stage))


def do_install(ctx, state):
    say("Installing %s" % ctx.prefix)
    old = install_tree(ctx.stage, ctx.prefix, rebuild=ctx.opts.get("rebuild"), marker=marker_record(ctx, state))
    state.set_build(ctx.key, replaced=old)
    ok("installed %s (%s)" % (ctx.prefix, human(dir_size(ctx.prefix))))


def do_verify(ctx, state):
    say("Verifying %s" % ctx.prefix)
    good, summary = verify_interpreter(ctx.prefix, ctx.series, ctx.release, offline=ctx.opts.get("offline"),
                                       with_tk=ctx.opts.get("with_tk"), expect_pip=ctx.opts.get("pip", True),
                                       cc_version=vstr(ctx.cc["version"]))
    old = state.build(ctx.key).get("replaced") or ""
    if not good:
        if old and os.path.isdir(old):
            aside = "%s.failed-%s" % (ctx.prefix, stamp())
            os.rename(ctx.prefix, aside)
            os.rename(old, ctx.prefix)
            state.set_build(ctx.key, replaced="")
            note("the new build is set aside in %s; the previous one is back" % aside)
        raise ToolError("%s failed verification: %s" % (ctx.prefix, "; ".join(f.splitlines()[0] for f in summary["failures"])))
    rec = marker_record(ctx, state, summary, verified=True)
    write_atomic(os.path.join(ctx.prefix, MARKER), json.dumps(rec, indent=1, sort_keys=True) + "\n")
    write_build_info(ctx.prefix, rec)
    if old and os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
        state.set_build(ctx.key, replaced="")


def do_link(ctx):
    link = link_current(ctx.opt_root, ctx.current_name, ctx.prefix)
    ok("%s -> %s" % (link, os.readlink(link)))


PATH_CAUTION = """\
  Never put <opt>/pypy2-current/bin or pypy3-current/bin on PATH: they hold `python` and
  `python3` links, so `python3` would BE PyPy for every script and build system on this
  machine (the HANDOFF's trap 1).  Call pypy / pypy3 by their full path, or link only those
  two names into a directory on PATH.  PyPy does not ship the same third-party modules, so
  anything that shells out to python3 and imports yaml, psutil or setuptools may fail in a
  way that looks like a broken build rather than a shadowed interpreter."""


def build_one(ctx, state, cat, machine):
    """fetch -> host -> extract -> [smoke] -> translate -> [setuptools] -> cffi -> package ->
    verify-stage -> install -> verify -> link.  Each recorded; a failure resumes at itself."""
    key = ctx.key
    then_prefix = state.build(key).get("prefix")
    if then_prefix and then_prefix != ctx.prefix:
        # The record is per release, shared by every --opt-root / --prefix: what was packaged,
        # installed and verified THERE says nothing about here.  (The translation and the cffi
        # modules do not depend on the prefix: they are kept.)
        note("%s was last built for %s; packaging, install and verification are done again for %s"
             % (ctx.stem, then_prefix, ctx.prefix))
        state.reset(key, PER_PREFIX)
    rec = read_json(os.path.join(ctx.prefix, MARKER)) if os.path.isdir(ctx.prefix) else None
    mine = bool(rec and rec.get("tool") == TOOL)
    if os.path.lexists(ctx.prefix) and not ctx.opts.get("rebuild"):
        if not mine:
            raise ToolError("%s exists and is not a build of this tool's: not touching it (--prefix puts the build "
                            "elsewhere)" % ctx.prefix)
        if rec.get("state") == "verified" and status_runs(ctx.prefix, ctx.series):
            say("%s: already built and verified in %s" % (ctx.stem, ctx.prefix))
            info("--rebuild builds it again")
            if ctx.link:
                do_link(ctx)
            state.set_build(key, status="done", stage="link" if ctx.link else "verify", error=None,
                            prefix=ctx.prefix)
            return
        if not state.done(key, "install"):
            raise ToolError("%s holds an UNVERIFIED build of this tool's: `verify %s` checks it, --rebuild replaces it"
                            % (ctx.prefix, ctx.prefix))
    state.set_build(key, target=ctx.target, release=ctx.release, series=ctx.series, prefix=ctx.prefix,
                    started=now(), status="running", error=None)
    run_stage(ctx, state, "fetch", lambda: do_fetch(ctx, state, cat), always=True)
    run_stage(ctx, state, "host", lambda: do_host(ctx, state, cat), always=True)
    fp = fingerprint(ctx)
    then = state.build(key).get("fingerprint")
    if then and then != fp:
        note("this build's inputs changed since the recorded run: %s" % "; ".join(fingerprint_diff(then, fp)))
        note("so the source tree and everything after it are made again")
        state.reset(key, AFTER_HOST)
        for d in (ctx.usession, ctx.pkgdir):
            rmtree(d)
    if ctx.opts.get("restart") or ctx.opts.get("rebuild"):
        state.reset(key, AFTER_HOST)                 # --rebuild: everything after the host, again
    state.set_build(key, fingerprint=fp)
    run_stage(ctx, state, "extract", lambda: do_extract(ctx),
              valid=lambda: os.path.isfile(os.path.join(ctx.goal, "targetpypystandalone.py")))
    if ctx.opts.get("smoke"):
        run_stage(ctx, state, "smoke", lambda: smoke_test(ctx.host, ctx.src_dir, ctx.cc, ctx.binutils, ctx.workdir,
                                                          ctx.opts.get("smoke_jit"), ctx.opts.get("low_memory"),
                                                          ctx.opts.get("verbose"), ctx.logs))
    installed = lambda: state.done(key, "install") and os.path.isdir(ctx.prefix)
    products = lambda: all(os.path.isfile(p) for p in translation_products(ctx)) or installed()
    run_stage(ctx, state, "translate", lambda: do_translate(ctx, state), valid=products)
    if ctx.needs_setuptools:
        run_stage(ctx, state, "setuptools", lambda: do_setuptools(ctx), valid=products)
    run_stage(ctx, state, "cffi", lambda: do_cffi(ctx, state, machine), valid=products)
    staged = lambda: bool(find_exe(ctx.stage, ctx.series)[0]) or installed()
    run_stage(ctx, state, "package", lambda: do_package(ctx, state), valid=staged)
    run_stage(ctx, state, "verify-stage", lambda: do_verify_stage(ctx, state), valid=staged)
    run_stage(ctx, state, "install", lambda: do_install(ctx, state), valid=installed)
    run_stage(ctx, state, "verify", lambda: do_verify(ctx, state),
              valid=lambda: (read_json(os.path.join(ctx.prefix, MARKER)) or {}).get("state") == "verified")
    if ctx.link:
        run_stage(ctx, state, "link", lambda: do_link(ctx),
                  valid=lambda: os.path.realpath(os.path.join(ctx.opt_root, ctx.current_name)) == os.path.realpath(ctx.prefix))
    for d in (ctx.usession, ctx.pkgdir):
        rmtree(d)                    # 4 GB of C and objects; kept after a failure, for --from compile
    state.set_build(key, status="done", finished=now(), error=None)


def status_runs(prefix, series=None):
    """The step's done-when: the interpreter runs and makes an ssl context."""
    exe, _ = find_exe(prefix, series)
    if not exe:
        return False
    try:
        return run([exe, "-c", SSL_SCRIPT], check=False, env=verify_env(), timeout=60).returncode == 0
    except ToolError:
        return False


# -------------------------------------------------------------- options, profiles

TARGETS = ("pypy2", "pypy3", "host")
DEFAULTS = {"target": "both", "pypy_version": "auto", "series": "auto", "host": "auto", "cc": "", "cxx": "",
            "binutils": "auto",
            "opt_root": "~/opt", "prefix": "", "workdir": "~/src/enterprise_pypy_tool", "link_current": None,
            "jobs": 0, "low_memory": False, "ignore_ram": False, "ignore_disk": False, "with_tk": False,
            "strip": False, "embed_deps": False, "lto": False, "extra_cflags": "", "source": "auto",
            "offline": False, "pip": True, "rebuild": False, "rebuild_host": False, "restart": False,
            "resume": False, "smoke": False, "smoke_jit": False, "from_compile": False, "install_deps": False,
            "yes": False, "verbose": False, "dry_run": False, "save_profile": ""}
# What a profile holds: the CHOICES.  Never --resume, --restart, --yes, --rebuild: one run's.
PROFILE_KEYS = ("target", "pypy_version", "series", "host", "cc", "binutils", "opt_root", "prefix", "workdir",
                "jobs", "extra_cflags", "source")
PROFILE_FLAGS = ("link_current", "low_memory", "with_tk", "strip", "embed_deps", "lto", "offline", "pip", "smoke")
PROFILE_HEADER = "# %s profile" % TOOL
PATH_KEYS = ("cc", "binutils", "opt_root", "prefix", "workdir", "source")


def parse_targets(text):
    """pypy2 | pypy3 | host | both | a comma list (the .sh's TARGETS=pypy2,pypy3)."""
    words = [w.strip() for w in (text or "both").replace("both", "pypy2,pypy3").split(",") if w.strip()]
    bad_ = [w for w in words if w not in TARGETS]
    if bad_ or not words:
        raise UsageError("--target is pypy2, pypy3, both (pypy2,pypy3) or host, not %r" % text)
    if "host" in words and len(words) > 1:
        raise UsageError("--target host builds only the translation host; give it alone")
    return [t for t in ("pypy2", "pypy3", "host") if t in words]


def check_options(opts):
    parse_targets(opts["target"])
    if opts["pypy_version"] not in ("auto", "latest") and not re.match(r"^\d+\.\d+\.\d+(rc\d+)?$", opts["pypy_version"] or ""):
        raise UsageError("--pypy-version is auto, latest or X.Y.Z (like 8.0.0), not %r" % opts["pypy_version"])
    if opts["series"] not in ("auto", "latest") and not re.match(r"^3\.\d+$", opts["series"] or ""):
        raise UsageError("--series is auto, latest or 3.N (like 3.11), not %r" % opts["series"])
    if opts["resume"] and opts["restart"]:
        raise UsageError("--resume and --restart say opposite things: give one")
    try:
        opts["jobs"] = int(opts["jobs"] or 0)
    except ValueError:
        raise UsageError("--jobs is a number")
    if opts["jobs"] < 0:
        raise UsageError("--jobs is a number >= 1 (0 = from the CPUs and the memory)")
    try:
        shlex.split(opts["extra_cflags"] or "")
    except ValueError as e:
        raise UsageError("--extra-cflags: %s" % e)
    if opts["prefix"] and parse_targets(opts["target"]) == ["pypy2", "pypy3"]:
        raise UsageError("--prefix names ONE prefix: give --target pypy2 or --target pypy3 with it")
    return opts


def profiles_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), TOOL, "profiles")


def profile_path(name, must_exist=False):
    """A bare name lives in the profiles directory; anything with a slash is a path."""
    name = os.path.expanduser(name)
    if os.sep in name or (must_exist and os.path.isfile(name)):
        return name
    base = os.path.join(profiles_dir(), name)
    if must_exist:
        return next((c for c in (base, base + ".ini") if os.path.isfile(c)), base + ".ini")
    return base if base.endswith(".ini") else base + ".ini"


def load_profile(path):
    path = profile_path(path, must_exist=True)
    cp = configparser.RawConfigParser()
    try:
        read = cp.read(path)
    except configparser.Error:
        read = []
    if not read or not cp.has_section("build"):
        raise UsageError("cannot read profile %s (no [build] section?)" % path)
    vals = {}
    for k, v in cp.items("build"):
        if k in PROFILE_FLAGS:
            vals[k] = v.strip().lower() in ("yes", "true", "1", "on")
        elif k == "jobs":
            vals[k] = int(v) if v.strip().isdigit() else 0
        elif k in PROFILE_KEYS:
            vals[k] = v
    return vals


def save_profile(name, opts):
    cp = configparser.RawConfigParser()
    cp.add_section("build")
    for k in PROFILE_KEYS:
        value = opts[k]
        if k in PATH_KEYS and value and (os.sep in str(value) or str(value).startswith("~")):
            value = os.path.abspath(os.path.expanduser(value))     # replayed from any directory
        cp.set("build", k, str(value))
    for k in PROFILE_FLAGS:
        if opts[k] is not None:                                     # link_current None: the default (yes)
            cp.set("build", k, "yes" if opts[k] else "no")
    path = profile_path(name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write("%s.  Replay with:  %s.py build --profile %s\n"
                % (PROFILE_HEADER, TOOL, os.path.basename(path)[:-4] if os.path.dirname(path) == profiles_dir() else path))
        cp.write(f)
    info("profile saved: %s" % path)
    return path


def profile_summary(v):
    return "%s | PyPy %s | series %s | host %s | cc %s" % (
        v.get("target", "both"), v.get("pypy_version", "auto"), v.get("series", "auto"), v.get("host", "auto"),
        (v.get("cc") or "auto").replace(os.path.expanduser("~"), "~"))


def list_profiles():
    found = []
    try:
        names = sorted(os.listdir(profiles_dir()))[:500]
    except OSError:
        names = []
    for n in names:
        path = os.path.join(profiles_dir(), n)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > 8192:
                continue
            with open(path, "r", errors="replace") as f:
                if not f.readline().startswith(PROFILE_HEADER):
                    continue
            vals = load_profile(path)
        except (OSError, ToolError):
            continue
        found.append({"name": n[:-4] if n.endswith(".ini") else n, "path": path,
                      "mtime": os.path.getmtime(path), "values": vals, "summary": profile_summary(vals)})
    found.sort(key=lambda pr: -pr["mtime"])
    return found


def merge_options(args):
    """Defaults, then the profile, then the command line; then checked."""
    opts = dict(DEFAULTS)
    prof = getattr(args, "profile", None)
    if prof:
        opts.update(load_profile(prof))
    for k in DEFAULTS:
        v = getattr(args, k, None)
        if v is not None:
            opts[k] = v
    if getattr(args, "from_", None):
        opts["from_compile"] = True
    check_options(opts)
    if opts.get("save_profile"):
        save_profile(opts["save_profile"], opts)
    return opts


def paths_of(opts):
    return expand(opts["workdir"]), expand(opts["opt_root"])


# ------------------------------------------------------------- resolve, plan

def resolve_ctx(opts, target, machine, cat, workdir, opt_root, probe=None, cc=None, pypy2_exe=None):
    ctx = Ctx(opts, target, workdir, opt_root, machine)
    if target == "pypy2":
        ctx.series = "2.7"
        ctx.release, w = pick_release(cat, "2.7", opts["pypy_version"])
        ctx.why = ["release %s: %s" % (ctx.release, w)]
    else:
        ctx.release, ctx.series, ctx.why = pick_pypy3(cat, opts["pypy_version"], opts["series"])
    ctx.cc = cc or resolve_cc(opts.get("cc"), opt_root)
    if not ctx.cc:
        raise ToolError("no C compiler here: `doctor --install-deps` installs one (or give --cc PATH)")
    ctx.binutils, ctx.binutils_why = resolve_binutils(opts.get("binutils"), opt_root)
    ctx.host = plan_host(opts, target, opt_root, workdir, probe, pypy2_exe=pypy2_exe)
    ctx.jobs = opts.get("jobs") or default_jobs(machine)
    ctx.src_cands, ctx.src_git = source_candidates(cat, ctx.release, ctx.series)
    return ctx


def chain_pypy3(ctxs, opts):
    """`--target both` with --host auto: pypy3's host is the pypy2 of this same run (PLAN step 7,
    "build pypy3 with that pypy2") -- shown so in the plan; once that pypy2 is verified, its real
    path is probed and used (cmd_build).  -> True when the run is chained that way."""
    if [c.target for c in ctxs] != ["pypy2", "pypy3"] or (opts.get("host") or "auto").strip() != "auto":
        return False
    exe = os.path.join(ctxs[0].prefix, "bin", "pypy")
    ctxs[1].host = {"mode": "built", "path": exe, "impl": "pypy", "version": "2.7.18", "pypy": ctxs[0].release,
                    "pycparser": "cffi._pycparser",
                    "why": "auto for pypy3: this run's pypy2 (%s), once it is built and verified -- pypy3 "
                           "translated by that pypy2; if it fails, pypy3 is not started" % ctxs[0].stem}
    return True


def estimate(ctx, state=None):
    """-> text.  Upstream: about 20 minutes for pypy2 on a fast machine with a PyPy host;
    2-3 times that under CPython; pypy3 about 1.3-1.5 times pypy2.  A recorded run beats it."""
    pypy_host = ctx.host.get("impl") == "pypy" or ctx.host.get("mode") in ("prebuilt", "built")
    lo, hi = (20, 60) if pypy_host else (60, 180)
    if ctx.is3:
        lo, hi = int(lo * 1.3), int(hi * 1.5)
    text = "translation %d-%d min with a %s host" % (lo, hi, "PyPy" if pypy_host else "CPython")
    if ctx.host.get("mode") == "cpython27":
        text = "the CPython 2.7.18 host 3-6 min (once, then reused) + " + text
    text += " + cffi and packaging 3-10 min"
    last = (state.build(ctx.key).get("translate_seconds") if state else 0) or 0
    if last:
        text += "  (the last translation here took %s)" % hms(last)
    return text


def stage_plan(ctx):
    names = ["fetch", "host", "extract"] + (["smoke"] if ctx.opts.get("smoke") else []) + ["translate"]
    if ctx.needs_setuptools:
        names.append("setuptools")
    names += ["cffi", "package", "verify-stage", "install", "verify"] + (["link"] if ctx.link else [])
    return names


def show_plan(ctx, state, both=False):
    say("%s: PyPy %s (Python %s)  ->  %s" % (ctx.target, ctx.release, ctx.series, ctx.prefix))
    for w in ctx.why:
        info("why        " + w)
    src = ctx.opts.get("source") or "auto"
    if src == "auto":
        info("source     %s" % ctx.src_cands[0][0])
        info("           else %s, else git %s %s" % (ctx.src_cands[-1][0], ctx.src_git[0], ctx.src_git[1]))
    else:
        info("source     %s" % ("git %s %s" % ctx.src_git if src == "git" else src))
    info("host       %s  %s" % (ctx.host["mode"], ctx.host.get("path") or "(downloaded at the host stage)"))
    info("           %s" % ctx.host.get("why", ""))
    info("compiler   %s  %s" % (ctx.cc["banner"], ctx.cc["path"]))
    info("           %s" % ctx.cc.get("why", ""))
    info("binutils   %s  (%s)" % (ctx.binutils or "-", ctx.binutils_why))
    info("jobs       make -j%d (one per CPU, 1.5 GB of available memory each)" % ctx.jobs)
    shown = dict(ctx.host, path=ctx.host.get("path") or "<the prebuilt pypy>")
    disp = Ctx(ctx.opts, ctx.target, ctx.workdir, ctx.opt_root, ctx.machine)
    disp.release, disp.series, disp.cc, disp.jobs, disp.host = ctx.release, ctx.series, ctx.cc, ctx.jobs, shown
    info("rpython    %s" % " ".join(shlex.quote(a) for a in rpython_argv(disp)))
    info("           cwd %s; CC=%s CFLAGS=%r LDFLAGS=''; PYPY_USESSION_DIR=%s, KEEP=1"
         % (ctx.goal, ctx.cc["path"], ctx.opts.get("extra_cflags") or "", ctx.usession))
    env = package_env(disp) if ctx.cc else {}
    info("package    package.py with %s" % " ".join("%s=1" % k for k in sorted(env) if k.startswith("PYPY_") and
                                                     env[k] == "1" and "USESSION" not in k))
    state_txt = ""
    if os.path.lexists(ctx.prefix):
        rec = read_json(os.path.join(ctx.prefix, MARKER)) or {}
        state_txt = ("  (EXISTS: this tool's, %s%s)" % (rec.get("state"), "; --rebuild replaces it" if not ctx.opts.get("rebuild") else "")
                     if rec.get("tool") == TOOL else "  (EXISTS and is not this tool's: the build will refuse)")
    info("prefix     %s%s" % (ctx.prefix, state_txt))
    info("link       %s" % (os.path.join(ctx.opt_root, ctx.current_name) + " -> it, once verified" if ctx.link
                            else "no (--no-link-current)"))
    marks = []
    for n in stage_plan(ctx):
        marks.append(n + ("(done)" if state and state.done(ctx.key, n) else ""))
    info("stages     %s" % " ".join(marks))
    info("estimate   %s" % estimate(ctx, state))


def resource_checks(ctxs, machine, opts, workdir, opt_root):
    """-> (fatal, warnings).  The .sh's rules (8 GB RAM, 20 GB disk) plus floors that refuse."""
    fatal, warns = [], []
    total, avail, swap = machine.get("mem_total_gb", 8), machine.get("mem_avail_gb", 8), machine.get("swap_free_gb", 0)
    if total < 8:
        warns.append("only %.1f GB RAM.  A 64-bit JIT translation peaks around 8 GB and will thrash or be "
                     "OOM-killed below that: add swap before starting a translation." % total)
    if ctxs:
        pypy_hosts = all(c.host.get("impl") == "pypy" or c.host.get("mode") in ("prebuilt", "built") for c in ctxs)
        need = 6 if pypy_hosts and all(not c.is3 for c in ctxs) else 8
        if avail + swap < need:
            msg = ("%.1f GB of memory available (+%.1f GB swap): a translation needs about %d GB.  %s"
                   % (avail, swap, need, "--low-memory (a PyPy host), more swap, or --ignore-ram"))
            (warns if opts.get("ignore_ram") else fatal).append(msg)
    host_new = any(c.host.get("mode") == "cpython27" for c in ctxs) or not ctxs
    need_disk = 10 * len(ctxs) + (1 if host_new else 0)        # no ctxs: only the host (--target host)
    wfree = free_gb(workdir)
    if wfree < need_disk:
        msg = "%.1f GB free at %s: about %d GB is needed (usession C + objects ~4 GB, source, package, host)" % (
            wfree, workdir, need_disk)
        (warns if opts.get("ignore_disk") else fatal).append(msg + ("" if opts.get("ignore_disk") else "; --ignore-disk"))
    elif wfree < 20 and ctxs:
        warns.append("only %.1f GB free at %s; ~20 GB is comfortable" % (wfree, workdir))
    if ctxs:
        ofree = free_gb(opt_root)
        if ofree < 1:
            (warns if opts.get("ignore_disk") else fatal).append("%.1f GB free at %s: a prefix is 0.2-0.4 GB" % (ofree, opt_root))
    return fatal, warns


def scratch_dir(workdir):
    d = os.path.join(workdir, "tmp")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def cmd_plan(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    targets = parse_targets(opts["target"])
    cc = resolve_cc(opts.get("cc"), opt_root)
    machine = detect_machine(cc)
    state = State(os.path.join(workdir, "state.json"))
    traps = env_traps()
    if traps:
        note("set in your shell, and NOT passed on to the build: " + "  ".join(traps))
    if targets == ["host"]:
        host = plan_host(opts, "pypy2", opt_root, workdir)
        say("the translation host only")
        info("host       %s  %s" % (host["mode"], host.get("path") or "(downloaded)"))
        info("           %s" % host["why"])
        if host["mode"] == "cpython27" and cc:
            info("configure  %s" % " ".join(shlex.quote(a) for a in cpy27_configure_args("<src>", os.path.dirname(
                os.path.dirname(cpy27_python(workdir))), cc)))
        info("nothing was touched (plan)")
        return 0
    if not cc:
        raise ToolError("no C compiler here: `doctor --install-deps` installs one (or give --cc PATH)")
    cat = get_catalog(opts, workdir, write_cache=False)
    ctxs = [resolve_ctx(opts, t, machine, cat, workdir, opt_root, cc=cc) for t in targets]
    chain_pypy3(ctxs, opts)
    for c in ctxs:
        show_plan(c, state, both=len(ctxs) > 1)
    fatal, warns = resource_checks(ctxs, machine, opts, workdir, opt_root)
    say("Checks")
    info("RAM %.1f GB (%.1f available, %.1f swap free); %d CPUs; %.1f GB free at %s"
         % (machine["mem_total_gb"], machine["mem_avail_gb"], machine["swap_free_gb"], machine["cpus"],
            free_gb(workdir), workdir))
    for w in warns:
        warn(w)
    for f in fatal:
        bad(f)
    info("nothing was touched (plan); `build` with the same options does it")
    return 0


def ask(prompt, default=""):
    try:
        ans = input("%s [%s]: " % (prompt, default)).strip()
    except EOFError:
        ans = ""
    return ans or default


def ask_yes(prompt, default):
    return ask(prompt + " (y/n)", "y" if default else "n").lower().startswith("y")


def ask_proceed(opts):
    if opts.get("yes") or not sys.stdin.isatty() or not sys.stdout.isatty():
        return True
    return ask_yes("\nProceed?", True)


def obtain_host(ctx, state, cat):
    """The host for a target, without a release (--target host)."""
    mode = ctx.host.get("mode")
    if mode == "cpython27":
        py = build_cpython27(ctx.workdir, ctx.cc, ctx.binutils, ctx.jobs, state, ctx.opts.get("offline"),
                             ctx.opts.get("verbose"), force=bool(ctx.opts.get("rebuild_host")))
        return probe_interpreter(py)
    if mode == "prebuilt":
        pr, _ = get_prebuilt_host(cat, ctx.workdir, ctx.opts.get("offline"))
        return pr
    return probe_interpreter(ctx.host.get("path"))


def cmd_build(args, force_targets=None):
    opts = merge_options(args)
    if force_targets:
        opts["target"] = force_targets
    if opts["dry_run"]:
        if force_targets:
            args.target = force_targets
        return cmd_plan(args)
    refuse_root("build")
    workdir, opt_root = paths_of(opts)
    targets = parse_targets(opts["target"])
    report, machine, cc = preflight(opts, targets, opt_root, workdir)
    if opts["install_deps"]:
        show_doctor(report, machine, cc)
        install_deps(report)
        report, machine, cc = preflight(opts, targets, opt_root, workdir)
    if not report["ok"]:
        show_doctor(report, machine, cc)
        raise ToolError("required pieces are missing (above): `doctor --install-deps` installs them")
    traps = env_traps()
    if traps:
        note("set in your shell, and NOT passed on to the build: " + "  ".join(traps))
    lock_dir(workdir)
    state = State(os.path.join(workdir, "state.json"))
    if targets == ["host"]:
        ctx = Ctx(opts, "pypy2", workdir, opt_root, machine)
        ctx.cc = cc
        ctx.binutils, ctx.binutils_why = resolve_binutils(opts.get("binutils"), opt_root)
        ctx.jobs = opts.get("jobs") or default_jobs(machine)
        ctx.host = plan_host(opts, "pypy2", opt_root, workdir)
        say("The translation host: %s" % ctx.host["why"])
        fatal, warns = resource_checks([], machine, opts, workdir, opt_root)
        for w in warns:
            warn(w)
        if fatal:
            raise ToolError("not starting: " + "; ".join(fatal))
        cat = get_catalog(opts, workdir) if ctx.host["mode"] == "prebuilt" else Catalog({})
        pr = obtain_host(ctx, state, cat)
        problem = host_problem(pr, ctx.host.get("path") or ctx.host["mode"])
        if problem:
            raise ToolError(problem)
        ok("translation host ready: %s (%s %s, pycparser %s)" % (pr["path"], pr["impl"], pr.get("pypy") or pr["version"],
                                                               pr["pycparser"]))
        return 0
    cat = get_catalog(opts, workdir)
    ctxs = [resolve_ctx(opts, t, machine, cat, workdir, opt_root, cc=cc) for t in targets]
    chained = chain_pypy3(ctxs, opts)
    for c in ctxs:
        show_plan(c, state, both=len(ctxs) > 1)
    fatal, warns = resource_checks(ctxs, machine, opts, workdir, opt_root)
    for w in warns:
        warn(w)
    if fatal:
        for f in fatal:
            bad(f)
        raise ToolError("not starting (above)")
    if not ask_proceed(opts):
        out("not started")
        return 1
    results = []
    for i, t in enumerate(targets):
        ctx = ctxs[i]
        try:
            if t == "pypy3" and chained:
                # PLAN step 7: pypy3 is translated by THIS run's pypy2 -- its verified prefix, by real
                # path (so --no-link-current and an older pypy2-current change nothing).  If it failed,
                # pypy3 does not start: hours on another host is not what `both` promised.
                first, err2 = results[0]
                if err2 is not None:
                    raise ToolError("pypy3 was NOT started: with --host auto it is translated by this run's "
                                    "pypy2, which failed (above).  Fix that and build again, or give pypy3 "
                                    "another host: --target pypy3 --host cpython27 | prebuilt | PATH")
                exe = find_exe(first.prefix, "2.7")[0] or os.path.join(first.prefix, "bin", "pypy")
                ctx = resolve_ctx(opts, t, machine, cat, workdir, opt_root, cc=cc, pypy2_exe=exe)
                info("pypy3's host: %s" % ctx.host["why"])
            build_one(ctx, state, cat, machine)
            results.append((ctx, None))
        except Interrupted:
            raise
        except ToolError as e:
            if len(targets) == 1:
                raise ToolError("%s\n  `build --target %s` again resumes at the stage that failed (`status` shows it)."
                                % (e, t))
            out("\n" + _c("31", "ERROR:") + " " + str(e))
            results.append((ctx, e))
    show_summary(results)
    return 0 if all(e is None for _, e in results) else 1


def show_summary(results):
    say("Summary")
    built = [c for c, e in results if e is None]
    if built:
        info("Built and verified:")
        for c in built:
            info("    %-22s %s%s" % (c.stem, c.prefix, ("   (%s)" % os.path.join(c.opt_root, c.current_name)) if c.link else ""))
    failed = [c for c, e in results if e is not None]
    if failed:
        out("  " + _c("31", "Failed or incomplete:") + " " + " ".join(c.target for c in failed))
        info("Scroll up for the specific reason; each failure printed one.")
    out("")
    out(PATH_CAUTION)


def cmd_host(args):
    return cmd_build(args, force_targets="host")


def cmd_smoke(args):
    opts = merge_options(args)
    refuse_root("run a smoke translation")
    workdir, opt_root = paths_of(opts)
    target = "pypy3" if opts["target"] == "pypy3" else "pypy2"
    report, machine, cc = preflight(opts, [target], opt_root, workdir)
    if not report["ok"]:
        show_doctor(report, machine, cc)
        raise ToolError("required pieces are missing (above): `doctor --install-deps` installs them")
    lock_dir(workdir)
    state = State(os.path.join(workdir, "state.json"))
    cat = get_catalog(opts, workdir)
    ctx = resolve_ctx(opts, target, machine, cat, workdir, opt_root, cc=cc)
    say("Smoke test with the %s source, host %s, %s" % (ctx.stem, ctx.host["mode"], ctx.cc["banner"]))
    run_stage(ctx, state, "fetch", lambda: do_fetch(ctx, state, cat), always=True)
    run_stage(ctx, state, "host", lambda: do_host(ctx, state, cat), always=True)
    if state.build(ctx.key).get("fingerprint") not in (None, fingerprint(ctx)):
        state.reset(ctx.key, AFTER_HOST)
    state.set_build(ctx.key, fingerprint=fingerprint(ctx))
    run_stage(ctx, state, "extract", lambda: do_extract(ctx),
              valid=lambda: os.path.isfile(os.path.join(ctx.goal, "targetpypystandalone.py")))
    smoke_test(ctx.host, ctx.src_dir, ctx.cc, ctx.binutils, workdir, opts.get("smoke_jit"), opts.get("low_memory"),
               opts.get("verbose"), ctx.logs)
    rmtree(os.path.join(workdir, "smoke"))
    return 0


# -------------------------------------------------------------- doctor, status

def cmd_doctor(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    targets = [t for t in parse_targets(opts["target"]) if t != "host"] or ["pypy2"]
    report, machine, cc = preflight(opts, targets, opt_root, workdir)
    if getattr(args, "json", False):
        emit_json(report)
        return 0 if report["ok"] else 1
    problems = show_doctor(report, machine, cc)
    say("This machine")
    info("%s, %s, glibc %s, %d CPUs, %.1f GB RAM (%.1f available, %.1f GB swap free), SELinux %s"
         % (machine["os"], machine["arch"], machine["glibc"] or "?", machine["cpus"], machine["mem_total_gb"],
            machine["mem_avail_gb"], machine["swap_free_gb"], machine["selinux"]))
    if machine["mem_total_gb"] < 8:
        warn("under 8 GB RAM: a JIT translation peaks around 8 GB -- add swap")
    g = glibc_version()
    if g and not ver_ge(g, GLIBC_FLOOR_V8):
        note("glibc %s: --host prebuilt takes a PyPy 7.3.x binary (8.x needs glibc 2.28)" % vstr(g))
    if machine["selinux"] == "enforcing":
        note("SELinux is enforcing: the JIT needs executable memory (an unconfined login has it; verify checks)")
    say("Network")
    for what, url, good, why in reachability():
        (ok if good else note)("%-30s %s  %s" % (what, url, why if good else "-- NOT reachable: " + why))
    if opts["install_deps"]:
        install_deps(report)
        report, machine, cc = preflight(opts, targets, opt_root, workdir)
        problems = show_doctor(report, machine, cc)
    return 1 if problems else 0


def cmd_status(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    data = status_data(opt_root, workdir)
    if getattr(args, "json", False):
        emit_json(data)
        return 0
    say("PyPy in %s" % opt_root)
    for name, target in sorted(data["current"].items()):
        link = os.path.join(opt_root, name)
        if target is None:
            info("%-14s not there" % name)
        elif not os.path.islink(link):
            warn("%s is a real directory, not a link" % link)
        else:
            entry = next((e for e in data["installed"] if os.path.realpath(e["prefix"]) == target), None)
            (ok if entry and entry["runs"] else bad)("%-14s -> %s%s" % (name, target, "" if entry and entry["runs"]
                                                                       else "  (does NOT run and make an ssl context)"))
    for e in data["installed"]:
        how = "this tool's, verified" if e["built_by_tool"] else "NOT built by this tool"
        info("  %-40s %-8s %-11s %s, %s" % (e["prefix"], e["version"] or "?", e["for"] or "?",
                                           "runs + ssl" if e["runs"] else "does NOT run/ssl", how))
        if not e["built_by_tool"] and os.path.basename(e["prefix"]) in ("pypy2", "pypy3") and not e["runs"]:
            note("     the HANDOFF's old %s (its ssl segfaults): left alone; the toolkit's are pypyN.M-vX.Y.Z"
                 % e["prefix"])
    rec = read_json(os.path.join(workdir, "state.json")) or {}
    hosts = rec.get("hosts") or {}
    say("Work directory %s" % workdir)
    c27 = hosts.get("cpython27")
    if c27:
        v, missing = cpy27_check(c27["path"])
        (ok if v and not missing else bad)("CPython 2.7 host %s (%s, built %s)" % (
            c27["path"], c27["fingerprint"].get("cc_banner", "?"), c27.get("built", "?")))
    for b in data["builds"]:
        line = "%-22s %-11s stage %s" % (b["id"], b["status"], b["stage"] or "-")
        (ok if b["status"] == "done" else bad if b["status"] in ("failed", "killed") else info)(
            line + ("  -- " + b["error"] if b["error"] else ""))
    if not data["builds"] and not c27:
        info("nothing built here yet")
    return 0


def cmd_verify(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    if getattr(args, "prefix_arg", None):
        prefixes = [expand(args.prefix_arg)]
    else:
        prefixes = [os.path.realpath(os.path.join(opt_root, "pypy%s-current" % t[-1]))
                    for t in parse_targets(opts["target"]) if t != "host"]
    bad_ = 0
    for prefix in prefixes:
        if not os.path.isdir(prefix):
            raise ToolError("no prefix %s" % prefix)
        rec = read_json(os.path.join(prefix, MARKER)) or {}
        mine = rec.get("tool") == TOOL
        say("Verifying %s%s" % (prefix, "" if mine else "  (not built by this tool)"))
        good, summary = verify_interpreter(prefix, rec.get("series") if mine else None,
                                           rec.get("release") if mine else None, offline=opts.get("offline"),
                                           with_tk=opts.get("with_tk"), expect_pip=opts.get("pip", True),
                                           cc_version=rec.get("cc_version", "") if mine else "")
        if mine and os.access(prefix, os.W_OK):
            rec["verify"] = summary
            rec["state"] = "verified" if good else "failed verification"
            write_atomic(os.path.join(prefix, MARKER), json.dumps(rec, indent=1, sort_keys=True) + "\n")
        if good:
            ok("%s is good" % prefix)
        else:
            bad_ += 1
            bad("%d problem(s): %s" % (len(summary["failures"]), "; ".join(f.splitlines()[0][:80] for f in summary["failures"])))
    return 1 if bad_ else 0


# -------------------------------------------------- versions, detect, toolchains

def cmd_versions(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    cat = load_catalog(opts, workdir)
    tag, g = arch_tag(), glibc_version()
    say("PyPy releases  (%s)" % cat.origin)
    count = getattr(args, "count", None) or 10
    rels = cat.all_releases(include_rc=True)[:count]
    for i, rel in enumerate(rels):
        srcs = []
        for s in cat.src_series(rel):
            mark = ""
            if s.startswith("3") and i < 3:
                mark = "(beta)" if cat.beta(rel, s)[0] else ""
            srcs.append(s + mark)
        bins = [s for s in sorted(cat.releases[rel].get("bin", {}), key=series_key) if cat.bin_name(rel, s, tag)]
        info("%-12s %-10s %-8s source: %-24s %s: %s" % (rel, cat.date(rel) or "", "rc/unstable" if cat.unstable(rel) else "stable",
                                                         " ".join(srcs) or "-", tag or "binaries", " ".join(bins) or "-"))
    say("What auto picks")
    try:
        rel, why = pick_release(cat, "2.7", "auto")
        info("pypy2  %s  -- %s" % (rel, why))
    except ToolError as e:
        info("pypy2  -- %s" % e)
    try:
        rel, s, why = pick_pypy3(cat, "auto", "auto")
        info("pypy3  pypy%s %s" % (s, rel))
        for w in why:
            info("       " + w)
    except ToolError as e:
        info("pypy3  -- %s" % e)
    try:
        rel, name = prebuilt_host_choice(cat, tag, g)
        info("--host prebuilt  %s  (glibc %s)" % (name, vstr(g) if g else "?"))
    except ToolError as e:
        info("--host prebuilt  -- %s" % e)
    return 0


def cmd_detect(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    try:
        cc = resolve_cc(opts.get("cc"), opt_root)
    except UsageError:
        cc = None
    try:
        binutils, bwhy = resolve_binutils(opts.get("binutils"), opt_root)
    except UsageError:
        binutils, bwhy = "", ""
    m = detect_machine(cc, binutils)
    g = glibc_version()
    m.update({"workdir": workdir, "workdir_free_gb": round(free_gb(workdir), 1), "opt_root": opt_root,
              "opt_root_free_gb": round(free_gb(opt_root), 1), "cc": (cc or {}).get("path", ""),
              "cc_banner": (cc or {}).get("banner", ""), "binutils": binutils,
              "prebuilt_v8_runs_here": bool(g) and ver_ge(g, GLIBC_FLOOR_V8), "env_traps": env_traps()})
    if getattr(args, "json", False):
        emit_json(m)
        return 0
    say("This machine")
    for k in ("os", "arch", "arch_tag", "glibc", "cpus", "mem_total_gb", "mem_avail_gb", "swap_free_gb", "pkg_manager",
              "selinux", "workdir", "workdir_free_gb", "opt_root", "opt_root_free_gb", "cc", "cc_banner", "binutils",
              "prebuilt_v8_runs_here"):
        info("%-22s %s" % (k, m[k]))
    info("%-22s %s" % ("headers", " ".join("%s:%s" % (k, "yes" if v else "NO") for k, v in sorted(m["headers"].items()))))
    if m["env_traps"]:
        info("%-22s %s" % ("shell variables (not passed on)", " ".join(m["env_traps"])))
    return 0


def cmd_toolchains(args):
    opts = merge_options(args)
    workdir, opt_root = paths_of(opts)
    dirs = tool_search_dirs()
    for extra in (os.path.join(opt_root, "gcc-current", "bin"), os.path.join(opt_root, "binutils-current", "bin")):
        if os.path.isdir(extra) and extra not in dirs:
            dirs.insert(0, extra)
    try:
        chosen = resolve_cc(opts.get("cc"), opt_root)
    except UsageError as e:
        chosen = None
        warn(str(e))
    say("C compilers")
    for c in find_compilers(dirs):
        mark = "  <- would be used" if chosen and os.path.realpath(c["path"]) == chosen["path"] else ""
        info("%-6s %-10s %s%s" % (c["kind"], vstr(c["version"]), c["path"], mark))
    say("binutils (as + ld)")
    bdir, bwhy = resolve_binutils(opts.get("binutils"), opt_root)
    for b in find_binutils(dirs):
        mark = "  <- would be used" if bdir and os.path.realpath(b["dir"]) == os.path.join(bdir, "bin") else ""
        info("%-8s %s%s" % (vstr(b["version"]), b["dir"], mark))
    info("would be used: %s (%s)" % (bdir or "the compiler's own", bwhy))
    say("Python 2 interpreters (translation hosts)")
    cands = find_system_hosts(opt_root, dirs)
    for p in (cpy27_python(workdir), os.path.join(opt_root, "pypy2-current", "bin", "pypy")):
        if os.path.exists(p) and p not in cands:
            cands.append(p)
    for p in cands:
        pr = probe_interpreter(p)
        problem = host_problem(pr, p)
        info("%-50s %s" % (p, ("ok: %s %s, pycparser %s" % (pr["impl"], pr.get("pypy") or pr["version"], pr["pycparser"]))
                           if not problem else problem.split(".  ")[0][len(p) + 1:][:70]))
    if not cands:
        info("none (Enterprise Linux 10, Debian 12 and Ubuntu 24.04 ship no Python 2): cpython27 builds one")
    say("PyPy3")
    for p in scan_names(dirs + [os.path.join(opt_root, "pypy3-current", "bin")], PYPY3_NAME):
        info(p)
    say("Translation hosts that would be used")
    for t in ("pypy2", "pypy3"):
        try:
            h = plan_host(opts, t, opt_root, workdir)
            info("%s: %s %s -- %s" % (t, h["mode"], h.get("path") or "", h["why"]))
        except ToolError as e:
            info("%s: %s" % (t, str(e).splitlines()[0]))
    return 0


# ------------------------------------------------------------------- clean

CLEAN_DIRS = ("usession", "package", "host", "smoke", "tmp")


def cmd_clean(args):
    """Only this tool's own directories, only in a work directory whose record is this tool's:
    the source trees, usession, package, host, smoke, tmp (--all: downloads, logs, the cache and
    the record too).  Never a prefix, never a -current link."""
    opts = merge_options(args)
    refuse_root("clean")
    workdir, opt_root = paths_of(opts)
    if not os.path.isdir(workdir):
        info("no work directory %s" % workdir)
        return 0
    rec = read_json(os.path.join(workdir, "state.json"))
    if rec is None or rec.get("tool") != TOOL:
        raise ToolError("%s holds no record of %s: not touching it" % (workdir, TOOL))
    lock_dir(workdir)
    before = dir_size(workdir)
    gone = []
    for p in glob.glob(os.path.join(glob.escape(workdir), "src", "*-src")) + \
            glob.glob(os.path.join(glob.escape(workdir), "src", ".extract-*")) + \
            glob.glob(os.path.join(glob.escape(workdir), ".extract-*")) + \
            [os.path.join(workdir, d) for d in CLEAN_DIRS]:
        if os.path.lexists(p):
            rmtree(p)
            gone.append(p)
    every = getattr(args, "all", False)
    if every:
        for d in ("downloads", "logs", "cache", "src"):
            p = os.path.join(workdir, d)
            if os.path.lexists(p):
                rmtree(p)
                gone.append(p)
        os.unlink(os.path.join(workdir, "state.json"))
        gone.append(os.path.join(workdir, "state.json"))
    else:
        state = State(os.path.join(workdir, "state.json"))
        for key in list(state.d["builds"]):
            b = state.d["builds"][key]
            b["stages"] = dict((k, v) for k, v in (b.get("stages") or {}).items() if k == "fetch")
            if b.get("status") != "done":
                b["status"], b["stage"] = "cleaned", "fetch"
        state.d["hosts"] = {}
        state.save()
    for p in gone:
        info("removed %s" % p)
    ok("freed %s in %s%s" % (human(max(0, before - dir_size(workdir))), workdir,
                             "" if every else "  (kept: downloads, logs, the record; --all removes them too)"))
    return 0


# ------------------------------------------------------------ profiles, wizard

def cmd_profiles(args):
    profs = list_profiles()
    say("Saved profiles  (%s)" % profiles_dir())
    if not profs:
        info("none yet: the wizard offers to save one, and so does --save-profile NAME")
    for pr in profs:
        out("  %-24s %s   %s" % (pr["name"], time.strftime("%Y-%m-%d %H:%M", time.localtime(pr["mtime"])), pr["summary"]))
    return 0


def ask_choice(title, options, default):
    """options: [(key, label)].  Accepts the number or the key."""
    if default not in [k for k, _ in options]:
        options = list(options) + [(default, "as given on the command line / in the profile")]
    out("\n" + _c("1", title))
    for i, (key, label) in enumerate(options, 1):
        out("  %2d) %-10s %s%s" % (i, key, label, "   <- default" if key == default else ""))
    while True:
        ans = ask("choose", default)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        if ans in [k for k, _ in options]:
            return ans
        out("  a number from the list, or one of the keys")


def choose_profile(profs):
    if not profs:
        return None
    say("Saved profiles")
    for i, pr in enumerate(profs, 1):
        out("  %2d) %-24s %s" % (i, pr["name"], pr["summary"]))
    out("   n) new -- answer the questions")
    while True:
        ans = ask("profile", "1")
        if ans.lower() in ("n", "new"):
            return None
        pick = profs[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(profs) else \
            next((pr for pr in profs if pr["name"] == ans), None)
        if pick:
            break
        out("  a number from the list, a profile's name, or n")
    how = ask("use it as it is, or edit it? (u/e)", "u")
    return {"path": pick["path"], "name": pick["name"], "as_is": not how.lower().startswith("e")}


def wizard(opts, opt_root):
    """Every question is also a flag, and the answers can be saved as a profile."""
    opts["target"] = ask_choice("What to build", [
        ("pypy2", "PyPy2.7 (step 6)"), ("pypy3", "PyPy3 (step 7), translated by pypy2-current when it runs"),
        ("both", "pypy2, then pypy3 with it (the .sh's TARGETS=pypy2,pypy3)"),
        ("host", "only the translation host (CPython 2.7.18)")], opts["target"])
    if opts["target"] != "host":
        opts["pypy_version"] = ask("PyPy release: auto (newest with the source), latest (release candidates too) "
                                   "or X.Y.Z", opts["pypy_version"])
    if opts["target"] in ("pypy3", "both"):
        opts["series"] = ask_choice("Which pypy3", [
            ("auto", "the newest series upstream does NOT call beta"), ("latest", "the newest, even if beta"),
            ("3.11", "pypy3.11 (8.0.1 is its last release)"), ("3.12", "pypy3.12 (beta in 8.0.0 and 8.0.1)")],
            opts["series"])
    opts["host"] = ask_choice("The Python 2 that runs RPython (the translation host)", [
        ("auto", "cpython27 for pypy2; for pypy3 this run's pypy2 (both), else <opt>/pypy2-current when it runs"),
        ("cpython27", "CPython 2.7.18 built here with the toolkit's gcc: 3-6 min, then translation 1-3 h"),
        ("prebuilt", "the official portable PyPy2: translation 20-60 min (the .sh's default)"),
        ("system", "a pypy / pypy2 / python2.7 already here"), ("built", "<opt>/pypy2-current")], opts["host"])
    out("\n" + _c("1", "The C compiler (RPython's C, and the CPython 2.7 host)"))
    comps = find_compilers()
    cur = os.path.join(opt_root, "gcc-current", "bin", "gcc")
    for i, c in enumerate(comps, 1):
        out("  %2d) %-5s %-10s %s" % (i, c["kind"], vstr(c["version"]), c["path"]))
    out("   a) auto -- %s (step 4) when it runs, else the system's gcc" % cur)
    while True:
        ans = ask("compiler: a number, a, or a path", opts["cc"] or "a")
        if ans.lower() in ("a", "auto"):
            opts["cc"] = ""
            break
        if ans.isdigit() and 1 <= int(ans) <= len(comps):
            opts["cc"] = comps[int(ans) - 1]["path"]
            break
        if os.sep in ans and compiler_info(expand(ans)):
            opts["cc"] = expand(ans)
            break
        out("  a number from the list, a, or the path of a gcc")
    opts["opt_root"] = ask("where prefixes and the -current links live", opts["opt_root"])
    if opts["target"] != "host":
        opts["link_current"] = ask_yes("point pypyN-current at the result once verified?", opts["link_current"] is not False)
        opts["smoke"] = ask_yes("translate a tiny target first (under a minute) to prove the host and the compiler?",
                                opts["smoke"])
    name = ask("save these answers as a profile? (a name, or empty)", opts.pop("_profile_name", ""))
    if name:
        save_profile(name, check_options(opts))
    return opts


def cmd_wizard(args):
    if getattr(args, "yes", None):
        note("wizard --yes: --yes asks nothing, so no questions -- the flags, the profile and the defaults, "
             "exactly as `build` takes them")
        return cmd_build(args)
    if not sys.stdin.isatty():
        out("Not a terminal: nothing to ask.  Use `build` with flags or --profile; see --help.")
        return 2
    refuse_root("build")
    reuse = choose_profile(list_profiles())
    if reuse:
        args.profile = reuse["path"]
    opts = merge_options(args)
    if reuse:
        opts["_profile_name"] = reuse["name"]
    if not (reuse and reuse["as_is"]):
        opts = wizard(opts, expand(opts["opt_root"]))
    opts.pop("_profile_name", None)
    check_options(opts)
    for k in DEFAULTS:
        setattr(args, k, opts[k])
    args.profile = None
    args.save_profile = None
    return cmd_build(args)


# -------------------------------------------------------------------- the parser

def add_common(p):
    g = p.add_argument_group("where")
    g.add_argument("--opt-root", dest="opt_root", help="where prefixes and the -current links live (default ~/opt)")
    g.add_argument("--workdir", help="downloads, sources, the host, records, logs (default ~/src/enterprise_pypy_tool)")
    g.add_argument("--profile", help="load choices from a saved profile (a name, or a path)")
    g.add_argument("--yes", "-y", action="store_true", default=None,
                   help="ask nothing (Proceed is yes; an earlier run is resumed, never deleted)")


def add_build_options(p):
    add_common(p)
    g = p.add_argument_group("what")
    g.add_argument("--target", help="pypy2 | pypy3 | both (default: pypy2 then pypy3) | host (only the 2.7 host)")
    g.add_argument("--pypy-version", "--version", dest="pypy_version",
                   help="auto (default: the newest release that ships the source) | latest (release candidates "
                        "too) | X.Y.Z")
    g.add_argument("--series", help="pypy3 only: auto (default: the newest pypy3.N upstream does not call beta) | "
                                    "latest (the newest, even beta) | 3.N")
    g.add_argument("--source", help="auto (default: downloads.python.org, else GitHub) | git | a tarball or tree")
    g.add_argument("--prefix", help="install here (default <opt-root>/pypy<2.7|3.N>-v<release>; never installed over)")
    g = p.add_argument_group("with what")
    g.add_argument("--host", help="auto (default) | cpython27 | prebuilt | system | built | PATH -- the Python 2 "
                                  "that runs RPython; never a CPython 3")
    g.add_argument("--cc", help="the C compiler (default: the real path of <opt>/gcc-current/bin/gcc, else /usr/bin/gcc); "
                                "CC from the environment is never used")
    g.add_argument("--cxx", help="accepted for the toolkit's common --cc/--cxx contract; PyPy has no C++ to "
                                 "compile, so it is only recorded")
    g.add_argument("--binutils", help="a prefix whose bin/ holds as and ld (default: the real path of "
                                      "<opt>/binutils-current) | system")
    g.add_argument("--jobs", type=int, help="make -j for RPython's C and the host (default: CPUs, 1.5 GB each)")
    g.add_argument("--extra-cflags", dest="extra_cflags", help="CFLAGS for RPython's C (it appends them), e.g. -std=gnu17")
    g.add_argument("--lto", action="store_true", default=None, help="rpython --lto")
    g.add_argument("--low-memory", dest="low_memory", action="store_true", default=None,
                   help="a PyPy host runs with PYPY_GC_MAX_DELTA=200MB --jit loop_longevity=300")
    g.add_argument("--with-tk", dest="with_tk", action="store_true", default=None, help="build and package _tkinter")
    g.add_argument("--strip", action="store_true", default=None, help="package without debug information")
    g.add_argument("--embed-deps", dest="embed_deps", action="store_true", default=None,
                   help="let package.py download and statically link OpenSSL, gdbm and xz (default: the system's)")
    g = p.add_argument_group("this run")
    g.add_argument("--link-current", dest="link_current", action="store_true", default=None,
                   help="point <opt>/pypyN-current at the result once verified (default)")
    g.add_argument("--no-link-current", dest="link_current", action="store_false", default=None,
                   help="build, install and verify, but leave the -current link alone")
    g.add_argument("--no-pip", dest="pip", action="store_false", default=None, help="no ensurepip in the prefix")
    g.add_argument("--offline", action="store_true", default=None,
                   help="no network: cached release list and downloads only; verify skips HTTPS")
    g.add_argument("--smoke", action="store_true", default=None,
                   help="translate a tiny RPython target first (under a minute) to prove the host and the compiler")
    g.add_argument("--smoke-jit", dest="smoke_jit", action="store_true", default=None,
                   help="the smoke test translates a toy interpreter WITH a JIT (rpython/jit/tl/targettlr.py, "
                        "--opt=jit) and runs it hot: proves the JIT backend")
    g.add_argument("--from", dest="from_", choices=["compile"],
                   help="compile: the last translation failed compiling its C -- run make there again")
    g.add_argument("--rebuild", action="store_true", default=None,
                   help="replace an existing prefix of this tool's (set aside, put back on failure)")
    g.add_argument("--rebuild-host", dest="rebuild_host", action="store_true", default=None,
                   help="build the CPython 2.7.18 host again")
    g.add_argument("--resume", action="store_true", default=None, help="continue an earlier run (the default)")
    g.add_argument("--restart", action="store_true", default=None,
                   help="redo everything after the download and the host")
    g.add_argument("--ignore-ram", dest="ignore_ram", action="store_true", default=None,
                   help="start even where the memory is under the floor")
    g.add_argument("--ignore-disk", dest="ignore_disk", action="store_true", default=None,
                   help="start even where less disk is free than needed")
    g.add_argument("--install-deps", dest="install_deps", action="store_true", default=None,
                   help="sudo <package manager> install what doctor says is missing")
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="the plan; touch nothing")
    g.add_argument("--save-profile", dest="save_profile", help="save this run's choices under a name")
    g.add_argument("--verbose", "-v", action="store_true", default=None, help="every line of every log on the console")


def build_parser():
    p = argparse.ArgumentParser(prog=TOOL + ".py", description="Build PyPy2 and PyPy3 from source (steps 6 and 7 "
                                "of the satellite toolkit), and the Python 2 that translates them.")
    p.add_argument("--tool-version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")
    for name, helptext in (
            ("build", "fetch, host, translate, cffi, package, install, verify, link -current (resumable)"),
            ("plan", "resolve everything and print it; touch nothing (= build --dry-run)"),
            ("host", "only the translation host (= build --target host)"),
            ("smoke", "a tiny RPython translation (25 s; --smoke-jit ~7 min): proves the host and the C compiler"),
            ("doctor", "what is missing, and the one command that installs it ([--json])"),
            ("versions", "what upstream has: releases, series, beta, binaries for this machine"),
            ("detect", "what this machine has ([--json])"),
            ("toolchains", "compilers, binutils, Python 2 hosts, pypy3s here; which would be used"),
            ("verify", "check an installed PyPy: modules, ssl context, JIT, pip, libraries"),
            ("wizard", "ask the questions (what you get with no command on a terminal)")):
        sp = sub.add_parser(name, help=helptext)
        add_build_options(sp)
        if name in ("doctor", "detect"):
            sp.add_argument("--json", action="store_true", help="as JSON on stdout (messages go to stderr)")
        if name == "verify":
            sp.add_argument("prefix_arg", metavar="PREFIX", nargs="?",
                            help="a PyPy prefix (default: <opt>/pypy2-current and/or pypy3-current per --target)")
        if name == "versions":
            sp.add_argument("--count", type=int, default=10, help="how many releases (default 10)")
    sp = sub.add_parser("status", help="what is installed where, the -current links, the builds ([--json])")
    add_common(sp)
    sp.add_argument("--json", action="store_true", help="the toolkit's status contract, as JSON on stdout")
    sp = sub.add_parser("clean", help="remove this tool's work trees (keeps downloads, logs, the record; --all too)")
    add_common(sp)
    sp.add_argument("--all", action="store_true", help="downloads, logs, the cache and the record too")
    sp = sub.add_parser("profiles", help="saved profiles")
    add_common(sp)
    return p


COMMANDS = {"build": cmd_build, "plan": cmd_plan, "host": cmd_host, "smoke": cmd_smoke, "doctor": cmd_doctor,
            "status": cmd_status, "verify": cmd_verify, "versions": cmd_versions, "detect": cmd_detect,
            "toolchains": cmd_toolchains, "clean": cmd_clean, "profiles": cmd_profiles, "wizard": cmd_wizard}


def _as_interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    argv = where_to_run(list(sys.argv[1:] if argv is None else argv), __file__, TOOL)
    parser = build_parser()
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help", "--tool-version") for a in argv):
        if any(a in ("--yes", "-y") for a in argv):       # the Child contract: --yes asks nothing
            out("--yes asks nothing, so there is no wizard: give a command -- `build` with flags or --profile "
                "NAME, `plan`, `doctor`, `status` ... (--help lists them)")
            return 2
        if sys.stdin.isatty() and sys.stdout.isatty():
            argv = ["wizard"] + argv                  # no command, a terminal: the wizard
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if not args.command:
        parser.print_help()
        out("\nStart with:  %s doctor" % (os.path.basename(sys.argv[0]) or TOOL + ".py"))
        return 2
    if getattr(args, "json", False):
        json_mode()
    # SIGTERM / SIGHUP take the ^C path so a finally runs and the record is right; a SIGHUP
    # somebody chose to ignore (`nohup ... &`) stays ignored.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, _as_interrupt)
    try:
        return COMMANDS[args.command](args)
    except UsageError as e:
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 2
    except Interrupted as e:
        out("\n" + _c("33", "INTERRUPTED:") + " " + str(e))
        return 130
    except ToolError as e:
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 1
    except OSError as e:                                     # a full disk, a permission: said plainly
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 1
    except KeyboardInterrupt:
        out("\ninterrupted -- `build` again shows where things stand and continues from there.")
        return 130
    finally:
        unlock_all()


def _ensure_platform_python():
    """Started as `python3 tool.py` where python3 is PyPy (HANDOFF trap 1)?  The tool is happy
    anywhere; this keeps behaviour identical to the platform's CPython."""
    impl = getattr(sys.implementation, "name", "cpython")
    if impl != "cpython" and os.access(PLATFORM_PYTHON, os.X_OK) and not os.environ.get(REEXEC_GUARD):
        os.environ[REEXEC_GUARD] = "1"
        os.execv(PLATFORM_PYTHON, [PLATFORM_PYTHON, "-I", os.path.abspath(__file__)] + sys.argv[1:])


if __name__ == "__main__":
    _ensure_platform_python()
    sys.exit(main())
