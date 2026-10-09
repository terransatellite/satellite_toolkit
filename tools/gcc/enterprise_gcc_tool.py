#!/usr/bin/python3 -I
# -*- coding: utf-8 -*-
"""enterprise_gcc_tool.py -- build GNU binutils and GCC from source, as ONE pair.

Steps 3 and 4 of the satellite toolkit:

    --component binutils   binutils-with-gold-<ver>, --enable-plugins --enable-lto --enable-gold,
                           built -g -O3 -march=native, installed to <opt>/binutils-<ver>,
                           <opt>/binutils-current pointed at it once it is verified
    --component gcc        (the default) GCC, configured --with-as/--with-ld = that binutils, so
                           "that gcc/binutils" is one pair from then on; <opt>/gcc-current

Successor to build_gcc_universal.sh (every knob of it is a flag here, see --help and the
README) and to the binutils line of binutils-gcc-notes-reboot.txt.  What it keeps from the
.sh: TRACK release|snapshot|git, the mirrors with fallback, listings fetched compressed, the xz
integrity test, contrib/download_prerequisites, the out-of-tree build, MODE profiled-lto|lto|
plain|quick, LANGUAGES never exported, the host compiler picked (an exported CC=clang is ignored,
with a warning), the development-compiler banner, "failed in stage 1: changing MODE will not
help", install-strip else install, the C++20 compile+link+run check with an rpath, the default
-march check, gcc-current and the PATH / LD_LIBRARY_PATH / rpath advice.

What it adds (the toolkit's contracts): a scrubbed environment, a lock and a resumable record
per build directory, logs per step with the FIRST real errors, disk and RAM checks, an install
through a staging directory and one rename (a prefix is whole or absent, never installed
over), `verify PREFIX`, `doctor [--json]`, `status [--json]`, `versions`, `detect`,
`toolchains`, profiles and a wizard, `clean`, --opt-root, and local-or-remote.

One file, standard library only, syntax held to Python 3.6.  Never run it as root: only
--install-deps calls sudo.

Licence: public domain / CC0.  No warranty.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("enterprise_gcc_tool.py needs Python 3.6 or newer")

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
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib

TOOL = "enterprise_gcc_tool"
VERSION = "0.1.0"
PLATFORM_PYTHON = "/usr/bin/python3"
USER_AGENT = TOOL + "/" + VERSION
MARKER = ".enterprise_gcc_tool.json"          # in every prefix this tool installed
COMPONENTS = ("gcc", "binutils")

# build_gcc_universal.sh:202-207.  The listings are fetched in this order; a tarball is tried
# from each in turn ("that mirror failed, trying the next one").
GCC_MIRRORS = ("https://ftp.gnu.org/gnu/gcc", "https://mirrors.kernel.org/gnu/gcc",
               "https://ftpmirror.gnu.org/gcc")
SNAP_MIRRORS = ("https://gcc.gnu.org/pub/gcc/snapshots",
                "https://mirrors.kernel.org/sourceware/gcc/snapshots")
BINUTILS_MIRRORS = ("https://ftp.gnu.org/gnu/binutils", "https://sourceware.org/pub/binutils/releases",
                    "https://mirrors.kernel.org/gnu/binutils", "https://ftpmirror.gnu.org/binutils")
# Where contrib/download_prerequisites fetches GMP/MPFR/MPC/ISL/gettext.  Its own base_url is
# hard-coded (plain http:// on the 14 and 15 branches); the archives are fetched here first,
# from each of these in turn, and the script then finds them present.
INFRA_MIRRORS = ("https://gcc.gnu.org/pub/gcc/infrastructure",
                 "https://sourceware.org/pub/gcc/infrastructure",
                 "https://mirrors.kernel.org/sourceware/gcc/infrastructure")
GCC_GIT = "https://gcc.gnu.org/git/gcc.git"
TRUNK_BASEVER_URL = "https://gcc.gnu.org/git/?p=gcc.git;a=blob_plain;f=gcc/BASE-VER;hb=refs/heads/master"
GOLD_SPLIT = (2, 44)          # from binutils 2.44 on, gold ships only in binutils-with-gold-<ver>

# MODE -- how hard to work for a faster compiler BINARY (build_gcc_universal.sh:119-132, 442-448).
# disk: what the .sh warned below (337-338), in GB; +5 for a git clone.  time: its estimates.
MODES = {
    "profiled-lto": {"target": "profiledbootstrap", "config": "bootstrap-lto", "disk": 30, "time": "3-5 h",
                     "what": "PGO + LTO bootstrap: the fastest gcc there is, the slowest to build"},
    "lto": {"target": "bootstrap", "config": "bootstrap-lto", "disk": 30, "time": "2-4 h",
            "what": "LTO bootstrap, no profiling: most of the win, much less time"},
    "plain": {"target": "bootstrap", "config": "", "disk": 15, "time": "1.5-3 h",
              "what": "an ordinary 3-stage bootstrap: always works"},
    "quick": {"target": "", "config": "", "disk": 10, "time": "30-60 min",
              "what": "stage 1 only, NO bootstrap: built by the host compiler, not self-verified"},
}
MODE_ORDER = ("profiled-lto", "lto", "plain", "quick")
GIT_EXTRA_DISK = 5
BINUTILS_DISK = 3             # an estimate (not measured): source + objects + staging
TRACKS = ("release", "snapshot", "git")

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
    s = UI.stream or sys.stdout
    try:
        s.write(text + "\n")
    except UnicodeEncodeError:
        s.write(text.encode("ascii", "replace").decode("ascii") + "\n")
    s.flush()


def emit_json(data, default=None):
    """The one thing --json puts on stdout."""
    sys.stdout.write(json.dumps(data, indent=1, sort_keys=True, default=default) + "\n")
    sys.stdout.flush()


def json_mode():
    """--json (the toolkit contract): stdout is the JSON and nothing else; every message,
    an ERROR included, goes to stderr."""
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
    # stdout, not stderr: redirected to one log the two streams interleave out of order.
    out(_c("33", "warning:") + " " + msg)


class ToolError(Exception):
    """A failure with a message fit to show the user.  No traceback."""


# ----------------------------------------------------------------------- process

# An ALLOW-list.  build_gcc_universal.sh only un-exported LANGUAGES and overrode CC/CXX, so
# LD_RUN_PATH, LD_LIBRARY_PATH, CFLAGS, MAKEFLAGS and /usr/local/bin all reached the build
# (HANDOFF.md traps 2, 5 and 6).  The proxy and CA variables are what an enterprise network
# needs for git, and WGETRC / CURL_CA_BUNDLE for download_prerequisites' wget or curl.
ENV_KEEP = ("HOME", "USER", "LOGNAME", "TERM", "TMPDIR", "LANG",
            "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "all_proxy", "ALL_PROXY", "ftp_proxy", "FTP_PROXY",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "CURL_CA_BUNDLE", "WGETRC")
# /usr/local/bin is left out on purpose: on the machine this was written for it holds a
# root-installed LLVM and Python 3.14, and configure goes looking for both (HANDOFF trap 5).
SYSTEM_PATH = ["/usr/bin", "/usr/sbin", "/bin", "/sbin"]
# What a shell commonly exports that a GCC or binutils build would silently obey.  LANGUAGES
# is GCC's own trap 6: gcc/Makefile reads it as the LIST OF MAKE TARGETS of `native`, and an
# exported LANGUAGES=c,c++,lto kills stage 1 in 90 seconds ("No rule to make target
# 'c,c++,lto'").  None of these is ever passed on; env_traps() names the ones that are set.
TRAP_VARS = ("CC", "CXX", "CPP", "CXXCPP", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LIBS",
             "LANGUAGES", "AS", "LD", "AR", "NM", "RANLIB", "OBJCOPY", "OBJDUMP", "READELF", "STRIP",
             "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
             "LD_RUN_PATH", "LD_PRELOAD", "GCC_EXEC_PREFIX", "COMPILER_PATH", "MAKEFLAGS", "MFLAGS",
             "GNUMAKEFLAGS", "CONFIG_SITE", "DESTDIR", "BOOT_CFLAGS", "PKG_CONFIG_PATH", "LLVM")
TRAP_PATTERN = re.compile(r"^((CC|CXX|AS|LD|AR|NM|RANLIB|STRIP|OBJDUMP|OBJCOPY)_FOR_(BUILD|TARGET)|"
                          r"\w+FLAGS_FOR_(BUILD|TARGET))$")


def scrubbed_env(extra_path=None, extra=None):
    """The environment every build step gets: the allow-list, PATH = [extra] + the system's
    directories (/usr/bin first), LC_ALL=C.  Nothing else -- in particular no LANGUAGES."""
    env = dict((k, os.environ[k]) for k in ENV_KEEP if k in os.environ)
    env["PATH"] = ":".join(list(extra_path or []) + SYSTEM_PATH)
    env["LC_ALL"] = "C"
    env.update(extra or {})
    return env


def env_traps(environ=None):
    environ = os.environ if environ is None else environ
    found = []
    for var in sorted(environ):
        if var in TRAP_VARS or TRAP_PATTERN.match(var):
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
    """Of the nearest EXISTING parent: on a fresh machine the build directory is not there yet."""
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


def tail(text, n):
    return "\n".join("    " + l for l in (text or "").rstrip().splitlines()[-n:])


def safe_name(text):
    """A ref or an id as a directory name."""
    return re.sub(r"[^\w.+-]", "_", text or "") or "_"


# ---------------------------------------------------------------------- versions

def numver(text):
    m = re.search(r"(\d+(?:\.\d+)+|\d+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def ver_ge(have, need):
    n = max(len(have), len(need))
    return tuple(have) + (0,) * (n - len(have)) >= tuple(need) + (0,) * (n - len(need))


def vstr(v):
    return ".".join(str(x) for x in v) if v else "?"


def runs(path):
    """build_everything.sh's test (136): a prefix counts as installed only if its main binary
    RUNS -- a directory proves nothing, a build killed during `make install` leaves one."""
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        return run([path, "--version"], check=False, env=scrubbed_env(), timeout=30).returncode == 0
    except ToolError:
        return False


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
    text = read_text("/proc/meminfo") if text is None else text
    vals = dict((m.group(1), int(m.group(2))) for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M))
    total = vals.get("MemTotal", 8 << 20) / float(1 << 20)
    avail = vals.get("MemAvailable", vals.get("MemFree", 4 << 20)) / float(1 << 20)
    return total, avail


X86_LEVELS = (
    ("x86-64-v2", ("cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3")),
    ("x86-64-v3", ("avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave")),
    ("x86-64-v4", ("avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl")),
)


def cpu_info(text=None):
    text = read_text("/proc/cpuinfo") if text is None else text
    m = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
    f = re.search(r"^flags\s*:\s*(.+)$", text, re.M)
    return {"model": m.group(1).strip() if m else "", "flags": f.group(1).split() if f else []}


def cpu_level(flags):
    """-> the highest x86-64 level these CPU flags satisfy ('' below v2)."""
    have, level = set(flags), ""
    for name, need in X86_LEVELS:
        if not all(f in have for f in need):
            break
        level = name
    return level


def cpu_count():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


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


def cxx_partner(cc, kind):
    """gcc-14 -> g++-14, x86_64-linux-gnu-gcc-13 -> ...-g++-13, clang-18 -> clang++-18: in the
    SAME directory.  (The .sh's ${HOST_CC%gcc}g++ paired /usr/bin/gcc-14 with whatever g++
    was on PATH.)"""
    d, base = os.path.split(cc)
    if kind == "gcc":
        name = re.sub(r"gcc", "g++", base, count=1) if "gcc" in base else "g++"
    else:
        name = re.sub(r"clang", "clang++", base, count=1)
    p = os.path.join(d, name)
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else ""


PROBE_CXX = ("#include <string_view>\n#include <optional>\n#include <iostream>\n"
             "static const unsigned char blob[] = {%s};\n"
             "int main(int argc, char **){std::optional<std::string_view> s(\"ok\");std::cout<<*s<<\"\\n\";"
             "return (s->size()==2 && blob[argc & 255] == (argc & 255)) ? 0 : 1;}\n"
             % ",".join(str(i) for i in range(256)))


def compiler_works(cxx, extra_path=None, scratch=None, flags=()):
    """Compile, link and RUN a C++17 program in the scrubbed environment the build will have.
    Catches gcc without g++ / libstdc++-devel, and -- through the data blob -- a gcc built
    against a NEWER assembler than the one it finds (gcc >= 15 writes `.base64`, binutils
    < 2.43 answers "unknown pseudo-op", but only for files that have such data)."""
    if not cxx:
        return False, "no C++ compiler"
    d = tempfile.mkdtemp(prefix="egt-cc-", dir=scratch)
    env = scrubbed_env(extra_path)
    try:
        src = os.path.join(d, "t.cpp")
        with open(src, "w") as f:
            f.write(PROBE_CXX)
        exe = os.path.join(d, "t")
        p = run([cxx, "-std=c++17"] + list(flags) + [src, "-o", exe], check=False, env=env, timeout=180)
        if p.returncode != 0:
            lines = [l for l in (p.stdout or "").strip().splitlines() if "rror" in l] or \
                (p.stdout or "").strip().splitlines() or ["compile failed"]
            return False, lines[0].strip()[:160]
        try:
            r = run([exe], check=False, env=env, timeout=30)
        except ToolError as e:
            return False, ("a program cannot be run from %s (%s): is that filesystem noexec?  TMPDIR "
                           "chooses another" % (d, str(e).strip().splitlines()[0][-60:]))
        return (True, "runs") if r.returncode == 0 else \
            (False, "its programs do not RUN in a clean environment: " +
             ((r.stdout or "").strip().splitlines() or ["?"])[-1][:120])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# Where compilers and binutils are looked for: /usr/bin first, the caller's PATH, then the
# usual homes of private and vendor toolchains.
TOOL_GLOBS = ["~/opt/*/bin", "~/.local/bin", "/usr/local/bin", "/usr/local/*/bin", "/opt/*/bin",
              "/opt/rh/*/root/usr/bin", "/usr/lib/llvm*/bin", "/usr/lib64/llvm*/bin"]
COMPILER_NAME = re.compile(r"^(gcc|clang)(-\d+(\.\d+)*)?$")


def tool_search_dirs(home=None, path_env=None):
    home = os.path.expanduser("~") if home is None else home
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    dirs = ["/usr/bin"] + [d for d in path_env.split(":") if d]
    for pat in TOOL_GLOBS:
        dirs.extend(sorted(glob.glob(home + pat[1:] if pat.startswith("~") else pat)))
    seen, res = set(), []
    for d in dirs:
        real = os.path.realpath(d)
        if real not in seen and os.path.isdir(d):
            seen.add(real)
            res.append(d)
    return res


def scan_names(dirs, pattern):
    """Executable FILES whose names match, one per real file."""
    seen, found = set(), []
    for d in dirs:
        try:
            names = sorted(os.listdir(d), key=lambda n: (len(n), n))
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            if not pattern.match(name) or not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                found.append(path)
    return found


def origin_of(path):
    d, name = os.path.split(path)
    if d in ("/usr/bin", "/bin"):
        return "system" if name in ("gcc", "clang", "cc") else "system, versioned"
    if d.startswith("/opt/rh/"):
        return "toolset"
    if re.match(r"^/usr/lib(64)?/llvm", d):
        return "distro LLVM package"
    return "private"


def describe_host(cc, ci):
    ci["cxx"] = cxx_partner(cc, ci["kind"])
    ci["origin"] = origin_of(cc)
    ci["system"] = ci["origin"] == "system"
    return ci


def find_host_compilers(dirs=None):
    """Every gcc and clang here: listed so they can be chosen.  The default stays the
    system's gcc, which is what upstream tests a bootstrap against."""
    found = []
    for cc in scan_names(tool_search_dirs() if dirs is None else dirs, COMPILER_NAME):
        ci = compiler_info(cc)
        if ci:
            found.append(describe_host(cc, ci))
    return found


def find_binutils(dirs=None):
    """Every GNU binutils set: a directory holding both `as` and `ld`."""
    sets, seen = [], set()
    dirs = tool_search_dirs() if dirs is None else dirs
    for d in sorted(dirs, key=lambda x: (os.path.realpath(x) != "/usr/bin", len(x))):
        a_, l_ = os.path.join(d, "as"), os.path.join(d, "ld")
        if not (os.access(a_, os.X_OK) and os.access(l_, os.X_OK)) or os.path.realpath(a_) in seen:
            continue
        seen.add(os.path.realpath(a_))
        try:
            line = ((run([a_, "--version"], check=False, env=scrubbed_env(), timeout=20).stdout or "")
                    .splitlines() or [""])[0]
        except ToolError:
            continue
        if "GNU assembler" not in line:
            continue
        v = numver(re.sub(r"^.*?(\d+\.\d+)", r"\1", line))
        sets.append({"dir": d, "version": v, "banner": line.strip(),
                     "system": os.path.realpath(d) == "/usr/bin",
                     "gold": os.access(os.path.join(d, "ld.gold"), os.X_OK)})
    return sets


def have_header(rel, bases=None):
    for base in bases or (["/usr/include", "/usr/local/include"] + glob.glob("/usr/include/*-linux-gnu*")):
        if os.path.exists(os.path.join(base, rel)):
            return True
    return False


def crb_repo(machine):
    """The id of the repository with texinfo and some -devel packages on Enterprise Linux:
    crb (EL 9+ rebuilds), powertools (EL 8), codeready-builder-for-rhel-N-ARCH-rpms (RHEL
    itself, a subscription-manager channel).  '' elsewhere (Fedora has them in its main
    repositories)."""
    osid, like = machine.get("os_id", ""), machine.get("os_like", "")
    major = (re.match(r"\d+", machine.get("os_version", "") or "") or re.match(r"", "")).group(0)
    if osid == "rhel":
        return "codeready-builder-for-rhel-%s-%s-rpms" % (major or "9", machine.get("arch", "x86_64"))
    if osid in ("almalinux", "rocky", "centos") or (osid != "fedora" and "rhel" in like.split()):
        return "powertools" if major == "8" else "crb"
    return ""


def enable_repo_command(repo):
    """-> the argv that enables it: RHEL's channel through subscription-manager (dnf
    config-manager cannot: 'No matching repo to modify'), the rebuilds' through dnf."""
    if not repo:
        return []
    if repo.startswith("codeready-builder-for-rhel-"):
        return ["sudo", "subscription-manager", "repos", "--enable", repo]
    return ["sudo", "dnf", "config-manager", "--set-enabled", repo]


BUILD_TOOLS = ("make", "bison", "flex", "makeinfo", "m4", "perl", "gawk", "awk", "tar", "xz", "bzip2",
               "gzip", "wget", "curl", "file", "which", "diff", "cmp", "find", "patch", "git", "sha512sum",
               "runtest", "expect", "gdc", "gnatmake", "readelf", "ldd")
HEADERS = {"zlib": "zlib.h", "zstd": "zstd.h", "libc": "stdio.h", "multilib": "gnu/stubs-32.h",
           "gmp": "gmp.h", "mpfr": "mpfr.h", "mpc": "mpc.h", "isl": "isl/version.h"}


def detect_machine():
    osr = parse_os_release(read_text("/etc/os-release"))
    total, avail = meminfo_gb()
    cpu = cpu_info()
    return {"os": "%s %s" % (osr.get("NAME", "Linux"), osr.get("VERSION_ID", "")),
            "os_id": osr.get("ID", ""), "os_like": osr.get("ID_LIKE", ""), "os_version": osr.get("VERSION_ID", ""),
            "arch": os.uname().machine, "cpus": cpu_count(),
            "cpu_model": cpu["model"], "cpu_level": cpu_level(cpu["flags"]),
            "mem_total_gb": round(total, 1), "mem_avail_gb": round(avail, 1),
            "pkg_manager": find_pkg_manager(osr),
            "host_compilers": find_host_compilers(), "binutils": find_binutils(),
            "tools": dict((t, which(t) or "") for t in BUILD_TOOLS),
            "headers": dict((k, have_header(h)) for k, h in sorted(HEADERS.items()))}


# ------------------------------------------------------------------------ fetching

def decode_body(body, encoding=""):
    """A listing as text, whatever the mirror sent.  build_gcc_universal.sh:209-211: some
    mirrors serve these listings Content-Encoding: gzip, and a plain fetch hands back binary
    that quietly matches no pattern at all -- which looks exactly like "no versions found".
    So gzip is ASKED for, and undone when the header says so OR the body starts with the
    gzip magic (a proxy that strips the header but not the encoding)."""
    if "gzip" in (encoding or "").lower() or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as e:
            raise ToolError("a gzip-encoded answer that does not decompress: %s" % e)
    return body


def http_get(url, timeout=60, compressed=True):
    """-> the body (bytes), decompressed.  Every failure is a ToolError naming the URL."""
    headers = {"User-Agent": USER_AGENT}
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            enc = r.headers.get("Content-Encoding", "") or ""
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise ToolError("cannot fetch %s: %s" % (url, e))
    return decode_body(body, enc)


