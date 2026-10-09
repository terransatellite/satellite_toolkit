#!/usr/bin/python3 -I
"""Tests for enterprise_python_tool.py.  stdlib unittest; no network, no root needed
(they pass as root too: what refuses root is told it is uid 1000), no CPython tree:
every input is a small fake made in a temp dir -- fake gcc / binutils / python3 are
shell scripts.  Scratch HOME-like directories only: never ~/opt, never ~/.config.

    /usr/bin/python3 -I tests/test_tool.py
"""

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import enterprise_python_tool as ept          # noqa: E402

TOOL_FILE = os.path.abspath(os.path.join(HERE, "..", "enterprise_python_tool.py"))
TOOLKIT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def put(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def release(name, pre=False, published=True, rid="1", date="2026-01-01"):
    return {"name": name, "pre_release": pre, "is_published": published,
            "resource_uri": "https://www.python.org/api/v2/downloads/release/%s/" % rid, "release_date": date + "T00:00:00Z"}


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ept-test-")
        # Never asked "run where?", never reading or writing the real ~/.config.
        p = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.root, "config"),
                                         "SATELLITE_TOOLKIT_WHERE": "local"})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        while ept._LOCKS:                      # the tool holds its lock until it exits; a test does not
            ept._LOCKS.pop().close()
        shutil.rmtree(self.root, ignore_errors=True)


class Syntax(unittest.TestCase):
    def test_tool_and_embedded_scripts_parse_as_python_36(self):
        with open(TOOL_FILE) as f:
            ast.parse(f.read(), feature_version=(3, 6))
        ast.parse(ept.CHECK_SCRIPT, feature_version=(3, 6))
        ast.parse(ept.VERIFY_SCRIPT, feature_version=(3, 6))

    def test_check_script_marker_matches(self):
        self.assertIn('MARK = "%s"' % ept.CHECK_MARK, ept.CHECK_SCRIPT)


class Versions(unittest.TestCase):
    API = [release("Python 3.14.8", rid="1126", date="2026-09-30"), release("Python 3.14.0", rid="900"),
           release("Python 3.15.0", rid="1200", date="2026-10-02"),
           release("Python 3.15.1", pre=True, rid="1201"),                 # pre-release: ignored
           release("Python 3.16.0", published=False, rid="1300"),          # unpublished: ignored
           release("Python 3.13.16", rid="1125"), release("Python 2.7.18", rid="5"),
           release("Python 3.15.0rc3", rid="1199"), {"name": None}, "junk"]

    def rels(self):
        return ept.parse_api_releases(json.dumps(self.API).encode())

    def test_api_parse_keeps_python3_finals_newest_first(self):
        self.assertEqual([r["version"] for r in self.rels()], ["3.15.0", "3.14.8", "3.14.0", "3.13.16"])
        self.assertEqual(self.rels()[1]["id"], "1126")
        self.assertEqual(self.rels()[1]["date"], "2026-09-30")

    def test_api_parse_rejects_non_json(self):
        with self.assertRaises(ept.ToolError):
            ept.parse_api_releases(b"<html>proxy says no</html>")

    def test_auto_skips_a_series_with_only_its_dot_zero(self):
        rel, why = ept.pick_version(self.rels(), "auto")
        self.assertEqual(rel["version"], "3.14.8")
        self.assertIn("3.15", why)

    def test_auto_takes_the_new_series_after_its_first_bugfix(self):
        rels = self.rels() + [{"version": "3.15.1", "id": "", "date": ""}]
        rels.sort(key=lambda r: ept.numver(r["version"]), reverse=True)
        self.assertEqual(ept.pick_version(rels, "auto")[0]["version"], "3.15.1")

    def test_latest_series_and_exact(self):
        self.assertEqual(ept.pick_version(self.rels(), "latest")[0]["version"], "3.15.0")
        self.assertEqual(ept.pick_version(self.rels(), "3.14")[0]["version"], "3.14.8")
        self.assertEqual(ept.pick_version(self.rels(), "3.14.0")[0]["version"], "3.14.0")
        self.assertEqual(ept.pick_version(self.rels(), None)[0]["version"], "3.14.8")

    def test_bad_requests_are_tool_errors(self):
        for want in ("3.14.99", "3.12", "bogus", "3"):
            with self.assertRaises(ept.ToolError):
                ept.pick_version(self.rels(), want)

    def test_exact_version_without_a_release_list(self):
        rel, why = ept.pick_version([], "3.14.8")
        self.assertEqual(rel["version"], "3.14.8")
        self.assertIn("not confirmed", why)

    def test_ftp_listing(self):
        html = '<a href="3.9.25/">3.9.25/</a><a href="3.15.0/">x</a><a href="3.14.8/">y</a><a href="2.7.18/">z</a>'
        self.assertEqual(ept.parse_ftp_index(html), ["3.15.0", "3.14.8", "3.9.25"])


MAKE_LOG = """Running code to generate profile data (this can take a while):
gcc: error: an error BEFORE the training run counts
Building with support for profile generation:
The necessary bits to build these optional modules were not found:
_dbm                      _tkinter                  _ssl
To find the necessary bits, look in configure.ac and config.log.

Checked 114 modules (36 built-in, 74 shared, 1 n/a on linux-x86_64, 0 disabled, 3 missing, 0 failed on import)
# Next, run the profile task to generate the profile information.
  ./python -m test --pgo --timeout=
0:00:01 load avg: 1.0 [ 1/44] test_array
test_foo failed: error: something the test suite expected
Rebuilding with profile guided optimizations:
The necessary bits to build these optional modules were not found:
_dbm                      _tkinter
To find the necessary bits, look in configure.ac and config.log.

Following modules built successfully but were removed because they could not be imported:
_zstd

Checked 114 modules (36 built-in, 75 shared, 1 n/a on linux-x86_64, 0 disabled, 2 missing, 1 failed on import)
"""


class BuildOutput(TempRoot):
    def test_module_report_reads_the_last_pass(self):
        rep = ept.module_report(MAKE_LOG)
        self.assertEqual(rep["missing"], ["_dbm", "_tkinter"])
        self.assertEqual(rep["removed"], ["_zstd"])
        self.assertIn("2 missing, 1 failed on import", rep["summary"])

    def test_module_report_without_a_summary(self):
        rep = ept.module_report("make: Nothing to be done\n")
        self.assertEqual(rep["missing"], [])
        self.assertEqual(rep["summary"], "")

    def test_judge_modules_refuses_a_python_without_ssl(self):
        rep = {"missing": ["_ssl", "_tkinter"], "failed": [], "removed": [], "disabled": [], "summary": ""}
        with self.assertRaises(ept.ToolError) as cm:
            ept.judge_modules(rep, {}, "dnf", "dnf")
        self.assertIn("openssl-devel", str(cm.exception))

    def test_judge_modules_lets_unused_modules_go(self):
        rep = {"missing": ["_tkinter", "_dbm"], "failed": [], "removed": [], "disabled": [], "summary": ""}
        ept.judge_modules(rep, {}, "dnf", "dnf")          # no exception

    def test_error_summary_ignores_test_failures_in_the_training_run(self):
        log = put(self.root, "make.log", MAKE_LOG + "gcc: fatal error: cannot execute 'cc1'\n")
        hits = ept.error_summary(log)
        self.assertTrue(any("cc1" in h for h in hits))
        self.assertTrue(any("BEFORE the training" in h for h in hits))
        self.assertFalse(any("test_foo" in h for h in hits))

    def test_phases_follow_the_makefile_not_its_first_message(self):
        # 3.14 prints "Running code to generate profile data" before the instrumented
        # build; the phases must still come out 1, 2, 3.
        log = put(self.root, "fake-make.sh", "#!/bin/sh\ncat <<'X'\n" + MAKE_LOG + "X\n")
        os.chmod(log, 0o755)
        import io
        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf
        try:
            rc, _ = ept.run_logged([log], self.root, ept.scrubbed_env(), os.path.join(self.root, "l", "make.log"), "make",
                                   phases=True)
        finally:
            sys.stdout = saved
        self.assertEqual(rc, 0)
        shown = [line.split(":")[0].strip() for line in buf.getvalue().splitlines() if "PGO" in line]
        self.assertEqual(shown, ["PGO 1/3", "PGO 2/3", "PGO 3/3"])
        self.assertIn("an error BEFORE", buf.getvalue())
        self.assertNotIn("test_foo", buf.getvalue())

    def test_configure_args(self):
        a = ept.configure_args("/p", "/usr/bin/gcc", "/usr/bin/g++", optimize=True, system_mpdec=True)
        self.assertEqual(a[0], "--prefix=/p")
        for flag in ("--enable-optimizations", "--with-lto", "--with-ensurepip=install",
                     "--enable-loadable-sqlite-extensions", "--with-system-libmpdec", "CC=/usr/bin/gcc",
                     "CXX=/usr/bin/g++"):
            self.assertIn(flag, a)
        self.assertNotIn("--enable-shared", a)
        b = ept.configure_args("/p", "/usr/bin/gcc", "", optimize=False, system_mpdec=False)
        self.assertNotIn("--enable-optimizations", b)
        self.assertNotIn("--with-system-libmpdec", b)
        self.assertFalse(any(x.startswith("CXX=") for x in b))


