#!/usr/bin/python3 -I
"""Tests for satellite_remote.py (the local-or-remote block every tool carries), sync_remote.py
and the copies of the block in the tools.  stdlib unittest; no network, no root, no real ssh:
a fake `ssh`, first on PATH, runs the "remote" command right here with a scratch home of its own.
HOME and SATELLITE_SSH_DIR always point at scratch folders, so the real ~/.config/satellite_toolkit
and ~/code/py/satellite_ssh (and the passwords it keeps) are never read.

RealSshdTests runs the same paths through a private sshd on 127.0.0.1 (a real ControlMaster,
ssh -tt, a connection killed under it).  It needs ssh, ssh-keygen and /usr/sbin/sshd and a
non-root account to run them as -- yourself, or, when run as root, SATELLITE_TEST_SSH_USER=<user>
-- and is skipped, saying why, without them.

    /usr/bin/python3 -I tests/test_remote.py [-v]
"""

import ast
import io
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

sys.dont_write_bytecode = True                  # no __pycache__ left in the repo
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPPORT = os.path.join(REPO, "tests", "support")
sys.path.insert(0, SUPPORT)
sys.path.insert(0, REPO)
import satellite_remote as sr                   # noqa: E402
import sync_remote                              # noqa: E402
import pty_harness                              # noqa: E402

PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
SR = os.path.join(REPO, "satellite_remote.py")
TOOLS = [rel for rel in sync_remote.TOOLS if os.path.exists(os.path.join(REPO, rel))]
KNOWN = {"tools/clang/enterprise_clang_tool.py": "clang",
         "tools/python/enterprise_python_tool.py": "python",
         "tools/kernel/enterprise_linux_kernel_tool.py": "linux_kernel"}
NAME_SAID = re.compile(r"runs there as session (\S+)")
LIMIT = 30                                      # seconds: no subprocess here may take longer

# The fake ssh.  It skips the options (those that take an argument take it), answers the login
# (-M ... -fN: $FAKE_SSH_LOGIN_RC, $FAKE_SSH_LOGIN_ERR) and the control calls (-O exit), and
# runs anything else as `sh -c '<the words>'` in $FAKE_REMOTE_HOME with HOME set to it -- what
# sshd does with a command.  $FAKE_SSH_DROP_AFTER=S: an `ssh -tt` (the attach) is cut after S
# seconds and exits 255; with $FAKE_SSH_DOWN=FILE as well, the machine is then gone: every
# later call is refused until FILE is removed.  Every call is logged to $FAKE_SSH_LOG.
FAKE_SSH = r'''#!/usr/bin/python3 -I
import json, os, signal, subprocess, sys
a = sys.argv[1:]
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a") as f:
        f.write(json.dumps(a) + "\n")
WITHARG = ("-S", "-o", "-p", "-i", "-O", "-l", "-F", "-E", "-J", "-b", "-c", "-e", "-m", "-w", "-W",
           "-L", "-R", "-D")
words, opts, i = [], {}, 0
while i < len(a) and a[i].startswith("-"):
    if a[i] in WITHARG and i + 1 < len(a):
        opts[a[i]] = a[i + 1]
        i += 2
    else:
        words.append(a[i])
        i += 1
target, cmd = (a[i] if i < len(a) else ""), a[i + 1:]
letters = set("".join(w[1:] for w in words))
sock, down = opts.get("-S"), os.environ.get("FAKE_SSH_DOWN")
if "-O" in opts:
    if opts["-O"] == "exit" and sock and os.path.exists(sock):
        os.unlink(sock)
    sys.exit(0)
if "M" in letters:
    if os.environ.get("FAKE_SSH_LOGIN_ERR"):
        sys.stderr.write(os.environ["FAKE_SSH_LOGIN_ERR"] + "\n")
    rc = int(os.environ.get("FAKE_SSH_LOGIN_RC", "0"))
    if rc == 0 and sock:
        open(sock, "w").close()
    sys.exit(rc)
if down and os.path.exists(down):
    sys.stderr.write("ssh: connect to host %s port 22: Connection refused\n" % target.split("@")[-1])
    sys.exit(255)
home = os.environ["FAKE_REMOTE_HOME"]
env = dict(os.environ, HOME=home)
env.pop("SATELLITE_TOOLKIT_WHERE", None)
os.chdir(home)
argv = ["sh", "-c", " ".join(cmd)] if cmd else ["sh"]
drop = os.environ.get("FAKE_SSH_DROP_AFTER")
if drop and "-tt" in words:
    p = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        sys.exit(p.wait(timeout=float(drop)))
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        if down:
            open(down, "w").close()
        sys.exit(255)
os.execvpe("sh", argv, env)
'''

# A driver that calls where_to_run() the way a tool's main() does, with a before= hook (and,
# for the same-second test, one fixed session name).  The hook writes what it saw to a record.
DRIVER = r'''import json, os, sys
sys.path.insert(0, %(repo)r)
import satellite_remote as sr
if os.environ.get("DRIVER_NAME"):
    sr._sr_session_name = lambda tool_name: os.environ["DRIVER_NAME"]
mode = os.environ.get("DRIVER_BEFORE", "")
def before(machine, folder, rest):
    there = os.path.join(os.environ["FAKE_REMOTE_HOME"], folder[2:])
    with open(os.environ["DRIVER_RECORD"], "a") as f:
        f.write(json.dumps({"folder": folder, "rest": rest, "python": machine.link.python,
                            "uploaded": os.path.isfile(os.path.join(there, ".complete"))}) + "\n")
    machine.link.run("echo hook-was-there > hook.txt", timeout=30)
    if mode == "rewrite":
        return rest + ["rewritten"]
    if mode == "error":
        raise sr.RemoteError("the hook says no")
    if mode == "interrupt":
        raise KeyboardInterrupt()
    return None
args = sr.where_to_run(None, sr.__file__, "satellite_remote", before=before if mode else None)
sys.stdout.write("here: %%s\n" %% " ".join(args))
'''

# Plays sudo on the far side: asks "[sudo] password for USER: " with echo off, the way sudo
# does, and says what it got: right, wrong or none (nothing came within the wait).
SUDO_PROG = r'''import os, select, sys, termios
user, mode, first, later = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
old = termios.tcgetattr(0)
new = termios.tcgetattr(0)
new[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, new)
got = []
try:
    for n in range(2 if mode == "refuse" else 1):
        sys.stdout.write("[sudo] password for %s: " % user)
        sys.stdout.flush()
        r, _, _ = select.select([0], [], [], first if n == 0 else later)
        line = os.read(0, 1024).decode().strip() if r else None
        got.append("none" if line is None else ("right" if line == "s3cret-test" else "wrong"))
        sys.stdout.write("\n" + ("Sorry, try again.\n" if mode == "refuse" and line is not None else ""))
        sys.stdout.flush()
finally:
    termios.tcsetattr(0, termios.TCSANOW, old)
print("RESULT " + " ".join(got))
'''

# A remote program that waits for a file named go in its home (at most 15 s), then exits 4.
WAIT_GO = ('echo begun; i=0; while [ ! -f go ] && [ $i -lt 150 ]; do sleep 0.1; i=$((i+1)); done; '
           'echo went; exit 4')