def url_exists(url, timeout=20):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.getcode() < 300
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


GCC_RELEASE_RE = re.compile(r"gcc-(\d+\.\d+\.\d+)/")
SNAPSHOT_RE = re.compile(r"(?<![\w.-])(\d+)-(\d{8})/")
BINUTILS_RE = re.compile(r"binutils-(with-gold-)?(\d+\.\d+(?:\.\d+)?)\.tar\.xz(?![.\w])")


def parse_gcc_releases(text):
    """build_gcc_universal.sh:216-219 -> every X.Y.Z, newest first (numerically: 9.5.0 < 16.2.0)."""
    return sorted(set(GCC_RELEASE_RE.findall(text or "")), key=numver, reverse=True)


def parse_snapshots(text, series=None):
    """-> ['17-20260823', ...] newest first; only SERIES' when one is given (225-233)."""
    found = set("%s-%s" % m for m in SNAPSHOT_RE.findall(text or ""))
    if series not in (None, "", "auto"):
        found = set(s for s in found if s.split("-")[0] == str(series))
    return sorted(found, key=lambda s: (int(s.split("-")[0]), s.split("-")[1]), reverse=True)


def parse_binutils(text):
    """-> {version: {"plain": bool, "gold": bool}}: which tarballs the listing has."""
    found = {}
    for gold, v in BINUTILS_RE.findall(text or ""):
        found.setdefault(v, {"plain": False, "gold": False})["gold" if gold else "plain"] = True
    return found


def binutils_pick(found, gold=True):
    """The newest version that can be built AS ASKED: with --gold, one that has gold (its
    with-gold tarball from 2.44 on, the plain one before); a future release with no gold
    tarball is skipped, not silently built without it.  -> version or ''."""
    for v in sorted(found, key=numver, reverse=True):
        has = found[v]
        if not gold and has["plain"]:
            return v
        if gold and (has["gold"] or (has["plain"] and not ver_ge(numver(v), GOLD_SPLIT))):
            return v
    return ""


def first_listing(mirrors, parse, get=None):
    """-> (what parse() found, the mirror): the first mirror whose listing parses to something.
    One that answers with nothing usable is treated like one that does not answer."""
    get = get or (lambda url: http_get(url, timeout=30))
    errors = []
    for m in mirrors:
        try:
            text = decode_body(get(m + "/")).decode("utf-8", "replace")
        except ToolError as e:
            errors.append("%s: %s" % (m, str(e).splitlines()[0][:160]))
            continue
        found = parse(text)
        if found:
            return found, m
        errors.append("%s: answered, but lists nothing that looks like a version" % m)
    raise ToolError("no mirror gave a usable listing:\n    " + "\n    ".join(errors))


def latest_gcc_release(get=None, exists=None):
    """-> (version, mirror).  The newest directory is confirmed with a HEAD of its tarball
    (a directory can appear before its tarball does), else the next newest."""
    exists = exists or url_exists
    versions, mirror = first_listing(GCC_MIRRORS, parse_gcc_releases, get)
    for v in versions[:3]:
        if exists("%s/gcc-%s/gcc-%s.tar.xz" % (mirror, v, v)):
            return v, mirror
    note("could not confirm a tarball for gcc-%s with a HEAD request: taking it anyway" % versions[0])
    return versions[0], mirror


def latest_snapshot(series, get=None):
    """-> (id, mirror).  series 'auto' = the highest series listed: trunk."""
    snaps, mirror = first_listing(SNAP_MIRRORS, lambda t: parse_snapshots(t, series), get)
    return snaps[0], mirror


def released_majors(get=None):
    """The GCC majors that have a release -- for the banner.  set() when nobody answers."""
    try:
        return set(numver(v)[0] for v in first_listing(GCC_MIRRORS, parse_gcc_releases, get)[0])
    except ToolError:
        return set()


def sha512_file(path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_sums(text):
    """`sha512sum` output -> {file name: digest}."""
    sums = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-9a-fA-F]{64,128})\s+\*?(\S+)\s*$", line.strip())
        if m:
            sums[os.path.basename(m.group(2))] = m.group(1).lower()
    return sums


class DownloadError(ToolError):
    """A download that failed.  transient: worth trying the same URL again (a timeout, a
    dropped or reset connection, a short read, HTTP 408/429/5xx) -- not a 404 or a 403."""

    def __init__(self, msg, transient=False):
        ToolError.__init__(self, msg)
        self.transient = transient


TRANSIENT_HTTP = (408, 429, 500, 502, 503, 504)       # curl --retry's list (the .sh's fetch_from)
DOWNLOAD_RETRIES = 3                                  # curl --retry 3
DOWNLOAD_RETRY_DELAY = 5                              #      --retry-delay 5


def transient_error(e):
    """curl --retry's idea of transient, + a reset or short read mid-transfer.  A name that
    does not resolve and a refused connection are not (curl does not retry them either: an
    offline machine would only wait longer)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in TRANSIENT_HTTP
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    if isinstance(e, (socket.gaierror, ConnectionRefusedError)):
        return False
    return isinstance(e, (socket.timeout, ConnectionError, http.client.IncompleteRead,
                          http.client.RemoteDisconnected, http.client.BadStatusLine))


def download(url, dest):
    """To dest.part, then renamed: a file with the final name is whole.  An HTTP error fails
    (never an HTML error page saved under the archive's name -- download_prerequisites' curl
    -LO without -f does exactly that, and then never fetches it again)."""
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
                    sys.stdout.write("\r\033[K  %s / %s" % (human(got), human(total) if total else "?"))
                    sys.stdout.flush()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        if isinstance(e, urllib.error.HTTPError):
            try:
                e.close()                                    # it holds the response's connection
            except Exception:
                pass
        raise DownloadError("download failed: %s\n  %s" % (url, e), transient_error(e))
    finally:
        if UI.tty:
            sys.stdout.write("\r\033[K")
    if total and got != total:
        raise DownloadError("download was cut short: %s of %s from %s" % (human(got), human(total), url), True)
    os.replace(part, dest)
    ok("downloaded %s in %s" % (human(got), hms(time.time() - started)))


def fetch_with_retries(fetch, url, dest):
    """One URL, tried again after a transient failure: DOWNLOAD_RETRIES more times,
    DOWNLOAD_RETRY_DELAY seconds apart (build_gcc_universal.sh:240, curl --retry 3
    --retry-delay 5).  A 404 or a 403 is final at once."""
    for attempt in range(DOWNLOAD_RETRIES + 1):
        try:
            return fetch(url, dest)
        except DownloadError as e:
            if not e.transient or attempt == DOWNLOAD_RETRIES:
                raise
            if os.path.exists(dest + ".part"):
                os.unlink(dest + ".part")
            warn("%s -- trying it again in %ds (%d of %d)" % (str(e).splitlines()[-1].strip()[:160],
                                                              DOWNLOAD_RETRY_DELAY, attempt + 1, DOWNLOAD_RETRIES))
            time.sleep(DOWNLOAD_RETRY_DELAY)


def download_from(urls, dest, fetch=None, check=None):
    """Each URL in turn (each one retried after a transient failure) until one gives a file
    that passes check(path) -> (ok, why).  -> the URL that did.  build_gcc_universal.sh:235-247."""
    fetch = fetch or download
    errors = []
    for i, url in enumerate(urls):
        info("downloading %s" % url)
        try:
            fetch_with_retries(fetch, url, dest)
            good, why = check(dest) if check else (True, "")
            if good:
                return url
            os.unlink(dest)
            raise ToolError(why)
        except ToolError as e:
            if os.path.exists(dest + ".part"):
                os.unlink(dest + ".part")
            errors.append("%s: %s" % (url, str(e).splitlines()[-1].strip()[:160]))
            if i + 1 < len(urls):
                warn("that mirror failed, trying the next one")
    raise ToolError("could not download %s from any mirror:\n    %s" % (os.path.basename(dest), "\n    ".join(errors)))


def xz_ok(path):
    """`xz -t` (build_gcc_universal.sh:368), done with the lzma module; the xz program only
    where this python was built without it."""
    try:
        import lzma
    except ImportError:
        lzma = None
    if lzma is None:
        if not which("xz"):
            return True, "not checked: neither the lzma module nor xz is here"
        p = run(["xz", "-t", path], check=False, env=scrubbed_env(), timeout=1800)
        return p.returncode == 0, (p.stdout or "").strip()[-200:] or "xz -t failed"
    try:
        with lzma.open(path) as f:
            while f.read(1 << 22):
                pass
    except (lzma.LZMAError, EOFError, OSError) as e:
        return False, str(e)
    return True, "ok"


def published_sha512(dirs, name, get=None):
    """The digest a mirror publishes in <dir>/sha512.sum (GCC release and snapshot
    directories have one), or ''."""
    get = get or (lambda url: http_get(url, timeout=30))
    for d in dirs:
        try:
            sums = parse_sums(decode_body(get(d + "/sha512.sum")).decode("utf-8", "replace"))
        except ToolError:
            continue
        if name in sums:
            return sums[name]
    return ""


def fetch_tarball(urls, dest, sum_dirs=(), get=None, fetch=None):
    """Download (mirror fallback), check against the mirror's sha512.sum when there is one,
    test the xz stream.  A corrupt tarball -- downloaded now or cached from before -- is
    deleted and fetched again ONCE."""
    name = os.path.basename(dest)
    for attempt in (1, 2):
        if not os.path.isfile(dest):
            download_from(urls, dest, fetch)
        else:
            ok("already here: %s" % dest)
        want = published_sha512(sum_dirs, name, get) if sum_dirs else ""
        if want:
            have = sha512_file(dest)
            if have != want:
                os.unlink(dest)
                if attempt == 1:
                    warn("%s does not match the mirror's sha512.sum: deleted, fetching it again" % name)
                    continue
                raise ToolError("%s does not match the published sha512 twice:\n  expected %s\n  got      %s"
                                % (name, want, have))
            ok("sha512 matches the mirror's sha512.sum")
        else:
            note("no published sha512 for %s: integrity rests on TLS and the xz check" % name)
        good, why = xz_ok(dest)
        if good:
            ok("xz integrity: %s" % why)
            return
        os.unlink(dest)
        if attempt == 1:
            warn("%s is corrupt (%s): deleted, fetching it again" % (name, why))
            continue
        raise ToolError("%s is corrupt twice (%s) -- a mirror or a proxy is mangling it" % (name, why))


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


# ---------------------------------------------------------------- the source trees

def is_sha(ref):
    return bool(re.match(r"^[0-9a-f]{7,40}$", ref or ""))


def gcc_tree_version(src):
    """-> (BASE-VER, DEV-PHASE, DATESTAMP) of a GCC tree, '' where it says nothing."""
    g = lambda n: read_text(os.path.join(src, "gcc", n)).strip()
    return g("BASE-VER"), g("DEV-PHASE"), g("DATESTAMP")


def git_short_sha(src):
    if not os.path.exists(os.path.join(src, ".git")) or not which("git"):
        return ""
    p = run(["git", "-C", src, "rev-parse", "--short", "HEAD"], check=False, env=scrubbed_env(), timeout=60)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def git_id(src, series="auto"):
    """build_gcc_universal.sh:360-362: name the prefix after what the tree IS."""
    bv = gcc_tree_version(src)[0] or ("%s.0.0" % series if str(series).isdigit() else "0.0.0")
    return "%s-git-%s" % (bv, git_short_sha(src) or "unknown")


def local_gcc_id(src):
    bv, phase, date = gcc_tree_version(src)
    if not bv:
        raise ToolError("%s is not a GCC source tree (no gcc/BASE-VER)" % src)
    sha = git_short_sha(src)
    if sha:
        return "%s-git-%s" % (bv, sha)
    if phase and date:
        return "%d-%s" % (numver(bv)[0], date)            # a snapshot, named as the .sh named them
    return bv


def binutils_tree_version(src):
    m = re.search(r"m4_define\(\[BFD_VERSION\],\s*\[([\d.]+)\]\)", read_text(os.path.join(src, "bfd", "version.m4")))
    if m:
        return m.group(1)
    m = re.search(r"^PACKAGE_VERSION='([\d.]+)'", read_text(os.path.join(src, "ld", "configure")), re.M)
    return m.group(1) if m else ""


def newest_on_disk(srcroot, pattern, key):
    """The newest unpacked tree matching pattern (a regex on the directory name) -- what an
    offline machine falls back to.  -> (id, dir) or ('', '')."""
    here = []
    for d in glob.glob(os.path.join(srcroot, "*")):
        m = re.match(pattern, os.path.basename(d))
        if m and os.path.isfile(os.path.join(d, "configure")):
            here.append((key(m.group(1)), m.group(1), d))
    if not here:
        return "", ""
    here.sort()
    return here[-1][1], here[-1][2]


def gcc_source(opts, workdir, get=None, exists=None):
    """-> {kind, track, id, dir, tarball, urls, sum_dirs, label, ref}.  Nothing is downloaded;
    the network is asked only for --gcc-version auto."""
    srcroot = os.path.join(workdir, "src")
    track, v = opts["track"], opts["gcc_version"]
    if opts["source"]:
        d = os.path.abspath(os.path.expanduser(opts["source"]))
        if not os.path.isfile(os.path.join(d, "configure")):
            raise ToolError("--source %s: no configure there" % d)
        return {"kind": "local", "track": track, "id": local_gcc_id(d), "dir": d, "tarball": "", "urls": [],
                "sum_dirs": [], "label": "%s  (yours: configured out of tree)" % d, "ref": ""}
    if track == "git":
        ref = opts["git_ref"]
        d = os.path.join(srcroot, "gcc-git-" + safe_name(ref))
        return {"kind": "git", "track": "git", "id": "git-" + safe_name(ref), "dir": d, "tarball": "", "urls": [],
                "sum_dirs": [], "ref": ref, "label": "git %s of %s -> %s" % (ref, opts["git_url"], d)}
    if track == "release":
        if v == "auto":
            try:
                v = latest_gcc_release(get, exists)[0]
            except ToolError as e:
                v, _ = newest_on_disk(srcroot, r"^gcc-(\d+\.\d+\.\d+)$", numver)
                if not v:
                    raise ToolError("could not reach a GNU mirror to discover the latest GCC release.\n  %s\n"
                                    "  Pass one explicitly:  --gcc-version 16.2.0" % str(e).replace("\n", "\n  "))
                note("the mirrors cannot be listed now: using gcc-%s, the newest source already here" % v)
        if not re.match(r"^\d+\.\d+\.\d+$", v):
            raise ToolError("for --track release, --gcc-version looks like 16.2.0 (or auto), got %r" % v)
        urls = ["%s/gcc-%s/gcc-%s.tar.xz" % (m, v, v) for m in GCC_MIRRORS]
        sums = ["%s/gcc-%s" % (m, v) for m in GCC_MIRRORS]
    else:
        if v == "auto":
            try:
                v = latest_snapshot(opts["series"], get)[0]
            except ToolError as e:
                pat = r"^gcc-(%s-\d{8})$" % (re.escape(opts["series"]) if opts["series"] != "auto" else r"\d+")
                v, _ = newest_on_disk(srcroot, pat, lambda s: (int(s.split("-")[0]), s.split("-")[1]))
                if not v:
                    raise ToolError("could not list the snapshots of series %s.\n  %s\n  Pass one explicitly:  "
                                    "--track snapshot --gcc-version 17-20260823" % (opts["series"], str(e).replace("\n", "\n  ")))
                note("the snapshot mirrors cannot be listed now: using %s, the newest already here" % v)
        if not re.match(r"^\d+-\d{8}$", v):
            raise ToolError("for --track snapshot, --gcc-version looks like 17-20260823 (or auto), got %r" % v)
        if opts["series"] not in ("auto", v.split("-")[0]):
            note("--gcc-version %s is a snapshot of series %s, not %s: the version wins" % (v, v.split("-")[0], opts["series"]))
        urls = ["%s/%s/gcc-%s.tar.xz" % (m, v, v) for m in SNAP_MIRRORS]
        sums = ["%s/%s" % (m, v) for m in SNAP_MIRRORS]
    d = os.path.join(srcroot, "gcc-" + v)
    return {"kind": "tarball", "track": track, "id": v, "dir": d, "tarball": d + ".tar.xz", "urls": urls,
            "sum_dirs": sums, "ref": "", "label": "%s %s -> %s" % (track, v, d)}


def binutils_source(opts, workdir, get=None):
    srcroot = os.path.join(workdir, "src")
    if opts["source"]:
        d = os.path.abspath(os.path.expanduser(opts["source"]))
        v = binutils_tree_version(d)
        if not v or not os.path.isfile(os.path.join(d, "configure")):
            raise ToolError("--source %s is not a binutils tree (no bfd/version.m4, no configure)" % d)
        if opts["gold"] and not os.path.isdir(os.path.join(d, "gold")):
            raise ToolError("--source %s has no gold/ directory: a binutils-with-gold tree, or --no-gold" % d)
        return {"kind": "local", "id": v, "dir": d, "tarball": "", "urls": [], "sum_dirs": [], "with_gold": False,
                "label": "%s  (yours: configured out of tree)" % d}
    v, gold = opts["binutils_version"], opts["gold"]
    if v == "auto":
        try:
            found = first_listing(BINUTILS_MIRRORS, parse_binutils, get)[0]
            v = binutils_pick(found, gold)
            if not v:
                raise ToolError("no binutils release on the mirrors has %s" % ("gold" if gold else "a plain tarball"))
        except ToolError as e:
            v, _ = newest_on_disk(srcroot, r"^binutils-(?:with-gold-)?(\d+\.\d+(?:\.\d+)?)$", numver)
            if not v:
                raise ToolError("could not discover the latest binutils.\n  %s\n  Pass one explicitly:  "
                                "--binutils-version 2.46" % str(e).replace("\n", "\n  "))
            note("the mirrors cannot be listed now: using binutils %s, the newest source already here" % v)
    if not re.match(r"^\d+\.\d+(\.\d+)?$", v):
        raise ToolError("--binutils-version looks like 2.46 or 2.45.1 (or auto), got %r" % v)
    with_gold = gold and ver_ge(numver(v), GOLD_SPLIT)
    stem = ("binutils-with-gold-%s" if with_gold else "binutils-%s") % v
    d = os.path.join(srcroot, stem)
    return {"kind": "tarball", "id": v, "dir": d, "tarball": d + ".tar.xz", "with_gold": with_gold,
            "urls": ["%s/%s.tar.xz" % (m, stem) for m in BINUTILS_MIRRORS], "sum_dirs": [],
            "label": "binutils %s%s -> %s" % (v, " (the with-gold tarball: gold left the main one in 2.44)"
                                              if with_gold else "", d)}


def git_fetch(src_dir, ref, url, update, log):
    """build_gcc_universal.sh:348-358, plus a SHA ref (`clone --branch` cannot take one)."""
    env = scrubbed_env()
    if os.path.isdir(os.path.join(src_dir, ".git")):
        if not update:
            info("reusing the clone at %s as it is (this build was started from it)" % src_dir)
            return
        info("updating the clone at %s" % src_dir)
        rc, _ = run_logged(["git", "-C", src_dir, "fetch", "--depth", "1", "origin", ref], src_dir, env, log, "git fetch")
        if rc == 0:
            rc = run(["git", "-C", src_dir, "checkout", "-q", "FETCH_HEAD"], check=False, env=env).returncode
        if rc != 0:
            warn("fetch failed; building whatever is already checked out")
        return
    if not which("git"):
        raise ToolError("--track git needs git")
    os.makedirs(os.path.dirname(src_dir), exist_ok=True)
    tmp = src_dir + ".cloning"
    shutil.rmtree(tmp, ignore_errors=True)
    if is_sha(ref):
        steps = [["git", "init", "-q", tmp], ["git", "-C", tmp, "remote", "add", "origin", url],
                 ["git", "-C", tmp, "fetch", "--depth", "1", "origin", ref],
                 ["git", "-C", tmp, "checkout", "-q", "FETCH_HEAD"]]
    else:
        info("git clone --depth 1 --branch %s  (shallow: the full history is several GB)" % ref)
        steps = [["git", "clone", "--depth", "1", "--branch", ref, url, tmp]]
    for cmd in steps:
        rc, _ = run_logged(cmd, os.path.dirname(src_dir), env, log, "git")
        if rc != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ToolError("%s failed -- see %s.  --track snapshot is a plain tarball." % (" ".join(cmd[:3]), log))
    os.rename(tmp, src_dir)


# ------------------------------------------------------------- GCC's prerequisites

PREREQ_RE = re.compile(r"^(gmp|mpfr|mpc|isl|gettext)='([^']+)'", re.M)


def parse_prereq_script(text):
    """contrib/download_prerequisites -> [(name, archive)]: gmp, mpfr, mpc, isl and, from
    GCC 14 on, gettext -- whatever THIS tree's script lists."""
    return PREREQ_RE.findall(text or "")