class Environment(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_build_environment_drops_the_shells_toolchain(self):
        os.environ.update({"CC": "/opt/clang/bin/clang", "CXX": "clang++", "LDFLAGS": "-fuse-ld=lld",
                           "LD_RUN_PATH": "/opt/x", "LD_LIBRARY_PATH": "/opt/y", "CONFIG_SITE": "/x",
                           "PYTHONPATH": "/z", "http_proxy": "http://proxy:3128", "SSL_CERT_FILE": "/ca.pem",
                           "PIP_INDEX_URL": "https://mirror/simple"})
        env = ept.scrubbed_env()
        for var in ("CC", "CXX", "LDFLAGS", "LD_RUN_PATH", "LD_LIBRARY_PATH", "CONFIG_SITE", "PYTHONPATH",
                    "PIP_INDEX_URL"):
            self.assertNotIn(var, env)
        self.assertEqual(env["http_proxy"], "http://proxy:3128")
        self.assertEqual(env["SSL_CERT_FILE"], "/ca.pem")
        self.assertEqual(env["PATH"], "/usr/bin:/usr/sbin:/bin:/sbin")
        self.assertEqual(env["LC_ALL"], "C")
        pip_env = ept.scrubbed_env(keep=ept.PIP_KEEP)
        self.assertEqual(pip_env["PIP_INDEX_URL"], "https://mirror/simple")

    def test_traps_are_listed(self):
        traps = ept.env_traps({"CC": "clang", "LD_RUN_PATH": "x" * 200, "HOME": "/h"})
        self.assertEqual(len(traps), 2)
        self.assertTrue(traps[1].endswith("..."))


class Packages(unittest.TestCase):
    def test_install_commands(self):
        rows = [r for r in ept.DEPS if r[0] in ("openssl", "sqlite")]
        self.assertEqual(ept.install_command(ept.packages_for(rows, "dnf"), "dnf", "dnf"),
                         "sudo dnf install -y openssl-devel sqlite-devel")
        self.assertEqual(ept.install_command(ept.packages_for(rows, "apt"), "apt-get", "apt"),
                         "sudo apt-get install -y libssl-dev libsqlite3-dev")
        self.assertEqual(ept.install_command([], "dnf", "dnf"), "")

    def test_every_dep_row_is_well_formed(self):
        for row in ept.DEPS:
            self.assertEqual(len(row), 8, row)            # key, probes, dnf, apt, zypper, pacman, level, what
            self.assertIn(row[6], ("required", "wanted", "optional", "fallback"))
            self.assertTrue(row[1])
            self.assertTrue(all(row[2:6]), row)
        for t in ept.BUILD_TOOLS:
            self.assertEqual(len(t), 5, t)
        for mod, key in ept.MODULE_DEP.items():
            self.assertIn(key, [r[0] for r in ept.DEPS], mod)

    def test_requirement_names(self):
        text = "requests>=2\nlxml[html_clean] ; python_version>'3.8'\n# comment\n-r other.txt\n\ntrafilatura==2.2.0\n"
        self.assertEqual(ept.requirement_names(text), ["requests", "lxml", "trafilatura"])

    def test_pip_hints(self):
        self.assertIn("libxslt-devel", ept.pip_hints("Building lxml ... Error: xslt-config not found"))
        self.assertEqual(ept.pip_hints("all fine"), "")


class Scan(TempRoot):
    def scraper(self):
        r = os.path.join(self.root, "scraper")
        put(r, "main.py", "import sys\nfrom pkg.mod import thing\nimport requests\n"
                          "def later():\n    import lazy_pkg.inner\n")
        put(r, "fetch_repos.py", "import subprocess\nif __name__ == '__main__':\n    pass\n")
        put(r, "pkg/__init__.py", "")
        put(r, "pkg/mod.py", "from .helper import x\nfrom . import sibling\nimport lxml.html\nthing = 1\n")
        put(r, "pkg/helper.py", "x = 1\n")
        put(r, "pkg/sibling.py", "from ..top import y\n")
        put(r, "top.py", "y = 2\n")
        put(r, "lazy_pkg/__init__.py", "")
        put(r, "lazy_pkg/inner.py", "import psutil\n")
        put(r, "old.py", "import bs4\n")                                   # not reachable: not the scraper
        put(r, "broken_old.py", "def f(:\n")                               # unreachable, so harmless
        put(r, "html/site/page.py", "import evil\n")                       # data, not code
        put(r, ".venv/lib/x.py", "import nope\n")
        put(r, "old_crawl_2026/x.py", "import nope2\n")
        return r

    def test_live_set_and_externals(self):
        scan = ept.scan_scraper(self.scraper(), entries=("main.py", "fetch_repos.py", "reseed.py"))
        self.assertEqual(scan["entries"], ["main.py", "fetch_repos.py"])
        self.assertEqual(scan["missing_entries"], ["reseed.py"])
        self.assertEqual(scan["modules"], ["fetch_repos", "lazy_pkg", "lazy_pkg.inner", "main", "pkg",
                                           "pkg.helper", "pkg.mod", "pkg.sibling", "top"])
        self.assertEqual(scan["externals"], ["lxml", "psutil", "requests", "subprocess", "sys"])
        self.assertEqual(scan["errors"], {})

    def test_data_dirs_are_not_code(self):
        files = ept.py_files(self.scraper())
        self.assertNotIn(os.path.join("html", "site", "page.py"), files)
        self.assertNotIn(os.path.join(".venv", "lib", "x.py"), files)
        self.assertNotIn(os.path.join("old_crawl_2026", "x.py"), files)
        self.assertIn("old.py", files)

    def test_copy_code_copies_code_only(self):
        src = self.scraper()
        dest = os.path.join(self.root, "copy")
        n = ept.copy_code(src, dest)
        self.assertEqual(n, len(ept.py_files(src)))
        self.assertTrue(os.path.isfile(os.path.join(dest, "pkg", "mod.py")))
        self.assertFalse(os.path.exists(os.path.join(dest, "html")))

    def test_module_name(self):
        self.assertEqual(ept.module_name(os.path.join("a", "b", "__init__.py")), "a.b")
        self.assertEqual(ept.module_name(os.path.join("a", "c.py")), "a.c")


class Venvs(TempRoot):
    def test_venv_info(self):
        v = os.path.join(self.root, "venv")
        put(v, "pyvenv.cfg", "home = /usr/bin\ninclude-system-site-packages = true\nversion = 3.12.14\n")
        vi = ept.venv_info(v)
        self.assertEqual(vi["home"], "/usr/bin")
        self.assertTrue(vi["system_site"])
        self.assertEqual(vi["version"], "3.12.14")
        self.assertIsNone(vi["marker"])
        self.assertIsNone(ept.venv_info(self.root))

    def test_busy_detects_the_crawler_however_it_was_started(self):
        scraper = os.path.join(self.root, "scraper")
        venv = os.path.join(scraper, ".venv")
        os.makedirs(os.path.join(venv, "bin"))
        proc = os.path.join(self.root, "proc")

        def fake(pid, ppid, argv, cwd, exe, environ=""):
            d = os.path.join(proc, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
            with open(os.path.join(d, "stat"), "w") as f:
                f.write("%d (%s) S %d 1 1 0 -1\n" % (pid, os.path.basename(exe), ppid))
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(environ.encode())
            os.symlink(cwd, os.path.join(d, "cwd"))
            os.symlink(exe, os.path.join(d, "exe"))
        fake(101, 50, [".venv/bin/python", "main.py"], scraper, "/usr/bin/python3.12")    # README style
        fake(102, 101, [".venv/bin/python", "main.py"], scraper, "/usr/bin/python3.12")   # its forked worker
        fake(201, 60, ["python3.12", "main.py"], scraper, "/usr/bin/python3.12")          # as seen 2026-10-03
        fake(301, 70, ["python", "x.py"], "/home", "/usr/bin/python3.12", "VIRTUAL_ENV=%s\0" % venv)
        fake(401, 80, ["bash"], scraper, "/usr/bin/bash")                                 # a shell there: fine
        fake(501, 90, ["/usr/bin/python3", "other.py"], "/home", "/usr/bin/python3.12")   # elsewhere: fine
        hits = ept.busy_processes([venv], cwd_roots=[scraper], proc_root=proc)
        self.assertEqual(sorted(h[0] for h in hits), [101, 102, 201, 301])
        self.assertIn("kill -INT 101 201 301", ept.busy_message("x", hits))
        self.assertEqual(sorted(h[0] for h in ept.busy_processes([venv], proc_root=proc)), [101, 102, 301])

    def test_pick_prefix_wants_a_built_interpreter(self):
        class A(object):
            prefix = os.path.join(self.root, "nothing-here")
            version = None
        with self.assertRaises(ept.ToolError):
            ept.pick_prefix(A(), "/x/claude_scraper")


class Fetching(TempRoot):
    def test_download_checks_sha256_and_renames(self):
        src = put(self.root, "src.tar.xz", "payload")
        url = "file://" + src
        dest = os.path.join(self.root, "dl", "Python-3.14.8.tar.xz")
        good = ept.sha256_file(src)
        ept.download(url, dest, sha256=good)
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        os.unlink(dest)
        with self.assertRaises(ept.ToolError):
            ept.download(url, dest, sha256="0" * 64)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_download_refetches_a_corrupt_file(self):
        src = put(self.root, "src.tar.xz", "payload")
        dest = put(self.root, "dl/Python-3.14.8.tar.xz", "corrupt")
        ept.download("file://" + src, dest, sha256=ept.sha256_file(src))
        with open(dest) as f:
            self.assertEqual(f.read(), "payload")

    def test_download_size_mismatch(self):
        src = put(self.root, "src.tar.xz", "payload")
        with self.assertRaises(ept.ToolError):
            ept.download("file://" + src, os.path.join(self.root, "d.tar.xz"), size=999)


class Misc(TempRoot):
    def test_state_round_trip(self):
        path = os.path.join(self.root, "w", "state.json")
        s = ept.State(path)
        s.set_build("3.14.8", make_seconds=600)
        s.add_check({"status": "ok"})
        s2 = ept.State(path)
        self.assertEqual(s2.build("3.14.8")["make_seconds"], 600)
        self.assertEqual(s2.last_seconds(), 600)
        self.assertEqual(s2.d["checks"][-1]["status"], "ok")

    def test_unreadable_state_starts_fresh(self):
        path = put(self.root, "state.json", "{not json")
        self.assertEqual(ept.State(path).d["builds"], {})

    def test_prefix_naming(self):
        self.assertEqual(ept.scraper_tag("/run/media/x/claude_scraper/"), "claude_scraper")
        self.assertEqual(ept.scraper_tag("/a/my scraper"), "my_scraper")
        self.assertTrue(ept.default_prefix("3.14.8", "/a/claude_scraper").endswith("/opt/cpython-3.14.8-claude_scraper"))

    def test_check_output_parsing(self):
        text = "noise\n%s%s\n" % (ept.CHECK_MARK, json.dumps({"results": [{"check": "x", "status": "ok"}]}))
        self.assertEqual(ept.parse_check_output(text)["results"][0]["check"], "x")
        self.assertIsNone(ept.parse_check_output("no marker"))
        self.assertIsNone(ept.parse_check_output(ept.CHECK_MARK + "{broken"))

    def test_check_script_classifies_modules(self):
        ns = {"__name__": "not_main"}
        exec(compile(ept.CHECK_SCRIPT, "check", "exec"), ns)
        std, third = ns["classify"](["json", "sqlite3", "__future__", "surely_not_installed_xyz"])
        self.assertEqual(std, ["json", "sqlite3", "__future__"])
        self.assertEqual(third, ["surely_not_installed_xyz"])

    def test_run_group_kills_what_the_test_leaves_behind(self):
        log = os.path.join(self.root, "out.log")
        code = ("import subprocess, sys; p = subprocess.Popen(['sleep', '120']); "
                "print('child', p.pid); sys.stdout.flush()")
        started = time.time()
        rc = ept.run_group([sys.executable, "-c", code], self.root, log, timeout=60)
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - started, 30)
        with open(log) as f:
            pid = int(f.read().split()[1])
        time.sleep(0.3)
        alive = subprocess.call(["kill", "-0", str(pid)], stderr=subprocess.DEVNULL) == 0
        if alive:                       # a zombie still answers kill -0; make sure it is not running
            with open("/proc/%d/stat" % pid) as f:
                self.assertIn(f.read().split()[2], ("Z", "X"))

    def test_run_group_timeout(self):
        log = os.path.join(self.root, "out.log")
        rc = ept.run_group([sys.executable, "-c", "import time; time.sleep(60)"], self.root, log, timeout=1)
        self.assertIsNone(rc)


class Ns(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Install(TempRoot):
    """do_install against a fake build tree whose Makefile plays `make install`."""

    def setup_build(self, with_pip=True):
        self.prefix = os.path.join(self.root, "opt", "cpython-3.14.8-claude_scraper")
        build = os.path.join(self.root, "build")
        pipdir = "$(DESTDIR)%s/lib/python3.14/site-packages/pip" % self.prefix
        put(build, "Makefile", "install:\n"
            "\tmkdir -p $(DESTDIR)%s/bin %s\n"
            "\tprintf '#!/bin/sh\\necho new\\n' > $(DESTDIR)%s/bin/python3\n"
            "\tchmod +x $(DESTDIR)%s/bin/python3\n" % (self.prefix, pipdir if with_pip else "", self.prefix, self.prefix))
        return Ns(stage=os.path.join(self.root, "stage"), logs=os.path.join(self.root, "logs"), prefix=self.prefix,
                  build=build, version="3.14.8", args=Ns(verbose=False))

    def test_rebuild_never_moves_a_directory_the_tool_did_not_make(self):
        ctx = self.setup_build()
        put(self.prefix, "precious.txt", "someone else's")
        with self.assertRaises(ept.ToolError):
            ept.do_install(ctx, None, True, ["--prefix=" + self.prefix])
        with open(os.path.join(self.prefix, "precious.txt")) as f:
            self.assertEqual(f.read(), "someone else's")
        self.assertEqual(glob_names(os.path.dirname(self.prefix)), ["cpython-3.14.8-claude_scraper"])

    def test_rebuild_sets_its_own_previous_build_aside(self):
        ctx = self.setup_build()
        put(self.prefix, ept.MARKER, json.dumps({"tool": ept.TOOL, "version": "3.14.8", "state": "verified",
                                                  "verify": {"good": True}, "configure": ["--prefix=x"]}))
        old = ept.do_install(ctx, None, True, ["--prefix=" + self.prefix])
        self.assertTrue(os.path.isfile(os.path.join(old, ept.MARKER)))
        self.assertTrue(os.access(os.path.join(self.prefix, "bin", "python3"), os.X_OK))
        rec = ept.prefix_record(self.prefix)
        self.assertEqual(rec["state"], "unverified")          # until cmd_build has verified it
        self.assertFalse(ept.verified(rec))

    def test_an_install_without_pip_is_refused(self):
        ctx = self.setup_build(with_pip=False)
        with self.assertRaises(ept.ToolError) as cm:
            ept.do_install(ctx, None, False, [])
        self.assertIn("no pip", str(cm.exception))
        self.assertFalse(os.path.exists(self.prefix))


def glob_names(d):
    return sorted(os.listdir(d))


class Verify(TempRoot):
    def test_wanted_modules_really_are_checked(self):
        prefix = os.path.join(self.root, "prefix")
        os.makedirs(os.path.join(prefix, "bin"))
        os.symlink(sys.executable, os.path.join(prefix, "bin", "python3"))
        saved = ept.MODULES_WANTED
        ept.MODULES_WANTED = (("_surely_not_a_module_xyz", (3, 0)),)
        try:
            good, summary = ept.verify_interpreter(prefix, offline=True, quiet=True)
        finally:
            ept.MODULES_WANTED = saved
        self.assertEqual(summary["missing"]["wanted"], ["_surely_not_a_module_xyz"])


class Clean(TempRoot):
    def args(self, workdir, all_=False):
        return Ns(workdir=workdir, scraper=os.path.join(self.root, "scraper"), all=all_)

    def test_refuses_a_directory_that_is_not_its_own(self):
        foreign = os.path.join(self.root, "someone-elses")
        put(foreign, "Python-3.14.8/keep.txt", "x")
        with self.assertRaises(ept.ToolError):
            ept.cmd_clean(self.args(foreign))
        self.assertTrue(os.path.exists(os.path.join(foreign, "Python-3.14.8", "keep.txt")))

    def test_refuses_the_scraper_and_what_holds_it(self):
        scraper = os.path.join(self.root, "scraper")
        put(scraper, "state.json", json.dumps({"tool": ept.TOOL}))
        for workdir in (scraper, self.root):
            with self.assertRaises(ept.ToolError):
                ept.cmd_clean(self.args(workdir))

    def test_removes_only_its_own_names(self):
        w = os.path.join(self.root, "work")
        put(w, "state.json", json.dumps({"tool": ept.TOOL, "builds": {}}))
        for d in ("Python-3.14.8/a", "build-3.14.8/b", "stage-3.14.8/c", "check-venv-3.14.8/d", "check-ab12cd34/e",
                  "important/f", "check-notes-of-mine/g", "downloads/Python-3.14.8.tar.xz"):
            put(w, d, "x")
        ept.cmd_clean(self.args(w))
        self.assertEqual(glob_names(w), [".lock", "check-notes-of-mine", "downloads", "important", "state.json"])


class Unpack(TempRoot):
    def tarball(self, text):
        src = os.path.join(self.root, "t", "Python-3.14.8")
        put(src, "configure", text)
        tb = os.path.join(self.root, "Python-3.14.8.tar.xz")
        subprocess.check_call(["tar", "-cJf", tb, "-C", os.path.dirname(src), "Python-3.14.8"])
        shutil.rmtree(os.path.dirname(src))
        return tb

    def test_a_different_tarball_is_unpacked_again_and_its_build_forgotten(self):
        w = os.path.join(self.root, "w")
        ctx = Ns(downloads=os.path.join(w, "downloads"), tarball=os.path.join(w, "downloads", "Python-3.14.8.tar.xz"),
                 src=os.path.join(w, "Python-3.14.8"), build=os.path.join(w, "build-3.14.8"), version="3.14.8")
        state = ept.State(os.path.join(w, "state.json"))
        tb = self.tarball("first")
        ctx.tar = {"url": "file://" + tb, "sha256": "", "size": 0}
        ept.fetch_and_unpack(ctx, state)
        put(ctx.build, "Makefile", "")
        state.set_build("3.14.8", make_done="yes")
        tb2 = self.tarball("second")
        ctx.tar = {"url": "file://" + tb2, "sha256": ept.sha256_file(tb2), "size": 0}
        ept.fetch_and_unpack(ctx, state)                    # checksum mismatch -> new tarball -> new tree
        with open(os.path.join(ctx.src, "configure")) as f:
            self.assertEqual(f.read(), "second")
        self.assertFalse(os.path.exists(ctx.build))
        self.assertNotIn("make_done", state.build("3.14.8"))


class Preflight(TempRoot):
    def test_writable_parent(self):
        ro = os.path.join(self.root, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        try:
            if os.access(ro, os.W_OK):
                self.skipTest("running with privileges that ignore permissions")
            self.assertIn("not writable", ept.writable_parent(os.path.join(ro, "a", "b"))[1])
        finally:
            os.chmod(ro, 0o755)
        os.symlink(os.path.join(self.root, "nowhere"), os.path.join(self.root, "dangling"))
        self.assertIn("dangling", ept.writable_parent(os.path.join(self.root, "dangling", "x"))[1])
        self.assertEqual(ept.writable_parent(os.path.join(self.root, "new", "deeper"))[1], "")

    def test_pick_prefix_versions(self):
        for bad in ("latest", "3", "x.y"):
            with self.assertRaises(ept.ToolError):
                ept.pick_prefix(Ns(prefix=None, version=bad), "/nowhere/claude_scraper")
        with self.assertRaises(ept.ToolError) as cm:
            ept.pick_prefix(Ns(prefix=None, version="3.99"), "/nowhere/claude_scraper")
        self.assertIn("3.99.x", str(cm.exception))

    def test_follow_up_flags_name_the_build_just_made(self):
        ctx = Ns(prefix=ept.default_prefix("3.13.16", ept.DEFAULT_SCRAPER), version="3.13.16",
                 workdir=ept.expand(ept.DEFAULT_WORKDIR), scraper=ept.expand(ept.DEFAULT_SCRAPER))
        self.assertEqual(ept.own_flags(ctx), ["--version", "3.13.16"])
        ctx.prefix = "/elsewhere/py"
        self.assertEqual(ept.own_flags(ctx), ["--prefix", "/elsewhere/py"])

    def test_output_survives_a_dead_terminal(self):
        saved = sys.stdout
        dead = open(os.devnull, "w")
        dead.close()
        sys.stdout = dead
        try:
            ept.out("nobody is listening")
            ept.clear_status()
        finally:
            sys.stdout = saved


class BuildTransaction(TempRoot):
    """cmd_build's rollback, with every step that needs a network, a compiler or ten
    minutes replaced by a stub; only the transaction logic is real."""

    def run_build(self, verify_good, smoke=None, no_check=None, venv_version=None, prefix_is_venv=False):
        prefix = os.path.join(self.root, "opt", "cpython-3.14.8-x")
        put(prefix, ept.MARKER, json.dumps({"tool": ept.TOOL, "version": "3.14.8", "state": "verified",
                                            "verify": {"good": True}, "configure": ["--prefix=" + prefix]}))
        put(prefix, "bin/OLD", "old")
        if prefix_is_venv:
            put(prefix, "pyvenv.cfg", "home = /usr/bin\nversion = 3.12.14\n")
        scraper = os.path.join(self.root, "scraper")
        if smoke:
            os.makedirs(scraper)
        if venv_version:
            put(scraper, ".venv/pyvenv.cfg", "home = %s\nversion = %s\n" % (os.path.join(prefix, "bin"), venv_version))
        stubs = {
            "resolve_release": lambda args: ({"version": "3.14.8", "id": "", "date": ""}, "test", "test"),
            "tarball_info": lambda rel: {"url": "", "sha256": "", "size": 0},
            "preflight": lambda ctx, install_deps=False: {},
            "show_plan": lambda ctx, m: None,
            "fetch_and_unpack": lambda ctx, state: None,
            "do_configure": lambda ctx, state, a, f: None,
            "do_make": lambda ctx, state: {"missing": [], "failed": [], "removed": [], "disabled": [], "summary": ""},
            "judge_modules": lambda rep, found, fam, mgr, **k: None,
            "compiler_banner": lambda cc: "cc",
            "verify_interpreter": lambda p, offline=False, **k: (verify_good, {"good": verify_good, "when": "t",
                                                                               "version": "3.14.8"}),
            "create_venv": lambda py, target: os.path.join(target, "bin", "python"),
            "pip_sync": lambda *a, **k: {},
            "run_check": lambda *a, **k: {"status": smoke or "ok", "results": []},
        }

        def fake_install(ctx, state, rebuild, args_list, old=""):
            self.assertTrue(old)                # cmd_build names the set-aside path itself
            os.rename(ctx.prefix, old)
            put(ctx.prefix, "bin/NEW", "new")
            put(ctx.prefix, ept.MARKER, json.dumps({"tool": ept.TOOL, "state": "unverified"}))
            return old
        stubs["do_install"] = fake_install
        saved = dict((k, getattr(ept, k)) for k in stubs)
        for k, v in stubs.items():
            setattr(ept, k, v)
        args = Ns(version=None, prefix=prefix, workdir=os.path.join(self.root, "w"), scraper=scraper, cc="", jobs=1,
                  no_optimize=False, install_deps=False, offline=True, constraints="", verbose=False, rebuild=True,
                  no_check=(not smoke) if no_check is None else no_check, keep=False)
        try:
            return prefix, ept.cmd_build(args), None
        except ept.ToolError as e:
            return prefix, None, e
        finally:
            for k, v in saved.items():
                setattr(ept, k, v)

    def leftovers(self, prefix):
        return sorted(n for n in os.listdir(os.path.dirname(prefix)) if n != os.path.basename(prefix))

    def test_a_build_that_does_not_verify_puts_the_previous_one_back(self):
        prefix, rc, err = self.run_build(verify_good=False)
        self.assertIn("previous build is back", str(err))
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "OLD")))
        failed = self.leftovers(prefix)
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].startswith("cpython-3.14.8-x.failed-"))

    def test_a_rebuild_that_fails_the_smoke_test_puts_the_previous_one_back(self):
        prefix, rc, err = self.run_build(verify_good=True, smoke="fail")
        self.assertIn("smoke test failed", str(err))
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "OLD")))
        self.assertTrue(self.leftovers(prefix)[0].startswith("cpython-3.14.8-x.failed-"))

    def test_a_good_rebuild_replaces_and_removes_the_previous_one(self):
        prefix, rc, err = self.run_build(verify_good=True, smoke="ok")
        self.assertIsNone(err)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "NEW")))
        self.assertEqual(self.leftovers(prefix), [])
        self.assertTrue(ept.verified(ept.prefix_record(prefix)))

    def test_the_previous_build_stays_when_the_scraper_could_not_be_tried(self):
        prefix, rc, err = self.run_build(verify_good=True, smoke=None, no_check=False)   # no scraper directory
        self.assertIsNone(err)
        old = self.leftovers(prefix)
        self.assertEqual(len(old), 1)
        self.assertTrue(old[0].startswith("cpython-3.14.8-x.old-"))

    def test_a_venv_is_never_taken_for_a_prefix(self):
        prefix, rc, err = self.run_build(verify_good=True, smoke="ok", prefix_is_venv=True)
        self.assertIn("is a venv", str(err))
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "OLD")))

    def test_a_rebuild_may_not_change_the_series_under_the_scrapers_venv(self):
        prefix, rc, err = self.run_build(verify_good=True, smoke="ok", venv_version="3.13.5")
        self.assertIn("ONE X.Y series", str(err))
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "OLD")))


