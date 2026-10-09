# A stand-in for ~/code/py/satellite_ssh in tests: "keeps" a password, logs in with a key.
# Copied by tests/test_remote.py as <stub dir>/satellite_ssh.py, with SATELLITE_SSH_DIR=<stub dir>.
import os, shutil, subprocess, tempfile
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
        # ControlPersist=10m, as satellite_remote's own login: a killed test leaves no master for good.
        if subprocess.call(["ssh", "-M", "-S", self.sock] + self.options
                           + ["-o", "ControlPersist=10m", "-fN", self.target]) != 0:
            self.close()
            raise Refused("the stub's ssh login to %s failed" % self.target)
        return self
    def ssh(self, *more):
        return ["ssh", "-S", self.sock] + self.options + list(more) + [self.target]
    def close(self):
        subprocess.call(["ssh", "-S", self.sock] + self.options + ["-O", "exit", self.target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.dir, ignore_errors=True)
def connect(user, host, rotate=False, options=()):
    return Session(user, host, options).open()