def prereqs_missing(src, names):
    """Done = every name the script lists is a link (or directory) resolving to a directory."""
    return [n for n in names if not os.path.isdir(os.path.join(src, n))]


def prefetch_prereqs(src, wanted, sums, fetch=None):
    """Each archive download_prerequisites would fetch, fetched first: from each of
    INFRA_MIRRORS in turn, through .part, and checked against the tree's own
    contrib/prerequisites.sha512 (a mismatch is deleted).  The script then finds them
    present and only checks, unpacks and links them."""
    fetched = []
    for name, archive in wanted:
        dest = os.path.join(src, archive)
        want = sums.get(archive, "")
        if os.path.isfile(dest) and want and sha512_file(dest) != want:
            warn("%s does not match contrib/prerequisites.sha512 (an error page saved under its name?): deleted" % archive)
            os.unlink(dest)
        if os.path.isfile(dest):
            continue

        def check(path, want=want):
            if not want:
                return True, ""
            return (sha512_file(path) == want), "sha512 does not match contrib/prerequisites.sha512"
        download_from(["%s/%s" % (m, archive) for m in INFRA_MIRRORS], dest, fetch, check)
        fetched.append(archive)
    return fetched


PREREQ_ADVICE = ("  The alternatives:\n"
                 "    --prereqs system, with the distribution's libraries:\n"
                 "        sudo dnf install gmp-devel mpfr-devel libmpc-devel   (isl-devel optional)\n"
                 "        sudo apt-get install libgmp-dev libmpfr-dev libmpc-dev   (libisl-dev optional)\n"
                 "    or --configure-extra '--with-gmp=DIR --with-mpfr=DIR --with-mpc=DIR' for your own builds.")


# ------------------------------------------------------------------- what is needed

# key -> {package manager: package(s)}.  build_gcc_universal.sh:169-174's lists, + m4.
PACKAGES = {
    "gcc": {"dnf": "gcc", "apt-get": "gcc", "zypper": "gcc", "pacman": "gcc"},
    "g++": {"dnf": "gcc-c++", "apt-get": "g++", "zypper": "gcc-c++", "pacman": "gcc"},
    "make": {"dnf": "make", "apt-get": "make", "zypper": "make", "pacman": "make"},
    "binutils": {"dnf": "binutils", "apt-get": "binutils", "zypper": "binutils", "pacman": "binutils"},
    "bison": {"dnf": "bison", "apt-get": "bison", "zypper": "bison", "pacman": "bison"},
    "flex": {"dnf": "flex", "apt-get": "flex", "zypper": "flex", "pacman": "flex"},
    "texinfo": {"dnf": "texinfo", "apt-get": "texinfo", "zypper": "texinfo", "pacman": "texinfo"},
    "m4": {"dnf": "m4", "apt-get": "m4", "zypper": "m4", "pacman": "m4"},
    "perl": {"dnf": "perl", "apt-get": "perl", "zypper": "perl", "pacman": "perl"},
    "gawk": {"dnf": "gawk", "apt-get": "gawk", "zypper": "gawk", "pacman": "gawk"},
    "diffutils": {"dnf": "diffutils", "apt-get": "diffutils", "zypper": "diffutils", "pacman": "diffutils"},
    "findutils": {"dnf": "findutils", "apt-get": "findutils", "zypper": "findutils", "pacman": "findutils"},
    "patch": {"dnf": "patch", "apt-get": "patch", "zypper": "patch", "pacman": "patch"},
    "tar": {"dnf": "tar", "apt-get": "tar", "zypper": "tar", "pacman": "tar"},
    "xz": {"dnf": "xz", "apt-get": "xz-utils", "zypper": "xz", "pacman": "xz"},
    "bzip2": {"dnf": "bzip2", "apt-get": "bzip2", "zypper": "bzip2", "pacman": "bzip2"},
    "gzip": {"dnf": "gzip", "apt-get": "gzip", "zypper": "gzip", "pacman": "gzip"},
    "wget": {"dnf": "wget", "apt-get": "wget", "zypper": "wget", "pacman": "wget"},
    "file": {"dnf": "file", "apt-get": "file", "zypper": "file", "pacman": "file"},
    "which": {"dnf": "which", "apt-get": "debianutils", "zypper": "which", "pacman": "which"},
    "zlib": {"dnf": "zlib-devel", "apt-get": "zlib1g-dev", "zypper": "zlib-devel", "pacman": "zlib"},
    "libc": {"dnf": "glibc-devel", "apt-get": "libc6-dev", "zypper": "glibc-devel", "pacman": "glibc"},
    "zstd": {"dnf": "libzstd-devel", "apt-get": "libzstd-dev", "zypper": "libzstd-devel", "pacman": "zstd"},
    "git": {"dnf": "git", "apt-get": "git", "zypper": "git", "pacman": "git"},
    "multilib": {"dnf": "glibc-devel.i686 libstdc++-devel.i686", "apt-get": "gcc-multilib g++-multilib",
                 "zypper": "glibc-devel-32bit gcc-c++-32bit", "pacman": "lib32-glibc lib32-gcc-libs"},
    "dejagnu": {"dnf": "dejagnu", "apt-get": "dejagnu", "zypper": "dejagnu", "pacman": "dejagnu"},
    "expect": {"dnf": "expect", "apt-get": "expect", "zypper": "expect", "pacman": "expect"},
    "gdc": {"dnf": "gcc-gdc", "apt-get": "gdc", "zypper": "gcc-d", "pacman": "gcc-d"},
    "gnat": {"dnf": "gcc-gnat", "apt-get": "gnat", "zypper": "gcc-ada", "pacman": "gcc-ada"},
    "gmp": {"dnf": "gmp-devel", "apt-get": "libgmp-dev", "zypper": "gmp-devel", "pacman": "gmp"},
    "mpfr": {"dnf": "mpfr-devel", "apt-get": "libmpfr-dev", "zypper": "mpfr-devel", "pacman": "mpfr"},
    "mpc": {"dnf": "libmpc-devel", "apt-get": "libmpc-dev", "zypper": "mpc-devel", "pacman": "libmpc"},
    "isl": {"dnf": "isl-devel", "apt-get": "libisl-dev", "zypper": "isl-devel", "pacman": "libisl"},
}
# On Enterprise Linux these live in CRB (EL 9/10; PowerTools on EL 8), which is off by default.
CRB_PACKAGES = ("texinfo", "dejagnu", "libmpc-devel", "isl-devel")


def requirements(opts, machine, host=None, host_works=None):
    """-> [(package key, what, present, level)] for THIS selection: component, track,
    languages, multilib, --check and --prereqs all change it.  Presence is PROBED (the tool
    runs, the header is there, the compiler builds a program), never asked of rpm/dpkg."""
    t, h = machine["tools"], machine["headers"]
    langs = [x.strip() for x in opts["languages"].split(",") if x.strip()]
    gcc_c = opts["component"] == "gcc"
    why_not = "" if host_works in (None, True) else ": " + str(host_works)
    rows = [
        ("gcc", "a C compiler to bootstrap with (gcc)", bool(host), "required"),
        ("g++", "a C++ compiler that builds and runs a program (g++)" + why_not,
         bool(host and host.get("cxx")) and host_works in (None, True), "required"),
        ("make", "make", bool(t.get("make")), "required"),
        ("binutils", "an assembler and linker (as, ld)", bool(machine.get("binutils")), "required"),
        ("bison", "bison", bool(t.get("bison")), "required"),
        ("flex", "flex", bool(t.get("flex")), "required"),
        ("texinfo", "makeinfo (texinfo)", bool(t.get("makeinfo")), "required"),
        ("m4", "m4", bool(t.get("m4")), "required"),
        ("perl", "perl", bool(t.get("perl")), "required"),
        ("tar", "tar", bool(t.get("tar")), "required"),
        ("xz", "xz", bool(t.get("xz")), "required"),
        ("zlib", "zlib headers (zlib.h)" + (": gcc is configured --with-system-zlib" if gcc_c else ""), bool(h.get("zlib")), "required"),
        ("libc", "the C library's headers (stdio.h)", bool(h.get("libc")), "required"),
        ("zstd", "zstd headers (zstd.h): zstd-compressed LTO / debug sections", bool(h.get("zstd")), "wanted"),
    ]
    if gcc_c:
        rows += [
            ("gawk", "gawk" if t.get("awk") else "an awk (gawk)", bool(t.get("gawk")), "wanted" if t.get("awk") else "required"),
            ("bzip2", "bzip2: the in-tree GMP/MPFR/ISL archives are .tar.bz2", bool(t.get("bzip2")),
             "required" if opts["prereqs"] == "in-tree" else "wanted"),
            ("gzip", "gzip", bool(t.get("gzip")), "required"),
            ("diffutils", "cmp and diff: the bootstrap compares stage 2 with stage 3", bool(t.get("cmp") and t.get("diff")), "required"),
            ("findutils", "find", bool(t.get("find")), "required"),
            ("file", "file (libtool asks it)", bool(t.get("file")), "required"),
            ("wget", "wget or curl: download_prerequisites' own fallback", bool(t.get("wget") or t.get("curl")), "wanted"),
            ("patch", "patch", bool(t.get("patch")), "wanted"),
            ("which", "which", bool(t.get("which")), "wanted"),
        ]
        if opts["track"] == "git" and not opts["source"]:
            rows.append(("git", "git: --track git", bool(t.get("git")), "required"))
        if opts["multilib"]:
            rows.append(("multilib", "32-bit C library headers (gnu/stubs-32.h): --multilib", bool(h.get("multilib")), "required"))
        if "d" in langs:
            rows.append(("gdc", "a host D compiler (gdc): --languages d", bool(t.get("gdc")), "required"))
        if "ada" in langs:
            rows.append(("gnat", "a host Ada compiler (gnatmake): --languages ada", bool(t.get("gnatmake")), "required"))
        if opts["prereqs"] == "system":
            rows += [("gmp", "GMP headers (gmp.h): --prereqs system", bool(h.get("gmp")), "required"),
                     ("mpfr", "MPFR headers (mpfr.h): --prereqs system", bool(h.get("mpfr")), "required"),
                     ("mpc", "MPC headers (mpc.h): --prereqs system", bool(h.get("mpc")), "required"),
                     ("isl", "ISL headers: Graphite loop optimisations", bool(h.get("isl")), "optional")]
    if opts["check"]:
        rows += [("dejagnu", "dejagnu (runtest): --check", bool(t.get("runtest")), "required"),
                 ("expect", "expect: --check", bool(t.get("expect")), "required")]
    return rows


def install_command(packages, pm):
    if not packages or not pm:
        return ""
    if pm == "pacman":
        return "sudo pacman -S --needed --noconfirm " + " ".join(packages)
    return "sudo %s install -y %s" % (pm, " ".join(packages))


def doctor_report(opts, machine, host=None, host_works=None, euid=None):
    """The contract's shape, exactly: {tool, ok, package_manager, missing: [{what, package,
    level}], enable_repos, install_command}.  ok is false when anything required is missing
    or this runs as root."""
    pm = machine.get("pkg_manager") or ""
    missing = []
    euid = os.geteuid() if euid is None else euid
    if euid == 0:
        missing.append({"what": "not root: this runs as root -- build as yourself (only --install-deps uses sudo)",
                        "package": "", "level": "required"})
    for key, what, present, level in requirements(opts, machine, host, host_works):
        if not present:
            missing.append({"what": what, "package": PACKAGES[key].get(pm, ""), "level": level})
    pk = []
    for m in missing:
        if m["level"] in ("required", "wanted"):
            for p in m["package"].split():
                if p not in pk:
                    pk.append(p)
    repo = crb_repo(machine) if pm == "dnf" else ""
    enable = [repo] if repo and any(p in CRB_PACKAGES for p in pk) else []
    return {"tool": TOOL, "ok": not any(m["level"] == "required" for m in missing),
            "package_manager": pm or None, "missing": missing, "enable_repos": enable,
            "install_command": install_command(pk, pm)}


# ------------------------------------------------------------------------- state

