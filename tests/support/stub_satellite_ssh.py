# A stand-in for ~/code/py/satellite_ssh in tests: "keeps" a password, logs in with a key.
import os, subprocess, tempfile
class Refused(Exception):
    pass
def has_password(user, host):
    return True
def read_kept(user, host, which="password"):
    return "s3cret-test" if which == "password" else None
class Session(object):
    def __init__(self, user, host, options=()):
        self.user, self.host, self.options = user, host, list(options)
        self.dir = tempfile.mkdtemp(prefix="stubssh-", dir="/tmp")
        self.sock = os.path.join(self.dir, "s")
        self.target = user + "@" + host
    def open(self):
        subprocess.check_call(["ssh", "-M", "-S", self.sock] + self.options + ["-o", "ControlPersist=yes", "-fN", self.target])
        return self
    def ssh(self, *more):
        return ["ssh", "-S", self.sock] + self.options + list(more) + [self.target]
    def close(self):
        subprocess.call(["ssh", "-S", self.sock] + self.options + ["-O", "exit", self.target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def connect(user, host, rotate=False, options=()):
    return Session(user, host, options).open()
