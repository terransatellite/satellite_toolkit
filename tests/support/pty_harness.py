# Drive a command on a pseudo-terminal like a person: answer prompts, press keys.
# usage: pty_harness.py SECONDS 'when-seen=>bytes' ... -- argv...
import os, pty, select, sys, time
args = sys.argv[1:]
cut = args.index("--")
limit, rules, argv = float(args[0]), args[1:cut], args[cut + 1:]
steps = []
for r in rules:
    when, _, what = r.partition("=>")
    steps.append([when.encode(), what.encode().decode("unicode_escape").encode("latin-1"), False])
pid, fd = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)
out, t0 = b"", time.time()
while time.time() - t0 < limit:
    r, _, _ = select.select([fd], [], [], 0.2)
    if fd in r:
        try:
            d = os.read(fd, 4096)
        except OSError:
            break
        if not d:
            break
        out += d
    for st in steps:
        if not st[2] and st[0] in out:
            time.sleep(0.3)
            os.write(fd, st[1])
            st[2] = True
_, status = os.waitpid(pid, 0)
sys.stdout.write(out.decode("utf-8", "replace").replace("\r", ""))
print("\n[harness: exit %d]" % (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1))