def wait_for(cond, timeout=10.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def put(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def stop_sessions(home):
    """Every keeper and program under home's sessions, killed: nothing outlives its test."""
    root = os.path.join(home, ".satellite_toolkit", "sessions")
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        try:
            keeper, prog = (int(x) for x in read(os.path.join(root, name, "pids")).split()[:2])
        except (OSError, ValueError):
            continue
        for kill, pid in ((os.killpg, prog), (os.kill, keeper)):
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass


_SCRATCH = {}


def setUpModule():
    # In-process tests read HOME (the remembered target) and SATELLITE_SSH_DIR too: never the
    # real ones.
    d = tempfile.mkdtemp(prefix="sr-test-home-")
    _SCRATCH.update(dir=d, env={k: os.environ.get(k) for k in ("HOME", "SATELLITE_SSH_DIR",
                                                               "SATELLITE_TOOLKIT_WHERE")})
    os.environ["HOME"] = os.path.join(d, "home")
    os.environ["SATELLITE_SSH_DIR"] = os.path.join(d, "no-satellite-ssh")
    os.environ.pop("SATELLITE_TOOLKIT_WHERE", None)
    os.makedirs(os.environ["HOME"])
    os.makedirs(os.environ["SATELLITE_SSH_DIR"])


def tearDownModule():
    for k, v in _SCRATCH["env"].items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_SCRATCH["dir"], ignore_errors=True)


# ================================================================ pure unit tests

class TakeOptions(unittest.TestCase):
    def take(self, *argv):
        return sr._sr_take(list(argv))

    def test_local_and_remote_forms(self):
        self.assertEqual(self.take("--local", "status")[1]["where"], "local")
        rest, o = self.take("--remote", "u@h", "status")
        self.assertEqual((rest, o["where"], o["target"]), (["status"], "remote", "u@h"))
        rest, o = self.take("--remote=u@h", "build", "-j", "4")
        self.assertEqual((rest, o["target"]), (["build", "-j", "4"], "u@h"))
        rest, o = self.take("--remote", "build")          # a word with no @ is the tool's
        self.assertEqual((rest, o["where"], o["target"]), (["build"], "remote", None))
        rest, o = self.take("--remote", "-x@y")           # an option is never a machine
        self.assertEqual((rest, o["target"]), (["-x@y"], None))
        self.assertIsNone(self.take("--remote=")[1]["target"])

    def test_attach_forms(self):
        name = "clang-20261008-235349"
        self.assertEqual(self.take("--attach")[1]["attach"], "")
        self.assertEqual(self.take("--attach=" + name)[1]["attach"], name)
        rest, o = self.take("--attach", name, "status")
        self.assertEqual((rest, o["attach"]), (["status"], name))
        rest, o = self.take("--attach", name + "-2")
        self.assertEqual((rest, o["attach"]), ([], name + "-2"))
        rest, o = self.take("--attach", "status")         # not shaped like a session: the tool's
        self.assertEqual((rest, o["attach"]), (["status"], ""))
        rest, o = self.take("--attach", "-v")
        self.assertEqual((rest, o["attach"]), (["-v"], ""))
        self.assertTrue(self.take("--sessions")[1]["sessions"])

    def test_double_dash_and_lookalikes_pass_through(self):
        rest, o = self.take("build", "--", "--local", "--remote=u@h", "--attach")
        self.assertEqual(rest, ["build", "--", "--local", "--remote=u@h", "--attach"])
        self.assertEqual((o["where"], o["attach"]), (None, None))
        for argv in (["--localversion=-x"], ["--localversion", "-gpu"], ["--remotely"],
                     ["--attachment"], ["--sessions-dir=x"]):
            self.assertEqual(self.take(*argv)[0], argv)
        rest, o = self.take("--local", "--localversion=-x", "build")
        self.assertEqual((rest, o["where"]), (["--localversion=-x", "build"], "local"))

    def test_internal_options(self):
        rest, o = self.take("--satellite-keeper=n1", "--satellite-size=24x80", "--", "sh", "-c", "x")
        self.assertEqual((o["keeper"], o["size"], rest), ("n1", "24x80", ["--", "sh", "-c", "x"]))
        self.assertEqual(self.take("--satellite-keeper-attach=n2")[1]["keeper_attach"], "n2")
        self.assertTrue(self.take("--satellite-keeper-list")[1]["keeper_list"])


class Helpers(unittest.TestCase):
    def test_split_target(self):
        self.assertEqual(sr._sr_split_target("u@h"), ("u", "h"))
        self.assertEqual(sr._sr_split_target("claude_code@192.168.1.20"), ("claude_code", "192.168.1.20"))
        self.assertEqual(sr._sr_split_target("u@[fe80::5a11:22ff:feba:9340]"), ("u", "fe80::5a11:22ff:feba:9340"))
        self.assertEqual(sr._sr_split_target("u@2001:db8::1"), ("u", "2001:db8::1"))
        for bad in ("", None, "uh", "@h", "u@", "u @h", "u@h x", "u@[]"):
            with self.assertRaises(sr.RemoteError, msg=repr(bad)):
                sr._sr_split_target(bad)

    def test_size_and_exit_code(self):
        self.assertEqual(sr._sr_size("24x80"), (24, 80))
        self.assertEqual(sr._sr_size("50X200"), (50, 200))
        for bad in (None, "", "x", "24", "1x80", "24x5", "axb", "24x80x1"):
            self.assertEqual(sr._sr_size(bad), (40, 120), bad)
        self.assertEqual(sr._sr_size("bad", (0, 0)), (0, 0))
        self.assertEqual(sr._sr_exit_code(3 << 8), 3)
        self.assertEqual(sr._sr_exit_code(0), 0)
        self.assertEqual(sr._sr_exit_code(signal.SIGKILL), 137)

    def test_home_quote(self):
        self.assertEqual(sr._sr_home_quote("~/.satellite_toolkit/code/x"), '"$HOME"/.satellite_toolkit/code/x')
        self.assertEqual(sr._sr_home_quote("~/a b"), '"$HOME"\'/a b\'')
        self.assertEqual(sr._sr_home_quote("a b"), "'a b'")
        self.assertEqual(sr._sr_home_quote("~"), "'~'")
        self.assertEqual(sr._sr_home_quote("$(rm -rf /)"), "'$(rm -rf /)'")

    def test_ssh_words(self):
        self.assertIn("ssh-keygen -R", sr._sr_ssh_words("Host key verification failed."))
        self.assertIn("ssh-keygen -R", sr._sr_ssh_words("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@"))
        self.assertIn("refused", sr._sr_ssh_words("u@h: Permission denied (publickey)."))
        self.assertIn("sshd running", sr._sr_ssh_words("connect to host h port 22: Connection refused"))
        self.assertIn("cannot be reached", sr._sr_ssh_words("connect to host h port 22: Connection timed out"))
        self.assertIn("cannot be reached", sr._sr_ssh_words("connect to host h port 22: No route to host"))
        self.assertEqual(sr._sr_ssh_words("something else"), "")

    def test_session_names_per_tool(self):
        shorts = {"enterprise_clang_tool": "clang", "enterprise_gcc_tool": "gcc",
                  "enterprise_python_tool": "python", "enterprise_pypy_tool": "pypy",
                  "enterprise_linux_kernel_tool": "linux_kernel", "satellite_toolkit": "satellite_toolkit",
                  "satellite_remote": "satellite_remote"}
        for tool, short in shorts.items():
            self.assertEqual(sr._sr_short(tool), short)
            name = sr._sr_session_name(tool)
            self.assertTrue(name.startswith(short + "-"), name)
            self.assertRegex(name, r"-\d{8}-\d{6}$")
            # --attach NAME takes it, and so does the keeper -- with the -2 of a second one too
            for n in (name, name + "-2", name + "-99"):
                self.assertTrue(sr._SR_NAME_SHAPE.match(n), n)
                self.assertTrue(re.match(r"^[A-Za-z0-9._-]{1,48}$", n), n)
        for word in ("status", "build", "-x", "clang-2026", "a b-20261008-235349", "x-20261008-235349-1000"):
            self.assertFalse(sr._SR_NAME_SHAPE.match(word), word)
        self.assertEqual(sr._sr_short("___"), "tool")

    def test_sudo_question(self):
        m = sr._SR_SUDO.search(b"building...\r\n[sudo] password for madness: ")
        self.assertEqual(m.group(1), b"madness")
        self.assertTrue(sr._SR_SUDO.search(b"[sudo] password for claude_code:"))
        for no in (b"[sudo] password for madness: \r\nSorry, try again.", b"password for madness: ",
                   b"echo '[sudo] password for madness: ' >> notes", b"Password: "):
            self.assertIsNone(sr._SR_SUDO.search(no), no)

    def test_wants_question(self):
        saved_tty, saved_env = sr._sr_tty, os.environ.get("SATELLITE_TOOLKIT_WHERE")
        try:
            sr._sr_tty = lambda: True
            self.assertTrue(sr._sr_wants_question(["status"]))
            for flag in ("-h", "--help", "--tool-version"):
                self.assertFalse(sr._sr_wants_question(["status", flag]), flag)
            # --version picks an LLVM / CPython / GCC release: a build like that still asks
            self.assertTrue(sr._sr_wants_question(["build", "--version", "19.1.0"]))
            self.assertTrue(sr._sr_wants_question(["build", "--version=19.1.0"]))
            os.environ["SATELLITE_TOOLKIT_WHERE"] = "local"
            self.assertFalse(sr._sr_wants_question(["status"]))
            os.environ["SATELLITE_TOOLKIT_WHERE"] = "remote"      # only "local" means anything
            self.assertTrue(sr._sr_wants_question(["status"]))
            sr._sr_tty = lambda: False
            self.assertFalse(sr._sr_wants_question(["status"]))
        finally:
            sr._sr_tty = saved_tty
            if saved_env is None:
                os.environ.pop("SATELLITE_TOOLKIT_WHERE", None)
            else:
                os.environ["SATELLITE_TOOLKIT_WHERE"] = saved_env

    def test_pack(self):
        d = tempfile.mkdtemp(prefix="sr-pack-")
        try:
            a = put(os.path.join(d, "tool.py"), "print(1)\n", 0o4777)
            b = put(os.path.join(d, "data.txt"), "x\n", 0o600)
            digest, blob = sr._sr_pack([(a, "tool.py"), (b, "lib/data.txt")])
            self.assertEqual(digest, sr._sr_pack([(b, "lib/data.txt"), (a, "tool.py")])[0])   # order-free
            self.assertNotEqual(digest, sr._sr_pack([(a, "tool2.py"), (b, "lib/data.txt")])[0])
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                members = {m.name: m for m in tar.getmembers()}
                self.assertEqual(sorted(members), ["lib/data.txt", "tool.py"])
                self.assertEqual(members["tool.py"].mode, 0o755)       # no setuid, no group/other write
                self.assertEqual(members["lib/data.txt"].mode, 0o600)
                self.assertEqual(tar.extractfile("tool.py").read(), b"print(1)\n")
            put(a, "print(2)\n", 0o755)
            self.assertNotEqual(digest, sr._sr_pack([(a, "tool.py"), (b, "lib/data.txt")])[0])
        finally:
            shutil.rmtree(d)

    def test_satellite_ssh_found_only_where_it_is_said(self):
        self.assertIsNone(sr._sr_satellite_ssh())         # scratch HOME, empty SATELLITE_SSH_DIR
        d = tempfile.mkdtemp(prefix="sr-stub-")
        saved = os.environ["SATELLITE_SSH_DIR"]
        try:
            shutil.copy(os.path.join(SUPPORT, "stub_satellite_ssh.py"), os.path.join(d, "satellite_ssh.py"))
            os.environ["SATELLITE_SSH_DIR"] = d
            mod = sr._sr_satellite_ssh()
            self.assertTrue(mod.has_password("u", "h"))
            self.assertEqual(mod.read_kept("u", "h"), "s3cret-test")
        finally:
            os.environ["SATELLITE_SSH_DIR"] = saved
            sys.modules.pop("satellite_ssh", None)
            if d in sys.path:
                sys.path.remove(d)
            shutil.rmtree(d)

    def test_command_words(self):
        m = sr.RemoteMachine("u@h")
        m.link.python = "/usr/bin/python3"
        self.assertEqual(m.command("~/.satellite_toolkit/code/t-abc", "t.py", ["status", "a b"]),
                         "/usr/bin/python3 -I \"$HOME\"/.satellite_toolkit/code/t-abc/t.py --local status 'a b'")


class RememberTarget(unittest.TestCase):
    def setUp(self):
        self.path = os.path.expanduser(sr._SR_CONFIG)
        self.assertTrue(self.path.startswith(_SCRATCH["dir"]))
        self.saved_tty = sr._sr_tty
        sr._sr_tty = lambda: False

    def tearDown(self):
        sr._sr_tty = self.saved_tty
        shutil.rmtree(os.path.dirname(self.path), ignore_errors=True)

    def test_corrupt_or_odd_config_is_empty(self):
        self.assertEqual(sr._sr_config(), {})
        for text in ("{not json", "[1, 2]", "null", ""):
            put(self.path, text)
            self.assertEqual(sr._sr_config(), {}, text)
        sr._sr_remember("u@h", "enterprise_clang_tool")     # over a corrupt file: it starts again
        self.assertEqual(sr._sr_config()["last"], "u@h")

    def test_remember_per_tool(self):
        sr._sr_remember("a@one", "enterprise_clang_tool")
        sr._sr_remember("b@two", "enterprise_python_tool")
        cfg = json.loads(read(self.path))
        self.assertEqual(cfg["last"], "b@two")
        self.assertEqual(cfg["by_tool"], {"enterprise_clang_tool": "a@one", "enterprise_python_tool": "b@two"})
        self.assertFalse(os.path.exists(self.path + ".writing"))
        o = {"target": None, "attach": "", "sessions": False}
        self.assertEqual(sr._sr_target(o, "enterprise_clang_tool"), "a@one")     # the tool's own first
        self.assertEqual(sr._sr_target(o, "enterprise_gcc_tool"), "b@two")       # then the last one
        self.assertEqual(sr._sr_target(dict(o, attach=None, sessions=True), "x"), "b@two")
        self.assertEqual(sr._sr_target(dict(o, target="c@three"), "x"), "c@three")
        with self.assertRaises(sr.RemoteError) as cm:      # a new run with nobody to ask
            sr._sr_target(dict(o, attach=None), "x")
        self.assertIn("the last one was b@two", str(cm.exception))


class WhereToRunInProcess(unittest.TestCase):
    def setUp(self):
        self.saved = (sys.argv, sr._sr_tty, sys.stderr)
        sr._sr_tty = lambda: False

    def tearDown(self):
        sys.argv, sr._sr_tty, sys.stderr = self.saved

    def test_argv_none_is_sys_argv(self):
        sys.argv = ["tool.py", "--local", "status", "--json"]
        self.assertEqual(sr.where_to_run(None, SR, "satellite_remote"), ["status", "--json"])
        self.assertEqual(sr.where_to_run(["build", "--local"], SR, "satellite_remote"), ["build"])
        self.assertEqual(sr.where_to_run([], SR, "satellite_remote"), [])

    def test_bad_target_exits_1(self):
        sys.stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            sr.where_to_run(["--remote=nohost", "status"], SR, "satellite_remote")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("ERROR: a remote machine is USER@HOST", sys.stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.expanduser(sr._SR_CONFIG)))


