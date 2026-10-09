#!/usr/bin/python3 -I
# -*- coding: utf-8 -*-
"""satellite_remote.py -- run a tool on THIS machine or on ANOTHER one over ssh, the same code
either way.  The author, 2026-10-08: "we need to make all of the scripts that we use have built-in
remote choices, so we will have to build 'local or remote?' option into every script that we use,
and then the same code requests for ssh for a remote machine, if the user chooses remote".

It is not imported: sync_remote.py writes the part between the two "embed" lines below into every
tool of the satellite toolkit, between

    # >>> satellite_remote >>> ...
    # <<< satellite_remote <<<

so each tool stays ONE file that can be copied anywhere and still go remote.  Edit it HERE, run
`./sync_remote.py`, and the tests check every copy is the same.

What a tool gets from it: one call, the first line of main():

    argv = where_to_run(list(sys.argv[1:] if argv is None else argv), __file__, TOOL)

  --local                     run here (and never ask)
  --remote[=USER@HOST]        run there; the target is asked when it is not given
  --attach[=SESSION]          back to a session running there (the newest of this tool's);
                              `--attach SESSION` too, when the word is shaped like a session name
  --sessions                  what runs, and what ran, there
  (none, on a terminal)       "run where?" -- Enter is this machine

THERE: the tool file is uploaded to ~/.satellite_toolkit/code/<tool>-<sha12>/ and run as
`/usr/bin/python3 -I <tool> <args> --local` inside a SESSION -- a keeper process that holds the
program on a pseudo-terminal and keeps everything it prints in a log, so a dropped ssh connection
does not stop a five-hour build.  This side attaches to it: what it prints streams here, what is
typed goes there, Ctrl-] detaches (it keeps running), --attach comes back.

The exit code of a remote run is the program's own once it ENDED there.  Otherwise it says that
the program is still running there, never that it is done:

  75   detached with Ctrl-] (EX_TEMPFAIL): it keeps running there; --attach goes back
  255  the connection dropped (ssh's own code for that): it keeps running there, or may be
  130  interrupted here with no terminal (Ctrl-C to a piped run): it keeps running there
  1    it could not go there at all (the login, the upload, the start; said in a sentence)

`before=` lets the caller act on the machine once a NEW session is about to start -- after the
login, the python there and the upload, before the start: before(machine, folder, rest) -> None
(rest as it is) or a new rest.  A RemoteError from it is said and nothing starts.

THE LOGIN: when ~/code/py/satellite_ssh keeps a password for USER@HOST (its 600 file), it logs in
with that, and types it again when the program there asks "[sudo] password for USER:" -- never
shown, never written anywhere new, answered once per question (a refused answer is not repeated:
an account that locks after failed attempts is never pushed there).  Otherwise plain ssh: a key,
or the password typed at this terminal.

Standard library only; syntax held to Python 3.6 (RHEL 8's platform python), like the tools.
Licence: public domain / CC0.  No warranty.
"""
# ---- embed from here ----
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
# ---- embed to here ----


if __name__ == "__main__":
    # Standalone it is a small launcher, mostly for its own tests:
    #   satellite_remote.py --remote=USER@HOST --sessions
    #   satellite_remote.py --remote=USER@HOST -- COMMAND...   (a session running COMMAND there)
    args = where_to_run(sys.argv[1:], __file__, "satellite_remote")
    if args[:1] == ["--"] and len(args) > 1:
        os.execvp(args[1], args[1:])
    sys.stdout.write("here: %s\n" % " ".join(args))
