#!/usr/bin/python3 -I
# Drive a command on a pseudo-terminal like a person: answer prompts, press keys.
#
#   pty_harness.py SECONDS 'when-seen=>bytes' ... -- argv...
#
# bytes go through unicode_escape, so '\r' is Enter and '\x1d' is Ctrl-].  Each rule fires once,
# 0.3 s after its text was seen.  At the time limit the command is stopped -- SIGTERM, then
# SIGKILL two seconds later -- and its exit is reported as -1: a command that never ends fails
# the test that drives it, it never hangs it.
#
# Tests import it instead:  code, output = pty_harness.run(argv, [(b"[l]: ", b"r\r")], limit=20)
import os
import pty
import select
import signal
import sys
import time


def _code(status):
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1


def _stop(pid):
    """SIGTERM, a moment, SIGKILL -> the wait status."""
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except OSError:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return status
            time.sleep(0.05)
    return os.waitpid(pid, 0)[1]


def run(argv, rules=(), limit=30.0, env=None, cwd=None, preexec=None):
    """argv on a pseudo-terminal of its own -> (exit code, everything it printed, as bytes).
    rules = [(seen, send)]: send -- bytes, or a callable returning bytes or None -- once seen
    is in the output.  The code is -1 when a signal ended it or it was still running at limit."""
    steps = [[seen, send, False] for seen, send in rules]
    pid, fd = pty.fork()
    if pid == 0:
        try:
            if cwd:
                os.chdir(cwd)
            if preexec is not None:
                preexec()
            if env is None:
                os.execvp(argv[0], argv)
            else:
                os.execvpe(argv[0], argv, env)
        except BaseException as e:
            os.write(2, ("pty_harness: cannot start %s: %s\n" % (argv[0], e)).encode())
        os._exit(127)
    out, t0, status, gone, stopped = b"", time.time(), None, 0.0, False
    try:
        while True:
            if time.time() - t0 >= limit:
                if status is None:          # never signal a pid already reaped: it may be reused
                    out += b"\n[pty_harness: time limit, stopped]"
                    status, stopped = _stop(pid), True
                break
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    d = os.read(fd, 65536)
                except OSError:              # EIO: nothing holds the terminal any more
                    d = b""
                if not d:
                    break
                out += d
            for st in steps:
                if not st[2] and st[0] in out:
                    st[2] = True
                    time.sleep(0.3)
                    what = st[1]() if callable(st[1]) else st[1]
                    if what:
                        os.write(fd, what)
            # It ended, but something it started still holds the terminal: what is there is
            # read for a moment more, then it is over.
            if status is None:
                done, st_now = os.waitpid(pid, os.WNOHANG)
                if done == pid:
                    status, gone = st_now, time.time()
            elif time.time() - gone > 1.0:
                break
        # The terminal is let go of (EOF / EIO) but it may still run: waited for up to the
        # limit, never longer.
        while status is None:
            done, st_now = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                status = st_now
            elif time.time() - t0 >= limit:
                out += b"\n[pty_harness: time limit, stopped]"
                status, stopped = _stop(pid), True
            else:
                time.sleep(0.05)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return (-1 if stopped else _code(status)), out


def main(args):
    cut = args.index("--")
    limit, rules, argv = float(args[0]), args[1:cut], args[cut + 1:]
    steps = []
    for r in rules:
        when, _, what = r.partition("=>")
        steps.append((when.encode(), what.encode().decode("unicode_escape").encode("latin-1")))
    code, out = run(argv, steps, limit)
    sys.stdout.write(out.decode("utf-8", "replace").replace("\r", ""))
    print("\n[harness: exit %d]" % code)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