class AfterAttach(unittest.TestCase):
    """What an attach that came back means: only an ENDED session's code is the program's."""

    class Machine(object):
        target = "u@h"

        def __init__(self, rows=None, down=False):
            self.rows, self.down = rows or [], down

        def sessions(self, folder, entry):
            if self.down:
                raise sr.RemoteError("ssh: connect to host h port 22: Connection refused")
            return self.rows

    def after(self, machine, code):
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            got = sr._sr_after_attach(machine, "~/f", "t.py", "s-1", code, "\n  back to it:  HINT")
            return got, sys.stderr.getvalue()
        finally:
            sys.stderr = saved

    def test_running_is_a_drop(self):
        code, said = self.after(self.Machine([{"name": "s-1", "state": "running", "exit": None}]), 137)
        self.assertEqual(code, 255)
        self.assertIn("the connection dropped -- it keeps running there", said)
        self.assertIn("back to it:  HINT", said)

    def test_unreachable_may_still_run(self):
        # The local ssh was killed (137): unreachable now still says "dropped", 255.
        code, said = self.after(self.Machine(down=True), 137)
        self.assertEqual(code, 255)
        self.assertIn("may still be running", said)
        self.assertIn("back to it:  HINT", said)

    def test_ended_is_the_programs_code(self):
        rows = [{"name": "s-0", "state": "running", "exit": None}, {"name": "s-1", "state": "ended", "exit": 4}]
        code, said = self.after(self.Machine(rows), 255)
        self.assertEqual(code, 4)
        self.assertIn("ended: exit 4", said)
        self.assertEqual(self.after(self.Machine([{"name": "s-1", "state": "ended", "exit": 0}]), 0)[0], 0)

    def test_stopped_and_unknown(self):
        self.assertEqual(self.after(self.Machine([{"name": "s-1", "state": "stopped", "exit": None}]), 0)[0], 1)
        self.assertEqual(self.after(self.Machine([{"name": "s-1", "state": "stopped", "exit": None}]), 137)[0], 137)
        self.assertEqual(self.after(self.Machine([]), 2)[0], 2)          # no such session: as the attach said