class State(object):
    """<build dir>/state.json, written whole and atomically on every change."""

    def __init__(self, path, fresh=False):
        self.path = path
        self.d = {} if fresh else read_json(path)
        if self.d is None:
            if os.path.exists(path):
                warn("%s is unreadable -- starting a fresh record" % path)
            self.d = {}
        self.d.setdefault("stages", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.d["tool"] = TOOL                   # what `clean` checks before it deletes anything
        write_atomic(self.path, json.dumps(self.d, indent=1, sort_keys=True) + "\n")

    def stage(self, name):
        return self.d["stages"].get(name, {})

    def set_stage(self, name, **kv):
        self.d["stages"].setdefault(name, {}).update(kv)
        self.save()

    def done(self, name):
        return self.stage(name).get("status") == "done"


_LOCKS = []


def lock_dir(path):
    """One instance per build directory, held until the process exits."""
    os.makedirs(path, exist_ok=True)
    f = open(os.path.join(path, ".lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as e:
        f.close()
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            raise ToolError("another %s is working in %s right now" % (TOOL, path))
        raise ToolError("cannot lock %s: %s  (a filesystem without locking -- NFS without lockd?)" % (path, e))
    f.truncate(0)                                            # who holds it, for a person looking
    f.write("%d\n" % os.getpid())
    f.flush()
    _LOCKS.append(f)


def lock_is_held(path):
    """Whether some process holds the directory's lock -- a record that says `running`
    while nobody does belongs to a run that was killed."""
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


class locked(object):
    """A lock held for one operation, and waited for: two instances fetching the same
    source would otherwise write the same file."""

    def __init__(self, path):
        self.path, self.f = path, None

    def __enter__(self):
        os.makedirs(self.path, exist_ok=True)
        self.f = open(os.path.join(self.path, ".lock"), "a")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            note("another %s is fetching into %s: waiting for it" % (TOOL, self.path))
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ------------------------------------------------------------- running make, logs

ERRORISH = re.compile(r"(configure: error|\berror:|\bError \d+|make(\[\d+\])?: \*\*\*|internal compiler error|"
                      r"undefined reference|No rule to make target|fatal error:|cannot find -l|\bKilled\b|"
                      r": (command )?not found\s*$|is missing on your system)")
OOMISH = re.compile(r"(Killed signal|\bKilled\b|signal 9|[Oo]ut of memory|std::bad_alloc|"
                    r"Cannot allocate memory|virtual memory exhausted|cannot allocate)")
DISKFULL = re.compile(r"(No space left on device|Disk quota exceeded|\bENOSPC\b)")


def errorish(line):
    return bool(ERRORISH.search(line)) and "warning:" not in line and "(ignored)" not in line


def fit_line(text, cols=None):
    cols = cols or shutil.get_terminal_size((100, 24)).columns
    return text[:max(cols - 1, 20)]


def error_summary(log_path, limit=14):
    """The FIRST real errors: the tail of a -j16 build is fifteen other jobs being stopped
    (the .sh showed the LAST eight grep hits, build_gcc_universal.sh:461)."""
    hits = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if errorish(line):
                    hits.append("%7d: %s" % (n, line.rstrip()[:300]))
                    if len(hits) >= limit:
                        break
    except OSError:
        pass
    return hits


def run_logged(cmd, cwd, env, log_path, label, verbose=False, stage_file=None):
    """Run cmd; everything goes to the log (the previous one kept as .prev); the console
    gets a one-line status on a terminal or a heartbeat every five minutes, plus the first
    40 error lines.  Its own session, so a closing terminal's SIGHUP does not stop make;
    ^C / SIGTERM stop the whole process group."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if os.path.exists(log_path):
        os.replace(log_path, log_path + ".prev")
    started = time.time()
    last, beat, drawn, shown_err = "", started, 0.0, 0
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, errors="replace", bufsize=1,
                                start_new_session=True)
    except OSError as e:
        raise ToolError("cannot run %s: %s" % (cmd[0], e))

    def status():
        stage = read_text(stage_file).strip() if stage_file else ""
        return fit_line("  [%s] %s%s  %s" % (hms(time.time() - started), label, " | " + stage if stage else "", last))
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write("# %s\n# cwd: %s\n" % (" ".join(shlex.quote(c) for c in cmd), cwd))
            for line in proc.stdout:
                log.write(line)
                text = line.rstrip("\n")
                if verbose:
                    out(text)
                    continue
                if text.strip():
                    last = text.strip()[:200]
                important = errorish(text)
                if important:
                    shown_err += 1
                    if shown_err <= 40:
                        if UI.tty:
                            sys.stdout.write("\r\033[K")
                        out("    " + text[:300])
                if UI.tty:
                    if time.time() - drawn >= 0.2:
                        drawn = time.time()
                        sys.stdout.write("\r\033[K" + status())
                        sys.stdout.flush()
                elif time.time() - beat >= 300:
                    beat = time.time()
                    out(status())
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
        if UI.tty:
            sys.stdout.write("\r\033[K")
    return rc, int(time.time() - started)


def failed_config_log(obj):
    """When make says "See `config.log' for more details": the sub-configure that failed --
    the newest config.log under the build tree ending in `configure: exit 1` -- and its
    first error lines.  -> (path, [lines]) or ('', [])."""
    found = []
    for dirpath, dirs, files in os.walk(obj):
        if dirpath[len(obj):].count(os.sep) >= 3:
            dirs[:] = []
        if "config.log" in files:
            p = os.path.join(dirpath, "config.log")
            if re.search(r"configure: exit [1-9]", read_text(p)[-4000:]):
                found.append((os.path.getmtime(p), p))
    if not found:
        return "", []
    path = sorted(found)[-1][1]
    lines = [l.rstrip() for l in read_text(path).splitlines() if re.search(r"\berror\b|configure: error", l)]
    return path, lines[:6]


def gcc_stage_of(obj, log_text, mode):
    """Which stage a failed GCC build was in: the toplevel Makefile writes stage_current on
    entering a stage (and stage_last); the .sh's fallback is 'stage1' in the last 300 lines
    of the log (465).  --mode quick has no stages: all of it is stage-1 work."""
    for name in ("stage_current", "stage_last"):
        s = read_text(os.path.join(obj, name)).strip()
        if s:
            return s
    if mode == "quick":
        return "stage1"
    return "stage1" if any("stage1" in l for l in (log_text or "").splitlines()[-300:]) else ""


def failure_report(component, obj, log_path, mode="", host_cc="", pair="", jobs=0):
    """The message for a failed make: the first errors, the sub-configure that failed, and
    what to do -- for GCC, whether changing --mode can help at all."""
    text = read_text(log_path)
    lines = ["make failed -- the first errors in %s:" % log_path]
    lines += ["  " + h for h in error_summary(log_path)] or ["  (no error line recognised: read the end of the log)"]
    if "config.log" in text:
        p, errs = failed_config_log(obj)
        if p:
            lines.append("  the configure that failed: %s" % p)
            lines += ["      " + e[:240] for e in errs]
    if DISKFULL.search(text):
        lines.append("The disk filled up.  Free some space where %s lives; `build` again resumes." % obj)
        return "\n".join(lines)
    if OOMISH.search(text):
        lines.append("Something was killed for memory.  Build again with fewer jobs (--jobs %d): it resumes."
                     % max(1, (jobs or 2) // 2))
        return "\n".join(lines)
    if component != "gcc":
        lines.append("binutils is built by %s with the system's as/ld.  Full log: %s" % (host_cc, log_path))
        return "\n".join(lines)
    stage = gcc_stage_of(obj, text, mode)
    tools = "the binutils in %s/bin, first on PATH" % pair if pair else "the system's binutils"
    if stage == "stage1":
        lines.append("The build failed during STAGE 1, with %s as the host compiler and %s.\n"
                     "  Stage 1 is the same work in every --mode, so changing --mode will NOT help.\n"
                     "  Stage-1 failures are nearly always the host toolchain (compiler or binutils), not GCC.\n"
                     "  Full log: %s" % (host_cc, tools, log_path))
    else:
        lines.append("The build failed in --mode %s, after stage 1%s.\n"
                     "  A bootstrap that clears stage 1 and then fails is usually the mode itself:\n"
                     "      build --mode plain --restart   (same options otherwise)\n"
                     "  Full log: %s" % (mode, " (in %s)" % stage if stage else "", log_path))
    return "\n".join(lines)


# ---------------------------------------------------------- configure and make lines

def gcc_configure_args(prefix, languages, with_tune, with_arch, cc, cxx, multilib, mode, pair="", extra=""):
    """build_gcc_universal.sh:414-435, in its order, plus the pairing with the built
    binutils (--with-as/--with-ld, its REALPATH) and --enable-host-shared for jit.  CC and
    CXX are configure ARGUMENTS, not just environment: they are recorded in config.status
    and inherited by every prerequisite's sub-configure (430)."""
    a = ["--prefix=" + prefix, "--enable-languages=" + languages, "--enable-checking=release",
         "--enable-threads=posix", "--enable-__cxa_atexit", "--enable-lto", "--enable-plugin",
         "--enable-default-pie", "--with-system-zlib", "--with-linker-hash-style=gnu", "--disable-werror"]
    if with_tune:
        a.append("--with-tune=" + with_tune)
    a += ["CC=" + cc, "CXX=" + cxx]
    if with_arch:
        a.append("--with-arch=" + with_arch)
    if not multilib:
        a.append("--disable-multilib")
    if mode == "quick":
        a.append("--disable-bootstrap")
    if pair:
        a += ["--with-as=" + os.path.join(pair, "bin", "as"), "--with-ld=" + os.path.join(pair, "bin", "ld")]
    if "jit" in [x.strip() for x in languages.split(",")]:
        a.append("--enable-host-shared")
    return a + shlex.split(extra or "")


def gcc_make_args(mode, jobs, boot):
    """build_gcc_universal.sh:442-457."""
    m = MODES[mode]
    a = ["make", "-j%d" % jobs]
    if m["config"]:
        a.append("BUILD_CONFIG=" + m["config"])
    if boot:
        a += ["BOOT_CFLAGS=-O2 -g " + boot, "BOOT_CXXFLAGS=-O2 -g " + boot]
    if m["target"]:
        a.append(m["target"])
    return a


def binutils_cflags(tune, arch):
    """reboot.txt:5-7: CFLAGS/CXXFLAGS="-g -O3 -march=native" for the binutils binaries.
    native (default) | portable | an ISA level or CPU (x86-64-v3, znver4) | raw flags."""
    tune = (tune or "native").strip()
    if tune in ("portable", "generic", "none"):
        return "-g -O3"
    if tune.startswith("-"):
        return "-g -O3 " + tune
    if tune == "native" and arch.startswith(("ppc", "powerpc")):
        return "-g -O3 -mcpu=native"                     # POWER has no -march
    return "-g -O3 -march=" + tune


def binutils_configure_args(prefix, gold, cflags, cc, cxx, extra=""):
    """binutils-gcc-notes-reboot.txt:3 exactly, plus the flags of :5-7 as configure ARGUMENTS
    (recorded in config.status, never inherited from a shell).  --enable-gold, never
    --enable-gold=default: that would make gold the prefix's `ld`."""
    a = ["--prefix=" + prefix, "--enable-plugins", "--enable-lto"]
    if gold:
        a.append("--enable-gold")
    a += ["CC=" + cc, "CXX=" + cxx, "CFLAGS=" + cflags, "CXXFLAGS=" + cflags]
    return a + shlex.split(extra or "")


def disk_needed(component, mode, track):
    if component == "binutils":
        return BINUTILS_DISK
    return MODES[mode]["disk"] + (GIT_EXTRA_DISK if track == "git" else 0)


def tree_languages(src):
    """The language names THIS tree has (gcc/*/config-lang.in `language=`)."""
    names = set()
    for p in glob.glob(os.path.join(src, "gcc", "*", "config-lang.in")):
        m = re.search(r"^language=\"?([\w+-]+)\"?", read_text(p), re.M)
        if m:
            names.add(m.group(1))
    return names


def check_languages(languages, src):
    known = tree_languages(src)
    if not known:
        return
    bad_ = [l for l in languages.split(",") if l and l not in known | set(["all", "default", "lto"])]
    if bad_:
        raise ToolError("--languages: %s not in this tree (it has: %s)" % (", ".join(bad_), ", ".join(sorted(known))))


# ----------------------------------------------------------------- choosing tools

def pick_host(opts, machine):
    """The compiler that bootstraps: /usr/bin/gcc, else gcc on the scrubbed PATH (the .sh,
    262-266) -- the combination upstream tests against.  --host-cc / --host-cxx override."""
    forced = opts["host_cc"]
    if forced:
        path = os.path.abspath(os.path.expanduser(forced)) if os.sep in forced else \
            (which(forced) or shutil.which(forced) or forced)
        ci = compiler_info(path)
        if not ci:
            raise ToolError("--host-cc %s is not a working gcc or clang" % forced)
        describe_host(path, ci)
    else:
        ci = None
        for cand in ("/usr/bin/gcc", which("gcc")):
            c = compiler_info(cand) if cand else None
            if c and c["kind"] == "gcc":
                ci = describe_host(cand, c)
                break
        if not ci:
            return None
        if not ci["cxx"]:
            ci["cxx"] = which("g++") or ""
    if opts["host_cxx"]:
        ci["cxx"] = os.path.abspath(os.path.expanduser(opts["host_cxx"]))
        if not os.access(ci["cxx"], os.X_OK):
            raise ToolError("--host-cxx %s is not executable" % ci["cxx"])
    if forced and not ci["cxx"]:
        raise ToolError("no C++ partner beside %s -- name it with --host-cxx" % ci["path"])
    return ci


def host_warnings(host, environ=None):
    """build_gcc_universal.sh:73-86, 276-281."""
    environ = os.environ if environ is None else environ
    lines = []
    cc = environ.get("CC", "")
    if host and cc and cc != host["path"] and (which(cc) or cc) != host["path"]:
        lines.append("ignoring CC=%s inherited from your environment.  Bootstrapping with %s instead: a "
                     "clang CC breaks GCC's prerequisite builds (their LTO probe goes through the system's "
                     "LLVMgold.so).  --host-cc %s if you genuinely want that." % (cc, host["path"], cc))
    if host and host["kind"] == "clang":
        lines.append("%s is clang.  It compiles GCC's own sources, but the in-tree GMP/MPFR/ISL probe for "
                     "LTO through the SYSTEM's LLVMgold.so, and a clang newer than that plugin fails stage 1 "
                     "with 'Invalid record' / 'Link Time Optimisation is not supported'.  A gcc is the safe "
                     "host." % host["path"])
    return lines


def host_identity(host):
    """What the record keeps of the host compiler: kind and version, not the banner (which
    changes with every distribution rebuild and must not make a build impossible to resume)."""
    return "%s %s" % (host["kind"], vstr(host["version"])) if host else "none"


def resolve_pair(opts, opt_root):
    """-> (binutils prefix REALPATH or '', why).  gcc is configured --with-as/--with-ld = this
    one, so "that gcc/binutils" stays one pair: the realpath, not the -current link, so that
    re-pointing binutils-current later does not change what an installed gcc runs."""
    b = opts["binutils"] or "auto"
    if opts["component"] != "gcc" or b == "system":
        return "", "the system's binutils" + (" (--binutils system)" if b == "system" else "")
    if b == "auto":
        cur = os.path.join(opt_root, "binutils-current")
        if runs(os.path.join(cur, "bin", "ld")) and runs(os.path.join(cur, "bin", "as")):
            real = os.path.realpath(cur)
            return real, "%s -> %s  (step 3's binutils)" % (cur, real)
        return "", ("the system's binutils: %s/bin/ld does not run (build --component binutils first, "
                    "or --binutils DIR)" % cur)
    d = os.path.abspath(os.path.expanduser(b))
    if os.path.basename(d) == "bin" and os.access(os.path.join(d, "as"), os.X_OK):
        d = os.path.dirname(d)
    if not all(os.access(os.path.join(d, "bin", t), os.X_OK) for t in ("as", "ld")):
        raise ToolError("--binutils %s: no bin/as and bin/ld there" % d)
    return os.path.realpath(d), "--binutils %s" % d


def with_tune_value(opt, arch):
    """--with-tune: 'auto' = native where GCC's config.gcc takes it (x86, aarch64), none
    elsewhere; 'none' = leave it out."""
    if opt in ("", "none"):
        return ""
    if opt == "auto":
        return "native" if arch in ("x86_64", "i686", "i586", "aarch64") else ""
    return opt


# ------------------------------------------------------------------------ the plan

STAGES = {
    "gcc": [("source", "the GCC source tree"),
            ("prereqs", "GMP, MPFR, MPC, ISL (+ gettext) in the tree: contrib/download_prerequisites"),
            ("configure", "configure, out of tree"),
            ("build", "make: the bootstrap"),
            ("check", "make -k check: the test suites (summarised, never fatal)"),
            ("install", "make install-strip into a staging directory, then one rename"),
            ("verify", "compile, link and run with what was installed"),
            ("activate", "<opt>/gcc-current")],
    "binutils": [("source", "the binutils source tree"),
                 ("configure", "configure, out of tree"),
                 ("build", "make"),
                 ("check", "make -k check (summarised, never fatal)"),
                 ("install", "make install into a staging directory, then one rename"),
                 ("verify", "assemble, link and run with what was installed"),
                 ("activate", "<opt>/binutils-current")]}
STAGE_ORDER = [n for n, _ in STAGES["gcc"]]
NEEDS = {"prereqs": ["source"], "configure": ["source", "prereqs"], "build": ["configure"],
         "check": ["build"], "install": ["build"], "verify": ["install"], "activate": ["verify"]}


def stage_names(component, opts):
    names = [n for n, _ in STAGES[component]]
    if component == "gcc" and opts["prereqs"] != "in-tree":
        names.remove("prereqs")
    if not opts["check"]:
        names.remove("check")
    return names


def stages_to_run(pipeline, is_done, has_product):
    """Every stage from the first one that has to run: one not done, or an earlier one a
    not-done stage needs whose product is gone (a cleaned build tree, a removed source).
    What follows a stage that runs is stale, so it runs too."""
    todo = set(n for n in pipeline if not is_done(n))
    changed = True
    while changed:
        changed = False
        for n in list(todo):
            for dep in NEEDS.get(n, []):
                if dep in pipeline and dep not in todo and not has_product(dep):
                    todo.add(dep)
                    changed = True
    if not todo:
        return []
    return pipeline[min(pipeline.index(n) for n in todo):]


class Ctx(object):
    """Everything a stage needs, resolved once."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def log(self, name):
        return os.path.join(self.root, "logs", name + ".log")

    def env(self):
        return scrubbed_env([os.path.join(self.pair, "bin")] if self.pair else None)

    def configure_args(self):
        o = self.opts
        if self.component == "gcc":
            return gcc_configure_args(self.prefix, o["languages"], self.with_tune, o["with_arch"],
                                      self.host["path"], self.host["cxx"], o["multilib"], o["mode"],
                                      self.pair, o["configure_extra"])
        return binutils_configure_args(self.prefix, self.gold, self.cflags, self.host["path"], self.host["cxx"],
                                       o["configure_extra"])

    def make_args(self):
        if self.component == "gcc":
            return gcc_make_args(self.opts["mode"], self.jobs, self.boot)
        return ["make", "-j%d" % self.jobs]

    def product(self):
        """What a finished `make` leaves in the build tree."""
        return os.path.join(self.obj, "gcc", "xgcc") if self.component == "gcc" else \
            os.path.join(self.obj, "ld", "ld-new")


def default_prefix(opt_root, component, ident):
    return os.path.join(opt_root, "%s-%s" % (component, ident))


def resolve(opts, machine, strict=True, get=None, exists=None, host=None):
    """Options + machine -> Ctx.  Nothing is downloaded; the network is asked only to turn
    an `auto` version into a number.  strict=False (plan, doctor): an unresolvable version is
    recorded in ctx.unresolved instead of raised."""
    workdir = os.path.abspath(os.path.expanduser(opts["workdir"]))
    opt_root = os.path.abspath(os.path.expanduser(opts["opt_root"]))
    comp = opts["component"]
    unresolved = ""
    try:
        src = gcc_source(opts, workdir, get, exists) if comp == "gcc" else binutils_source(opts, workdir, get)
    except ToolError as e:
        if strict:
            raise
        src, unresolved = None, str(e)
    host = host if host is not None else pick_host(opts, machine)
    pair, pair_why = resolve_pair(opts, opt_root)
    ident = src["id"] if src else "<version>"            # unresolved: shown as a placeholder, never built
    root = os.path.join(workdir, "build", "%s-%s" % (comp, ident))
    prefix = os.path.abspath(os.path.expanduser(opts["prefix"])) if opts["prefix"] else \
        default_prefix(opt_root, comp, "<BASE-VER>-git-<sha>" if src and src["kind"] == "git" else ident)
    gold = bool(opts["gold"]) and comp == "binutils"
    return Ctx(opts=opts, machine=machine, component=comp, src=src, unresolved=unresolved, id=ident,
               workdir=workdir, opt_root=opt_root, root=root, obj=os.path.join(root, "obj"), prefix=prefix,
               host=host, pair=pair, pair_why=pair_why, jobs=opts["jobs"] or machine["cpus"],
               boot="" if opts["boot_arch"].strip() in ("", "none") else opts["boot_arch"].strip(),
               with_tune=with_tune_value(opts["with_tune"], machine["arch"]),
               cflags=binutils_cflags(opts["binutils_tune"], machine["arch"]), gold=gold,
               verbose=opts["verbose"], state=None, fresh=True)


def fingerprint(ctx):
    """Everything that reaches configure or make: a build directory started with other
    values cannot be continued with these."""
    o = ctx.opts
    prefix = os.path.abspath(os.path.expanduser(o["prefix"])) if o["prefix"] else "auto:" + ctx.opt_root
    fp = {"component": ctx.component, "id": ctx.src["id"] if ctx.src else ctx.id, "host": host_identity(ctx.host),
          "configure_extra": o["configure_extra"], "prefix": prefix}
    if ctx.component == "gcc":
        fp.update(mode=o["mode"], languages=o["languages"], multilib=bool(o["multilib"]), boot=ctx.boot,
                  with_arch=o["with_arch"], with_tune=ctx.with_tune, binutils=ctx.pair or "system",
                  prereqs=o["prereqs"])
    else:
        fp.update(gold=ctx.gold, cflags=ctx.cflags)
    return fp


def fingerprint_diff(then, now_):
    return ["%-16s then %s, now %s" % (k, json.dumps(then.get(k)), json.dumps(now_.get(k)))
            for k in sorted(set(then) | set(now_)) if then.get(k) != now_.get(k)]


# -------------------------------------------------------------------- the stages

def stage_source(ctx):
    s = ctx.src
    if s["kind"] == "local":
        ok("using %s as it is" % s["dir"])
    elif s["kind"] == "git":
        git_fetch(s["dir"], s["ref"], ctx.opts["git_url"], ctx.fresh, ctx.log("source"))
    else:
        srcroot = os.path.dirname(s["dir"])
        with locked(srcroot):                                # two instances, one download
            if os.path.isfile(os.path.join(s["dir"], "configure")):
                ok("reusing the source already at %s" % s["dir"])
            else:
                fetch_tarball(s["urls"], s["tarball"], s["sum_dirs"])
                info("unpacking %s" % os.path.basename(s["tarball"]))
                extract(s["tarball"], s["dir"])
    if not os.path.isfile(os.path.join(s["dir"], "configure")):
        raise ToolError("the unpacked tree at %s has no configure -- an unexpected archive layout" % s["dir"])
    if ctx.component == "gcc" and not gcc_tree_version(s["dir"])[0]:
        raise ToolError("%s is not a GCC tree (no gcc/BASE-VER)" % s["dir"])
    if s["kind"] == "git":
        refine_git_id(ctx)


def refine_git_id(ctx):
    """build_gcc_universal.sh:359-363, 376-382: the prefix is only knowable after a checkout,
    so the collision check lives here."""
    ctx.id = git_id(ctx.src["dir"], ctx.opts["series"])
    info("the tree is %s" % ctx.id)
    if not ctx.opts["prefix"]:
        ctx.prefix = default_prefix(ctx.opt_root, "gcc", ctx.id)
    if ctx.state is not None:
        ctx.state.d.update(id=ctx.id, prefix=ctx.prefix)
        ctx.state.save()
    if os.path.lexists(ctx.prefix):
        raise ToolError("%s already exists.  Never install over a prefix that something may be using.  "
                        "Remove it, or pass --prefix NEW." % ctx.prefix)


PREREQ_MARK = ".%s-prereqs-unpacking" % TOOL     # in the GCC tree while download_prerequisites runs


def prereq_package_dir(archive):
    """gmp-6.2.1.tar.bz2 -> gmp-6.2.1: what download_prerequisites unpacks it to (its own
    ${ar%.tar*}), and what its <name> link points at."""
    return re.sub(r"\.tar(\.[A-Za-z0-9]+)?$", "", os.path.basename(archive))


def clear_prereq_trees(src, wanted):
    """Remove the unpacked GMP/MPFR/MPC/ISL/gettext directories and their links, keep the
    archives.  -> what was removed.  Links are unlinked, never followed."""
    removed = []
    for name, archive in wanted:
        for rel in (name, prereq_package_dir(archive)):
            if not rel or rel.startswith(".") or os.sep in rel:
                continue
            path = os.path.join(src, rel)
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                continue
            removed.append(rel)
    return removed


def stage_prereqs(ctx, fetch=None):
    """download_prerequisites unpacks an archive only when its directory is not there yet
    ([ -e "$directory/$package" ] || tar -xf ...), so an unpack cut short (^C, SIGTERM, a
    crash) would be taken as whole by the next run and fail hours later in configure or stage
    1.  A mark in the tree says an unpack is under way: found at the start, what it unpacked
    is removed first (the checked archives stay) and the script unpacks again."""
    src = ctx.src["dir"]
    script = os.path.join(src, "contrib", "download_prerequisites")
    if not os.path.isfile(script):
        raise ToolError("%s has no contrib/download_prerequisites.\n%s" % (src, PREREQ_ADVICE))
    wanted = parse_prereq_script(read_text(script))
    names = [n for n, _ in wanted]
    mark = os.path.join(src, PREREQ_MARK)
    if os.path.exists(mark):
        removed = clear_prereq_trees(src, wanted)
        warn("an earlier unpack of the prerequisites was cut short: removed %s; the archives are kept and "
             "unpacked again" % (" ".join(removed) or "nothing (none was unpacked yet)"))
    elif wanted and not prereqs_missing(src, names):
        ok("prerequisites already in the tree: %s" % " ".join(names))
        return
    sums = parse_sums(read_text(os.path.join(src, "contrib", "prerequisites.sha512")))
    try:
        prefetch_prereqs(src, wanted, sums, fetch)
    except ToolError as e:
        raise ToolError("%s\n%s" % (e, PREREQ_ADVICE))
    log = ctx.log("prereqs")
    write_atomic(mark, "%s: download_prerequisites unpacking since %s (pid %d)\n" % (TOOL, now(), os.getpid()))
    rc, _ = run_logged(["./contrib/download_prerequisites"], src, scrubbed_env(), log, "download_prerequisites",
                       ctx.verbose)
    missing = prereqs_missing(src, names)
    if rc != 0 or missing:
        raise ToolError("contrib/download_prerequisites failed (exit %d%s) -- see %s\n%s\n%s"
                        % (rc, ", still missing: " + " ".join(missing) if missing else "", log,
                           tail(read_text(log), 8), PREREQ_ADVICE))
    os.unlink(mark)
    ok("in the tree: %s" % " ".join(names))


def stage_configure(ctx):
    src, obj = ctx.src["dir"], ctx.obj
    rs, ro = os.path.realpath(src), os.path.realpath(obj)
    if ro == rs or ro.startswith(rs + os.sep):
        raise ToolError("the build directory %s is inside the source tree: GCC requires it outside" % obj)
    if ctx.component == "gcc":
        check_languages(ctx.opts["languages"], src)
        if "jit" in ctx.opts["languages"].split(","):
            note("--languages jit: configured --enable-host-shared (the compiler is then a little slower)")
    if os.path.isdir(obj):
        shutil.rmtree(obj)                                   # the .sh's rm -rf "$BUILD" (412): a configure starts clean
    os.makedirs(obj)
    args = ctx.configure_args()
    log = ctx.log("configure")
    rc, _ = run_logged([os.path.join(src, "configure")] + args, obj, ctx.env(), log, "configure", ctx.verbose)
    if rc != 0 or not os.path.isfile(os.path.join(obj, "Makefile")):
        raise ToolError("configure %s -- see %s\n%s" % ("failed (exit %d)" % rc if rc else "produced no Makefile", log,
                                                       "\n".join(error_summary(log, 6)) or tail(read_text(log), 10)))
    ctx.state.d["configure"] = args
    ctx.state.save()


def stage_build(ctx):
    if ctx.component == "gcc":
        m = MODES[ctx.opts["mode"]]
        info("MODE=%s  target=%s  BUILD_CONFIG=%s" % (ctx.opts["mode"], m["target"] or "all", m["config"] or "none"))
        info("This is the long part.  profiled-lto compiles GCC several times on purpose; that is what makes "
             "the final binary fast.")
    log = ctx.log("build")
    rc, _ = run_logged(ctx.make_args(), ctx.obj, ctx.env(), log, "make", ctx.verbose,
                       stage_file=os.path.join(ctx.obj, "stage_current"))
    if rc != 0:
        raise ToolError(failure_report(ctx.component, ctx.obj, log, ctx.opts["mode"], ctx.host["path"] if ctx.host else "?",
                                       ctx.pair, ctx.jobs))
    if not os.path.exists(ctx.product()):
        raise ToolError("make exited 0 but %s is not there -- see %s" % (ctx.product(), log))


def check_summary(text):
    """DejaGnu's totals, added up over every summary in the log."""
    totals = {}
    for m in re.finditer(r"^# of ([\w ]+?)\s+(\d+)\s*$", text or "", re.M):
        totals[m.group(1)] = totals.get(m.group(1), 0) + int(m.group(2))
    return totals


def stage_check(ctx):
    log = ctx.log("check")
    rc, secs = run_logged(["make", "-k", "check", "-j%d" % ctx.jobs], ctx.obj, ctx.env(), log, "make check", ctx.verbose)
    totals = check_summary(read_text(log))
    for k in sorted(totals):
        info("%-28s %d" % (k, totals[k]))
    if rc != 0 or totals.get("unexpected failures"):
        warn("the test suites report failures (exit %d) -- not fatal; see %s" % (rc, log))
    ctx.state.d["check"] = totals
    ctx.state.save()


def install_record(ctx):
    o = ctx.opts
    rec = {"tool": TOOL, "tool_version": VERSION, "component": ctx.component, "id": ctx.id,
           "source": ctx.src["label"] if ctx.src else "", "host": ctx.host["banner"] if ctx.host else "",
           "configure": ctx.state.d.get("configure") or ctx.configure_args(), "built": now(), "verify": "unverified"}
    if ctx.component == "gcc":
        rec.update(version=gcc_tree_version(ctx.src["dir"])[0], track=o["track"], mode=o["mode"],
                   languages=o["languages"], multilib=bool(o["multilib"]), boot_arch=ctx.boot,
                   with_arch=o["with_arch"], with_tune=ctx.with_tune, binutils=ctx.pair)
    else:
        rec.update(version=ctx.id, gold=ctx.gold, cflags=ctx.cflags)
    return rec


def build_info_text(rec):
    lines = ["Built by %s on %s" % (rec.get("tool_version") and "%s %s" % (TOOL, rec["tool_version"]) or TOOL,
                                    rec.get("built", "?"))]
    for k in ("component", "id", "version", "track", "mode", "languages", "multilib", "boot_arch", "with_arch",
              "with_tune", "binutils", "gold", "cflags", "host", "source"):
        if k in rec:
            lines.append("%-10s: %s" % (k, rec[k] if rec[k] not in ("", None) else "-"))
    lines.append("configure : %s" % " ".join(shlex.quote(a) for a in rec.get("configure", [])))
    return "\n".join(lines) + "\n"


def do_install(prefix, obj, env, log, rec, main_rel, strip=True, verbose=False):
    """make install-strip (else make install, build_gcc_universal.sh:480) with DESTDIR = a
    staging directory BESIDE the prefix, then ONE rename: the prefix is either complete or
    not there, and an existing one is never installed over.  Serial: a parallel install
    races."""
    if os.path.lexists(prefix):
        raise ToolError("%s already exists.  Never install over a prefix that something may be using.  "
                        "Remove it, or pass --prefix NEW." % prefix)
    parent = os.path.dirname(prefix)
    os.makedirs(parent, exist_ok=True)
    staging = os.path.join(parent, ".%s-staging-%s" % (TOOL, os.path.basename(prefix)))
    staged = staging + prefix
    targets = ["install-strip", "install"] if strip else ["install"]
    try:
        for i, target in enumerate(targets):
            shutil.rmtree(staging, ignore_errors=True)
            rc, _ = run_logged(["make", target, "DESTDIR=" + staging], obj, env, log, "make " + target, verbose)
            if rc == 0:
                break
            if i + 1 < len(targets):
                warn("make %s failed: the staging directory is wiped and plain `make install` tried" % target)
        else:
            raise ToolError("make install failed (exit %d) -- see %s\n%s" % (rc, log, "\n".join(error_summary(log, 8))))
        if not os.access(os.path.join(staged, main_rel), os.X_OK):
            raise ToolError("the install put no %s under %s -- see %s" % (main_rel, staged, log))
        write_atomic(os.path.join(staged, MARKER), json.dumps(rec, indent=1, sort_keys=True) + "\n")
        write_atomic(os.path.join(staged, "BUILD-INFO.txt"), build_info_text(rec))
        size = dir_size(staged)
        try:
            os.rename(staged, prefix)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            tmp = "%s.partial-%d" % (prefix, os.getpid())      # another filesystem: copy, then rename
            try:
                shutil.copytree(staged, tmp, symlinks=True)
                os.rename(tmp, prefix)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    ok("installed: %s  (%s)" % (prefix, human(size)))


def stage_install(ctx):
    main_rel = os.path.join("bin", "gcc" if ctx.component == "gcc" else "ld")
    strip = ctx.opts["strip"] and ctx.component == "gcc"     # binutils keeps the -g its CFLAGS asked for
    do_install(ctx.prefix, ctx.obj, ctx.env(), ctx.log("install"), install_record(ctx), main_rel, strip, ctx.verbose)


# ------------------------------------------------------------------------- verify

class VerifySkipped(Exception):
    """A check that cannot be made here: said, not counted."""


class Checks(object):
    def __init__(self):
        self.results = []

    def check(self, name, fn):
        try:
            detail = fn()
            ok("%-44s %s" % (name, detail or ""))
            self.results.append((name, True))
        except VerifySkipped as e:
            note("%-44s skipped (%s)" % (name, e))
        except (ToolError, OSError, AssertionError) as e:
            bad("%-44s %s" % (name, (str(e).strip().splitlines() or ["failed"])[-1][:200]))
            self.results.append((name, False))

    def failed(self):
        return [n for n, good in self.results if not good]


CXX20_SUM = """#include <string>
#include <vector>
#include <numeric>
#include <cstdio>
int main() {
    std::vector<int> v(10); std::iota(v.begin(), v.end(), 1);
    std::string s = "sum=" + std::to_string(std::accumulate(v.begin(), v.end(), 0));
    std::puts(s.c_str());
    return s == "sum=55" ? 0 : 1;
}
"""
C_HELLO = '#include <stdio.h>\nint main(void){puts("ok-c");return 0;}\n'
LANG_HELLO = {   # language -> (driver, file name, source, extra flags, expected)
    "fortran": ("gfortran", "h.f90", "program h\n  print '(a)', 'ok-fortran'\nend program h\n", [], "ok-fortran"),
    "d": ("gdc", "h.d", 'import std.stdio;\nvoid main(){ writeln("ok-d"); }\n', [], "ok-d"),
    "go": ("gccgo", "h.go", 'package main\nimport "fmt"\nfunc main(){ fmt.Println("ok-go") }\n', [], "ok-go"),
    "objc": ("gcc", "h.m", '#include <stdio.h>\nint main(void){puts("ok-objc");return 0;}\n', [], "ok-objc"),
}


def default_march(help_text):
    """`gcc -Q --help=target` -> the baked-in -march ('' when the output has none)."""
    m = re.search(r"^\s*-march=\s+(\S+)", help_text or "", re.M)
    return m.group(1) if m else ""


def same_file(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b) or os.path.samefile(a, b)
    except OSError:
        return False


def verify_gcc(prefix, rec=None, scratch=None):
    """build_gcc_universal.sh:485-519 and the checks added here.  -> Checks."""
    rec = rec or read_json(os.path.join(prefix, MARKER)) or {}
    b = lambda n: os.path.join(prefix, "bin", n)
    libdirs = [os.path.join(prefix, "lib64"), os.path.join(prefix, "lib")]
    rpath = ["-Wl,-rpath," + d for d in libdirs]
    env = scrubbed_env()
    env_ld = scrubbed_env(extra={"LD_LIBRARY_PATH": ":".join(libdirs)})
    pair = rec.get("binutils") or ""
    d = tempfile.mkdtemp(prefix="egt-verify-", dir=scratch)   # beside the build, not under a /tmp that may be noexec
    c = Checks()

    def put(name, text):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def build_run(cmd, expect, exe="a.out", run_env=None):
        exe = os.path.join(d, exe)
        if os.path.exists(exe):
            os.unlink(exe)
        run(cmd + ["-o", exe], env=env, cwd=d, timeout=600)
        o = (run([exe], env=run_env or env, cwd=d, timeout=120).stdout or "").strip()
        assert o == expect, "ran, but printed %r, expected %r" % (o[:80], expect)
        return expect
    try:
        say("Verifying %s" % prefix)

        def present():
            for n in ("gcc", "g++"):
                assert os.access(b(n), os.X_OK), "no %s" % b(n)
            return run([b("gcc"), "--version"], env=env).stdout.splitlines()[0]
        c.check("gcc and g++ installed", present)
        t = put("t.cc", CXX20_SUM)
        c.check("C++20 compile + link + run (rpath)", lambda: build_run(
            [b("g++"), "-std=c++20", "-O2", t] + rpath, "sum=55", "t", env_ld))

        def rpath_only():
            exe = os.path.join(d, "t")
            if not os.path.exists(exe):
                raise VerifySkipped("the C++20 program was not built")
            o = (run([exe], env=env, timeout=60).stdout or "").strip()
            assert o == "sum=55", "without LD_LIBRARY_PATH it printed %r" % o[:80]
            if which("ldd"):
                libs = run(["ldd", exe], env=env, check=False).stdout or ""
                m = re.search(r"libstdc\+\+\.so\.\d+ => (\S+)", libs)
                assert m and os.path.realpath(m.group(1)).startswith(os.path.realpath(prefix) + os.sep), \
                    "its libstdc++ is %s, not this prefix's" % (m.group(1) if m else "?")
                return "libstdc++ from %s" % os.path.dirname(m.group(1))
            return "runs"
        c.check("the same program with the rpath alone", rpath_only)
        c.check("C: compile, link, run", lambda: build_run([b("gcc"), put("h.c", C_HELLO)], "ok-c"))
        c.check("-O2 -flto: compile, link, run", lambda: build_run(
            [b("g++"), "-std=c++20", "-O2", "-flto", t] + rpath, "sum=55"))

        def probe():
            good, why = compiler_works(b("g++"), scratch=d, flags=rpath)
            assert good, why
            return "a C++17 program with a data blob, in a clean environment"
        c.check("finds its own assembler (.base64 data)", probe)

        def paired():
            if not pair:
                raise VerifySkipped("not paired: it uses the system's binutils")
            got = []
            for tool in ("as", "ld"):
                p = (run([b("gcc"), "-print-prog-name=" + tool], env=env).stdout or "").strip()
                assert same_file(p, os.path.join(pair, "bin", tool)), "%s is %s, not %s/bin/%s" % (tool, p, pair, tool)
                got.append(p)
            return " ".join(got)
        c.check("as and ld are the paired binutils", paired)

        def gold():
            gold_dir = os.path.join(pair, "bin") if pair else "/usr/bin"
            if not os.access(os.path.join(gold_dir, "ld.gold"), os.X_OK):
                raise VerifySkipped("no ld.gold in %s" % gold_dir)
            return build_run([b("gcc"), "-fuse-ld=gold", put("g.c", C_HELLO)], "ok-c")
        c.check("-fuse-ld=gold: link, run", gold)

        def march():
            want = rec.get("with_arch") or ""
            if not want:
                raise VerifySkipped("--with-arch was not set: the portable default")
            got = default_march(run([b("gcc"), "-Q", "--help=target"], env=env).stdout)
            if want == "native":
                assert got, "no -march in gcc -Q --help=target"
                return "native, which is %s here" % got
            assert got == want, "the default -march is %r, not %r" % (got, want)
            return got
        c.check("the baked-in default -march", march)

        def provenance():
            if rec.get("mode") == "quick":
                raise VerifySkipped("--mode quick: built by the host compiler, no bootstrap")
            if not which("readelf"):
                raise VerifySkipped("no readelf")
            cc1plus = (run([b("gcc"), "-print-prog-name=cc1plus"], env=env).stdout or "").strip()
            o = run(["readelf", "-p", ".comment", cc1plus], env=env, check=False).stdout or ""
            if "GCC:" not in o:
                raise VerifySkipped("no .comment section to read in %s" % cc1plus)
            v = (run([b("gcc"), "-dumpfullversion"], env=env).stdout or "").strip()
            assert ("GCC: (GNU) " + v) in o, "cc1plus was not built by GCC %s itself: %s" % (
                v, "; ".join(sorted(set(re.findall(r"GCC: \([^)]*\) [\d.]+", o)))))
            return "cc1plus carries 'GCC: (GNU) %s'" % v
        c.check("the compiler was built by itself (bootstrap)", provenance)
        langs = [x.strip() for x in (rec.get("languages") or "").split(",")]
        for lang in ("fortran", "d", "go", "objc"):
            if lang not in langs and "all" not in langs:
                continue
            drv, fn, text, flags, expect = LANG_HELLO[lang]

            def hello(drv=drv, fn=fn, text=text, flags=flags, expect=expect, named=lang in langs):
                if not os.access(b(drv), os.X_OK):
                    if not named:
                        raise VerifySkipped("--languages all did not build %s here" % drv)
                    raise AssertionError("no %s installed" % b(drv))
                return build_run([b(drv), put(fn, text)] + rpath + flags, expect, run_env=env_ld)
            c.check("%s: compile, link, run" % lang, hello)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return c


def verify_binutils(prefix, cc="/usr/bin/gcc", rec=None, scratch=None):
    """GNU tools that say so; gold when it was built; the plugin; and the system's gcc
    assembling and linking through THIS as/ld (-B), the program then run.  -> Checks."""
    rec = rec or read_json(os.path.join(prefix, MARKER)) or {}
    bindir = os.path.join(prefix, "bin")
    b = lambda n: os.path.join(bindir, n)
    env = scrubbed_env()
    d = tempfile.mkdtemp(prefix="egt-verify-", dir=scratch)
    c = Checks()
    B = ["-B", bindir + os.sep]

    def build_run(cmd, expect):
        exe = os.path.join(d, "a.out")
        if os.path.exists(exe):
            os.unlink(exe)
        src = os.path.join(d, "h.c")
        with open(src, "w") as f:
            f.write(C_HELLO)
        run(cmd + [src, "-o", exe], env=env, cwd=d, timeout=300)
        o = (run([exe], env=env, cwd=d, timeout=60).stdout or "").strip()
        assert o == expect, "ran, but printed %r" % o[:80]
        return expect
    try:
        say("Verifying %s" % prefix)

        def gnu_tools():
            vers = set()
            for t in ("as", "ld", "ld.bfd", "ar", "nm", "objdump", "readelf", "strip"):
                assert os.access(b(t), os.X_OK), "no %s" % b(t)
                line = (run([b(t), "--version"], env=env).stdout or "").splitlines()[0]
                assert "GNU" in line, "%s --version: %s" % (t, line)
                vers.add(vstr(numver(re.sub(r"^.*\)\s*", "", line)) or numver(line)))
            return "as ld ld.bfd ar nm objdump readelf strip: GNU %s" % " ".join(sorted(vers))
        c.check("the GNU tools run", gnu_tools)

        def gold():
            if not os.access(b("ld.gold"), os.X_OK):
                if rec.get("gold"):
                    raise AssertionError("gold was asked for, and there is no %s" % b("ld.gold"))
                raise VerifySkipped("built without gold")
            line = (run([b("ld.gold"), "--version"], env=env).stdout or "").splitlines()[0]
            assert "GNU gold" in line, line
            return line
        c.check("ld.gold", gold)
        def plugin():
            assert "-plugin" in (run([b("ld"), "--help"], env=env).stdout or ""), \
                "ld --help lists no -plugin: built without --enable-plugins?"
            return "yes"
        c.check("ld supports plugins (-plugin)", plugin)
        if not cc or not os.access(cc, os.X_OK):
            note("no C compiler to link test programs with: the link checks are skipped")
        else:
            def prog_names():
                got = []
                for t in ("as", "ld"):
                    p = (run([cc] + B + ["-print-prog-name=" + t], env=env).stdout or "").strip()
                    assert same_file(p, b(t)), "%s -B %s finds %s, not %s" % (cc, bindir, p, b(t))
                    got.append(p)
                return " ".join(got)
            c.check("%s -B finds this as and ld" % os.path.basename(cc), prog_names)
            c.check("C: assemble + link with them, run", lambda: build_run([cc] + B, "ok-c"))

            def gold_link():
                if not os.access(b("ld.gold"), os.X_OK):
                    raise VerifySkipped("built without gold")
                return build_run([cc] + B + ["-fuse-ld=gold"], "ok-c")
            c.check("-fuse-ld=gold: link, run", gold_link)
            c.check("-O2 -flto through the plugin: link, run", lambda: build_run([cc] + B + ["-O2", "-flto"], "ok-c"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return c


def set_verify_mark(prefix, verdict):
    path = os.path.join(prefix, MARKER)
    rec = read_json(path)
    if rec is not None and rec.get("tool") == TOOL:
        rec["verify"] = verdict
        rec["verified"] = now()
        try:
            write_atomic(path, json.dumps(rec, indent=1, sort_keys=True) + "\n")
        except OSError as e:
            warn("cannot record the verification in %s: %s" % (path, e))


def stage_verify(ctx):
    rec = read_json(os.path.join(ctx.prefix, MARKER))
    if ctx.component == "gcc":
        c = verify_gcc(ctx.prefix, rec, ctx.root)
    else:
        c = verify_binutils(ctx.prefix, ctx.host["path"] if ctx.host else which("gcc"), rec, ctx.root)
    failed = c.failed()
    set_verify_mark(ctx.prefix, "failed" if failed else "ok")
    if failed:
        raise ToolError("%d of %d checks failed: %s\n  Installed in %s; NOT linked as %s-current."
                        % (len(failed), len(c.results), ", ".join(failed), ctx.prefix, ctx.component))
    ok("all %d checks passed" % len(c.results))


def link_current(opt_root, component, prefix):
    """<opt>/<component>-current -> prefix, atomically: a symlink beside it, renamed over.
    A real directory of that name is never touched.  -> True when linked."""
    current = os.path.join(opt_root, "%s-current" % component)
    if os.path.isdir(current) and not os.path.islink(current):
        warn("%s is a real directory, not a link: left alone.  Move it aside, then:  ln -sfn %s %s"
             % (current, prefix, current))
        return False
    os.makedirs(opt_root, exist_ok=True)
    tmp = current + ".new"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(prefix, tmp)
    os.replace(tmp, current)
    ok("%s -> %s" % (current, prefix))
    return True


def stage_activate(ctx):
    if ctx.opts["link_current"] is False:
        note("not linked (--no-link-current):  ln -sfn %s %s"
             % (ctx.prefix, os.path.join(ctx.opt_root, ctx.component + "-current")))
        ctx.state.d["linked"] = False
        return
    ctx.state.d["linked"] = link_current(ctx.opt_root, ctx.component, ctx.prefix)


def advice(component, prefix, opt_root, linked):
    """build_gcc_universal.sh:529-547 -- through the -current link when there is one, so the
    next rebuild is one ln and not an edit of every shell profile."""
    base = os.path.join(opt_root, component + "-current") if linked else prefix
    if component == "binutils":
        return ["Installed: %s" % prefix, "",
                "To use it, put it ahead of the system's binutils:",
                "",
                "    export PATH=\"%s/bin:$PATH\"" % base,
                "",
                "or hand it to one compile:   gcc -B %s/bin/ ..." % base,
                "`--component gcc` pairs the next GCC with it (--binutils auto reads binutils-current)."]
    return ["Installed: %s" % prefix, "",
            "To use it, put it ahead of the system compiler:",
            "",
            "    export PATH=\"%s/bin:$PATH\"" % base,
            "    export LD_LIBRARY_PATH=\"%s/lib64:%s/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\"" % (base, base),
            "",
            "That LD_LIBRARY_PATH matters.  Programs you build with this gcc link against its newer",
            "libstdc++, and at run time the dynamic loader must find it -- the system one is older and",
            "will refuse with a GLIBCXX_x.y.z version error.  The alternative, if you would rather not",
            "set it globally, is to bake the path into each binary at link time:",
            "",
            "    g++ -Wl,-rpath,%s/lib64 ..." % base]


# ------------------------------------------------------------------ options / CLI

class UsageError(ToolError):
    """A wrong option: exit 2, like argparse's own."""


DEFAULTS = {"component": "gcc", "track": "release", "series": "auto", "git_ref": "master", "git_url": GCC_GIT,
            "gcc_version": "auto", "binutils_version": "auto", "source": "", "prefix": "",
            "workdir": "~/src/enterprise_gcc_tool", "opt_root": "~/opt", "mode": "profiled-lto",
            "languages": "c,c++,lto", "multilib": False, "boot_arch": "-march=native -mtune=native",
            "with_arch": "", "with_tune": "auto", "host_cc": "", "host_cxx": "", "binutils": "auto",
            "gold": True, "binutils_tune": "native", "configure_extra": "", "prereqs": "in-tree", "jobs": 0,
            "link_current": None, "strip": True, "check": False, "keep_build": False,
            "install_deps": False, "yes": False, "verbose": False, "ignore_disk": False,
            "resume": False, "restart": False, "dry_run": False, "build_id": "",
            "profile": "", "save_profile": ""}
# What a profile holds: the CHOICES.  Never --resume, --restart, --yes: those belong to one run.
PROFILE_KEYS = ("component", "track", "series", "git_ref", "git_url", "gcc_version", "binutils_version", "source",
                "prefix", "workdir", "opt_root", "mode", "languages", "boot_arch", "with_arch", "with_tune",
                "host_cc", "host_cxx", "binutils", "binutils_tune", "configure_extra", "prereqs", "jobs")
PROFILE_FLAGS = ("multilib", "gold", "link_current", "strip", "check", "keep_build")
PROFILE_HEADER = "# %s profile" % TOOL
PATH_KEYS = ("source", "prefix", "workdir", "opt_root", "host_cc", "host_cxx")


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
    return check_options(opts)


def check_options(opts):
    if opts["component"] not in COMPONENTS:
        raise UsageError("--component is gcc or binutils, not %r" % opts["component"])
    if opts["track"] not in TRACKS:
        raise UsageError("--track is release, snapshot or git, not %r (build_gcc_universal.sh's TRACK)" % opts["track"])
    if opts["mode"] not in MODES:
        raise UsageError("--mode is one of %s, not %r" % (" | ".join(MODE_ORDER), opts["mode"]))
    if opts["prereqs"] not in ("in-tree", "system"):
        raise UsageError("--prereqs is in-tree or system")
    if not (opts["series"] == "auto" or re.match(r"^\d+$", str(opts["series"]))):
        raise UsageError("--series is a GCC major number (17) or auto")
    v = opts["gcc_version"]
    if opts["component"] == "gcc" and v != "auto" and not opts["source"]:
        if opts["track"] == "release" and not re.match(r"^\d+\.\d+\.\d+$", v):
            raise UsageError("for --track release, --gcc-version looks like 16.2.0, got %r" % v)
        if opts["track"] == "snapshot" and not re.match(r"^\d+-\d{8}$", v):
            raise UsageError("for --track snapshot, --gcc-version looks like 17-20260823, got %r" % v)
        if opts["track"] == "git":
            raise UsageError("--track git builds --git-ref (a branch, tag or commit), not a --gcc-version")
    if opts["binutils_version"] != "auto" and not re.match(r"^\d+\.\d+(\.\d+)?$", opts["binutils_version"]):
        raise UsageError("--binutils-version looks like 2.46 (or auto), got %r" % opts["binutils_version"])
    if not re.match(r"^[a-z0-9+-]+(,[a-z0-9+-]+)*$", opts["languages"] or ""):
        raise UsageError("--languages is a comma list like c,c++,lto,fortran -- got %r" % opts["languages"])
    if opts["track"] == "git" and not (opts["git_ref"] or "").strip():
        raise UsageError("--track git needs a --git-ref")
    for k in ("with_arch", "with_tune"):
        if re.search(r"\s", opts[k] or ""):
            raise UsageError("--%s takes one word (x86-64-v3, znver4, native), got %r" % (k.replace("_", "-"), opts[k]))
    if opts["resume"] and opts["restart"]:
        raise UsageError("--resume and --restart say opposite things: give one")
    try:
        opts["jobs"] = int(opts["jobs"] or 0)
    except ValueError:
        raise UsageError("--jobs is a number")
    if opts["jobs"] < 0:
        raise UsageError("--jobs is a number >= 1 (0 = one per CPU)")
    bid = opts["build_id"]
    if bid and (os.sep in bid or bid in (".", "..")):
        raise UsageError("--build-id is the NAME of a directory under <workdir>/build, not a path: %r" % bid)
    try:
        shlex.split(opts["configure_extra"] or "")
    except ValueError as e:
        raise UsageError("--configure-extra: %s" % e)
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
        raise ToolError("cannot read profile %s (no [build] section?)" % path)
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
            value = os.path.abspath(os.path.expanduser(value))   # replayed from any directory
        cp.set("build", k, str(value))
    for k in PROFILE_FLAGS:
        if opts[k] is not None:                               # link_current: None means "ask, default yes"
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
    if v.get("component") == "binutils":
        return "binutils %s%s | tune %s" % (v.get("binutils_version", "auto"), "" if v.get("gold", True) else " no gold",
                                            v.get("binutils_tune", "native"))
    which_ = v.get("source") or ("git " + v.get("git_ref", "master") if v.get("track") == "git" else
                                 "%s %s" % (v.get("track", "release"), v.get("gcc_version", "auto")))
    return "gcc %s | %s | %s | -march=%s" % (which_.replace(os.path.expanduser("~"), "~"), v.get("mode", "profiled-lto"),
                                            v.get("languages", "c,c++,lto"), v.get("with_arch") or "portable")


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


def refuse_root(what):
    if os.geteuid() == 0:
        raise ToolError("do not %s as root: it builds and installs into your home, and root-owned files would "
                        "be left there.  Run it as yourself; only --install-deps uses sudo, by itself." % what)


# ------------------------------------------------------------------- doctor

def show_doctor(report, opts, machine, host, pair_why=""):
    say("Preflight: %s" % opts["component"])
    traps = env_traps()
    if traps:
        note("set in your shell, and NOT passed on to the build: " + "  ".join(traps))
    for line in host_warnings(host):
        warn(line)
    if host:
        info("host compiler : %s  (%s; C++: %s)" % (host["banner"], host["path"], host.get("cxx") or "NONE"))
    if opts["component"] == "gcc" and pair_why:
        info("as / ld       : %s" % pair_why)
    for m in report["missing"]:
        line = "%s%s" % (m["what"], "   [%s]" % m["package"] if m["package"] else "")
        if m["level"] == "required":
            bad(line)
        else:
            note("%s (%s)" % (line, m["level"]))
    if report["ok"]:
        ok("everything required is here")
    if report["install_command"]:
        info("to install %s:" % ("what is missing" if not report["ok"] else "the wanted ones too"))
        for repo in report["enable_repos"]:
            info("    %s" % " ".join(enable_repo_command(repo)))
        info("    %s" % report["install_command"])
    elif not report["ok"] and not report["package_manager"]:
        info("no dnf, apt-get, zypper or pacman here: install those by hand")
    return sum(1 for m in report["missing"] if m["level"] == "required")


def preflight(opts, machine):
    """-> (report, host, pair_why).  The host's C++ compiler is TESTED (in the environment the
    build will have, with the paired binutils first on PATH)."""
    host = pick_host(opts, machine)
    opt_root = os.path.abspath(os.path.expanduser(opts["opt_root"]))
    try:
        pair, pair_why = resolve_pair(opts, opt_root)
    except ToolError as e:
        pair, pair_why = "", str(e)
    works = None
    if host and host.get("cxx"):
        good, why = compiler_works(host["cxx"], [os.path.join(pair, "bin")] if pair else None)
        works = True if good else why
    return doctor_report(opts, machine, host, works), host, pair_why


# What only some selections need, each installed by a command of its own after the base ones
# (build_gcc_universal.sh:192-197 installed the 32-bit packages that way): with dnf (strict by
# default) or apt-get, one name the enabled repositories lack fails the whole command, and
# glibc-devel.i686 without i686 repositories, or gcc-gdc on EL, must not cost flex and texinfo.
OPTION_GROUPS = (
    ("multilib", ("multilib",), "32-bit headers unavailable; multilib may fail"),
    ("the host D compiler", ("gdc",), "no host D compiler: --languages d will be refused"),
    ("the host Ada compiler", ("gnat",), "no host Ada compiler: --languages ada will be refused"),
    ("the test-suite tools", ("dejagnu", "expect"), "dejagnu/expect unavailable: --check will be refused"),
    ("git", ("git",), "git unavailable: --track git will be refused"),
    ("the system's GMP/MPFR/MPC", ("gmp", "mpfr", "mpc", "isl"), "the libraries are unavailable: "
     "--prereqs system will be refused (--prereqs in-tree needs none of them)"))


def install_steps(report):
    """doctor's one install_command, cut into the commands --install-deps runs, in order:
    [(what, [packages], warning if it fails)].  First the base packages every build of this
    selection requires; then each option's own; then the wanted ones.  The doctor contract's
    single install_command stays as it is (the main file installs everything once)."""
    pm = report.get("package_manager") or ""
    key_of = dict((v[pm], k) for k, v in sorted(PACKAGES.items()) if v.get(pm))
    option_key = dict((k, i) for i, (_, keys, _) in enumerate(OPTION_GROUPS) for k in keys)
    base, wanted, options = [], [], [[] for _ in OPTION_GROUPS]
    for m in report.get("missing", []):
        if m["level"] not in ("required", "wanted") or not m["package"]:
            continue
        key = key_of.get(m["package"], "")
        where = options[option_key[key]] if key in option_key else base if m["level"] == "required" else wanted
        for p in m["package"].split():
            if p not in where:
                where.append(p)
    steps = []
    if base:
        steps.append(("the packages this build requires", base, "the package manager reported a failure -- "
                      "carrying on with what is there"))
    for (what, _, why), pk in zip(OPTION_GROUPS, options):
        if pk:
            steps.append((what, pk, why))
    if wanted:
        steps.append(("the wanted packages", wanted, "some wanted packages did not install -- the build "
                      "goes ahead without them"))
    return steps


def install_deps(report, runner=None):
    runner = runner or (lambda argv: run(argv, capture=False, interactive=True, check=False).returncode)
    pm = report["package_manager"]
    steps = install_steps(report)
    if not pm or not steps:
        note("nothing to install")
        return
    say("Installing with %s (sudo)" % pm)
    if pm == "dnf":
        for repo in report.get("enable_repos") or []:
            if runner(enable_repo_command(repo)) != 0:
                warn("could not enable the %s repository -- what lives there (texinfo, ...) may not install" % repo)
    if pm == "apt-get":
        runner(["sudo", "apt-get", "update"])
    for what, packages, why in steps:
        info("%s: %s" % (what, " ".join(packages)))
        if runner(shlex.split(install_command(packages, pm))) != 0:
            warn(why)


def cmd_doctor(args):
    opts = merge_options(args)
    machine = detect_machine()
    report, host, pair_why = preflight(opts, machine)
    if getattr(args, "json", False):
        emit_json(report)
        return 0 if report["ok"] else 1
    problems = show_doctor(report, opts, machine, host, pair_why)
    if opts["install_deps"]:
        refuse_root("run --install-deps")
        install_deps(report)
        report, host, pair_why = preflight(opts, detect_machine())
        problems = show_doctor(report, opts, machine, host, pair_why)
    return 1 if problems else 0


# --------------------------------------------------------------------- the plan

def dev_banner(ctx, majors=None):
    """build_gcc_universal.sh:327-333, saying 'has not shipped' only when it has not: a
    snapshot of a release branch is a newer state of a shipped series."""
    track = ctx.opts["track"]
    if ctx.component != "gcc" or track == "release" or (ctx.src and ctx.src["kind"] == "local"):
        return []
    ident = ctx.id if ctx.src and not ctx.id.startswith("git-") else ""      # git: known once cloned
    series = numver(ident)[0] if numver(ident) else \
        (int(ctx.opts["series"]) if track == "snapshot" and ctx.opts["series"].isdigit() else None)
    name = "GCC %d" % series if series is not None else \
        ("git %s" % ctx.opts["git_ref"] if track == "git" else "the newest snapshot")
    if majors is None:
        majors = released_majors() if series is not None else set()
    if series is not None and series in majors:
        what = "A snapshot of the GCC %d release branch: newer than %d's last release, not itself a release." \
            % (series, series)
    elif series is not None and majors:
        what = "GCC %d has not shipped.  It can miscompile valid code." % series
    else:
        what = "%s is not a release (which GCC majors have shipped could not be checked).  It can miscompile " \
               "valid code." % name
    return ["---------------------------------------------------------------",
            " --track %s: this is a DEVELOPMENT compiler, not a release." % track,
            " " + what,
            " Install it alongside your working compiler, never over it.",
            "---------------------------------------------------------------"]


def show_plan(ctx):
    o, m = ctx.opts, ctx.machine
    say("Plan: %s" % ctx.component)
    info("machine     : %s, %s, %d CPUs, %.0f GB RAM (%.0f GB available)"
         % (m["os"], m["arch"], m["cpus"], m["mem_total_gb"], m["mem_avail_gb"]))
    if ctx.src is None:
        warn("the version cannot be resolved now: %s" % ctx.unresolved.splitlines()[0])
        for line in ctx.unresolved.splitlines()[1:]:
            info("              " + line.strip())
    if ctx.component == "gcc":
        info("track       : %s%s" % (o["track"], "  (series %s)" % o["series"] if o["track"] == "snapshot" else
                                     "  (%s of %s)" % (o["git_ref"], o["git_url"]) if o["track"] == "git" else ""))
    info("id          : %s" % (ctx.id if ctx.src else "auto (unresolved)"))
    if ctx.component == "gcc":
        mi = MODES[o["mode"]]
        info("mode        : %s -- %s; make %s, BUILD_CONFIG=%s; about %s" % (
            o["mode"], mi["what"], mi["target"] or "(all)", mi["config"] or "none", mi["time"]))
        info("languages   : %s" % o["languages"])
        info("multilib    : %s" % ("yes (needs the 32-bit C library headers)" if o["multilib"] else "no (--disable-multilib)"))
    else:
        info("gold        : %s" % ("yes (--enable-gold; ld stays ld.bfd)" if ctx.gold else "no"))
        info("CFLAGS      : %s  (CXXFLAGS the same; --binutils-tune)" % ctx.cflags)
    info("prefix      : %s%s" % (ctx.prefix, "  (EXISTS: never installed over)" if os.path.lexists(ctx.prefix) else
                                     "  (named after the tree once it is cloned)" if "<BASE-VER>" in ctx.prefix else ""))
    info("source      : %s" % (ctx.src["label"] if ctx.src else "?"))
    info("build       : %s  (out of tree)" % ctx.obj)
    info("jobs        : %d" % ctx.jobs)
    if ctx.host:
        info("host cc     : %s" % ctx.host["path"])
        info("              %s" % ctx.host["banner"])
        info("host c++    : %s" % (ctx.host.get("cxx") or "NONE"))
    else:
        info("host cc     : NONE -- install gcc and g++")
    if ctx.component == "gcc":
        info("boot flags  : %s" % (ctx.boot or "(none)"))
        info("default     : -march=%s -mtune=%s" % (o["with_arch"] or "<unset, portable>", ctx.with_tune or "<unset>"))
        info("as / ld     : %s" % ctx.pair_why)
    need = disk_needed(ctx.component, o["mode"], o["track"])
    info("free disk   : %.0f GB where the build goes (this plan wants about %d GB)" % (free_gb(ctx.root), need))
    info("RAM         : %.0f GB" % m["mem_total_gb"])
    if ctx.host:
        info("configure   : %s" % " ".join(shlex.quote(a) for a in ctx.configure_args()))
        info("make        : %s" % " ".join(shlex.quote(a) for a in ctx.make_args()))
        info("install     : %s DESTDIR=<staging beside the prefix>, then one rename"
             % ("make install-strip (else make install)" if o["strip"] and ctx.component == "gcc" else "make install"))
    cur = os.path.join(ctx.opt_root, ctx.component + "-current")
    info("current     : %s -> the prefix once verified%s" % (cur, "   (NOT: --no-link-current)" if o["link_current"] is False else ""))


def ram_warnings(machine, opts, jobs):
    """build_gcc_universal.sh:342-344, and the per-job figure for the LTO modes."""
    lines = []
    if opts["component"] != "gcc":
        return lines
    if machine["mem_total_gb"] < 4:
        lines.append("only %.0f GB RAM; an LTO bootstrap links large objects and may OOM.  Consider --mode plain, "
                     "or a smaller --jobs." % machine["mem_total_gb"])
    elif MODES[opts["mode"]]["config"] and machine["mem_avail_gb"] / max(jobs, 1) < 1.5:
        lines.append("%.0f GB available for %d jobs: under 1.5 GB a job, the LTO links may be killed for memory.  "
                     "--jobs %d is safer." % (machine["mem_avail_gb"], jobs, max(1, int(machine["mem_avail_gb"] / 1.5))))
    return lines


def cmd_plan(args):
    opts = merge_options(args)
    machine = detect_machine()
    report, host, pair_why = preflight(opts, machine)
    problems = show_doctor(report, opts, machine, host, pair_why)
    ctx = resolve(opts, machine, strict=False, host=host)
    show_plan(ctx)
    for line in dev_banner(ctx):
        out(_c("33", "  " + line))
    for line in ram_warnings(machine, opts, ctx.jobs):
        warn(line)
    st = State(os.path.join(ctx.root, "state.json"))
    previous = previous_build_summary(st, ctx.root, fingerprint(ctx))
    if previous:
        say("Previous build in %s" % ctx.root)
        for line in previous:
            info(line)
    return 1 if problems else 0


# ------------------------------------------------------ resume, or start over

def when(stamp):
    m = re.match(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)", stamp or "")
    return "%s %s" % (m.group(1), m.group(2)) if m else (stamp or "?")


def previous_build_summary(state, root, fp=None):
    """What an earlier run left in the build directory, as lines; [] when there is none."""
    stages = state.d["stages"]
    try:
        on_disk = sorted(e for e in os.listdir(root) if e not in (".lock", "state.json"))
    except OSError:
        on_disk = []
    if not stages and not on_disk:
        return []
    lines = []
    held = lock_is_held(root)
    label = "stages      :"
    for name in STAGE_ORDER:
        s = stages.get(name)
        if not s:
            continue
        status, error = s.get("status", "?"), s.get("error", "")
        if status == "running" and not held:
            status, error = "killed", "(recorded as running, and nothing is running now)"
        lines.append(("%s %-10s %-12s %s %s" % (label, name, status, hms(s["seconds"]) if s.get("seconds") else "",
                                                error)).rstrip())
        label = " " * len(label)
    lines.append("on disk     : %s%s" % (human(dir_size(root)), "  (%s)" % ", ".join(on_disk) if on_disk else ""))
    then = state.d.get("fingerprint")
    if fp is not None and isinstance(then, dict):
        if then == fp:
            lines.append("options     : the same as this run's")
        elif not state.d["stages"]:
            lines.append("options     : the record's differ; nothing was built, so this run's count")
        else:
            lines.append("options     : DIFFERENT from this run's -- it cannot be continued with these:")
            lines += ["              " + l for l in fingerprint_diff(then, fp)]
    return lines


RESUME_QUESTION = {
    True: ("Resume it (r), delete everything and start over (d), or quit (q)?", ("resume", "restart", "quit"), "r"),
    False: ("Delete everything and start over (d), or quit (q)?", ("restart", "quit"), "d")}
RESUME_ANSWERS = {"r": "resume", "resume": "resume", "d": "restart", "delete": "restart", "restart": "restart",
                  "q": "quit", "quit": "quit", "exit": "quit"}
RESUME_LETTER = {"resume": "r", "restart": "d", "quit": "q"}


def resume_decision(opts, state, fingerprint_ok, ask=None):
    """"resume", "restart" (delete it all, then build) or "quit" for a build directory with
    an earlier run.  Nobody to ask (no terminal, --yes): a build that can be continued is
    continued, one started with other options is NOT deleted on a guess -- quit."""
    if opts["restart"]:
        return "restart"
    if not state.d["stages"]:
        return "resume"
    if fingerprint_ok and (opts["resume"] or ask is None):
        return "resume"
    if not fingerprint_ok and (opts["resume"] or ask is None):
        return "quit"
    question, offered, default = RESUME_QUESTION[bool(fingerprint_ok)]
    while True:
        answer = ask(question, default)
        if answer is None:
            return "quit"
        meaning = RESUME_ANSWERS.get((answer or "").strip().lower() or default)
        if meaning in offered:
            return meaning
        out("  one of: %s" % ", ".join(RESUME_LETTER[m] for m in offered))


def ask_eof(prompt, default):
    try:
        return input("\n  %s [%s]: " % (prompt, default)).strip()
    except EOFError:
        return None


def wipe_build_dir(root):
    """Everything in the build directory but the lock this process holds.  Links are
    removed, never followed.  The record goes first."""
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return
    for e in sorted(entries, key=lambda e: e != "state.json"):
        if e == ".lock":
            continue
        p = os.path.join(root, e)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


# --------------------------------------------------------------------- build

def cmd_build(args):
    opts = merge_options(args)
    if opts["dry_run"]:
        return cmd_plan(args)
    refuse_root("build")
    machine = detect_machine()
    report, host, pair_why = preflight(opts, machine)
    show_doctor(report, opts, machine, host, pair_why)
    if opts["install_deps"] and report["install_command"]:
        install_deps(report)
        machine = detect_machine()
        report, host, pair_why = preflight(opts, machine)
        show_doctor(report, opts, machine, host, pair_why)
    if not report["ok"]:
        raise ToolError("preflight found %d blocking problem(s) -- fix them, or re-run with --install-deps"
                        % sum(1 for m in report["missing"] if m["level"] == "required"))
    if not host or not host.get("cxx"):
        raise ToolError("no host compiler to bootstrap with: install gcc and g++, or --host-cc / --host-cxx")
    ctx = resolve(opts, machine, host=host)
    build_root = os.path.join(ctx.workdir, "build")
    if os.path.dirname(os.path.normpath(ctx.root)) != os.path.normpath(build_root):
        raise ToolError("refusing to build in %s: not a directory under %s" % (ctx.root, build_root))
    lock_dir(ctx.root)
    state_path = os.path.join(ctx.root, "state.json")
    ctx.state = State(state_path)
    if ctx.src["kind"] == "git" and ctx.state.d.get("id") and ctx.state.stage("source").get("status") == "done":
        ctx.id = ctx.state.d["id"]                           # the commit this build was started from
        if not opts["prefix"]:
            ctx.prefix = default_prefix(ctx.opt_root, "gcc", ctx.id)
    fp = fingerprint(ctx)
    then = ctx.state.d.get("fingerprint")
    fingerprint_ok = then in (None, fp)
    interactive = UI.tty and sys.stdin.isatty() and not opts["yes"]
    previous = previous_build_summary(ctx.state, ctx.root, fp)
    if previous:
        say("Previous build in %s" % ctx.root)
        for line in previous:
            info(line)
    decision = resume_decision(opts, ctx.state, fingerprint_ok, ask_eof if interactive else None)
    if decision == "quit":
        if not fingerprint_ok and (not interactive or opts["resume"]):
            raise ToolError("%s was started with other options:\n      %s\n  Finish it with those, or delete it "
                            "with --restart." % (ctx.root, "\n      ".join(fingerprint_diff(then or {}, fp))))
        info("stopped: nothing was changed.")
        return 0
    reclaim = 0.0
    if decision == "restart" and previous:
        reclaim = dir_size(ctx.root) / float(1 << 30)
        ctx.state = State(state_path, fresh=True)
    elif previous and not interactive and not opts["resume"]:
        note("resuming it: nobody to ask (no terminal, or --yes); --restart would start over instead")
    ctx.fresh = decision == "restart" or not ctx.state.d["stages"]
    show_plan(ctx)
    for line in dev_banner(ctx):
        out(_c("33", "  " + line))
    if opts["save_profile"]:
        save_profile(opts["save_profile"], opts)
    pipeline = stage_names(ctx.component, opts)
    products = {"source": lambda: os.path.isfile(os.path.join(ctx.src["dir"], "configure")),
                "prereqs": lambda: not prereqs_missing(ctx.src["dir"], [n for n, _ in parse_prereq_script(
                    read_text(os.path.join(ctx.src["dir"], "contrib", "download_prerequisites")))] or ["gmp"]),
                "configure": lambda: os.path.isfile(os.path.join(ctx.obj, "Makefile")),
                "build": lambda: os.path.exists(ctx.product()),
                "check": lambda: True,
                "install": lambda: os.path.isdir(ctx.prefix),
                "verify": lambda: ctx.state.done("verify")}
    has_product = (lambda n: False) if decision == "restart" else (lambda n: products.get(n, lambda: True)())
    gone = stale_install(ctx, pipeline)
    if gone:
        warn("the record says %s was installed, but %s does not run (removed, or broken since): %s again"
             % (ctx.prefix, main_binary(ctx.component), " + ".join(gone)))
    todo = stages_to_run(pipeline, lambda n: n not in gone and ctx.state.done(n), has_product)
    cur = os.path.join(ctx.opt_root, ctx.component + "-current")
    if not todo and opts["link_current"] is True and os.path.isdir(ctx.prefix) and \
            os.path.realpath(cur) != os.path.realpath(ctx.prefix):
        todo = ["activate"]                                  # asked for explicitly: point it here again
    if not todo:
        ok("nothing to do: every stage of this build is done.  (installed in %s)" % (ctx.state.d.get("prefix") or ctx.prefix))
        return 0
    if "install" in todo and os.path.lexists(ctx.prefix) and not (ctx.src["kind"] == "git" and "source" in todo):
        raise ToolError("%s already exists.  Never install over a prefix that something may be using.  Remove it, "
                        "or pass --prefix NEW." % ctx.prefix)
    need = disk_needed(ctx.component, opts["mode"], opts["track"])
    have = free_gb(ctx.root) + reclaim
    if "build" in todo and have < need:
        if not opts["ignore_disk"]:
            raise ToolError("%.0f GB free where the build goes (%s); this plan wants about %d GB.\n  --workdir DIR "
                            "moves it; --ignore-disk goes ahead anyway." % (have, ctx.root, need))
        warn("only %.0f GB free, about %d GB wanted: going ahead (--ignore-disk)" % (have, need))
    for line in ram_warnings(machine, opts, ctx.jobs):
        warn(line)
    info("to do       : " + " -> ".join(todo))
    if decision == "restart" and previous:
        info("start over  : everything in %s is deleted first; the source and the prefixes stay" % ctx.root)
    ask_link = opts["link_current"] is None
    if ask_link:
        opts["link_current"] = True
    if interactive:
        try:
            if ask_link and "activate" in todo:
                opts["link_current"] = (input("\n  Point %s at it once verified? (y/n) [y] " % cur).strip()
                                        or "y").lower().startswith("y")
            if not (input("\n  Proceed? (y/n) [y] ").strip() or "y").lower().startswith("y"):
                info("stopped before building anything.")
                return 0
        except EOFError:
            info("stopped before building anything: the input ended.")
            return 0
    if decision == "restart" and previous:
        wipe_build_dir(ctx.root)
        note("removed the previous build from %s" % ctx.root)
    ctx.state.d.update(fingerprint=fp, component=ctx.component, id=ctx.id, prefix=ctx.prefix,
                       source=ctx.src["dir"], tarball=ctx.src.get("tarball", ""),
                       options=dict((k, opts[k]) for k in sorted(opts)))
    ctx.state.save()
    run_stages(ctx, todo)
    linked = bool(ctx.state.d.get("linked"))
    say("Done")
    for line in advice(ctx.component, ctx.prefix, ctx.opt_root, linked):
        info(line)
    if not opts["keep_build"] and os.path.isdir(ctx.obj) and ctx.state.done("verify"):
        size = dir_size(ctx.obj)
        shutil.rmtree(ctx.obj, ignore_errors=True)
        note("removed the build tree (%s); the record and the logs stay in %s.  --keep-build keeps it."
             % (human(size), ctx.root))
    return 0


def stale_install(ctx, pipeline):
    """Done is asked of the machine (PLAN.md section 3), not only of the record: an install
    recorded done whose prefix no longer RUNS is not done, nor is what follows it.  -> those
    stages ([] when the record holds).  The NEEDS cascade then brings back the build, and the
    configure when the build tree was removed after the verified install."""
    if "install" not in pipeline or not ctx.state.done("install"):
        return []
    if runs(os.path.join(ctx.prefix, main_binary(ctx.component))):
        return []
    return pipeline[pipeline.index("install"):]


STAGE_FNS = {"source": stage_source, "prereqs": stage_prereqs, "configure": stage_configure, "build": stage_build,
             "check": stage_check, "install": stage_install, "verify": stage_verify, "activate": stage_activate}


def run_stages(ctx, todo):
    what = dict(STAGES[ctx.component])
    began = time.time()
    for n in todo:
        say("%s  --  %s" % (n, what[n]))
        t0 = time.time()
        before = ctx.state.stage(n)
        prior = before.get("seconds", 0) if before.get("status") in ("interrupted", "failed", "running") else 0
        ctx.state.set_stage(n, status="running", started=now(), finished="", error="", seconds=prior)
        try:
            STAGE_FNS[n](ctx)
        except KeyboardInterrupt:
            ctx.state.set_stage(n, status="interrupted", finished=now(), seconds=prior + int(time.time() - t0))
            raise
        except Exception as e:
            ctx.state.set_stage(n, status="failed", finished=now(), seconds=prior + int(time.time() - t0),
                                error=(str(e).splitlines() or [type(e).__name__])[0][:300])
            raise
        ctx.state.set_stage(n, status="done", finished=now(), seconds=prior + int(time.time() - t0))
        ok("%s done in %s" % (n, hms(time.time() - t0)))
    info("all stages in %s" % hms(time.time() - began))


# ------------------------------------------------------------------- status

def main_binary(component):
    return os.path.join("bin", "gcc" if component == "gcc" else "ld")


def prefix_info(path, component):
    """One installed prefix -> the status contract's `installed` entry: {name, version,
    prefix, runs, built_by_tool, compiler, for}, + what this tool recorded in its marker
    (verify, mode, languages, the paired binutils; gold) as extra keys."""
    main_bin = os.path.join(path, main_binary(component))
    good = runs(main_bin)
    version = ""
    if good:
        try:
            o = run([main_bin, "-dumpfullversion"] if component == "gcc" else [main_bin, "--version"],
                    check=False, env=scrubbed_env(), timeout=30).stdout or ""
            version = vstr(numver(o.splitlines()[0] if o else "")) if o else ""
        except ToolError:
            version = ""
    rec = read_json(os.path.join(path, MARKER)) or {}
    mine = rec.get("tool") == TOOL
    d = {"name": component, "version": version or (str(rec.get("version") or "") if mine else "") or None,
         "prefix": path, "runs": good, "built_by_tool": mine,
         "compiler": (rec.get("host") or None) if mine else None, "for": None,
         "verify": rec.get("verify", "") if mine else ""}
    if component == "gcc":
        pair = rec.get("binutils", "") if mine else ""
        d.update(mode=rec.get("mode", "") if mine else "", languages=rec.get("languages", "") if mine else "",
                 paired_binutils=pair, paired_binutils_present=bool(pair) and runs(os.path.join(pair, "bin", "ld")))
    else:
        d.update(gold=os.access(os.path.join(path, "bin", "ld.gold"), os.X_OK))
    return d


def build_summary(rec, held):
    """A build record -> (status, stage, error): done | running | killed (recorded running,
    nobody holds the lock) | failed | interrupted | incomplete | new, the stage that is
    running or failed (else the first one not done; the last one when done), and the
    error recorded for it."""
    stages = rec.get("stages") or {}
    st = lambda n: (stages.get(n) or {}).get("status", "") if isinstance(stages.get(n), dict) else ""
    try:
        pipeline = stage_names(rec.get("component") or "gcc", merge_saved(rec.get("options") or {}))
    except (KeyError, ValueError, TypeError, AttributeError):
        pipeline = [n for n in STAGE_ORDER if n in stages]
    pipeline += [n for n in sorted(stages) if n not in pipeline]
    for want, status in (("running", "running" if held else "killed"), ("failed", "failed"),
                         ("interrupted", "interrupted")):
        for n in pipeline:
            if st(n) == want:
                return status, n, (stages[n].get("error") or ("interrupted" if want == "interrupted" else None))
    if not any(st(n) for n in pipeline):
        return "new", (pipeline[0] if pipeline else None), None
    todo = [n for n in pipeline if st(n) != "done"]
    if not todo:
        return "done", pipeline[-1], None
    return "incomplete", todo[0], None


def merge_saved(saved):
    o = dict(DEFAULTS)
    o.update((k, v) for k, v in saved.items() if k in DEFAULTS)
    return o


def status_data(opt_root, workdir):
    """The toolkit's status contract (PLAN.md section 3), exactly, at the top level:
    {tool, opt_root, current: {"gcc-current": TARGET|null, "binutils-current": ...},
    installed: [{name, version, prefix, runs, built_by_tool, compiler, for}],
    builds: [{id, status, stage, error}]} -- + extra keys this tool's own `status` reads:
    workdir, components.<c>.current (the link in detail) and .leftovers, and in each entry
    what the marker or the record says beyond the contract.  A build's id is its directory's
    name, what `clean --build-id` takes."""
    data = {"tool": TOOL, "opt_root": opt_root, "workdir": workdir, "current": {}, "installed": [],
            "builds": [], "components": {}}
    for comp in COMPONENTS:
        cur = os.path.join(opt_root, comp + "-current")
        data["current"][comp + "-current"] = os.path.realpath(cur) if os.path.lexists(cur) else None
        target = os.path.realpath(cur) if os.path.islink(cur) else ""
        cur_main = os.path.join(cur, main_binary(comp))
        current = {"link": cur, "target": target, "is_link": os.path.islink(cur), "exists": os.path.lexists(cur),
                   "version": prefix_info(cur, comp)["version"] or "" if os.path.exists(cur_main) else "",
                   "runs": runs(cur_main)}
        leftovers = []
        for p in sorted(glob.glob(os.path.join(glob.escape(opt_root), comp + "-*"))):
            base = os.path.basename(p)
            if base in (comp + "-current", comp + "-current.new") or os.path.islink(p) or not os.path.isdir(p):
                continue
            if not os.path.lexists(os.path.join(p, main_binary(comp))):
                leftovers.append({"path": p, "why": "no bin/%s: not an installed prefix" % os.path.basename(main_binary(comp))})
                continue
            data["installed"].append(prefix_info(p, comp))
        for p in sorted(glob.glob(os.path.join(glob.escape(opt_root), ".%s-staging-%s-*" % (TOOL, comp)))):
            leftovers.append({"path": p, "why": "an install's staging directory, left by an interrupted run"})
        data["components"][comp] = {"current": current, "leftovers": leftovers}
    for sp in sorted(glob.glob(os.path.join(glob.escape(workdir), "build", "*", "state.json"))):
        rec = read_json(sp) or {}                            # read quietly: this may be printed as JSON
        root = os.path.dirname(sp)
        held = lock_is_held(root)
        stages = {}
        for name, s in sorted((rec.get("stages") or {}).items()):
            status = s.get("status", "?") if isinstance(s, dict) else "?"
            stages[name] = "killed" if status == "running" and not held else status
        status, stage, error = build_summary(rec, held)
        data["builds"].append({"id": os.path.basename(root), "status": status, "stage": stage, "error": error,
                               "dir": root, "component": rec.get("component", ""), "version": rec.get("id", ""),
                               "stages": stages, "prefix": rec.get("prefix", ""), "running": held,
                               "by_this_tool": rec.get("tool") == TOOL})
    return data


def cmd_status(args):
    opts = merge_options(args)
    data = status_data(os.path.abspath(os.path.expanduser(opts["opt_root"])),
                       os.path.abspath(os.path.expanduser(opts["workdir"])))
    if getattr(args, "json", False):
        emit_json(data)
        return 0
    for comp in COMPONENTS:
        c = data["components"][comp]
        say("%s in %s" % (comp, data["opt_root"]))
        cur = c["current"]
        if cur["is_link"]:
            (ok if cur["runs"] else bad)("%s -> %s  %s" % (cur["link"], cur["target"], cur["version"] or "(does not run)"))
        elif cur["exists"]:
            warn("%s is a real directory, not a link" % cur["link"])
        else:
            info("%s: not there" % cur["link"])
        for p in data["installed"]:
            if p["name"] != comp:
                continue
            extra = ""
            if comp == "gcc" and p.get("paired_binutils"):
                extra = "  as/ld %s%s" % (p["paired_binutils"], "" if p["paired_binutils_present"] else " (GONE: this gcc is broken)")
            (info if p["runs"] else warn)("  %-44s %-10s %s%s%s" % (
                p["prefix"], p["version"] or "", "this tool" if p["built_by_tool"] else "not this tool's",
                ", verify " + p["verify"] if p.get("verify") else "", extra) + ("" if p["runs"] else "  DOES NOT RUN"))
        for p in c["leftovers"]:
            warn("leftover: %s -- %s" % (p["path"], p["why"]))
    if data["builds"]:
        say("Builds in %s" % data["workdir"])
        for b in data["builds"]:
            info("%s  (%s %s)  %s%s%s" % (b["dir"], b["component"], b["version"], b["status"].upper(),
                                         " at " + b["stage"] if b["stage"] and b["status"] != "done" else "",
                                         ": " + b["error"] if b["error"] else ""))
            info("    " + "  ".join("%s=%s" % (k, v) for k, v in sorted(b["stages"].items())))
    return 0


# ------------------------------------------------------------- versions, detect

def versions_report(count=10, get=None):
    """-> [(section, [lines] or None, error or '')]: what upstream has, each source asked
    on its own, so one mirror that is down does not hide the rest."""
    get = get or (lambda url: http_get(url, timeout=30))
    sections = []

    def section(title, fn):
        try:
            sections.append((title, fn(), ""))
        except ToolError as e:
            sections.append((title, None, str(e)))

    def releases():
        vs, mirror = first_listing(GCC_MIRRORS, parse_gcc_releases, get)
        return ["  ".join(vs[:count]), "(from %s)" % mirror]

    def snapshots():
        snaps, mirror = first_listing(SNAP_MIRRORS, parse_snapshots, get)
        newest = {}
        for s in snaps:
            newest.setdefault(int(s.split("-")[0]), s)
        return ["  ".join(newest[k] for k in sorted(newest, reverse=True)), "(from %s)" % mirror]

    def trunk():
        return ["BASE-VER %s" % get(TRUNK_BASEVER_URL).decode("utf-8", "replace").strip()[:20]]

    def binutils():
        found, mirror = first_listing(BINUTILS_MIRRORS, parse_binutils, get)
        lines = []
        for v in sorted(found, key=numver, reverse=True)[:count]:
            has = found[v]
            lines.append("%-8s %s" % (v, "with-gold tarball" if has["gold"] else
                                      "gold inside" if has["plain"] and not ver_ge(numver(v), GOLD_SPLIT) else "NO gold"))
        return lines + ["(from %s)" % mirror]
    section("GCC releases  (--track release --gcc-version X.Y.Z)", releases)
    section("GCC snapshots, the newest of each series  (--track snapshot --series N)", snapshots)
    section("GCC trunk  (--track git)", trunk)
    section("binutils  (--component binutils --binutils-version X.Y)", binutils)
    return sections


def cmd_versions(args):
    sections = versions_report(int(getattr(args, "count", 10) or 10))
    for title, lines, err in sections:
        say(title)
        if lines is not None:
            for line in lines:
                info(line)
        else:
            warn(err.splitlines()[0])
            for line in err.splitlines()[1:]:
                info("  " + line.strip())
    if all(lines is None for _, lines, _ in sections):
        raise ToolError("nothing upstream answered (is the network up, or a proxy in the way?) -- see above")
    return 0


def cmd_detect(args):
    m = detect_machine()
    if getattr(args, "json", False):
        emit_json(m, default=str)
        return 0
    say("This machine")
    info("%s, %s, %d CPUs (%s%s), %.0f GB RAM (%.0f available), packages: %s"
         % (m["os"], m["arch"], m["cpus"], m["cpu_model"] or "?", ", " + m["cpu_level"] if m["cpu_level"] else "",
            m["mem_total_gb"], m["mem_avail_gb"], m["pkg_manager"] or "?"))
    for c in m["host_compilers"]:
        info("compiler : %-50s %s  [%s]" % (c["banner"][:50], c["path"], c["origin"]))
    for bset in m["binutils"]:
        info("binutils : %-8s %s%s" % (vstr(bset["version"]), bset["dir"], "  (gold)" if bset["gold"] else ""))
    info("tools    : " + " ".join("%s=%s" % (k, "yes" if v else "no") for k, v in sorted(m["tools"].items())))
    info("headers  : " + " ".join("%s=%s" % (k, "yes" if v else "no") for k, v in sorted(m["headers"].items())))
    return 0


def compiler_tags(c):
    tags = [c["origin"]]
    if not c.get("cxx"):
        tags.append("no C++ partner beside it")
    if c["kind"] == "clang":
        tags.append("NOT recommended for GCC: the in-tree GMP/MPFR LTO probe goes through the system's LLVMgold.so")
    return tags


def cmd_toolchains(args):
    opts = merge_options(args)
    m = detect_machine()
    host = pick_host(opts, m)
    say("Host compilers  (--host-cc PATH [--host-cxx PATH]; default: /usr/bin/gcc, else gcc on the system PATH)")
    for c in m["host_compilers"]:
        out("  - %-5s %-10s %s%s" % (c["kind"], vstr(c["version"]), c["path"].replace(os.path.expanduser("~"), "~"),
                                     "    <- would be used" if host and c["path"] == host["path"] else ""))
        tags = compiler_tags(c)
        if not getattr(args, "no_check", False) and c.get("cxx"):
            good, why = compiler_works(c["cxx"])
            tags.append("builds and runs a C++17 program" if good else "BROKEN in a clean environment: " + why)
        out("        " + "; ".join(tags))
    if host and host["path"] not in [c["path"] for c in m["host_compilers"]]:
        out("  - %-5s %-10s %s    <- would be used (--host-cc)" % (host["kind"], vstr(host["version"]), host["path"]))
    opt_root = os.path.abspath(os.path.expanduser(opts["opt_root"]))
    try:
        pair, why = resolve_pair(dict(opts, component="gcc"), opt_root)
    except ToolError as e:
        pair, why = "", str(e)
    say("GNU binutils sets  (--binutils auto|system|DIR: what gcc is configured --with-as/--with-ld to)")
    for bset in m["binutils"]:
        out("  - %-8s %s%s%s" % (vstr(bset["version"]), bset["dir"].replace(os.path.expanduser("~"), "~"),
                                 "  (gold)" if bset["gold"] else "",
                                 "    <- gcc would be paired with it" if pair and same_file(os.path.join(pair, "bin"), bset["dir"])
                                 else "    (the system's)" if bset["system"] else ""))
    info("gcc would use: %s" % why)
    return 0


# ---------------------------------------------------------------- verify, clean

def cmd_verify(args):
    prefix = os.path.abspath(os.path.expanduser(args.prefix_arg))
    rec = read_json(os.path.join(prefix, MARKER)) or {}
    if os.access(os.path.join(prefix, "bin", "gcc"), os.X_OK):
        c = verify_gcc(prefix, rec)
    elif os.access(os.path.join(prefix, "bin", "ld"), os.X_OK) and os.access(os.path.join(prefix, "bin", "as"), os.X_OK):
        host = pick_host(merge_options(args), {}) or {}
        c = verify_binutils(prefix, host.get("path") or which("gcc") or "", rec)
    else:
        raise ToolError("%s holds neither bin/gcc nor bin/as + bin/ld" % prefix)
    failed = c.failed()
    if rec.get("tool") == TOOL and os.access(prefix, os.W_OK):
        set_verify_mark(prefix, "failed" if failed else "ok")
    if failed:
        bad("%d of %d checks failed: %s" % (len(failed), len(c.results), ", ".join(failed)))
        return 1
    ok("all %d checks passed" % len(c.results))
    return 0


def cmd_clean(args):
    """Only this tool's own directories: <workdir>/build/<name> (its record says it is this
    tool's), and with --sources the source tree and tarball that record names, under
    <workdir>/src.  Never a prefix, never a -current link."""
    opts = merge_options(args)
    refuse_root("clean")
    workdir = os.path.abspath(os.path.expanduser(opts["workdir"]))
    if opts["build_id"]:
        root = os.path.join(workdir, "build", opts["build_id"])
    else:
        try:
            ctx = resolve(opts, detect_machine(), host={})
        except ToolError as e:
            names = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(workdir, "build", "*", "state.json")))
            raise ToolError("%s\n  Name the build directory instead: --build-id NAME  (here: %s)"
                            % (str(e).splitlines()[0], ", ".join(names) or "none"))
        root = ctx.root
    if not os.path.isdir(root):
        info("no build directory %s" % root)
        return 0
    rec = read_json(os.path.join(root, "state.json"))
    if rec is None or rec.get("tool") != TOOL:
        raise ToolError("%s holds no record of %s: not touching it" % (root, TOOL))
    lock_dir(root)
    size = dir_size(root)
    keep = (".lock",) if getattr(args, "all", False) else (".lock", "state.json", "logs")
    for e in os.listdir(root):
        if e in keep:
            continue
        p = os.path.join(root, e)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
    freed = size - dir_size(root)
    if getattr(args, "sources", False):
        srcroot = os.path.join(workdir, "src") + os.sep
        for p in (rec.get("source", ""), rec.get("tarball", "")):
            if p and os.path.abspath(p).startswith(srcroot) and os.path.lexists(p):
                s = dir_size(p) if os.path.isdir(p) else os.path.getsize(p)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
                freed += s
                info("removed %s" % p)
            elif p and not os.path.abspath(p).startswith(srcroot):
                note("left alone (not under %s): %s" % (srcroot, p))
    ok("freed %s in %s%s" % (human(freed), root, "" if getattr(args, "all", False) else
                             "  (kept: the record and the logs; --all removes them too)"))
    return 0


# ----------------------------------------------------------- profiles, the wizard

def cmd_profiles(args):
    profs = list_profiles()
    say("Saved profiles  (%s)" % profiles_dir())
    if not profs:
        info("none yet: the wizard offers to save one, and so does --save-profile NAME")
    for pr in profs:
        out("  %-24s %s   %s" % (pr["name"], time.strftime("%Y-%m-%d %H:%M", time.localtime(pr["mtime"])), pr["summary"]))
    return 0


def ask(prompt, default=""):
    try:
        ans = input("%s [%s]: " % (prompt, default)).strip()
    except EOFError:
        ans = ""
    return ans or default


def ask_choice(title, options, default):
    """options: [(key, label)].  Accepts the number or the key."""
    if default not in [k for k, _ in options]:
        options = list(options) + [(default, "as given on the command line / in the profile")]
    out("\n" + _c("1", title))
    for i, (key, label) in enumerate(options, 1):
        out("  %2d) %-14s %s%s" % (i, key, label, "   <- default" if key == default else ""))
    while True:
        ans = ask("choose", default)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        if ans in [k for k, _ in options]:
            return ans
        out("  a number from the list, or one of the keys")


def ask_yes(prompt, default):
    return ask(prompt + " (y/n)", "y" if default else "n").lower().startswith("y")


def choose_profile(profs):
    """The FIRST thing the wizard shows.  -> {path, name, as_is} or None (new)."""
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


def wizard(opts, machine):
    """Every question is also a flag, and every answer can be saved as a profile.  Enter
    through all of them builds what the .sh built: the newest release, profiled-lto,
    c,c++,lto, the system's gcc, portable default -march, tuned for this CPU."""
    opts["component"] = ask_choice("What to build", [("gcc", "GCC (step 4), paired with the binutils of step 3"),
                                                     ("binutils", "GNU binutils with gold (step 3)")], opts["component"])
    if opts["component"] == "binutils":
        opts["binutils_version"] = ask("binutils version (auto = the newest with gold)", opts["binutils_version"])
        opts["gold"] = ask_yes("build gold (ld.gold; from 2.44 it needs the with-gold tarball)", opts["gold"])
        opts["binutils_tune"] = ask_choice("Tuning of the binutils binaries", [
            ("native", "-g -O3 -march=native (reboot.txt's line): this CPU and newer"),
            ("portable", "-g -O3: any machine of this architecture"),
            ("x86-64-v3", "-g -O3 -march=x86-64-v3: AVX2 machines")], opts["binutils_tune"])
    else:
        cur = "git" if opts["track"] == "git" else "snapshot" if opts["track"] == "snapshot" else \
            "latest" if opts["gcc_version"] == "auto" else "version"
        kind = ask_choice("Which GCC?", [("latest", "the newest RELEASE (tested, shipped: use this one)"),
                                         ("version", "a release you name: X.Y.Z"),
                                         ("snapshot", "a weekly snapshot of a series: a DEVELOPMENT compiler"),
                                         ("git", "a git branch, tag or commit as it stands now: a DEVELOPMENT compiler")], cur)
        if kind == "latest":
            opts["track"], opts["gcc_version"] = "release", "auto"
        elif kind == "version":
            opts["track"] = "release"
            opts["gcc_version"] = ask("version (X.Y.Z)", opts["gcc_version"] if opts["gcc_version"] != "auto" else "")
        elif kind == "snapshot":
            opts["track"] = "snapshot"
            opts["series"] = ask("series (a major number, or auto = trunk)", opts["series"])
            opts["gcc_version"] = ask("snapshot (N-YYYYMMDD, or auto = the newest)", "auto")
        else:
            opts["track"], opts["gcc_version"] = "git", "auto"
            opts["git_ref"] = ask("branch, tag or commit", opts["git_ref"])
        opts["mode"] = ask_choice("How hard to work for a faster compiler", [
            (k, "%s; about %s, ~%d GB" % (MODES[k]["what"], MODES[k]["time"], MODES[k]["disk"])) for k in MODE_ORDER],
            opts["mode"])
        opts["languages"] = ask("languages (add fortran,go,d,objc,ada as needed)", opts["languages"]).replace(" ", "")
        opts["multilib"] = ask_yes("multilib (32-bit too; needs the 32-bit C library headers)", opts["multilib"])
        opts["boot_arch"] = ask("flags the compiler ITSELF is built with ('none' for none)", opts["boot_arch"])
        levels = [("x86-64-v2", "SSE4.2 era, 2009+"), ("x86-64-v3", "AVX2 era, 2013+"), ("x86-64-v4", "AVX-512")] \
            if machine["arch"] == "x86_64" else []
        out("\n  The DEFAULT -march of the installed compiler is what every program you build with it gets.  "
            "Anything\n  but portable makes those programs fail with SIGILL on older machines; an ISA level "
            "licenses exactly\n  what __builtin_cpu_supports checks, a CPU name more.")
        arch = ask_choice("Default -march baked into the compiler", [("portable", "unset: runs everywhere (recommended)")]
                          + levels + [("native", "this CPU: binaries stop running on older machines")],
                          opts["with_arch"] or "portable")
        opts["with_arch"] = "" if arch == "portable" else arch
        opts["with_tune"] = ask("default -mtune (auto = native where GCC takes it; none)", opts["with_tune"])
        opts["binutils"] = ask("as/ld to pair it with: auto (<opt>/binutils-current when it runs), system, or a directory",
                               opts["binutils"])
    out("\n" + _c("1", "The compiler that builds it"))
    usable = [c for c in machine["host_compilers"] if c.get("cxx")]
    for i, c in enumerate(usable, 1):
        out("  %2d) %-5s %-10s %s%s" % (i, c["kind"], vstr(c["version"]), c["path"],
                                       "   (NOT recommended for GCC)" if c["kind"] == "clang" else ""))
    out("   a) auto -- /usr/bin/gcc, else gcc on the system PATH")
    default = next((str(i) for i, c in enumerate(usable, 1) if c["path"] == opts["host_cc"]), opts["host_cc"] or "a")
    while True:
        ans = ask("compiler: a number, a, or a path", default)
        if ans.lower() in ("a", "auto"):
            opts["host_cc"] = opts["host_cxx"] = ""
            break
        if ans.isdigit() and 1 <= int(ans) <= len(usable):
            opts["host_cc"], opts["host_cxx"] = usable[int(ans) - 1]["path"], ""
            break
        if os.sep in ans and compiler_info(os.path.expanduser(ans)):
            opts["host_cc"], opts["host_cxx"] = os.path.expanduser(ans), ""
            break
        out("  a number from the list, a, or the path of a gcc")
    opts["prefix"] = ask("\ninstall prefix (empty = <opt>/%s-<version>)" % opts["component"], opts["prefix"] or "")
    opts["link_current"] = ask_yes("point %s-current at it once verified?" % opts["component"], opts["link_current"] is not False)
    name = ask("save these answers as a profile? (a name, or empty)", opts.get("_profile_name", ""))
    opts.pop("_profile_name", None)
    if name:
        save_profile(name, check_options(opts))
    return opts


def cmd_wizard(args):
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
        opts = wizard(opts, detect_machine())
    opts.pop("_profile_name", None)
    check_options(opts)
    for k in DEFAULTS:                                   # hand the answers to `build` as if they were flags
        setattr(args, k, opts[k])
    args.profile = None
    return cmd_build(args)


# -------------------------------------------------------------------- the parser

def add_common(p):
    g = p.add_argument_group("where")
    g.add_argument("--opt-root", dest="opt_root", help="where prefixes and the -current links live (default ~/opt)")
    g.add_argument("--workdir", help="sources, build trees, records and logs (default ~/src/enterprise_gcc_tool)")
    g.add_argument("--profile", help="load choices from a saved profile (a name, or a path)")
    g.add_argument("--yes", "-y", action="store_true", default=None,
                   help="ask nothing: Proceed is yes, an earlier run is resumed (never deleted; --restart does that)")


def add_build_options(p):
    add_common(p)
    g = p.add_argument_group("what to build")
    g.add_argument("--component", choices=COMPONENTS, help="gcc (default) or binutils")
    g.add_argument("--track", help="release (default) | snapshot | git -- WHICH GCC (the .sh's TRACK)")
    g.add_argument("--series", help="the snapshot series, a major number (default auto = the highest listed: trunk)")
    g.add_argument("--gcc-version", "--version", dest="gcc_version",
                   help="auto (default) | X.Y.Z for --track release | N-YYYYMMDD for --track snapshot")
    g.add_argument("--git-ref", dest="git_ref", help="branch, tag or commit for --track git (default master)")
    g.add_argument("--gcc-git", "--git-url", dest="git_url", help="the GCC git repository (default %s)" % GCC_GIT)
    g.add_argument("--binutils-version", dest="binutils_version",
                   help="auto (default: the newest that has gold) | X.Y[.Z]  -- for --component binutils")
    g.add_argument("--source", help="a source tree you already have (configured out of tree)")
    g.add_argument("--prefix", help="where to install (default <opt-root>/<component>-<version>; must not exist)")
    g = p.add_argument_group("how (gcc)")
    g.add_argument("--mode", help="profiled-lto (default) | lto | plain | quick")
    g.add_argument("--languages", help="--enable-languages (default c,c++,lto); never exported to make")
    g.add_argument("--multilib", action="store_true", default=None, help="build the 32-bit libraries too")
    g.add_argument("--no-multilib", dest="multilib", action="store_false", default=None, help="--disable-multilib (default)")
    g.add_argument("--boot-arch", dest="boot_arch",
                   help="flags the compiler ITSELF is built with (default '-march=native -mtune=native'; none)")
    g.add_argument("--with-arch", dest="with_arch", help="the DEFAULT -march of the installed gcc (default: unset, portable)")
    g.add_argument("--with-tune", dest="with_tune", help="the default -mtune (default auto = native on x86/aarch64; none)")
    g.add_argument("--binutils", help="auto (default: <opt>/binutils-current when it runs) | system | DIR: "
                   "configure --with-as=DIR/bin/as --with-ld=DIR/bin/ld")
    g.add_argument("--prereqs", help="in-tree (default: contrib/download_prerequisites) | system")
    g = p.add_argument_group("how (binutils)")
    g.add_argument("--gold", action="store_true", default=None, help="build gold (default)")
    g.add_argument("--no-gold", dest="gold", action="store_false", default=None, help="no gold")
    g.add_argument("--binutils-tune", "--tune", dest="binutils_tune",
                   help="native (default: CFLAGS/CXXFLAGS -g -O3 -march=native) | portable | x86-64-v3 | raw flags")
    g = p.add_argument_group("how (both)")
    g.add_argument("--host-cc", dest="host_cc", help="the compiler that bootstraps (default /usr/bin/gcc; CC from "
                   "the environment is ignored)")
    g.add_argument("--host-cxx", dest="host_cxx", help="its C++ partner, if not the one beside it")
    g.add_argument("--configure-extra", dest="configure_extra", help="more configure arguments, one string")
    g.add_argument("--jobs", "-j", type=int, help="make -j (default: CPUs)")
    g.add_argument("--no-strip", dest="strip", action="store_false", default=None,
                   help="gcc: make install, not install-strip")
    g.add_argument("--check", action="store_true", default=None, help="make -k check after the build (summarised, never fatal)")
    g.add_argument("--keep-build", dest="keep_build", action="store_true", default=None,
                   help="keep the build tree after a verified install")
    g.add_argument("--link-current", dest="link_current", action="store_true", default=None,
                   help="point <opt>/<component>-current at the result once verified (the default; asked on a terminal)")
    g.add_argument("--no-link-current", dest="link_current", action="store_false", default=None,
                   help="build, install and verify, but leave the -current link alone")
    g = p.add_argument_group("this run")
    g.add_argument("--resume", action="store_true", default=None, help="continue the earlier run, without being asked")
    g.add_argument("--restart", action="store_true", default=None, help="delete the earlier run first, without being asked")
    g.add_argument("--install-deps", dest="install_deps", action="store_true", default=None,
                   help="sudo <package manager> install what doctor says is missing")
    g.add_argument("--ignore-disk", dest="ignore_disk", action="store_true", default=None,
                   help="build even where less disk is free than the plan wants")
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="the plan; build nothing")
    g.add_argument("--save-profile", dest="save_profile", help="save this run's choices under a name")
    g.add_argument("--verbose", "-v", action="store_true", default=None, help="the whole build output on the console")


def build_parser():
    p = argparse.ArgumentParser(prog=TOOL + ".py", description="Build GNU binutils and GCC from source, as one "
                                "pair: gcc configured --with-as/--with-ld = the binutils this tool built.")
    p.add_argument("--tool-version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")
    for name, helptext in (("build", "fetch, configure, make, install, verify, link -current (resumable)"),
                           ("plan", "resolve everything and print it; build nothing (= build --dry-run)"),
                           ("doctor", "what is missing, and the one command that installs it ([--json])"),
                           ("wizard", "ask the questions (what you get with no command on a terminal)"),
                           ("toolchains", "every gcc, clang and binutils here; which would be used"),
                           ("clean", "remove a build directory's tree (keeps the record and logs; --all removes them)")):
        sp = sub.add_parser(name, help=helptext)
        add_build_options(sp)
        if name == "doctor":
            sp.add_argument("--json", action="store_true", help="the toolkit's doctor contract, as JSON")
        if name == "clean":
            sp.add_argument("--all", action="store_true", help="the record and the logs too")
            sp.add_argument("--sources", action="store_true", help="and the source tree + tarball that build used")
            sp.add_argument("--build-id", dest="build_id", help="the build directory's name (see status)")
        if name == "toolchains":
            sp.add_argument("--no-check", dest="no_check", action="store_true", help="do not test-compile with each")
    sp = sub.add_parser("status", help="what is installed where, the -current links, the builds")
    add_common(sp)
    sp.add_argument("--json", action="store_true", help="as JSON")
    sp = sub.add_parser("versions", help="what upstream has now: GCC releases, snapshots, trunk, binutils")
    add_common(sp)
    sp.add_argument("--count", type=int, default=10, help="how many of each (default 10)")
    sp = sub.add_parser("detect", help="what this machine has")
    add_common(sp)
    sp.add_argument("--json", action="store_true", help="as JSON")
    sp = sub.add_parser("verify", help="run the post-install checks against a prefix (gcc or binutils)")
    add_build_options(sp)
    sp.add_argument("prefix_arg", metavar="PREFIX")
    sp = sub.add_parser("profiles", help="saved profiles")
    add_common(sp)
    return p


COMMANDS = {"build": cmd_build, "plan": cmd_plan, "doctor": cmd_doctor, "status": cmd_status,
            "versions": cmd_versions, "detect": cmd_detect, "verify": cmd_verify, "clean": cmd_clean,
            "toolchains": cmd_toolchains, "profiles": cmd_profiles, "wizard": cmd_wizard}


def _as_interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    argv = where_to_run(list(sys.argv[1:] if argv is None else argv), __file__, TOOL)
    parser = build_parser()
    if not any(a in COMMANDS for a in argv) and sys.stdin.isatty() and sys.stdout.isatty() \
            and not any(a in ("-h", "--help", "--tool-version") for a in argv):
        argv = ["wizard"] + argv                      # no command, a terminal: the wizard
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if not args.command:
        parser.print_help()
        out("\nStart with:  %s doctor" % (os.path.basename(sys.argv[0]) or TOOL + ".py"))
        return 2
    # SIGTERM / SIGHUP take the ^C path so a finally runs and the record is right;
    # a SIGHUP somebody chose to ignore (`nohup ... &`) stays ignored.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, _as_interrupt)
    saved = (UI.stream, UI.tty, UI.color)
    if getattr(args, "json", False):
        json_mode()
    try:
        return run_command(args)
    finally:
        UI.stream, UI.tty, UI.color = saved


def run_command(args):
    try:
        return COMMANDS[args.command](args)
    except UsageError as e:
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 2
    except ToolError as e:
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 1
    except OSError as e:                                     # a full disk, a permission: said plainly
        out("\n" + _c("31", "ERROR:") + " " + str(e))
        return 1
    except KeyboardInterrupt:
        out("\ninterrupted -- `build` again shows where things stand and continues from there.")
        return 130


def _ensure_platform_python():
    """Started as `python3 tool.py` where python3 is PyPy (HANDOFF trap 1)?  The tool is happy
    anywhere; this keeps behaviour identical to the platform's CPython."""
    impl = getattr(sys.implementation, "name", "cpython")
    if impl != "cpython" and os.access(PLATFORM_PYTHON, os.X_OK) and not os.environ.get("EGT_NO_REEXEC"):
        os.environ["EGT_NO_REEXEC"] = "1"
        os.execv(PLATFORM_PYTHON, [PLATFORM_PYTHON, "-I", os.path.abspath(__file__)] + sys.argv[1:])


if __name__ == "__main__":
    _ensure_platform_python()
    sys.exit(main())