class Records(TempRoot):
    def test_a_venv_record_is_not_a_build_record(self):
        v = os.path.join(self.root, "venv")
        put(v, ept.MARKER, json.dumps({"tool": ept.TOOL, "prefix": "/x"}))            # no configure line
        self.assertIsNone(ept.prefix_record(v))
        put(v, ept.MARKER, json.dumps({"tool": ept.TOOL, "configure": ["--prefix=/x"]}))
        self.assertIsNotNone(ept.prefix_record(v))
        put(v, "pyvenv.cfg", "home = /usr/bin\n")                                      # but it IS a venv
        self.assertIsNone(ept.prefix_record(v))
        self.assertNotEqual(ept.MARKER, ept.VENV_MARKER)

    def test_same_series(self):
        self.assertTrue(ept.same_series("3.14.8", "3.14.0"))
        self.assertFalse(ept.same_series("3.13.5", "3.14.8"))
        self.assertFalse(ept.same_series("", ""))

    def test_crb_command(self):
        self.assertIn("subscription-manager", ept.crb_command({"ID": "rhel", "VERSION_ID": "10.2"}))
        self.assertIn("powertools", ept.crb_command({"ID": "rocky", "VERSION_ID": "8.10"}))
        self.assertIn("--set-enabled crb", ept.crb_command({"ID": "almalinux", "VERSION_ID": "10.2"}))

    def test_eperm_from_an_exec_policy_is_an_error_not_a_ctrl_c(self):
        import errno as errno_
        saved = ept.subprocess.run

        def denied(*a, **k):
            raise OSError(errno_.EPERM, "Operation not permitted")
        ept.subprocess.run = denied
        try:
            with self.assertRaises(ept.ToolError):
                ept.run(["/some/probe"])
            with self.assertRaises(KeyboardInterrupt):
                ept.run(["sudo", "dnf", "install"])
        finally:
            ept.subprocess.run = saved

    def test_clean_escapes_its_path_and_accepts_a_legacy_record(self):
        w = os.path.join(self.root, "w[1]")
        put(w, "state.json", json.dumps({"builds": {}, "checks": []}))                   # before the "tool" key
        put(w, "Python-3.14.8/x", "x")
        put(self.root, "w1/Python-3.14.8/precious", "x")                                  # what w[1] would glob to
        ept.cmd_clean(Ns(workdir=w, scraper=os.path.join(self.root, "scraper"), all=False))
        self.assertFalse(os.path.exists(os.path.join(w, "Python-3.14.8")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "w1", "Python-3.14.8", "precious")))

    def test_ftp_fallback_says_what_actually_happened(self):
        saved = ept.http_get, ept.url_exists

        def http_get(url, timeout=60):
            if "api" in url:
                raise ept.ToolError("cannot fetch the API")
            return b'<a href="3.14.8/">3.14.8/</a>'
        ept.http_get, ept.url_exists = http_get, lambda url, timeout=20: True
        try:
            rels, source = ept.get_releases("3.14")
            self.assertEqual([r["version"] for r in rels], ["3.14.8"])
            with self.assertRaises(ept.ToolError) as cm:
                ept.get_releases("3.99")
            self.assertIn("has no finished release for 3.99", str(cm.exception))
        finally:
            ept.http_get, ept.url_exists = saved