class HeldMarker(unittest.TestCase):
    """The attach on the far side: the exit mark split across reads is never printed, and a
    tail that only looked like its start is."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sr-mark-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def attach(self, chunks, read_hello=True):
        """The keeper's side played by a thread: it takes the hello and sends chunks, then
        closes.  -> (the code, what the attach printed)."""
        self.n = getattr(self, "n", 0) + 1
        path = os.path.join(self.d, "socket-%d" % self.n)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        hello = []

        def serve():
            c, _ = srv.accept()
            try:
                buf = b""
                while read_hello and b"\n" not in buf:
                    buf += c.recv(256)
                hello.append(buf)
                for ch in chunks:
                    c.sendall(ch)
                    time.sleep(0.05)
            finally:
                c.close()
                srv.close()

        def too_long(signum, frame):
            raise AssertionError("the attach did not come back")
        t = threading.Thread(target=serve)
        t.daemon = True
        t.start()
        sys.stdout.flush()
        saved = os.dup(0), os.dup(1)
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            with tempfile.TemporaryFile() as out:
                os.dup2(null, 0)
                os.dup2(out.fileno(), 1)
                old = signal.signal(signal.SIGALRM, too_long)
                signal.alarm(20)                    # it waits on a socket: never for ever
                try:
                    code = sr._sr_attach_socket(self.d, "s-1", path, {"state": "running", "exit": None}, 24, 80)
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old)
                    os.dup2(saved[0], 0)
                    os.dup2(saved[1], 1)
                out.seek(0)
                printed = out.read()
        finally:
            for fd in saved + (null,):
                os.close(fd)
            t.join(5)
            srv.close()
        if read_hello:
            self.assertEqual(hello, [b"ATTACH 65536 24 80\n"])
        return code, printed

    def test_marker_split_across_reads(self):
        self.assertEqual(self.attach([b"hello\x1b]777;satel", b"lite-remote-exit;42\x07"]), (42, b"hello"))
        self.assertEqual(self.attach([b"abc\x1b]77", b"7;satellite-remote-exit;5\x07after"]), (5, b"abc"))
        self.assertEqual(self.attach([b"x" * 100000, b"\x1b", b"]777;satellite-remote-exit;", b"9", b"\x07"]),
                         (9, b"x" * 100000))

    def test_lookalike_tail_is_printed(self):
        put(os.path.join(self.d, "exit_code"), "3\n")
        put(os.path.join(self.d, "ended"), "now\n")
        self.assertEqual(self.attach([b"A\x1b]7", b"Z more"]), (3, b"A\x1b]7Z more"))
        os.unlink(os.path.join(self.d, "exit_code"))
        self.assertEqual(self.attach([b"plain\x1b]77"]), (1, b"plain\x1b]77"))     # cut off: no code

    def test_a_reset_connection_is_not_a_traceback(self):
        # The keeper's side closes with the hello unread: the attach sees a reset, not an end.
        put(os.path.join(self.d, "exit_code"), "7\n")
        self.assertEqual(self.attach([], read_hello=False), (7, b""))


# ================================================================ the keeper, with no ssh

class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sr-keep-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.home, "TMPDIR": self.tmp,
                    "SATELLITE_SSH_DIR": os.path.join(self.tmp, "none"), "LANG": "C.UTF-8"}

    def tearDown(self):
        stop_sessions(self.home)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sr(self, args, data=None, timeout=LIMIT):
        p = subprocess.run([PY, "-I", SR] + args, input=data, stdin=None if data is not None else subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def start(self, name, *cmd):
        rc, out, err = self.sr(["--satellite-keeper=" + name, "--satellite-size=24x80", "--"] + list(cmd))
        self.assertEqual(rc, 0, err)
        word = out.split()
        self.assertEqual((len(word), word[0]), (3, "ok"), out)
        return word[1], int(word[2])

    def attach(self, name, data=None):
        return self.sr(["--satellite-keeper-attach=" + name, "--satellite-size=24x80"], data)

    def sdir(self, name):
        return os.path.join(self.home, ".satellite_toolkit", "sessions", name)

    def listing(self):
        rc, out, err = self.sr(["--satellite-keeper-list"])
        self.assertEqual(rc, 0, err)
        return {s["name"]: s for s in json.loads(out)}

    def test_reply_and_unique_names(self):
        names = [self.start("k-20261008-120000", "sh", "-c", "exit 0")[0] for _ in range(3)]
        self.assertEqual(names, ["k-20261008-120000", "k-20261008-120000-2", "k-20261008-120000-3"])
        for n in names:
            self.assertTrue(os.path.isfile(os.path.join(self.sdir(n), "cmd.json")))

    def test_bad_name_or_no_command(self):
        for args in (["--satellite-keeper=bad name", "--", "true"], ["--satellite-keeper=../x", "--", "true"],
                     ["--satellite-keeper=", "--", "true"], ["--satellite-keeper=k1", "--"]):
            rc, out, err = self.sr(args)
            self.assertEqual(rc, 2, args)
            self.assertIn("a session needs a plain name and a command", err)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".satellite_toolkit", "x")))

    def test_attach_returns_the_exit_code_and_list_says_it(self):
        name, pid = self.start("k1", "sh", "-c", "echo hello-from-keeper; exit 3")
        rc, out, err = self.attach(name)
        self.assertEqual(rc, 3, err)
        self.assertIn("hello-from-keeper", out)
        self.assertNotIn("\x1b]777", out)
        self.assertTrue(wait_for(lambda: self.listing()[name]["state"] == "ended"))
        s = self.listing()[name]
        self.assertEqual((s["exit"], s["argv"]), (3, ["sh", "-c", "echo hello-from-keeper; exit 3"]))
        self.assertEqual(read(os.path.join(self.sdir(name), "exit_code")).strip(), "3")
        self.assertEqual(os.stat(os.path.join(self.sdir(name), "output.log")).st_mode & 0o777, 0o600)

    def test_input_reaches_the_program(self):
        name, _ = self.start("k2", "sh", "-c", "read x; echo got:$x; exit 4")
        rc, out, err = self.attach(name, b"hello\n")
        self.assertEqual(rc, 4, err)
        self.assertIn("got:hello", out)

    def test_reattach_after_the_end_replays_the_tail(self):
        name, _ = self.start("k3", "sh", "-c", "head -c 100000 /dev/zero | tr '\\0' y; echo; echo LAST-LINE; exit 5")
        self.assertTrue(wait_for(lambda: os.path.exists(os.path.join(self.sdir(name), "exit_code"))
                                 and not os.path.exists(os.path.join(self.sdir(name), "socket"))))
        rc, out, err = self.attach(name)
        self.assertEqual(rc, 5, err)
        self.assertIn("LAST-LINE", out)
        self.assertLessEqual(len(out), sr._SR_TAIL)          # the tail, not the whole log
        self.assertIn("[%s ended: exit 5]" % name, err)

    def test_killed_keeper_is_stopped(self):
        name, prog = self.start("k4", "sh", "-c", "sleep 20")
        keeper = int(read(os.path.join(self.sdir(name), "pids")).split()[0])
        self.assertEqual(self.listing()[name]["state"], "running")
        os.kill(keeper, signal.SIGKILL)
        os.killpg(prog, signal.SIGKILL)
        # (a killed keeper is gone once something reaps it; in a container that can take seconds)
        self.assertTrue(wait_for(lambda: self.listing()[name]["state"] == "stopped", 15, 0.2))
        rc, out, err = self.attach(name)
        self.assertEqual(rc, 1)
        self.assertIn("stopped without an exit code", err)

    def test_long_home_gets_a_short_socket(self):
        self.home = self.env["HOME"] = os.path.join(self.tmp, "h" * 90)
        os.makedirs(self.home)
        name, _ = self.start("k5", "sh", "-c", "read x; echo got:$x; exit 6")
        sdir = self.sdir(name)
        self.assertGreaterEqual(len(os.path.join(sdir, "socket")), 100)
        short = read(os.path.join(sdir, "socket.path")).strip()
        self.assertTrue(short.startswith(os.path.join(self.tmp, "satellite-remote-%d" % os.getuid())), short)
        self.assertLess(len(short), 100)
        rc, out, err = self.attach(name, b"long\n")
        self.assertEqual(rc, 6, err)
        self.assertIn("got:long", out)

    def test_a_daemon_left_on_the_terminal_does_not_hold_the_session(self):
        # The daemon must outlive the hangup the end of sh sends its process group, or it
        # lets go of the terminal at once and the drain is never needed.  The sleep 0.5
        # sets the trap before sh ends; tearDown's killpg still reaches it.
        name, _ = self.start("k6", "sh", "-c",
                             "(trap '' HUP; sleep 12) & sleep 0.5; echo parent-done; exit 2")
        t0 = time.time()
        rc, out, err = self.attach(name)
        self.assertEqual(rc, 2, err)
        self.assertIn("parent-done", out)
        self.assertLess(time.time() - t0, 9)

    def test_a_late_hello_still_gets_the_end(self):
        # Connected while it ran, hello only after it ended: the keeper waits for it.
        name, _ = self.start("k8", "sh", "-c", WAIT_GO.replace("exit 4", "exit 8"))
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.settimeout(LIMIT)
            self.assertTrue(wait_for(lambda: os.path.exists(os.path.join(self.sdir(name), "socket"))))
            c.connect(os.path.join(self.sdir(name), "socket"))
            put(os.path.join(self.home, "go"), "")
            self.assertTrue(wait_for(lambda: os.path.exists(os.path.join(self.sdir(name), "exit_code"))))
            time.sleep(1.5)                     # the keeper has seen the end, and goes round again
            c.sendall(b"ATTACH 65536 24 80\n")
            got = b""
            while not got.endswith(b"\x07"):
                data = c.recv(65536)
                if not data:
                    break
                got += data
        finally:
            c.close()
        self.assertIn(b"begun", got)
        self.assertTrue(got.endswith(sr._SR_EXIT + b"8\x07"), got[-80:])

    def test_much_output_still_ends_with_the_code(self):
        name, _ = self.start("k7", "sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' x; echo; echo END; exit 9")
        rc, out, err = self.attach(name)
        self.assertEqual(rc, 9, err)
        self.assertTrue(out.rstrip().endswith("END"), out[-200:])
        self.assertNotIn("\x1b]777", out)


# ================================================================ end to end, with a fake ssh

class FakeSsh(unittest.TestCase):
    """A scratch HOME here, another there, and the fake ssh first on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sr-e2e-")
        self.here, self.there = os.path.join(self.tmp, "here"), os.path.join(self.tmp, "there")
        for d in (self.here, self.there, os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "no-ssh-stub")):
            os.makedirs(d)
        put(os.path.join(self.tmp, "bin", "ssh"), FAKE_SSH, 0o755)
        self.log = os.path.join(self.tmp, "ssh.log")
        self.env = {"PATH": os.path.join(self.tmp, "bin") + ":/usr/bin:/bin", "HOME": self.here,
                    "SATELLITE_SSH_DIR": os.path.join(self.tmp, "no-ssh-stub"), "TMPDIR": self.tmp,
                    "FAKE_REMOTE_HOME": self.there, "FAKE_SSH_LOG": self.log, "LANG": "C.UTF-8"}

    def tearDown(self):
        stop_sessions(self.there)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_sr(self, args, env=None, timeout=LIMIT, script=None):
        """satellite_remote.py (or script) args, with no terminal -> (code, stdout, stderr)."""
        p = subprocess.run([PY, "-I", "-B", script or SR] + list(args), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=dict(self.env, **(env or {})), timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def on_pty(self, args, rules=(), env=None, limit=20):
        code, out = pty_harness.run([PY, "-I", "-B", SR] + list(args), rules, limit, env=dict(self.env, **(env or {})))
        return code, out.decode("utf-8", "replace")

    def name_in(self, text):
        m = NAME_SAID.search(text)
        self.assertTrue(m, text)
        return m.group(1)

    def sessions_dir(self, name=""):
        return os.path.join(self.there, ".satellite_toolkit", "sessions", name)

    def calls(self):
        try:
            return [json.loads(l) for l in read(self.log).splitlines()]
        except OSError:
            return []

    def remembered(self):
        try:
            return json.loads(read(os.path.join(self.here, ".config", "satellite_toolkit", "remote.json")))
        except OSError:
            return None

    def ended_with(self, name, timeout=15):
        path = os.path.join(self.sessions_dir(name), "exit_code")
        self.assertTrue(wait_for(lambda: os.path.exists(path), timeout), name)
        return int(read(path))


class FakeSshTests(FakeSsh):
    def test_exit_code_and_output_come_back(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "echo remote-says-hi; exit 3"])
        self.assertEqual(rc, 3, err)
        self.assertIn("remote-says-hi", out)
        name = self.name_in(err)
        self.assertTrue(name.startswith("satellite_remote-"))
        self.assertIn("session %s on u@h ended: exit 3" % name, err)
        login = [c for c in self.calls() if "-M" in c][0]
        self.assertIn("ControlPersist=10m", login)
        self.assertIn("BatchMode=yes", login)             # nobody at a terminal: no password asked
        self.assertTrue(any("-O" in c and "exit" in c for c in self.calls()))   # the login closed
        self.assertEqual(self.remembered(), {"last": "u@h", "by_tool": {"satellite_remote": "u@h"}})
        self.assertFalse(os.path.exists(os.path.join(self.there, ".config")))  # nothing remembered THERE

    def test_upload_once_into_its_digest_folder(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "true"])
        self.assertEqual(rc, 0, err)
        self.assertIn("sending satellite_remote", err)
        digest = sr._sr_pack([(SR, "satellite_remote.py")])[0][:12]
        folder = os.path.join(self.there, ".satellite_toolkit", "code", "satellite_remote-" + digest)
        self.assertEqual(sorted(os.listdir(folder)), [".complete", "satellite_remote.py"])
        self.assertEqual(read(os.path.join(folder, "satellite_remote.py")), read(SR))
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(os.path.dirname(folder)), ["satellite_remote-" + digest])
        rc, out, err = self.run_sr(["--remote=u@h", "--", "true"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("sending", err)

    def test_sessions_and_attach(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "echo first-run; exit 3"])
        self.assertEqual(rc, 3, err)
        first = self.name_in(err)
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "echo second-run; exit 4"])
        self.assertEqual(rc, 4, err)
        second = self.name_in(err)
        self.assertNotEqual(first, second)
        rc, out, err = self.run_sr(["--sessions"])          # no --remote: the remembered machine
        self.assertEqual(rc, 0, err)
        self.assertIn("sessions on u@h:", out)
        self.assertRegex(out, re.escape(first) + r"\s+ended \(exit 3\).*-- sh -c echo first-run")
        self.assertRegex(out, re.escape(second) + r"\s+ended \(exit 4\)")
        rc, out, err = self.run_sr(["--remote=u@h", "--attach"])      # the newest
        self.assertEqual((rc, "second-run" in out), (4, True), err)
        rc, out, err = self.run_sr(["--attach=" + first])
        self.assertEqual((rc, "first-run" in out), (3, True), err)
        rc, out, err = self.run_sr(["--remote=u@h", "--attach", first])   # with a space
        self.assertEqual((rc, "first-run" in out), (3, True), err)
        rc, out, err = self.run_sr(["--remote=u@h", "--attach=bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("there is no session bogus here", out)          # said there, on its terminal

    def test_attach_alone_picks_this_tools_running_session(self):
        # --attach with no name: this tool's sessions only, and a running one before a newer one
        # that has ended.  satellite_toolkit-... is another tool's, running, and sorts after ours.
        older, newer, other = ("satellite_remote-20261008-100000", "satellite_remote-20261008-110000",
                               "satellite_toolkit-20261008-120000")
        for name, prog in ((older, WAIT_GO), (newer, "echo newer-ended; exit 3"),
                           (other, WAIT_GO.replace("exit 4", "exit 6"))):
            rc, out, err = self.run_sr(["--satellite-keeper=" + name, "--satellite-size=24x80", "--",
                                        "sh", "-c", prog], env={"HOME": self.there})
            self.assertEqual((rc, out.split()[:2]), (0, ["ok", name]), err)
        self.assertEqual(self.ended_with(newer), 3)
        attach = lambda: [c for c in self.calls() if any("--satellite-keeper-attach=" in w for w in c)]
        p = subprocess.Popen([PY, "-I", "-B", SR, "--remote=u@h", "--attach"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        try:
            # The choice is made before the attach is asked for: only then may the program end.
            self.assertTrue(wait_for(attach, LIMIT))
            put(os.path.join(self.there, "go"), "")
            out, err = p.communicate(timeout=LIMIT)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 4, err.decode("utf-8", "replace"))   # older's: not 3, not 6
        self.assertIn("--satellite-keeper-attach=" + older, " ".join(attach()[0]))

    def test_attach_with_nothing_there(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--attach"])
        self.assertEqual(rc, 1)
        self.assertIn("no session of satellite_remote on u@h", err)
        rc, out, err = self.run_sr(["--remote=u@h", "--sessions"])
        self.assertEqual((rc, out.strip().splitlines()[-1].strip()), (0, "none"))

    def test_no_target_without_a_terminal(self):
        for args in (["--remote", "--", "true"], ["--sessions"], ["--attach"]):
            rc, out, err = self.run_sr(args)
            self.assertEqual(rc, 1, args)
            self.assertIn("--remote needs USER@HOST when nobody is at a terminal", err)
        self.assertEqual(self.calls(), [])

    def test_bad_target_and_failed_login_are_not_remembered(self):
        rc, out, err = self.run_sr(["--remote=nohost", "--", "true"])
        self.assertEqual(rc, 1)
        self.assertIn("a remote machine is USER@HOST, not 'nohost'", err)
        rc, out, err = self.run_sr(["--remote=u@h", "--", "true"],
                                   env={"FAKE_SSH_LOGIN_RC": "255", "FAKE_SSH_LOGIN_ERR": "Host key verification failed."})
        self.assertEqual(rc, 1)
        self.assertIn("cannot log in to u@h", err)
        self.assertIn("ssh-keygen -R", err)
        rc, out, err = self.run_sr(["--remote=u@h", "--", "true"],
                                   env={"FAKE_SSH_LOGIN_RC": "255", "FAKE_SSH_LOGIN_ERR": "u@h: Permission denied (publickey)."})
        self.assertIn("the login was refused", err)
        self.assertIsNone(self.remembered())
        self.assertEqual([c for c in self.calls() if "-M" not in c], [])   # nothing ran past the login
        rc, out, err = self.run_sr(["--remote=u@h", "--", "true"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_sr(["--remote=v@other", "--", "true"], env={"FAKE_SSH_LOGIN_RC": "255"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.remembered()["last"], "u@h")           # the one that let us in

    def test_local_never_goes(self):
        rc, out, err = self.run_sr(["--local", "a", "b"])
        self.assertEqual((rc, out.strip()), (0, "here: a b"), err)
        rc, out, err = self.run_sr(["a"], env={"SATELLITE_TOOLKIT_WHERE": "local"})
        self.assertEqual((rc, out.strip()), (0, "here: a"), err)
        rc, out, err = self.run_sr(["a"])                  # no terminal, no option: here, unasked
        self.assertEqual((rc, out.strip()), (0, "here: a"), err)
        self.assertNotIn("Run where", out + err)
        self.assertEqual(self.calls(), [])

    def test_dropped_connection_is_not_the_end(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", WAIT_GO], env={"FAKE_SSH_DROP_AFTER": "1.5"})
        self.assertEqual(rc, 255, err)
        self.assertIn("begun", out)
        name = self.name_in(err)
        self.assertIn("the connection dropped -- it keeps running there", err)
        self.assertIn("back to it:  satellite_remote.py --remote=u@h --attach=" + name, err)
        self.assertNotIn("ended: exit", err)
        self.assertFalse(os.path.exists(os.path.join(self.sessions_dir(name), "exit_code")))
        put(os.path.join(self.there, "go"), "")
        self.assertEqual(self.ended_with(name), 4)          # it went on there, and ended its own way
        rc, out, err = self.run_sr(["--attach=" + name])
        self.assertEqual(rc, 4, err)
        self.assertIn("went", out)

    def test_dropped_and_gone_may_still_run(self):
        down = os.path.join(self.tmp, "down")
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", WAIT_GO],
                                   env={"FAKE_SSH_DROP_AFTER": "1.5", "FAKE_SSH_DOWN": down})
        self.assertEqual(rc, 255, err)
        name = self.name_in(err)
        self.assertIn("cannot be reached again -- session %s may still be running there" % name, err)
        self.assertIn("--attach=" + name, err)
        os.unlink(down)
        put(os.path.join(self.there, "go"), "")
        self.assertEqual(self.ended_with(name), 4)

    def test_interrupt_without_a_terminal(self):
        outf, errf = os.path.join(self.tmp, "out"), os.path.join(self.tmp, "err")
        with open(outf, "wb") as o, open(errf, "wb") as e:
            p = subprocess.Popen([PY, "-I", "-B", SR, "--remote=u@h", "--", "sh", "-c", WAIT_GO],
                                 stdin=subprocess.DEVNULL, stdout=o, stderr=e, env=self.env)
        try:
            self.assertTrue(wait_for(lambda: "begun" in read(outf), 15), read(errf))
            p.send_signal(signal.SIGINT)
            self.assertEqual(p.wait(LIMIT), 130)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        err = read(errf)
        name = self.name_in(err)
        self.assertIn("interrupted here -- it keeps running there", err)
        self.assertIn("--attach=" + name, err)
        self.assertNotIn("Traceback", err)
        put(os.path.join(self.there, "go"), "")
        self.assertEqual(self.ended_with(name), 4)

    def test_remote_program_gets_its_arguments_unchanged(self):
        args = ["a b", "$HOME", "'q'", "*", "--local", "--remote=x@y"]
        rc, out, err = self.run_sr(["--remote=u@h", "--", "/usr/bin/python3", "-c",
                                    "import sys, json; print(json.dumps(sys.argv[1:]))"] + args)
        self.assertEqual(rc, 0, err)
        self.assertIn(json.dumps(args), out.replace("\r", ""))


class SameSecondTests(FakeSsh):
    """Two runs that ask for one session name in the same second both start (the second as
    NAME-2), and each attach finds its own."""

    def setUp(self):
        FakeSsh.setUp(self)
        self.driver = put(os.path.join(self.tmp, "driver.py"), DRIVER % {"repo": REPO})
        self.env["DRIVER_NAME"] = "satellite_remote-20261008-120000"

    def test_second_is_name_2(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "exit 3"], script=self.driver)
        self.assertEqual(rc, 3, err)
        self.assertEqual(self.name_in(err), "satellite_remote-20261008-120000")
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "exit 5"], script=self.driver)
        self.assertEqual(rc, 5, err)                        # 5, not 3: it attached to its own
        self.assertEqual(self.name_in(err), "satellite_remote-20261008-120000-2")

    def test_three_at_once(self):
        # ... on a machine the tool was never sent to: three uploads of it at once, too.
        procs = [subprocess.Popen([PY, "-I", "-B", self.driver, "--remote=u@h", "--", "sh", "-c", "sleep 0.5; exit %d" % n],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
                 for n in (11, 12, 13)]
        results = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=LIMIT)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            results.append((p.returncode, err.decode("utf-8", "replace")))
        for n, (rc, err) in zip((11, 12, 13), results):
            self.assertEqual(rc, n, err)
            self.assertNotIn("File exists", err)
        base = "satellite_remote-20261008-120000"
        self.assertEqual(sorted(self.name_in(err) for rc, err in results), [base, base + "-2", base + "-3"])
        code = os.path.join(self.there, ".satellite_toolkit", "code")
        self.assertEqual(len(os.listdir(code)), 1, os.listdir(code))       # one folder, no .part left
        folder = os.path.join(code, os.listdir(code)[0])
        self.assertEqual(sorted(os.listdir(folder)), [".complete", "satellite_remote.py"])


class BeforeHookTests(FakeSsh):
    def setUp(self):
        FakeSsh.setUp(self)
        self.driver = put(os.path.join(self.tmp, "driver.py"), DRIVER % {"repo": REPO})
        self.record = os.path.join(self.tmp, "record")
        self.env["DRIVER_RECORD"] = self.record

    def hook_calls(self):
        try:
            return [json.loads(l) for l in read(self.record).splitlines()]
        except OSError:
            return []

    def drive(self, args, mode):
        return self.run_sr(args, env={"DRIVER_BEFORE": mode}, script=self.driver)

    def test_called_for_a_new_session_and_can_change_the_arguments(self):
        rc, out, err = self.drive(["--remote=u@h", "--", "sh", "-c", 'echo "args:$*"', "zero"], "rewrite")
        self.assertEqual(rc, 0, err)
        self.assertIn("args:rewritten", out)
        calls = self.hook_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["rest"], ["--", "sh", "-c", 'echo "args:$*"', "zero"])
        self.assertTrue(calls[0]["folder"].startswith("~/.satellite_toolkit/code/satellite_remote-"))
        self.assertTrue(calls[0]["uploaded"])               # after the upload
        self.assertTrue(calls[0]["python"])                 # after the python there was found
        self.assertEqual(read(os.path.join(self.there, "hook.txt")).strip(), "hook-was-there")
        rc, out, err = self.drive(["--remote=u@h", "--", "sh", "-c", 'echo "args:$*"', "zero"], "none")
        self.assertEqual(rc, 0, err)
        self.assertIn("args:", out)
        self.assertNotIn("rewritten", out)                  # None: the arguments as they were
        self.assertEqual(len(self.hook_calls()), 2)

    def test_not_called_for_attach_or_sessions(self):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "sh", "-c", "exit 3"])
        self.assertEqual(rc, 3, err)
        name = self.name_in(err)
        for args in (["--remote=u@h", "--sessions"], ["--remote=u@h", "--attach"], ["--remote=u@h", "--attach=" + name],
                     ["--remote=u@h", "--attach", name]):
            rc, out, err = self.drive(args, "rewrite")
            self.assertIn(rc, (0, 3), (args, err))
        self.assertEqual(self.hook_calls(), [])

    def test_its_error_starts_nothing(self):
        rc, out, err = self.drive(["--remote=u@h", "--", "sh", "-c", "echo should-not-run"], "error")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: the hook says no", err)
        self.assertNotIn("should-not-run", out)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.isdir(self.sessions_dir()) and os.listdir(self.sessions_dir()))
        self.assertEqual(len(self.hook_calls()), 1)
        rc, out, err = self.drive(["--remote=u@h", "--", "sh", "-c", "echo should-not-run"], "interrupt")
        self.assertEqual(rc, 130)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.isdir(self.sessions_dir()) and os.listdir(self.sessions_dir()))

    def test_local_run_never_calls_it(self):
        rc, out, err = self.drive(["--local", "x"], "rewrite")
        self.assertEqual((rc, out.strip()), (0, "here: x"), err)
        self.assertEqual(self.hook_calls(), [])


