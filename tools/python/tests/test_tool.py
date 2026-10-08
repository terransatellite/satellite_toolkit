#!/usr/bin/python3 -I
"""Tests for enterprise_python_tool.py.  stdlib unittest; no network, no root, no
compiler, no CPython tree: every input is a small fake made in a temp dir.

    /usr/bin/python3 -I tests/test_tool.py
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import enterprise_python_tool as ept          # noqa: E402

TOOL_FILE = os.path.join(HERE, "..", "enterprise_python_tool.py")


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
            self.assertEqual(len(row), 6, row)
            self.assertIn(row[4], ("required", "wanted", "optional", "fallback"))
            self.assertTrue(row[1])
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
            "judge_modules": lambda rep, found, fam, mgr: None,
            "compiler_banner": lambda cc: "cc",
            "verify_interpreter": lambda p, offline=False: (verify_good, {"good": verify_good, "when": "t",
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