# ======================================================================== the toolkit

def script(root, rel, body):
    path = put(root, rel, "#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


def fake_gcc(root, rel, banner="gcc (GCC) 16.2.0", version="16.2.0", pinned=""):
    """Answers --version, -dumpfullversion and -print-prog-name like gcc; pinned = the
    directory a gcc configured --with-as/--with-ld names for as and ld."""
    prog = ('  -print-prog-name=*) n="${1#-print-prog-name=}"; echo "%s$n";;\n' % (pinned + "/" if pinned else ""))
    return script(root, rel, 'case "$1" in\n  --version) echo "%s"; echo "Copyright (C) 2026 Free Software '
                  'Foundation, Inc.";;\n  -dumpfullversion|-dumpversion) echo %s;;\n%s  *) exit 0;;\nesac\n'
                  % (banner, version, prog))


def fake_binutils(root, rel, version="2.46"):
    for name in ("as", "ld", "ar", "readelf"):
        script(root, "%s/bin/%s" % (rel, name), 'echo "GNU %s (GNU Binutils) %s"\n'
               % ("assembler" if name == "as" else name, version))
    return os.path.join(root, rel)


def fake_python(root, rel, version="3.14.8", runs=True):
    """bin/python3 of a prefix: answers the version question (and detect's)."""
    body = ('case "$*" in\n  *platform*) echo "CPython %s %s";;\n  *) echo %s;;\nesac\n'
            % (version, os.path.join(root, rel), version)) if runs else "exit 1\n"
    return script(root, rel + "/bin/python3", body)


def marker(prefix, **kv):
    rec = {"tool": "%s %s" % (ept.TOOL, ept.VERSION), "version": "3.14.8", "configure": ["--prefix=" + prefix],
           "state": "verified", "verify": {"good": True}, "built": "2026-10-09T10:00:00+0000",
           "compiler": "/usr/bin/gcc (gcc (GCC) 14.2.1)", "for": "general", "optimized": True}
    rec.update(kv)
    put(prefix, ept.MARKER, json.dumps(rec))
    return rec


class Captured(TempRoot):
    """main() as the main file runs it: what reaches the real stdout and stderr."""

    def run_main(self, *argv, **kw):
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.object(ept.sys, "stdout", o), mock.patch.object(ept.sys, "stderr", e), \
                mock.patch.object(ept.os, "geteuid", return_value=kw.get("euid", 1000)):
            code = ept.main(list(argv))
        return code, o.getvalue(), e.getvalue()


class TheFile(unittest.TestCase):
    def setUp(self):
        with open(TOOL_FILE) as f:
            self.text = f.read()

    def test_pure_ascii_and_python_36(self):
        bad = [n for n, line in enumerate(self.text.split("\n"), 1) if any(ord(c) > 127 for c in line)]
        self.assertEqual(bad, [])
        ast.parse(self.text, feature_version=(3, 6))
        ast.parse(ept.GENERAL_SMOKE_SCRIPT, feature_version=(3, 6))
        with open(os.path.abspath(__file__)) as f:
            self.assertTrue(all(ord(c) < 128 for c in f.read()))

    def test_isolated_platform_python_shebang_and_pypy_reexec(self):
        self.assertEqual(self.text.split("\n")[0], "#!/usr/bin/python3 -I")
        self.assertIn('os.execv(PLATFORM_PYTHON, [PLATFORM_PYTHON, "-I"', self.text)

    def test_the_remote_block_is_what_sync_remote_writes(self):
        lines = self.text.split("\n")
        begins = [i for i, l in enumerate(lines) if l.startswith("# >>> satellite_remote >>>")]
        ends = [i for i, l in enumerate(lines) if l == "# <<< satellite_remote <<<"]
        self.assertEqual((len(begins), len(ends)), (1, 1))
        sys.path.insert(0, TOOLKIT)
        try:
            import sync_remote
        finally:
            sys.path.remove(TOOLKIT)
        if not os.path.exists(sync_remote.SOURCE):
            self.skipTest("no satellite_remote.py beside sync_remote.py")
        self.assertEqual(sync_remote.synced(self.text, sync_remote.block()), self.text,
                         "the block differs from satellite_remote.py: run ./sync_remote.py")

    def test_where_to_run_is_the_first_statement_of_main(self):
        tree = ast.parse(self.text)
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        self.assertEqual(first.targets[0].id, "argv")
        self.assertEqual(first.value.func.id, "where_to_run")


class GeneralMode(TempRoot):
    def args(self, **kw):
        a = Ns(purpose="general", opt_root=os.path.join(self.root, "opt"), workdir=os.path.join(self.root, "w"),
               scraper=os.path.join(self.root, "scraper"), version=None, prefix=None, cc="", binutils="", jobs=1,
               no_optimize=False, link_current=None, constraints="")
        a.__dict__.update(kw)
        return a

    def test_prefix_paths_and_record_key_per_mode(self):
        opt = os.path.join(self.root, "opt")
        self.assertEqual(ept.default_prefix("3.15.0", "/x/claude_scraper", opt, "general"),
                         os.path.join(opt, "cpython-3.15.0"))
        self.assertEqual(ept.default_prefix("3.15.0", "/x/claude_scraper", opt),
                         os.path.join(opt, "cpython-3.15.0-claude_scraper"))
        rel = {"version": "3.15.0"}
        g = ept.Ctx(self.args(), rel)
        s = ept.Ctx(self.args(purpose="scraper"), rel)
        self.assertEqual((g.prefix, g.key, os.path.basename(g.build), os.path.basename(g.stage),
                          os.path.basename(g.check_venv), os.path.basename(g.logs)),
                         (os.path.join(opt, "cpython-3.15.0"), "3.15.0-general", "build-3.15.0-general",
                          "stage-3.15.0-general", "check-venv-3.15.0-general", "3.15.0-general"))
        self.assertEqual((s.key, os.path.basename(s.build), os.path.basename(s.stage), os.path.basename(s.logs)),
                         ("3.15.0", "build-3.15.0", "stage-3.15.0", "3.15.0"))          # as it always was
        self.assertEqual(g.src, s.src)                                                  # one source tree
        self.assertTrue(g.link_current)
        self.assertFalse(s.link_current)
        self.assertFalse(ept.Ctx(self.args(link_current=False), rel).link_current)
        self.assertTrue(ept.Ctx(self.args(purpose="scraper", link_current=True), rel).link_current)

    def test_the_two_modes_keep_separate_records(self):
        state = ept.State(os.path.join(self.root, "w", "state.json"))
        rel = {"version": "3.14.8"}
        for purpose, secs in (("scraper", 500), ("general", 900)):
            ctx = ept.Ctx(self.args(purpose=purpose), rel)
            state.set_build(ctx.key, make_done="t", make_seconds=secs)
        again = ept.State(state.path)
        self.assertEqual(again.build("3.14.8")["make_seconds"], 500)
        self.assertEqual(again.build("3.14.8-general")["make_seconds"], 900)

    def test_auto_in_general_mode_is_the_latest(self):
        rels = [{"version": v, "id": "", "date": ""} for v in ("3.15.0", "3.14.8", "3.14.0")]
        with mock.patch.object(ept, "get_releases", return_value=(rels, "python.org API")):
            rel, why, _ = ept.resolve_release(self.args())
            self.assertEqual(rel["version"], "3.15.0")
            self.assertIn("auto is latest", why)
            self.assertEqual(ept.resolve_release(self.args(version="auto"))[0]["version"], "3.15.0")
            self.assertEqual(ept.resolve_release(self.args(version="3.14"))[0]["version"], "3.14.8")
            self.assertEqual(ept.resolve_release(self.args(purpose="scraper"))[0]["version"], "3.14.8")

    def toolchain(self):
        opt = os.path.join(self.root, "opt")
        bdir = fake_binutils(opt, "binutils-2.46")
        fake_gcc(opt, "gcc-16.2.0/bin/gcc")
        fake_gcc(opt, "gcc-16.2.0/bin/g++")
        os.symlink(bdir, os.path.join(opt, "binutils-current"))
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))
        return opt, bdir

    def test_cc_and_binutils_by_real_path_ar_named_and_first_on_path(self):
        opt, bdir = self.toolchain()
        ctx = ept.Ctx(self.args(cc=os.path.join(opt, "gcc-current", "bin", "gcc"),
                                binutils=os.path.join(opt, "binutils-current")), {"version": "3.15.0"})
        gcc = os.path.join(opt, "gcc-16.2.0", "bin", "gcc")
        self.assertEqual((ctx.cc, ctx.cxx, ctx.binutils), (gcc, gcc[:-3] + "g++", bdir))
        a = ctx.configure(True)
        self.assertIn("CC=" + gcc, a)
        self.assertIn("CXX=" + gcc[:-3] + "g++", a)
        self.assertIn("AR=" + os.path.join(bdir, "bin", "ar"), a)
        self.assertEqual(ept.ctx_env(ctx)["PATH"].split(":")[:2], [os.path.join(bdir, "bin"), "/usr/bin"])
        # what this gcc will really run: bare names, found on the build's PATH -- the binutils first
        self.assertEqual(ept.prog_paths(ctx.cc, ctx.path_extra),
                         {"as": os.path.join(bdir, "bin", "as"), "ld": os.path.join(bdir, "bin", "ld")})
        pinned = os.path.join(self.root, "elsewhere", "bin")
        fake_gcc(opt, "gcc-17/bin/gcc", pinned=pinned)               # configured --with-as/--with-ld
        self.assertEqual(ept.prog_paths(os.path.join(opt, "gcc-17", "bin", "gcc"), ctx.path_extra)["ld"],
                         os.path.join(pinned, "ld"))
        self.assertEqual(ept.Ctx(self.args(binutils=os.path.join(bdir, "bin")), {"version": "3.15.0"}).binutils, bdir)
        self.assertEqual(ept.Ctx(self.args(), {"version": "3.15.0"}).configure(True)[-1][:3], "CXX")  # no AR

    def test_a_binutils_without_as_and_ld_or_a_missing_cc_is_refused(self):
        os.makedirs(os.path.join(self.root, "empty", "bin"))
        with self.assertRaises(ept.ToolError) as cm:
            ept.binutils_dir(os.path.join(self.root, "empty"))
        self.assertIn("no as", str(cm.exception))
        with self.assertRaises(ept.ToolError):
            ept.pick_cc(os.path.join(self.root, "nope", "gcc"))
        self.assertEqual(ept.real_tool_path("/usr/bin/gcc"), "/usr/bin/gcc")      # the name stays

    def test_the_cxx_partner_by_name_beside_the_c_compiler(self):
        self.assertEqual(ept.cxx_name("/o/bin/gcc"), ("g++", "g++"))
        self.assertEqual(ept.cxx_name("/usr/bin/gcc-14"), ("g++-14", "g++"))
        self.assertEqual(ept.cxx_name("/usr/bin/x86_64-linux-gnu-gcc-13"), ("x86_64-linux-gnu-g++-13", "g++"))
        self.assertEqual(ept.cxx_name("/usr/bin/clang-18"), ("clang++-18", "clang++"))
        self.assertEqual(ept.cxx_name("/usr/bin/cc"), ("c++", "c++"))
        self.assertEqual(ept.cxx_name("/usr/bin/tcc"), ("", ""))
        opt, _ = self.toolchain()
        gcc = os.path.join(opt, "gcc-16.2.0", "bin", "gcc")
        self.assertEqual(ept.cxx_partner(gcc), gcc[:-3] + "g++")
        fake_gcc(opt, "gcc-onlyc/bin/gcc")
        self.assertEqual(ept.cxx_partner(os.path.join(opt, "gcc-onlyc", "bin", "gcc")), "")

    def test_a_cc_without_a_cxx_beside_it_is_never_paired_quietly_with_the_systems(self):
        opt, _ = self.toolchain()
        onlyc = fake_gcc(opt, "gcc-onlyc/bin/gcc")
        sys_gxx = os.path.join(self.root, "sys", "g++")
        script(self.root, "sys/g++", "exit 0\n")
        with mock.patch.object(ept, "which", lambda name, path=None: sys_gxx if name == "g++" else ""):
            g = ept.Ctx(self.args(cc=onlyc), {"version": "3.15.0"})
            self.assertIn("no C++ compiler beside --cc " + onlyc, g.cxx_problem)
            self.assertIn("--cxx PATH", g.cxx_problem)
            self.assertIn("the system's is " + sys_gxx, g.cxx_problem)
            # scraper mode: said, and the system's taken as before
            s = ept.Ctx(self.args(purpose="scraper", cc=onlyc), {"version": "3.15.0"})
            self.assertEqual(s.cxx, sys_gxx)
            self.assertIn("CXX is " + sys_gxx, s.cxx_problem)
            # --cxx names it knowingly: no problem, CXX= is that one
            k = ept.Ctx(self.args(cc=onlyc, cxx=sys_gxx), {"version": "3.15.0"})
            self.assertEqual((k.cxx, k.cxx_problem), (sys_gxx, ""))
            self.assertIn("CXX=" + sys_gxx, k.configure(True))
        # the default compiler (no --cc) keeps the system's pair, no problem
        d = ept.Ctx(self.args(), {"version": "3.15.0"})
        self.assertEqual(d.cxx_problem, "")
        # --cxx by its real directory, like --cc
        c = ept.Ctx(self.args(cc=os.path.join(opt, "gcc-current", "bin", "gcc"),
                              cxx=os.path.join(opt, "gcc-current", "bin", "g++")), {"version": "3.15.0"})
        self.assertEqual(c.cxx, os.path.join(opt, "gcc-16.2.0", "bin", "g++"))
        with self.assertRaises(ept.ToolError) as cm:
            ept.Ctx(self.args(cxx=os.path.join(self.root, "nope", "g++")), {"version": "3.15.0"})
        self.assertIn("--cxx", str(cm.exception))

    def test_general_mode_doctor_reports_the_missing_cxx_as_required(self):
        ctx = Ns(cc="/o/gcc-onlyc/bin/gcc", general=True, cxx_problem="no C++ compiler beside --cc /o/gcc-onlyc/bin/gcc")
        r = ept.doctor_report(ctx, dict((row[0], True) for row in ept.DEPS), True, euid=1000, osr=ALMA,
                              pm=("dnf", "dnf"), have=lambda name: "/usr/bin/" + name)
        self.assertFalse(r["ok"])
        self.assertIn({"what": ctx.cxx_problem, "package": "", "level": "required"}, r["missing"])
        ctx.general = False                                        # scraper mode: a warning in preflight only
        r = ept.doctor_report(ctx, dict((row[0], True) for row in ept.DEPS), True, euid=1000, osr=ALMA,
                              pm=("dnf", "dnf"), have=lambda name: "/usr/bin/" + name)
        self.assertTrue(r["ok"])

    def test_the_comment_a_compiler_leaves(self):
        self.assertEqual(ept.expected_comment("gcc (GCC) 16.2.0"), "GCC: (GNU) 16.2.0")
        self.assertEqual(ept.expected_comment("gcc (GCC) 14.2.1 20250110 (Red Hat 14.2.1-7)"),
                         "GCC: (GNU) 14.2.1 20250110 (Red Hat 14.2.1-7)")
        self.assertEqual(ept.expected_comment("gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0"),
                         "GCC: (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0")
        self.assertEqual(ept.expected_comment("x86_64-linux-gnu-gcc-13 (Ubuntu 13.3.0) 13.3.0"),
                         "GCC: (Ubuntu 13.3.0) 13.3.0")
        self.assertEqual(ept.expected_comment("Ubuntu clang version 18.1.3 (1ubuntu1)"),
                         "Ubuntu clang version 18.1.3 (1ubuntu1)")
        self.assertEqual(ept.expected_comment("tcc version 0.9.27"), "")

    def test_the_python3_link_inside_the_prefix(self):
        prefix = os.path.join(self.root, "p")
        script(prefix, "bin/python3.13", "exit 0\n")
        script(prefix, "bin/python3.13-config", "exit 0\n")
        self.assertTrue(ept.ensure_python3_link(prefix))
        self.assertEqual(os.readlink(os.path.join(prefix, "bin", "python3")), "python3.13")
        self.assertFalse(ept.ensure_python3_link(prefix))                # make install made it: left alone

    def test_a_new_tarball_forgets_both_modes_builds(self):
        w = os.path.join(self.root, "w")
        state = ept.State(os.path.join(w, "state.json"))
        for key in ("3.14.8", "3.14.8-general"):
            state.set_build(key, make_done="t")
            put(w, "build-%s/Makefile" % key, "")
        src = os.path.join(self.root, "t", "Python-3.14.8")
        put(src, "configure", "x")
        tb = os.path.join(self.root, "Python-3.14.8.tar.xz")
        subprocess.check_call(["tar", "-cJf", tb, "-C", os.path.dirname(src), "Python-3.14.8"])
        ctx = ept.Ctx(self.args(workdir=w), {"version": "3.14.8"})
        ctx.tar = {"url": "file://" + tb, "sha256": "", "size": 0}
        ept.fetch_and_unpack(ctx, state)
        self.assertFalse(os.path.exists(os.path.join(w, "build-3.14.8")))
        self.assertFalse(os.path.exists(os.path.join(w, "build-3.14.8-general")))
        self.assertNotIn("make_done", state.build("3.14.8"))
        self.assertNotIn("make_done", state.build("3.14.8-general"))