class SudoTests(FakeSsh):
    """satellite_ssh keeps a password (the stub): the sudo question there is answered with it."""

    def setUp(self):
        FakeSsh.setUp(self)
        stub = os.path.join(self.tmp, "stub")
        os.makedirs(stub)
        shutil.copy(os.path.join(SUPPORT, "stub_satellite_ssh.py"), os.path.join(stub, "satellite_ssh.py"))
        self.env["SATELLITE_SSH_DIR"] = stub
        put(os.path.join(self.there, "sudo_prog.py"), SUDO_PROG)

    def sudo(self, *args):
        rc, out, err = self.run_sr(["--remote=u@h", "--", "/usr/bin/python3", "-I", "sudo_prog.py"] + list(args))
        name = self.name_in(err)
        self.assertEqual(rc, 0, err)
        log = read(os.path.join(self.sessions_dir(name), "output.log"), "rb")
        for text in (out, err, log.decode("utf-8", "replace")):
            self.assertNotIn("s3cret-test", text)
        m = re.search(r"RESULT ([a-z ]+)", out)
        self.assertTrue(m, out)
        return m.group(1).split()

    def test_answered_for_the_login_user(self):
        self.assertEqual(self.sudo("u", "ok", "10", "0"), ["right"])
        self.assertTrue(any(c[:1] == ["-M"] for c in self.calls()))      # the stub's own login

    def test_not_answered_for_another_user(self):
        self.assertEqual(self.sudo("root", "ok", "2.5", "0"), ["none"])

    def test_a_refused_answer_is_not_repeated(self):
        self.assertEqual(self.sudo("u", "refuse", "10", "2.5"), ["right", "none"])