class CurrentLink(TempRoot):
    def test_atomic_link_relink_and_a_real_directory_is_left_alone(self):
        opt = os.path.join(self.root, "opt")
        a, b = os.path.join(opt, "cpython-3.14.8"), os.path.join(opt, "cpython-3.15.0")
        os.makedirs(a)
        os.makedirs(b)
        self.assertTrue(ept.link_current(opt, a))
        self.assertEqual(os.readlink(os.path.join(opt, ept.CURRENT)), a)
        self.assertTrue(ept.link_current(opt, b))
        self.assertEqual(os.readlink(os.path.join(opt, ept.CURRENT)), b)
        self.assertFalse(os.path.lexists(os.path.join(opt, ept.CURRENT + ".new")))
        os.unlink(os.path.join(opt, ept.CURRENT))
        put(opt, ept.CURRENT + "/mine", "x")
        self.assertFalse(ept.link_current(opt, a))
        self.assertTrue(os.path.isfile(os.path.join(opt, ept.CURRENT, "mine")))


class GeneralBuild(TempRoot):
    """cmd_build in general mode with every slow step stubbed: install, verify (with the
    compiler proof asked for), the general smoke test, then cpython-current."""

    def run_build(self, verify_good=True, smoke_good=True, link=None, current_dir=False, no_check=False,
                  banner="gcc (GCC) 16.2.0"):
        opt = os.path.join(self.root, "opt")
        self.prefix = os.path.join(opt, "cpython-3.14.8")
        if current_dir:
            put(opt, ept.CURRENT + "/mine", "x")
        self.calls = {}

        def verify(p, offline=False, quiet=False, expect_compiler=""):
            self.calls["expect"] = expect_compiler
            return verify_good, {"good": verify_good, "when": "t", "version": "3.14.8"}

        def smoke(python, target, keep=False):
            self.calls["smoke"] = (python, target)
            return smoke_good, "" if smoke_good else "the venv's pip does not run"

        def fake_install(ctx, state, rebuild, args_list, old=""):
            put(ctx.prefix, "bin/python3", "")
            put(ctx.prefix, ept.MARKER, json.dumps({"tool": ept.TOOL, "state": "unverified",
                                                    "configure": args_list}))
            return ""
        stubs = {
            "resolve_release": lambda args: ({"version": "3.14.8", "id": "", "date": ""}, "test", "test"),
            "tarball_info": lambda rel: {"url": "", "sha256": "", "size": 0},
            "preflight": lambda ctx, install_deps=False: {},
            "show_plan": lambda ctx, m: None,
            "fetch_and_unpack": lambda ctx, state: None,
            "do_configure": lambda ctx, state, a, f: None,
            "do_make": lambda ctx, state: {"missing": [], "failed": [], "removed": [], "disabled": [], "summary": ""},
            "judge_modules": lambda rep, found, fam, mgr, **k: None,
            "compiler_banner": lambda cc: banner,
            "prog_paths": lambda cc, extra=None: {"as": "/b/bin/as", "ld": "/b/bin/ld"},
            "verify_interpreter": verify, "general_smoke": smoke, "do_install": fake_install,
        }
        args = Ns(purpose="general", version=None, prefix=None, workdir=os.path.join(self.root, "w"), opt_root=opt,
                  scraper=os.path.join(self.root, "scraper"), cc="", binutils="", jobs=1, no_optimize=False,
                  install_deps=False, offline=True, constraints="", verbose=False, rebuild=False, no_check=no_check,
                  keep=False, link_current=link)
        with mock.patch.multiple(ept, **stubs):
            try:
                return ept.cmd_build(args), None
            except ept.ToolError as e:
                return None, e

    def record(self):
        return ept.State(os.path.join(self.root, "w", "state.json")).build("3.14.8-general")

    def test_a_good_build_is_verified_with_its_compiler_smoke_tested_and_linked(self):
        rc, err = self.run_build()
        self.assertEqual((rc, err), (0, None))
        current = os.path.join(self.root, "opt", ept.CURRENT)
        self.assertEqual(os.readlink(current), self.prefix)
        self.assertEqual(self.calls["expect"], "gcc (GCC) 16.2.0")                 # the .comment proof asked
        self.assertEqual(self.calls["smoke"][0], os.path.join(self.prefix, "bin", "python3"))
        rec = ept.prefix_record(self.prefix)
        self.assertEqual((rec["for"], rec["as"], rec["state"]), ("general", "/b/bin/as", "verified"))
        self.assertIn("for general use", ept.read_text(os.path.join(self.prefix, "BUILD-INFO.txt")))
        self.assertEqual((self.record()["status"], self.record()["stage"], self.record()["for"]),
                         ("done", "link", "general"))
        self.assertIn("export PATH=\"%s/bin:$PATH\"" % current, "\n".join(self.printed))

    def test_a_failed_smoke_test_rolls_back_and_links_nothing(self):
        rc, err = self.run_build(smoke_good=False)
        self.assertIn("general smoke test failed", str(err))
        self.assertFalse(os.path.lexists(self.prefix))
        self.assertTrue(any(n.startswith("cpython-3.14.8.failed-") for n in os.listdir(os.path.dirname(self.prefix))))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "opt", ept.CURRENT)))
        self.assertEqual((self.record()["status"], self.record()["stage"]), ("failed", "smoke"))
        self.assertIn("smoke test failed", self.record()["error"])

    def test_a_build_that_does_not_verify_links_nothing(self):
        rc, err = self.run_build(verify_good=False)
        self.assertIn("does not verify", str(err))
        self.assertNotIn("smoke", self.calls)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "opt", ept.CURRENT)))
        self.assertEqual(self.record()["stage"], "verify")

    def test_no_link_current_and_a_real_directory_named_cpython_current(self):
        rc, _ = self.run_build(link=False)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "opt", ept.CURRENT)))
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        self.printed = []
        rc, _ = self.run_build(current_dir=True)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "opt", ept.CURRENT, "mine")))
        self.assertIn("real directory", "\n".join(self.printed))

    def test_no_check_skips_the_smoke_test(self):
        rc, _ = self.run_build(no_check=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("smoke", self.calls)

    def test_a_reused_prefix_is_proved_against_the_compiler_its_record_names(self):
        rc, err = self.run_build()
        self.assertEqual((rc, err), (0, None))
        while ept._LOCKS:
            ept._LOCKS.pop().close()
        # The same compiler path, a distro update later: it answers another version now.
        # `build` again only re-verifies, against what BUILT it (as `verify` does).
        rc, err = self.run_build(banner="gcc (GCC) 16.2.1")
        self.assertEqual((rc, err), (0, None))
        self.assertEqual(self.calls["expect"], "gcc (GCC) 16.2.0")
        self.assertIn("this build was made when it answered", "\n".join(self.printed))
        rec = ept.prefix_record(self.prefix)
        self.assertEqual((rec["compiler_banner"], rec["verify"]["good"]), ("gcc (GCC) 16.2.0", True))

    def setUp(self):
        TempRoot.setUp(self)
        self.printed = []
        p = mock.patch.object(ept, "out", lambda text="": self.printed.append(text))
        p.start()
        self.addCleanup(p.stop)


class GeneralSmoke(TempRoot):
    def test_a_real_venv_from_this_python(self):
        target = os.path.join(self.root, "check-venv-x-general")
        saved = ept.out
        ept.out = lambda text="": None
        try:
            good, why = ept.general_smoke(sys.executable, target)
        except ept.ToolError as e:
            self.skipTest("this python cannot make a venv with pip here: %s" % str(e).splitlines()[0])
        finally:
            ept.out = saved
        self.assertTrue(good, why)
        self.assertFalse(os.path.exists(target))                     # removed once it passed

    def test_a_pip_that_is_not_the_venvs_own_fails(self):
        target = os.path.join(self.root, "v")

        def fake_venv(python, t):
            return script(t, "bin/python", 'echo "pip 25.2 from /usr/lib/python3/dist-packages/pip (python 3.14)"\n')
        with mock.patch.object(ept, "create_venv", fake_venv), mock.patch.object(ept, "out", lambda text="": None):
            good, why = ept.general_smoke("/x/python3", target)
        self.assertFalse(good)
        self.assertIn("not its own", why)
        self.assertTrue(os.path.isdir(target))                       # kept for a look


class VerifyGeneral(TempRoot):
    def prefix(self):
        p = os.path.join(self.root, "prefix")
        os.makedirs(os.path.join(p, "bin"))
        os.symlink(sys.executable, os.path.join(p, "bin", "python3"))
        return p

    def facts(self, comments):
        return mock.patch.object(ept, "elf_facts", lambda path: {"runpath": [], "needed": [], "comment": comments})

    def test_the_sh_core_and_nice_lists_are_checked(self):
        good, summary = ept.verify_interpreter(self.prefix(), offline=True, quiet=True)
        self.assertIn("sh_core", summary["missing"])
        self.assertIn("sh_nice", summary["missing"])
        self.assertEqual(summary["missing"]["sh_core"], [])

    def test_the_binary_must_name_the_compiler_asked_for(self):
        p = self.prefix()
        with self.facts(["GCC: (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0"]):
            good, summary = ept.verify_interpreter(p, offline=True, quiet=True, expect_compiler="gcc (GCC) 16.2.0")
        self.assertFalse(good)
        self.assertTrue(any("not in the binary's .comment" in f for f in summary["failures"]))
        with self.facts(["GCC: (Ubuntu 13.3.0) 13.3.0", "GCC: (GNU) 16.2.0"]):    # crt files keep the system's
            good, summary = ept.verify_interpreter(p, offline=True, quiet=True, expect_compiler="gcc (GCC) 16.2.0")
        self.assertEqual(summary["compiler_proof"], "GCC: (GNU) 16.2.0")
        self.assertNotIn("failures", summary)


DOCTOR_KEYS = ["enable_repos", "install_command", "missing", "ok", "package_manager", "tool"]
ALMA = {"ID": "almalinux", "ID_LIKE": "rhel centos fedora", "VERSION_ID": "10.2"}


class Doctor(TempRoot):
    def report(self, found_missing=(), osr=ALMA, pm=("dnf", "dnf"), general=True, cc="/usr/bin/gcc", cc_ok=True,
               euid=1000, missing_tools=()):
        ctx = Ns(cc=cc, general=general)
        found = dict((row[0], row[0] not in found_missing) for row in ept.DEPS)
        return ept.doctor_report(ctx, found, cc_ok, euid=euid, osr=osr, pm=pm,
                                 have=lambda name: name not in missing_tools)

    def test_the_contract_shape_and_crb_on_enterprise_linux(self):
        r = self.report(found_missing=("mpdecimal", "tk", "libxml2"), missing_tools=("pkg-config",))
        self.assertEqual(sorted(r), DOCTOR_KEYS)
        self.assertTrue(r["ok"])
        self.assertEqual(r["package_manager"], "dnf")
        self.assertEqual(r["enable_repos"], ["crb"])
        self.assertEqual(r["install_command"], "sudo dnf install -y pkgconfig mpdecimal-devel")
        levels = dict((m["package"], m["level"]) for m in r["missing"])
        self.assertEqual(levels, {"pkgconfig": "wanted", "mpdecimal-devel": "wanted", "tk-devel": "optional"})
        for m in r["missing"]:
            self.assertEqual(sorted(m), ["level", "package", "what"])
        scraper = self.report(found_missing=("libxml2",), general=False)
        self.assertEqual([(m["package"], m["level"]) for m in scraper["missing"]], [("libxml2-devel", "optional")])
        self.assertEqual(self.report(found_missing=("mpdecimal",), osr={"ID": "rocky", "VERSION_ID": "8.10"})
                         ["enable_repos"], ["powertools"])
        self.assertIn("codeready-builder-for-rhel-10-",
                      self.report(found_missing=("mpdecimal",), osr={"ID": "rhel", "VERSION_ID": "10.1"})
                      ["enable_repos"][0])
        self.assertEqual(self.report(found_missing=("mpdecimal",), osr={"ID": "fedora", "VERSION_ID": "43"})
                         ["enable_repos"], [])

    def test_packages_per_distribution(self):
        cases = ((("apt-get", "apt"), "apt-get", "sudo apt-get install -y libssl-dev libmpdec-dev"),
                 (("zypper", "zypper"), "zypper", "sudo zypper install -y libopenssl-devel mpdecimal-devel"),
                 (("pacman", "pacman"), "pacman", "sudo pacman -S --needed --noconfirm openssl mpdecimal"),
                 (("yum", "dnf"), "dnf", "sudo yum install -y openssl-devel mpdecimal-devel"))
        for pm, contract, cmd in cases:
            r = self.report(found_missing=("openssl", "mpdecimal"), pm=pm, osr={"ID": "x"})
            self.assertEqual((r["package_manager"], r["install_command"], r["ok"]), (contract, cmd, False), pm)
        r = self.report(found_missing=("openssl",), pm=("", ""), osr={"ID": "x"})
        self.assertEqual((r["package_manager"], r["install_command"]), (None, ""))

    def test_root_no_compiler_and_a_broken_one(self):
        self.assertFalse(self.report(euid=0)["ok"])
        r = self.report(cc="", pm=("apt-get", "apt"), osr={"ID": "ubuntu"})
        self.assertEqual(r["missing"][0]["package"], "build-essential")
        self.assertEqual(len(r["missing"]), 1)                       # headers cannot be asked without one
        r = self.report(cc_ok="t.c: cannot execute 'cc1'")
        self.assertFalse(r["ok"])
        self.assertIn("cc1", r["missing"][0]["what"])

    def test_which_package_manager_fits_the_distribution(self):
        every = lambda name: name in ("dnf", "apt-get", "zypper", "pacman", "yum")
        self.assertEqual(ept.pkg_manager({"ID": "ubuntu", "ID_LIKE": "debian"}, every), ("apt-get", "apt"))
        self.assertEqual(ept.pkg_manager(ALMA, every), ("dnf", "dnf"))
        self.assertEqual(ept.pkg_manager({"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"}, every),
                         ("zypper", "zypper"))
        self.assertEqual(ept.pkg_manager({"ID": "arch"}, every), ("pacman", "pacman"))
        self.assertEqual(ept.pkg_manager({"ID": "centos", "VERSION_ID": "7"}, lambda n: n == "yum"), ("yum", "dnf"))
        self.assertEqual(ept.pkg_manager({}, lambda n: False), ("", ""))


class DoctorJson(Captured):
    def test_doctor_json_through_main_is_pure_json_and_creates_nothing(self):
        work = os.path.join(self.root, "w")
        found = dict((row[0], row[0] != "zstd") for row in ept.DEPS)
        with mock.patch.object(ept, "compiler_works", return_value=(True, "ok")), \
                mock.patch.object(ept, "check_deps", return_value=found), \
                mock.patch.object(ept, "pkg_manager", return_value=("apt-get", "apt")):
            code, stdout, stderr = self.run_main("doctor", "--json", "--for", "general", "--opt-root",
                                                 os.path.join(self.root, "opt"), "--workdir", work, "--yes")
        self.assertEqual(code, 0, stderr)
        r = json.loads(stdout)
        self.assertEqual(sorted(r), DOCTOR_KEYS)
        self.assertEqual(r["install_command"], "sudo apt-get install -y libzstd-dev")
        self.assertFalse(os.path.exists(work))
        self.assertIsNone(ept.UI.stream)                         # main() leaves the process as it found it

    def test_as_root_doctor_json_answers_and_says_so(self):
        with mock.patch.object(ept, "compiler_works", return_value=(True, "ok")), \
                mock.patch.object(ept, "check_deps", return_value={}):
            code, stdout, stderr = self.run_main("doctor", "--json", "--opt-root", self.root, "--workdir",
                                                 os.path.join(self.root, "w"), euid=0)
        self.assertEqual(code, 1)
        self.assertIn("not root", json.loads(stdout)["missing"][0]["what"])

    def test_an_opt_root_that_cannot_be_made_is_required(self):
        put(self.root, "afile", "x")
        with mock.patch.object(ept, "compiler_works", return_value=(True, "ok")), \
                mock.patch.object(ept, "check_deps", return_value={}):
            code, stdout, stderr = self.run_main("doctor", "--json", "--opt-root", os.path.join(self.root, "afile", "opt"),
                                                 "--workdir", os.path.join(self.root, "w"))
        r = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIn("--opt-root", r["missing"][-1]["what"])
        self.assertEqual(r["missing"][-1]["level"], "required")

    def test_a_cc_that_is_not_there_is_reported_not_raised(self):
        with mock.patch.object(ept, "compiler_works", return_value=(True, "ok")), \
                mock.patch.object(ept, "check_deps", return_value={}):
            code, stdout, stderr = self.run_main("doctor", "--json", "--cc", os.path.join(self.root, "gcc-current",
                                                 "bin", "gcc"), "--opt-root", self.root)
        self.assertEqual(code, 1)
        r = json.loads(stdout)
        self.assertIn("--cc", r["missing"][0]["what"])

    def test_with_json_an_error_goes_to_stderr_and_stdout_stays_empty(self):
        for argv, want in ((("status", "--json", "--profile", "nosuch"), 1),
                           (("doctor", "--json", "--profile", "nosuch"), 1),
                           (("detect", "--json", "--profile", "nosuch"), 1)):
            code, stdout, stderr = self.run_main(*(argv + ("--opt-root", self.root)))
            self.assertEqual(code, want, argv)
            self.assertEqual(stdout, "", argv)
            self.assertIn("ERROR:", stderr, argv)
            self.assertIn("nosuch", stderr, argv)
        code, stdout, stderr = self.run_main("status", "--profile", "nosuch", "--opt-root", self.root)
        self.assertIn("ERROR:", stdout)                          # without --json, as always: stdout
        self.assertEqual(stderr, "")


STATUS_KEYS = ["builds", "current", "installed", "opt_root", "tool"]
INSTALLED_KEYS = ["built_by_tool", "compiler", "for", "name", "prefix", "runs", "version"]
BUILD_KEYS = ["error", "id", "stage", "status"]


class Status(Captured):
    def fake_opt(self):
        opt = os.path.join(self.root, "opt")
        ours = os.path.join(opt, "cpython-3.14.8")
        fake_python(opt, "cpython-3.14.8")
        marker(ours, compiler="/o/gcc-16.2.0/bin/gcc (gcc (GCC) 16.2.0)", binutils="/o/binutils-2.46")
        scr = os.path.join(opt, "cpython-3.14.8-claude_scraper")
        fake_python(opt, "cpython-3.14.8-claude_scraper")
        marker(scr, **{"for": "/run/media/x/claude_scraper"})
        fake_python(opt, "cpython-3.13.1", "3.13.1")                       # hand-built: not ours
        fake_python(opt, "cpython-3.12.0", "3.12.0", runs=False)           # there, but it does not run
        put(opt, "cpython-3.14.8.old-20261009-1000/bin/python3", "")
        put(opt, "cpython-3.15.0/README", "")                               # no bin/python3
        os.symlink(ours, os.path.join(opt, ept.CURRENT))
        w = os.path.join(self.root, "w")
        put(w, "state.json", json.dumps({"tool": ept.TOOL, "checks": [{"status": "ok"}], "builds": {
            "3.14.8-general": {"status": "done", "stage": "link"},
            "3.15.0-general": {"status": "running", "stage": "make", "pid": 2 ** 22 + 7},
            "3.13.9-general": {"status": "failed", "stage": "configure", "error": "configure failed (exit 1)"},
            "3.14.8": {"make_done": "t", "verified": "t"}}}))
        return opt, w, ours

    def test_status_is_the_toolkit_contract(self):
        opt, w, ours = self.fake_opt()
        d = ept.status_data(opt, w)
        self.assertTrue(set(STATUS_KEYS) <= set(d))
        self.assertEqual((d["tool"], d["opt_root"]), (ept.TOOL, opt))
        self.assertEqual(d["current"], {"cpython-current": ours})
        for e in d["installed"]:
            self.assertTrue(set(INSTALLED_KEYS) <= set(e), e)
        by = dict((os.path.basename(e["prefix"]), e) for e in d["installed"])
        self.assertEqual(sorted(by), ["cpython-3.12.0", "cpython-3.13.1", "cpython-3.14.8",
                                      "cpython-3.14.8-claude_scraper"])
        self.assertEqual(dict((k, by["cpython-3.14.8"][k]) for k in INSTALLED_KEYS),
                         {"name": "cpython", "version": "3.14.8", "prefix": ours, "runs": True, "built_by_tool": True,
                          "compiler": "/o/gcc-16.2.0/bin/gcc (gcc (GCC) 16.2.0)", "for": "general"})
        self.assertTrue(by["cpython-3.14.8"]["current"])
        self.assertEqual(by["cpython-3.14.8-claude_scraper"]["for"], "/run/media/x/claude_scraper")
        self.assertEqual((by["cpython-3.13.1"]["runs"], by["cpython-3.13.1"]["built_by_tool"],
                          by["cpython-3.13.1"]["compiler"], by["cpython-3.13.1"]["for"], by["cpython-3.13.1"]["version"]),
                         (True, False, None, None, "3.13.1"))                # the menu's third state
        self.assertEqual((by["cpython-3.12.0"]["runs"], by["cpython-3.12.0"]["version"]), (False, None))
        self.assertEqual(sorted(os.path.basename(p["path"]) for p in d["leftovers"]),
                         ["cpython-3.14.8.old-20261009-1000", "cpython-3.15.0"])
        builds = dict((b["id"], dict((k, b[k]) for k in BUILD_KEYS)) for b in d["builds"])
        self.assertEqual(builds["3.14.8-general"], {"id": "3.14.8-general", "status": "done", "stage": "link",
                                                    "error": None})
        self.assertEqual(builds["3.15.0-general"]["status"], "killed")       # recorded running, nobody holds the lock
        self.assertEqual(builds["3.13.9-general"], {"id": "3.13.9-general", "status": "failed", "stage": "configure",
                                                    "error": "configure failed (exit 1)"})
        self.assertEqual(builds["3.14.8"]["status"], "done")                   # a record from before the status
        self.assertEqual(d["link"]["version"], "3.14.8")

    def test_nothing_there_is_null_and_empty(self):
        d = ept.status_data(os.path.join(self.root, "opt"), os.path.join(self.root, "w"))
        self.assertEqual((d["current"], d["installed"], d["builds"]), ({"cpython-current": None}, [], []))

    def test_status_json_through_main_is_pure_json_and_the_text_says_it_too(self):
        opt, w, ours = self.fake_opt()
        code, stdout, stderr = self.run_main("status", "--json", "--opt-root", opt, "--workdir", w, euid=0)
        self.assertEqual(code, 0)
        d = json.loads(stdout)
        self.assertTrue(set(STATUS_KEYS) <= set(d))
        self.assertEqual(d["mode"], "scraper")
        code, stdout, stderr = self.run_main("status", "--for", "general", "--opt-root", opt, "--workdir", w)
        self.assertEqual(code, 0)
        self.assertIn("<- current", stdout)
        self.assertIn("DOES NOT RUN", stdout)
        self.assertIn("KILLED at make", stdout)
        self.assertIn("not this tool's", stdout)


class Profiles(TempRoot):
    def test_round_trip_and_what_is_never_stored(self):
        a = Ns(purpose="general", version=None, scraper=ept.DEFAULT_SCRAPER, workdir="~/w", opt_root="~/o",
               prefix=None, cc="/x/gcc-16.2.0/bin/gcc", binutils="/x/binutils-2.46", jobs=6, constraints="",
               no_optimize=True, link_current=False, rebuild=True, yes=True, install_deps=True)
        path = ept.save_profile("mine", a)
        text = ept.read_text(path)
        self.assertTrue(text.startswith(ept.PROFILE_HEADER))
        for line in ("for = general", "optimize = no", "link_current = no", "jobs = 6"):
            self.assertIn("\n" + line + "\n", text)
        for never in ("rebuild", "yes", "install_deps", "offline"):
            self.assertNotIn("\n%s =" % never, text)
        v = ept.load_profile("mine")
        self.assertEqual((v["purpose"], v["version"], v["cc"], v["binutils"], v["jobs"], v["no_optimize"],
                          v["link_current"], v["opt_root"]),
                         ("general", None, "/x/gcc-16.2.0/bin/gcc", "/x/binutils-2.46", 6, True, False,
                          os.path.expanduser("~/o")))
        self.assertEqual([p["name"] for p in ept.list_profiles()], ["mine"])
        put(ept.profiles_dir(), "stray.ini", "[build]\nfor = general\n")       # no header: not one of ours
        self.assertEqual([p["name"] for p in ept.list_profiles()], ["mine"])
        self.assertIn("general | CPython latest", ept.list_profiles()[0]["summary"])

    def test_defaults_then_profile_then_command_line(self):
        ept.save_profile("p", Ns(purpose="general", version="3.14", cc="/x/gcc", binutils="", jobs=0,
                                 no_optimize=False, link_current=None))
        args = ept.build_parser().parse_args(["build", "--profile", "p", "--version", "3.13"])
        ept.merge_args(args)
        self.assertEqual((args.purpose, args.version, args.cc, args.opt_root, args.link_current),
                         ("general", "3.13", "/x/gcc", os.path.expanduser(ept.DEFAULT_OPT), None))
        args = ept.build_parser().parse_args(["build", "--profile", "p", "--for", "scraper", "--no-link-current"])
        ept.merge_args(args)
        self.assertEqual((args.purpose, args.version, args.link_current), ("scraper", "3.14", False))
        self.assertEqual(ept.build_parser().parse_args(["build", "--activate"]).link_current, True)
        self.assertEqual(ept.build_parser().parse_args(["build", "--no-activate"]).link_current, False)


class Main(Captured):
    def test_every_command_takes_opt_root_yes_and_for(self):
        p = ept.build_parser()
        for cmd in ept.COMMANDS:
            a = p.parse_args([cmd, "--opt-root", "/x/opt", "--yes", "--for", "general"])
            self.assertEqual((a.opt_root, a.yes, a.purpose), ("/x/opt", True, "general"), cmd)
        self.assertEqual(p.parse_args(["verify", "/some/prefix"]).prefix_arg, "/some/prefix")

    def test_cxx_is_taken_by_build_plan_doctor_wizard_toolchains_and_kept_in_a_profile(self):
        p = ept.build_parser()
        for cmd in ("build", "plan", "doctor", "wizard", "toolchains"):
            self.assertEqual(p.parse_args([cmd, "--cxx", "/x/g++"]).cxx, "/x/g++", cmd)
        args = ept.merge_args(p.parse_args(["build", "--for", "general", "--cc", "/x/gcc", "--cxx", "/x/g++"]))
        ept.save_profile("withcxx", args)
        self.assertEqual(ept.load_profile("withcxx")["cxx"], "/x/g++")
        self.assertIn("(c++ /x/g++)", ept.list_profiles()[0]["summary"])

    def test_a_cxx_that_is_not_there_is_reported_not_raised(self):
        with mock.patch.object(ept, "compiler_works", return_value=(True, "ok")), \
                mock.patch.object(ept, "check_deps", return_value={}):
            code, stdout, stderr = self.run_main("doctor", "--json", "--cxx", os.path.join(self.root, "no", "g++"),
                                                 "--opt-root", self.root, "--workdir", os.path.join(self.root, "w"))
        self.assertEqual(code, 1, stderr)
        self.assertIn("--cxx", json.loads(stdout)["missing"][0]["what"])

    def test_general_check_finds_a_bare_python_on_path_never_in_the_current_directory(self):
        opt = os.path.join(self.root, "opt")
        prefix = os.path.join(opt, "cpython-3.14.8")
        fake_python(opt, "cpython-3.14.8")
        os.makedirs(os.path.join(self.root, "path"))
        os.symlink(os.path.join(prefix, "bin", "python3"), os.path.join(self.root, "path", "python3"))
        os.symlink(prefix, os.path.join(opt, ept.CURRENT))
        self.assertEqual(ept.python_prefix(os.path.join(prefix, "bin", "python3")), prefix)
        cur = os.path.join(opt, ept.CURRENT)
        self.assertEqual(ept.python_prefix(os.path.join(cur, "bin", "python3")), cur)     # the name kept
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.root, "path")}):
            self.assertEqual(ept.python_prefix("python3"), prefix)                         # by the link's target
            with self.assertRaises(ept.ToolError) as cm:
                ept.python_prefix("python3-nosuch")
            self.assertIn("on your PATH", str(cm.exception))
        script(self.root, "shims/python3", "exit 0\n")
        with self.assertRaises(ept.UsageError):
            ept.python_prefix(os.path.join(self.root, "shims", "python3"))
        other = script(prefix, "bin/python3.13", "exit 0\n")       # bin/python3 is another interpreter
        with self.assertRaises(ept.UsageError) as cm:
            ept.python_prefix(other)
        self.assertIn("another interpreter", str(cm.exception))
        # through main, from a directory that has nothing to do with it
        seen = {}

        def verify(args):
            seen["prefix"] = args.prefix_arg
            return 0
        here = os.getcwd()
        os.chdir(os.path.join(self.root, "path"))
        try:
            with mock.patch.dict(os.environ, {"PATH": os.path.join(self.root, "path")}), \
                    mock.patch.object(ept, "cmd_verify", verify), \
                    mock.patch.object(ept, "general_smoke", lambda py, target, keep=False: (True, "")):
                code, stdout, stderr = self.run_main("check", "--for", "general", "--python", "python3",
                                                     "--opt-root", opt, "--workdir", os.path.join(self.root, "w"))
        finally:
            os.chdir(here)
        self.assertEqual((code, seen), (0, {"prefix": prefix}), stdout + stderr)

    def test_exit_codes(self):
        with mock.patch.object(ept.sys.stdin, "isatty", return_value=False):
            self.assertEqual(self.run_main()[0], 2)                                 # no command: the help
            self.assertEqual(self.run_main("wizard")[0], 2)                         # nobody to ask
        self.assertEqual(self.run_main("frobnicate")[0], 2)                         # argparse
        self.assertEqual(self.run_main("build", "--for", "bogus")[0], 2)
        code, stdout, _ = self.run_main("venv", "--for", "general", "--opt-root", self.root)
        self.assertEqual(code, 2)                                                   # general mode has no scraper
        self.assertIn("no scraper", stdout)
        for cmd in ("build", "plan", "clean", "wizard"):
            code, stdout, _ = self.run_main(cmd, "--opt-root", self.root, euid=0)
            self.assertEqual(code, 1, cmd)
            self.assertIn("as root", stdout)

        def interrupted(args):
            raise KeyboardInterrupt()
        with mock.patch.dict(ept.COMMANDS, {"profiles": interrupted}):
            self.assertEqual(self.run_main("profiles")[0], 130)

    def test_dry_run_is_the_plan_and_builds_nothing(self):
        called = []
        with mock.patch.multiple(ept, resolve_release=lambda args: ({"version": "3.15.0", "id": ""}, "t", "t"),
                                 tarball_info=lambda rel: {"url": "u", "sha256": "", "size": 0},
                                 preflight=lambda ctx, install_deps=False: {"mpdecimal": True},
                                 show_plan=lambda ctx, m: called.append(ctx.prefix),
                                 fetch_and_unpack=lambda *a: called.append("FETCHED")):
            code, stdout, stderr = self.run_main("build", "--dry-run", "--for", "general", "--opt-root", self.root,
                                                 "--workdir", os.path.join(self.root, "w"))
        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(called, [os.path.join(self.root, "cpython-3.15.0")])
        self.assertFalse(os.path.exists(os.path.join(self.root, "w")))

    def test_verify_a_prefix_through_main_with_json(self):
        prefix = os.path.join(self.root, "opt", "cpython-3.14.8")
        fake_python(os.path.join(self.root, "opt"), "cpython-3.14.8")
        marker(prefix, compiler_banner="gcc (GCC) 16.2.0")
        os.symlink(prefix, os.path.join(self.root, "opt", ept.CURRENT))
        seen = {}

        def verify(p, offline=False, quiet=False, expect_compiler=""):
            seen.update(p=p, expect=expect_compiler, quiet=quiet)
            return True, {"good": True, "when": "now", "compiler_proof": "GCC: (GNU) 16.2.0"}
        with mock.patch.object(ept, "verify_interpreter", verify):
            code, stdout, stderr = self.run_main("verify", "--json", "--opt-root", os.path.join(self.root, "opt"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(seen, {"p": prefix, "expect": "gcc (GCC) 16.2.0", "quiet": True})
        r = json.loads(stdout)
        self.assertEqual((r["good"], r["for"], r["real"]), (True, "general", prefix))
        self.assertEqual(ept.prefix_record(prefix)["verify"]["when"], "now")       # recorded
        code, stdout, stderr = self.run_main("verify", os.path.join(self.root, "nothing"))
        self.assertEqual(code, 1)
        self.assertIn("no bin/python3", stdout)


class Detection(TempRoot):
    def test_current_links_come_first_and_are_shown_by_both_paths(self):
        opt = os.path.join(self.root, "opt")
        fake_gcc(opt, "gcc-16.2.0/bin/gcc")
        fake_gcc(opt, "gcc-16.2.0/bin/g++")
        fake_binutils(opt, "binutils-2.46")
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))
        os.symlink(os.path.join(opt, "binutils-2.46"), os.path.join(opt, "binutils-current"))
        fake_python(opt, "cpython-3.14.8")
        marker(os.path.join(opt, "cpython-3.14.8"))
        dirs = ept.tool_search_dirs(opt, home=self.root, path_env="")
        self.assertIn(os.path.join(opt, "gcc-current", "bin"), dirs)
        self.assertNotIn(os.path.join(opt, "gcc-16.2.0", "bin"), dirs)           # the same real directory
        mine = [d for d in dirs if d.startswith(opt)]
        [gcc] = ept.find_compilers(mine, opt)
        self.assertEqual((gcc["kind"], gcc["version"], gcc["path"], gcc["real"], gcc["origin"]),
                         ("gcc", (16, 2, 0), os.path.join(opt, "gcc-current", "bin", "gcc"),
                          os.path.join(opt, "gcc-16.2.0", "bin", "gcc"), "toolkit, gcc-current"))
        self.assertEqual(gcc["cxx"], os.path.join(opt, "gcc-current", "bin", "g++"))
        [b] = ept.find_binutils(mine, opt)
        self.assertEqual((b["version"], b["prefix"], b["system"]), ((2, 46), os.path.join(opt, "binutils-2.46"), False))
        [py] = ept.find_pythons(mine, opt)
        self.assertEqual((py["impl"], py["version"], py["built_by_tool"], py["for"]), ("CPython", "3.14.8", True,
                                                                                         "general"))
        self.assertEqual(ept.toolkit_pair(opt), (os.path.join(opt, "gcc-16.2.0", "bin", "gcc"),
                                                 os.path.join(opt, "binutils-2.46")))

    def test_the_wizard_offers_the_toolkits_pair_in_general_mode(self):
        opt = os.path.join(self.root, "opt")
        fake_gcc(opt, "gcc-16.2.0/bin/gcc")
        fake_gcc(opt, "gcc-16.2.0/bin/g++")             # its partner beside it: no C++ question
        fake_binutils(opt, "binutils-2.46")
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))
        os.symlink(os.path.join(opt, "binutils-2.46"), os.path.join(opt, "binutils-current"))
        dirs = [os.path.join(opt, "gcc-current", "bin"), os.path.join(opt, "binutils-current", "bin")]
        machine = {"compilers": ept.find_compilers(dirs, opt), "binutils": ept.find_binutils(dirs, opt),
                   "system_cc": "/usr/bin/gcc"}
        args = ept.merge_args(ept.build_parser().parse_args(["wizard", "--opt-root", opt]))
        answers = iter(["general", "", "", "", "", "n", "", "wiz"])
        printed = []
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch.object(ept, "out", lambda text="": printed.append(text)):
            ept.wizard(args, detect=lambda o: machine)
        self.assertEqual((args.purpose, args.version, args.cc, args.binutils, args.no_optimize, args.link_current),
                         ("general", "latest", os.path.join(opt, "gcc-16.2.0", "bin", "gcc"),
                          os.path.join(opt, "binutils-2.46"), True, True))
        self.assertEqual(ept.load_profile("wiz")["binutils"], os.path.join(opt, "binutils-2.46"))

    def test_the_wizard_asks_for_the_cxx_when_none_is_beside_the_compiler(self):
        opt = os.path.join(self.root, "opt")
        fake_gcc(opt, "gcc-16.2.0/bin/gcc")                   # no g++ beside it
        gxx = fake_gcc(self.root, "elsewhere/bin/g++")
        fake_binutils(opt, "binutils-2.46")
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))
        os.symlink(os.path.join(opt, "binutils-2.46"), os.path.join(opt, "binutils-current"))
        dirs = [os.path.join(opt, "gcc-current", "bin"), os.path.join(opt, "binutils-current", "bin")]
        machine = {"compilers": ept.find_compilers(dirs, opt), "binutils": ept.find_binutils(dirs, opt),
                   "system_cc": "/usr/bin/gcc"}
        self.assertEqual(machine["compilers"][0]["cxx"], "")     # detect: only what is beside it
        args = ept.merge_args(ept.build_parser().parse_args(["wizard", "--opt-root", opt]))
        answers = iter(["general", "", "", "", os.path.join(self.root, "nope"), gxx, "", "n", "", ""])
        printed = []
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch.object(ept, "out", lambda text="": printed.append(text)):
            ept.wizard(args, detect=lambda o: machine)
        self.assertEqual((args.cc, args.cxx), (os.path.join(opt, "gcc-16.2.0", "bin", "gcc"), gxx))
        text = "\n".join(printed)
        self.assertIn("NONE there -- general mode refuses that", text)
        self.assertIn("--cxx %s: not an executable" % os.path.join(self.root, "nope"), text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