class TerminalTests(FakeSsh):
    """On a terminal: the question, typing, Ctrl-], --attach coming back."""

    def test_enter_means_here(self):
        code, out = self.on_pty(["one", "two"], [(b"[l]: ", b"\r")])
        self.assertEqual(code, 0, out)
        self.assertIn("Run where?", out)
        self.assertIn("here: one two", out)
        self.assertEqual(self.calls(), [])

    def test_help_and_the_environment_never_ask(self):
        code, out = self.on_pty(["--help"], limit=10)
        self.assertEqual((code, "Run where?" in out, "here: --help" in out), (0, False, True), out)
        code, out = self.on_pty(["x"], env={"SATELLITE_TOOLKIT_WHERE": "local"}, limit=10)
        self.assertEqual((code, "Run where?" in out, "here: x" in out), (0, False, True), out)

    def test_r_then_the_machine_then_it_is_remembered(self):
        code, out = self.on_pty(["--", "sh", "-c", "echo ran-there; exit 3"],
                                [(b"[l]: ", b"r\r"), (b"(USER@HOST): ", b"u@h\r")])
        self.assertEqual(code, 3, out)
        self.assertIn("ran-there", out)
        login = [c for c in self.calls() if "-M" in c][0]
        self.assertNotIn("BatchMode=yes", login)           # a person is there: a password may be typed
        code, out = self.on_pty(["--", "sh", "-c", "echo again; exit 4"],
                                [(b"[l]: ", b"r\r"), (b"[u@h]: ", b"\r")])
        self.assertEqual(code, 4, out)
        self.assertIn("again", out)

    def test_typing_reaches_the_program(self):
        code, out = self.on_pty(["--remote=u@h", "--", "sh", "-c", "echo ready; read x; echo got:$x; exit 7"],
                                [(b"ready", b"hello\r")])
        self.assertEqual(code, 7, out)
        self.assertIn("got:hello", out)

    def test_ctrl_bracket_detaches_and_attach_comes_back(self):
        code, out = self.on_pty(["--remote=u@h", "--", "sh", "-c", "echo waiting; read x; echo got:$x; exit 5"],
                                [(b"waiting", b"\x1d")])
        self.assertEqual(code, 75, out)
        self.assertIn("detached -- it keeps running there.", out)
        name = self.name_in(out)
        self.assertIn("back to it:  satellite_remote.py --remote=u@h --attach=" + name, out)
        self.assertFalse(os.path.exists(os.path.join(self.sessions_dir(name), "exit_code")))
        code, out = self.on_pty(["--remote=u@h", "--attach"], [(b"waiting", b"again\r")])
        self.assertEqual(code, 5, out)
        self.assertIn("got:again", out)
        self.assertEqual(self.ended_with(name), 5)


# ================================================================ sync_remote.py and the tools

class SyncTests(unittest.TestCase):
    BODY = "line one\nline two"

    def test_synced(self):
        text = "head\n# >>> satellite_remote >>>\nold\nold too\n# <<< satellite_remote <<<\ntail\n"
        new = sync_remote.synced(text, self.BODY)
        self.assertEqual(new, "head\n%s\n%s\n%s\ntail\n" % (sync_remote.BEGIN, self.BODY, sync_remote.END))
        self.assertEqual(sync_remote.synced(new, self.BODY), new)                 # idempotent
        self.assertIsNone(sync_remote.synced("no markers\n", self.BODY))
        self.assertIsNone(sync_remote.synced(text + text, self.BODY))             # two pairs
        self.assertIsNone(sync_remote.synced("# <<< satellite_remote <<<\n# >>> satellite_remote >>>\n", self.BODY))
        self.assertIsNone(sync_remote.synced("# >>> satellite_remote >>>\n", self.BODY))
        self.assertIsNone(sync_remote.synced("  # <<< satellite_remote <<<\n# >>> satellite_remote >>>\n", self.BODY))

    def test_block_is_between_the_embed_lines(self):
        lines = read(SR).split("\n")
        self.assertEqual(sync_remote.block(), "\n".join(lines[lines.index(sync_remote.FROM) + 1:lines.index(sync_remote.TO)]))
        self.assertIn("def where_to_run(", sync_remote.block())

    def test_sync_on_a_copy(self):
        d = tempfile.mkdtemp(prefix="sr-sync-")
        try:
            for f in ("sync_remote.py", "satellite_remote.py"):
                shutil.copy(os.path.join(REPO, f), os.path.join(d, f))
            tool = put(os.path.join(d, "tools", "clang", "enterprise_clang_tool.py"),
                       "head\n# >>> satellite_remote >>>\nold\n# <<< satellite_remote <<<\ntail\n", 0o751)

            def sync(*args):
                p = subprocess.run([PY, "-I", "-B", os.path.join(d, "sync_remote.py")] + list(args),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=LIMIT)
                return p.returncode, p.stdout.decode()
            rc, out = sync("--check")
            self.assertEqual(rc, 1, out)
            self.assertIn("differs: tools/clang/enterprise_clang_tool.py", out)
            self.assertIn("head\n# >>> satellite_remote >>>\nold\n", read(tool))     # --check wrote nothing
            rc, out = sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("written: tools/clang/enterprise_clang_tool.py", out)
            self.assertIn("satellite_toolkit.py: not there yet, skipped", out)
            self.assertEqual(os.stat(tool).st_mode & 0o7777, 0o751)
            self.assertEqual(read(tool), "head\n%s\n%s\n%s\ntail\n" % (sync_remote.BEGIN, sync_remote.block(), sync_remote.END))
            self.assertEqual(sync("--check")[0], 0)
            rc, out = sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("every tool here (1 of %d)" % len(sync_remote.TOOLS), out)
            rc, out = sync("--check", "--strict")
            self.assertEqual(rc, 1, out)
            self.assertIn("! satellite_toolkit.py: missing", out)
            put(tool, "no markers at all\n", 0o751)
            rc, out = sync()
            self.assertEqual(rc, 1, out)
            self.assertIn("no (or more than one) marker pair", out)
            self.assertEqual([f for f in os.listdir(os.path.dirname(tool)) if f.endswith(".syncing")], [])
        finally:
            shutil.rmtree(d)


def _main_of(path):
    tree = ast.parse(read(path))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    return tree, (mains[-1] if mains else None)


def _bound(nodes):
    """Top-level names bound by nodes -> {name: [what binds it]}; an import says what it imports."""
    out = {}

    def add(name, what):
        out.setdefault(name, []).append(what)

    def targets(t):
        if isinstance(t, ast.Name):
            add(t.id, "assign")
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                targets(e)
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            add(n.name, "def")
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                targets(t)
        elif isinstance(n, getattr(ast, "AnnAssign", ())):
            targets(n.target)
        elif isinstance(n, ast.Import):
            for a in n.names:
                add(a.asname or a.name.split(".")[0], "import " + a.name)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                add(a.asname or a.name, "from %s import %s" % (n.module, a.name))
        elif isinstance(n, (ast.If, ast.Try, getattr(ast, "TryExcept", ast.Try))):
            inner = list(getattr(n, "body", [])) + list(getattr(n, "orelse", [])) + list(getattr(n, "finalbody", []))
            for h in getattr(n, "handlers", []):
                inner.extend(h.body)
            for k, v in _bound(inner).items():
                out.setdefault(k, []).extend(v)
    return out


class ContractTests(unittest.TestCase):
    """What PLAN.md section 3 promises of every tool, checked on the real tree."""

    def test_the_tools_are_there(self):
        for rel in KNOWN:
            self.assertIn(rel, TOOLS)

    def test_sync_check_passes(self):
        p = subprocess.run([PY, "-I", "-B", os.path.join(REPO, "sync_remote.py"), "--check"], cwd=REPO,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=LIMIT)
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        for f in ("sync_remote.py", "satellite_remote.py"):
            self.assertTrue(os.access(os.path.join(REPO, f), os.X_OK), f)
            self.assertTrue(read(os.path.join(REPO, f)).startswith("#!/usr/bin/python3 -I\n"), f)

    def test_every_tool_holds_exactly_the_block(self):
        body = sync_remote.block().split("\n")
        for rel in TOOLS:
            lines = read(os.path.join(REPO, rel)).split("\n")
            begins = [i for i, l in enumerate(lines) if l.startswith("# >>> satellite_remote >>>")]
            ends = [i for i, l in enumerate(lines) if l == sync_remote.END]
            self.assertEqual((len(begins), len(ends)), (1, 1), rel)
            self.assertEqual(lines[begins[0]], sync_remote.BEGIN, rel)
            self.assertTrue(lines[begins[0] + 1:ends[0]] == body, "%s: its block differs from satellite_remote.py's "
                            "(run ./sync_remote.py)" % rel)

    def test_main_hands_argv_to_where_to_run_first(self):
        want = ast.dump(ast.parse("list(sys.argv[1:] if argv is None else argv)", mode="eval").body)
        for rel in TOOLS:
            tree, main = _main_of(os.path.join(REPO, rel))
            self.assertIsNotNone(main, rel)
            self.assertEqual([a.arg for a in main.args.args], ["argv"], rel)
            self.assertEqual(len(main.args.defaults), 1, rel)
            body = main.body[1:] if ast.get_docstring(main) is not None else main.body
            first = body[0]
            self.assertIsInstance(first, ast.Assign, "%s: main()'s first statement is not argv = where_to_run(...)" % rel)
            self.assertEqual([getattr(t, "id", None) for t in first.targets], ["argv"], rel)
            call = first.value
            self.assertIsInstance(call, ast.Call, rel)
            self.assertEqual(getattr(call.func, "id", None), "where_to_run", rel)
            self.assertEqual(ast.dump(call.args[0]), want, rel)
            self.assertEqual(getattr(call.args[1], "id", None), "__file__", rel)
            self.assertEqual(getattr(call.args[2], "id", None), "TOOL", rel)
            if rel in KNOWN:              # the line PLAN.md gives, word for word
                self.assertEqual(ast.dump(first), ast.dump(ast.parse(
                    "argv = where_to_run(list(sys.argv[1:] if argv is None else argv), __file__, TOOL)").body[0]))

    def test_tool_names_give_their_session_names(self):
        for rel, short in KNOWN.items():
            tree = ast.parse(read(os.path.join(REPO, rel)))
            tool = [n.value for n in tree.body if isinstance(n, ast.Assign)
                    and [getattr(t, "id", None) for t in n.targets] == ["TOOL"]]
            self.assertEqual(len(tool), 1, rel)
            self.assertEqual(sr._sr_short(ast.literal_eval(tool[0])), short)

    def test_python36_syntax_and_ascii(self):
        files = [SR, os.path.join(REPO, "sync_remote.py"), os.path.abspath(__file__)]
        files += [os.path.join(SUPPORT, f) for f in os.listdir(SUPPORT) if f.endswith(".py")]
        files += [os.path.join(REPO, rel) for rel in TOOLS]
        for path in files:
            with open(path, "rb") as f:
                data = f.read()
            bad = [n + 1 for n, line in enumerate(data.split(b"\n")) if any(b > 127 for b in bytearray(line))]
            self.assertEqual(bad, [], "%s: not ASCII on lines %s" % (path, bad[:10]))
            ast.parse(data.decode("ascii"), filename=path, feature_version=(3, 6))
        for snippet in (FAKE_SSH, DRIVER % {"repo": REPO}, SUDO_PROG):
            ast.parse(snippet, feature_version=(3, 6))

    def test_block_names_are_its_own(self):
        for rel in TOOLS:
            text = read(os.path.join(REPO, rel))
            lines = text.split("\n")
            a = [i for i, l in enumerate(lines) if l.startswith("# >>> satellite_remote >>>")][0] + 1
            b = lines.index(sync_remote.END) + 1
            tree = ast.parse(text)
            inside = _bound([n for n in tree.body if a < n.lineno < b])
            outside = _bound([n for n in tree.body if not a < n.lineno < b])
            for name in set(inside) & set(outside):
                kinds = set(inside[name] + outside[name])
                # the same standard module imported on both sides is fine; anything else clashes
                self.assertTrue(len(kinds) == 1 and kinds.pop().startswith("import "),
                                "%s: %s is defined both in the block and in the tool (%s / %s)"
                                % (rel, name, inside[name], outside[name]))

    def test_tools_strip_local_and_refuse_a_bad_machine(self):
        d = tempfile.mkdtemp(prefix="sr-tools-")
        env = {"PATH": "/usr/bin:/bin", "HOME": d, "SATELLITE_SSH_DIR": d, "LANG": "C.UTF-8"}
        try:
            for rel in TOOLS:
                path = os.path.join(REPO, rel)
                p = subprocess.run([PY, "-I", "-B", path, "--local", "--help"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=LIMIT)
                self.assertEqual(p.returncode, 0, "%s --local --help: %s" % (rel, p.stderr.decode()[-2000:]))
                self.assertIn("usage", p.stdout.decode().lower(), rel)
                p = subprocess.run([PY, "-I", "-B", path, "--remote=nohost", "status"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=LIMIT)
                self.assertEqual(p.returncode, 1, rel)
                self.assertIn("ERROR: a remote machine is USER@HOST", p.stderr.decode(), rel)
            self.assertFalse(os.path.exists(os.path.join(d, ".config", "satellite_toolkit")))   # nothing remembered
        finally:
            shutil.rmtree(d)


class ToolsThroughFakeSsh(FakeSsh):
    def test_each_tool_runs_there(self):
        for rel in TOOLS:
            path = os.path.join(REPO, rel)
            tool = os.path.splitext(os.path.basename(path))[0]
            rc, out, err = self.run_sr(["--remote=u@h", "--help"], script=path)
            self.assertEqual(rc, 0, "%s: %s" % (rel, err[-2000:]))
            self.assertIn("usage", out.lower(), rel)
            name = self.name_in(err)
            self.assertTrue(name.startswith(sr._sr_short(tool) + "-"), name)
            cmd = json.loads(read(os.path.join(self.sessions_dir(name), "cmd.json")))["argv"]
            self.assertEqual(cmd[3:], ["--local", "--help"], rel)
            self.assertTrue(cmd[2].startswith(os.path.join(self.there, ".satellite_toolkit", "code", tool + "-")), cmd)


# ================================================================ a real sshd, when there is one

def _ssh_account():
    """-> (pwd entry, "") for the account a private sshd and its client can run as, or (None, why)."""
    for prog in ("ssh", "ssh-keygen"):
        if not shutil.which(prog):
            return None, "no %s here" % prog
    if not os.access("/usr/sbin/sshd", os.X_OK):
        return None, "no /usr/sbin/sshd here"
    if os.geteuid() != 0:
        return pwd.getpwuid(os.geteuid()), ""
    name = os.environ.get("SATELLITE_TEST_SSH_USER")
    if not name:
        return None, "running as root: SATELLITE_TEST_SSH_USER=<a non-root user> runs these as that user"
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None, "SATELLITE_TEST_SSH_USER=%s: no such user" % name
    if pw.pw_uid == 0:
        return None, "SATELLITE_TEST_SSH_USER=%s is root" % name
    return pw, ""


class RealSshdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pw, why = _ssh_account()
        if pw is None:
            raise unittest.SkipTest(why)
        cls.pw = pw
        cls.tmp = tempfile.mkdtemp(prefix="sr-sshd-")
        cls.sshd = None
        try:
            cls._start()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def _as_user(cls):
        if os.geteuid() == 0:
            os.setgroups(os.getgrouplist(cls.pw.pw_name, cls.pw.pw_gid))
            os.setgid(cls.pw.pw_gid)
            os.setuid(cls.pw.pw_uid)

    @classmethod
    def _own(cls, path):
        if os.geteuid() == 0:
            os.chown(path, cls.pw.pw_uid, cls.pw.pw_gid)

    @classmethod
    def _start(cls):
        t = cls.tmp
        cls.here, cls.there = os.path.join(t, "here"), os.path.join(t, "there")
        for d in (t, cls.here, cls.there, os.path.join(t, "no-ssh-stub")):
            os.makedirs(d, exist_ok=True)
            cls._own(d)
        for key in ("host_key", "user_key"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", os.path.join(t, key)],
                           check=True, stdout=subprocess.DEVNULL, preexec_fn=cls._as_user, timeout=LIMIT)
        put(os.path.join(t, "authorized_keys"), read(os.path.join(t, "user_key.pub")), 0o600)
        put(os.path.join(t, "force.sh"), "#!/bin/sh\nexport HOME=%s\nunset SATELLITE_TOOLKIT_WHERE\ncd \"$HOME\" || exit 1\n"
            "if [ -n \"$SSH_ORIGINAL_COMMAND\" ]; then exec /bin/sh -c \"$SSH_ORIGINAL_COMMAND\"; fi\nexec /bin/sh\n"
            % cls.there, 0o755)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        put(os.path.join(t, "sshd_config"), "\n".join([
            "Port %d" % cls.port, "ListenAddress 127.0.0.1", "HostKey %s/host_key" % t, "PidFile %s/sshd.pid" % t,
            "AuthorizedKeysFile %s/authorized_keys" % t, "PasswordAuthentication no",
            "KbdInteractiveAuthentication no", "PubkeyAuthentication yes", "UsePAM no", "StrictModes no",
            "PermitTTY yes", "PermitUserRC no", "PrintMotd no", "ForceCommand %s/force.sh" % t, "LogLevel VERBOSE", ""]))
        shutil.copy(SR, os.path.join(t, "satellite_remote.py"))
        for f in ("authorized_keys", "force.sh", "sshd_config", "satellite_remote.py"):
            cls._own(os.path.join(t, f))
        cls.sshd_log = open(os.path.join(t, "sshd.log"), "wb")
        cls.sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", os.path.join(t, "sshd_config")],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=cls.sshd_log,
                                    preexec_fn=cls._as_user)

        def listening():
            if cls.sshd.poll() is not None:
                return True
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.5).close()
                return True
            except OSError:
                return False
        wait_for(listening, 10)
        if cls.sshd.poll() is not None:
            raise unittest.SkipTest("the private sshd did not start: %s" % cls._log_tail())
        cls.target = "%s@127.0.0.1" % cls.pw.pw_name
        cls.options = ("-F /dev/null -p %d -i %s/user_key -o IdentitiesOnly=yes -o UserKnownHostsFile=%s/known_hosts "
                       "-o LogLevel=ERROR" % (cls.port, t, t))
        cls.env = {"PATH": "/usr/bin:/bin", "HOME": cls.here, "SATELLITE_SSH_DIR": os.path.join(t, "no-ssh-stub"),
                   "SATELLITE_SSH_OPTIONS": cls.options, "LANG": "C.UTF-8", "USER": cls.pw.pw_name,
                   "LOGNAME": cls.pw.pw_name}
        # A plain login and one with a terminal, first: when THEY fail it is this machine's sshd
        # setup, not satellite_remote, and the class is skipped saying so.
        for flags in (["-o", "StrictHostKeyChecking=accept-new", "-T"], ["-tt"]):
            p = subprocess.run(["ssh"] + cls.options.split() + ["-o", "BatchMode=yes"] + flags + [cls.target, "echo login-ok"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cls.env,
                               preexec_fn=cls._as_user, timeout=LIMIT)
            if b"login-ok" not in p.stdout:
                raise unittest.SkipTest("a login to the private sshd (%s) failed: %s %s%s" % (
                    " ".join(flags), p.stderr.decode("utf-8", "replace").strip(), cls._log_tail(),
                    "  (a user-mode sshd's pty: is the user in the tty group?)" if "-tt" in flags else ""))

    @classmethod
    def _log_tail(cls):
        try:
            return read(os.path.join(cls.tmp, "sshd.log"))[-1500:]
        except OSError:
            return ""

    @classmethod
    def tearDownClass(cls):
        stop_sessions(getattr(cls, "there", "/nonexistent"))
        if cls.sshd is not None and cls.sshd.poll() is None:
            cls.sshd.terminate()
            try:
                cls.sshd.wait(10)
            except subprocess.TimeoutExpired:
                cls.sshd.kill()
                cls.sshd.wait()
        # any ssh master or client of ours a failed test left (their command lines name our key)
        key = os.path.join(cls.tmp, "user_key")
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if key.encode() in read("/proc/%s/cmdline" % pid, "rb"):
                        os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        if getattr(cls, "sshd_log", None):
            cls.sshd_log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tool(self, args, timeout=LIMIT):
        p = subprocess.run([PY, "-I", "-B", os.path.join(self.tmp, "satellite_remote.py")] + list(args),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                           preexec_fn=self._as_user, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def masters(self):
        key = os.path.join(self.tmp, "user_key").encode()
        found = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    cmd = read("/proc/%s/cmdline" % pid, "rb")
                except OSError:
                    continue
                if key in cmd and b"-M\0" in cmd:
                    found.append(pid)
        return found

    def name_in(self, text):
        m = NAME_SAID.search(text)
        self.assertTrue(m, text)
        return m.group(1)

    def ended_with(self, name, timeout=15):
        path = os.path.join(self.there, ".satellite_toolkit", "sessions", name, "exit_code")
        self.assertTrue(wait_for(lambda: os.path.exists(path), timeout), name)
        return int(read(path))

    def test_exit_code_output_and_the_master_closed(self):
        rc, out, err = self.tool(["--remote=" + self.target, "--", "sh", "-c", "echo real-hello; exit 4"])
        self.assertEqual(rc, 4, err + self._log_tail())
        self.assertIn("real-hello", out)
        self.assertTrue(wait_for(lambda: not self.masters(), 5), self.masters())

    def test_typing_detach_and_attach(self):
        tool = [PY, "-I", "-B", os.path.join(self.tmp, "satellite_remote.py")]
        code, out = pty_harness.run(
            tool + ["--remote=" + self.target, "--", "sh", "-c", "echo ready; read x; echo got:$x; echo waiting; read y; exit 5"],
            [(b"ready", b"hello\r"), (b"waiting", b"\x1d")], 30, env=self.env, preexec=self._as_user)
        out = out.decode("utf-8", "replace")
        self.assertEqual(code, 75, out + self._log_tail())
        self.assertIn("got:hello", out)
        self.assertIn("detached -- it keeps running there.", out)
        name = self.name_in(out)
        code, out = pty_harness.run(tool + ["--remote=" + self.target, "--attach", name],
                                    [(b"waiting", b"bye\r")], 30, env=self.env, preexec=self._as_user)
        self.assertEqual(code, 5, out.decode("utf-8", "replace"))
        self.assertEqual(self.ended_with(name), 5)

    def test_killed_connection_is_a_drop(self):
        outf, errf = os.path.join(self.tmp, "drop.out"), os.path.join(self.tmp, "drop.err")
        with open(outf, "wb") as o, open(errf, "wb") as e:
            p = subprocess.Popen([PY, "-I", "-B", os.path.join(self.tmp, "satellite_remote.py"), "--remote=" + self.target,
                                  "--", "sh", "-c", WAIT_GO], stdin=subprocess.DEVNULL, stdout=o, stderr=e,
                                 env=self.env, preexec_fn=self._as_user)
        try:
            self.assertTrue(wait_for(lambda: "begun" in read(outf), 20), read(errf))
            # the ssh -tt this run attached with: its child
            kids = []
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        stat = read("/proc/%s/stat" % pid)
                        cmd = read("/proc/%s/cmdline" % pid, "rb")
                    except OSError:
                        continue
                    if int(stat.rsplit(")", 1)[1].split()[1]) == p.pid and b"-tt\0" in cmd:
                        kids.append(int(pid))
            self.assertEqual(len(kids), 1, kids)
            os.kill(kids[0], signal.SIGKILL)
            self.assertEqual(p.wait(LIMIT), 255)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        err = read(errf)
        name = self.name_in(err)
        self.assertIn("the connection dropped -- it keeps running there", err)
        put(os.path.join(self.there, "go"), "")
        self.assertEqual(self.ended_with(name), 4)
        os.unlink(os.path.join(self.there, "go"))


if __name__ == "__main__":
    unittest.main()
