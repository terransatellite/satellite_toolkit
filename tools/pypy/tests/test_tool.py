#!/usr/bin/python3 -I
"""Tests for enterprise_pypy_tool.py.  stdlib unittest; no network (fake fetchers, file:// URLs,
a cached index), no root, no real translation: fake interpreters (a /usr/bin/python3 wrapper
that answers like a PyPy), a fake RPython that writes a fake pypy-c, a fake package.py.  They
pass when run as root: the tool refuses root, so the tests that drive `clean` say they are uid
1000.  The end-to-end tests run every stage of a build -- fetch, host, extract, translate, cffi,
package, verify-stage, install, verify, link -- against those fakes."""

import ast
import contextlib
import gzip
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_FILE = os.path.abspath(os.path.join(HERE, "..", "enterprise_pypy_tool.py"))
TOOLKIT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
LEGACY_LOG = os.path.join(TOOLKIT, "legacy", "python_log-2026-08-29.txt")
sys.path.insert(0, os.path.dirname(TOOL_FILE))
import enterprise_pypy_tool as ept          # noqa: E402

PY3 = "/usr/bin/python3"


def put(root, rel, text="", mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


def script(root, rel, body):
    return put(root, rel, "#!/bin/sh\n" + body, 0o755)


# A PyPy as far as the tool can tell: /usr/bin/python3 with sys.pypy_version_info, the
# translation info, PyPy's own modules stubbed, a Python version of our choosing -- and a config
# that makes it lose a module, crash in ssl, lack the JIT or pip.
FAKEPY = r'''
import json, os, signal, sys, types, runpy, warnings
warnings.simplefilter("ignore")
cfg = json.load(open(sys.argv[1]))
exe = sys.argv[2]
args = sys.argv[3:]
while args and args[0] in ("-E", "-s", "-I", "-B"):
    args.pop(0)
for m in ("json", "ssl", "zlib", "bz2", "lzma", "ctypes", "sqlite3", "hashlib", "decimal", "select", "socket",
          "pyexpat", "binascii", "_multiprocessing", "ensurepip", "venv", "curses", "readline", "uuid", "dbm",
          "dbm.gnu", "syslog", "grp", "resource", "_posixshmem", "audioop", "urllib.request"):
    try:
        __import__(m)
    except Exception:
        sys.modules[m] = types.ModuleType(m)
for m in ("__pypy__", "pypyjit", "_cffi_backend", "_blake2", "_sha3", "_pypy_util_cffi_inner", "gdbm", "_gdbm",
          "_tkinter", "_ssl", "dbm.gnu", "lzma", "curses", "readline", "audioop"):
    if m not in sys.modules:
        sys.modules[m] = types.ModuleType(m)
for m in cfg.get("missing", []):
    sys.modules[m] = None
sys.pypy_version_info = tuple(cfg.get("pypy", [8, 0, 0])) + ("final", 0)
sys.pypy_translation_info = {"translation.jit": cfg.get("jit", True)}
sys.version_info = tuple(cfg.get("version", [2, 7, 18])) + ("final", 0)
if args[:1] == ["-c"]:
    code = args[1]
    sys.argv = ["-c"] + args[2:]
    if "create_default_context" in code:
        if cfg.get("ssl") == "crash":
            os.kill(os.getpid(), signal.SIGSEGV)
        if cfg.get("ssl") == "fail":
            print("ssl.SSLError: no certificates")
            sys.exit(1)
        sys.exit(0)
    exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})
    sys.exit(0)
if args[:2] == ["-m", "pip"]:
    if not cfg.get("pip", True):
        print("No module named pip")
        sys.exit(1)
    if "--version" in args:
        print("pip 24.0 from /fake/pip (python %d.%d)" % tuple(cfg.get("version", [2, 7])[:2]))
    sys.exit(0)
if args[:2] == ["-m", "ensurepip"]:
    pip = os.path.join(os.path.dirname(exe), "pip")
    if not os.path.exists(pip) and os.path.basename(os.path.dirname(exe)) == "bin":
        with open(pip, "w") as f:
            f.write("#!%s\nimport sys\n" % exe)
        os.chmod(pip, 0o755)
    sys.exit(0)
if args and args[0].endswith(".py"):
    sys.argv = list(args)
    runpy.run_path(args[0], run_name="__main__")
    sys.exit(0)
sys.exit(0)
'''


def fake_interp(root, name="pypy", **cfg):
    """-> the path of a wrapper that behaves like a PyPy (see FAKEPY)."""
    d = os.path.join(root, "fakeinterp-" + name.replace("/", "_"))
    os.makedirs(d, exist_ok=True)
    put(d, "fakepy.py", FAKEPY)
    put(d, "cfg.json", json.dumps(cfg))
    return script(d, name, 'exec %s -I %s %s "$0" "$@"\n' % (PY3, os.path.join(d, "fakepy.py"), os.path.join(d, "cfg.json")))


# A Python 2.7 with pycparser, as far as the tool can tell; anything else it is asked to run
# (the fake rpython, the fake package.py) runs under /usr/bin/python3.
FAKE_HOST = r'''
case "$*" in
  *"-c import sys, json"*)
    echo '{"major": 2, "version": "2.7.18", "impl": "cpython", "pypy": "", "pycparser": "2.21", "cffi": "1.15.1", "curses": "_curses", "executable": "'"$0"'"}'
    exit 0;;
esac
while [ "$1" = "-E" ] || [ "$1" = "-s" ]; do shift; done
if [ "$1" = "--jit" ]; then shift; shift; fi
exec %s -I "$@"
''' % PY3

FAKE_RPYTHON = r'''
import json, os, shutil, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
env = os.environ
if "--batch" not in args or not any(a.startswith("--opt=") for a in args) or not env.get("CC") \
        or not env.get("PYPY_USESSION_DIR") or env.get("PYPY_USESSION_KEEP") != "1" or "LD_LIBRARY_PATH" in env:
    print("fake rpython: bad arguments or environment", args)
    sys.exit(2)
cfg = json.load(open(os.path.join(here, "fake.json")))
phases = ["Annotating&simplifying", "RTyping", "JIT compiler generation", "lltype back-end optimisations",
          "inserting stack checks", "Creating database for generating c source", "Generating c source",
          "Compiling c source"]
print("[translation:info] usession directory: %s/usession-x-0" % env["PYPY_USESSION_DIR"])
for p in phases[:cfg.get("phases", len(phases))]:
    print("[translation:info] %s..." % p)
    print("....+++***%%%")
    if p == "Generating c source":
        print("[translation:info] written: %s/usession-x-0/testing_1/testing_1.c" % env["PYPY_USESSION_DIR"])
if cfg.get("fail"):
    print(cfg["fail"])
    sys.exit(cfg.get("rc", 1))
target = args[-1]
if target.endswith("targetpypystandalone.py"):
    shutil.copy2(cfg["interp"], cfg["exe_c"])
    with open("lib%s.so" % cfg["exe_c"], "w") as f:
        f.write("not really a shared object")
else:
    name = os.path.basename(target)[:-3] + "-c"
    said = "2500000000" if name == "targettlr-c" else "debug: hello world"
    with open(name, "w") as f:
        f.write("#!/bin/sh\n[ -n \"$1\" ] && [ ! -f \"$1\" ] && exit 9\necho '%s'\n" % said)
    os.chmod(name, 0o755)
print("[Timer] annotate                       --- 56.1 s")
print("[Timer] Total:                         --- 71.8 s")
print("[translation:info] created: %s" % os.path.abspath(cfg.get("exe_c", "x")))
'''

FAKE_PACKAGE = r'''
import os, shutil, sys
args = sys.argv[1:]
name, builddir = args[args.index("--archive-name") + 1], args[args.index("--builddir") + 1]
missing = [k for k in ("PYPY_NO_EMBED_DEPENDENCIES", "PYPY_NO_REBUILD_EXTENSIONS", "PYPY_NO_MAKE_PORTABLE",
                       "PIP_NO_INDEX") if os.environ.get(k) != "1"]
if missing or "CC" not in os.environ:
    print("fake package.py: environment not set: %s" % missing)
    sys.exit(3)
if os.path.exists(os.path.join(os.getcwd(), "FAIL_PACKAGE")):
    print("Traceback (most recent call last):\nOSError: fake package.py failure")
    sys.exit(1)
print("without: %s" % " ".join(a for a in args if a.startswith("--without-")))
goal = os.path.join(os.getcwd(), "pypy", "goal")
exe_c = [n for n in os.listdir(goal) if n.startswith("pypy") and n.endswith("-c")][0]
tree = os.path.join(builddir, name)
bindir = os.path.join(tree, "bin")
os.makedirs(bindir)
if exe_c == "pypy-c":
    exe, links = "pypy", ("pypy2.7", "pypy2", "python", "python2.7", "python2")
else:
    exe, links = exe_c[:-2], ("pypy", "pypy3", "python", "python3")
shutil.copy2(os.path.join(goal, exe_c), os.path.join(bindir, exe))
for l in links:
    os.symlink(exe, os.path.join(bindir, l))
with open(os.path.join(builddir, name + ".tar.gz"), "w") as f:
    f.write("an archive")
print("Ready in %s" % builddir)
'''


def fake_source(root, stem, interp, exe_c, **cfg):
    """A PyPy source tree with the fakes in it, as a tarball -> its path."""
    top = os.path.join(root, "srcbuild", stem + "-src")
    put(top, "rpython/bin/rpython", FAKE_RPYTHON, 0o755)
    put(top, "rpython/bin/fake.json", json.dumps(dict(cfg, interp=interp, exe_c=exe_c)))
    put(top, "rpython/translator/goal/targetnopstandalone.py", "# the smoke target\n")
    put(top, "rpython/jit/tl/targettlr.py", "# the JIT smoke target\n")
    for d in ("rpython", "rpython/jit", "rpython/jit/tl"):
        put(top, d + "/__init__.py", "")
    put(top, "rpython/jit/tl/tlr.py", "SQUARE = b'square bytecode'\n")
    put(top, "pypy/goal/targetpypystandalone.py", "# the target\n")
    put(top, "pypy/tool/release/package.py", FAKE_PACKAGE)
    put(top, "lib_pypy/pypy_tools/build_cffi_imports.py", "print('fake build_cffi_imports')\n")
    put(top, "lib-python/2.7/os.py", "")
    put(top, "LICENSE", "MIT\n")
    tb = os.path.join(root, "%s-src.tar.gz" % stem)
    subprocess.check_call(["tar", "-czf", tb, "-C", os.path.join(root, "srcbuild"), stem + "-src"])
    shutil.rmtree(os.path.join(root, "srcbuild"))
    return tb


FAKE_CC = {"path": "/usr/bin/gcc", "kind": "gcc", "version": (14, 2, 0), "banner": "gcc (GCC) 14.2.0",
           "why": "test"}


class Quiet(unittest.TestCase):
    """Every test: a scratch HOME, the tool's output swallowed, its UI and signals put back."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eppt-test-")
        self.ui = (ept.UI.stream, ept.UI.tty, ept.UI.color)
        self.sigs = dict((s, signal.getsignal(s)) for s in (signal.SIGTERM, signal.SIGHUP))
        self.env = mock.patch.dict(os.environ, {"HOME": os.path.join(self.tmp, "home"),
                                                "XDG_CONFIG_HOME": os.path.join(self.tmp, "home", ".config"),
                                                "SATELLITE_TOOLKIT_WHERE": "local"})
        self.env.start()
        os.makedirs(os.path.join(self.tmp, "home"))
        self.out = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.out)
        self.redirect.__enter__()
        ept.UI.stream, ept.UI.tty, ept.UI.color = None, False, False

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.env.stop()
        ept.UI.stream, ept.UI.tty, ept.UI.color = self.ui
        for s, h in self.sigs.items():
            signal.signal(s, h)
        ept.unlock_all()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ the file itself

class TheFile(unittest.TestCase):
    def setUp(self):
        with open(TOOL_FILE) as f:
            self.text = f.read()

    def test_parses_as_python_36(self):
        ast.parse(self.text, feature_version=(3, 6))

    def test_pure_ascii(self):
        bad = [n for n, line in enumerate(self.text.split("\n"), 1) if any(ord(c) > 127 for c in line)]
        self.assertEqual(bad, [])

    def test_isolated_platform_python_shebang_and_pypy_reexec(self):
        self.assertEqual(self.text.split("\n")[0], "#!/usr/bin/python3 -I")
        self.assertIn('os.execv(PLATFORM_PYTHON, [PLATFORM_PYTHON, "-I"', self.text)
        self.assertEqual(ept.PLATFORM_PYTHON, "/usr/bin/python3")
        self.assertEqual(ept.REEXEC_GUARD, "EPPT_NO_REEXEC")

    def test_scripts_run_by_the_interpreters_parse(self):
        """VERIFY_SCRIPT, HOST_PROBE, the import table, the 2.7 check: written for Python 2 AND 3."""
        for code in (ept.VERIFY_SCRIPT, ept.HOST_PROBE, ept.IMPORT_TABLE, ept.CPY27_CHECK, ept.JIT_SCRIPT):
            ast.parse(code)
            self.assertNotIn("f\"", code)
            self.assertNotIn("f'", code)

    def test_stdlib_only(self):
        tree = ast.parse(self.text)
        mods = set()
        for n in tree.body:                         # module level: what every run needs
            if isinstance(n, ast.Import):
                mods |= set(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        for m in mods:
            if m in ("pty",):
                continue
            __import__(m)                 # importable with the platform python alone


class RemoteBlock(unittest.TestCase):
    def setUp(self):
        with open(TOOL_FILE) as f:
            self.text = f.read()
        self.lines = self.text.split("\n")

    def test_one_marker_pair_at_top_level_after_the_constants(self):
        begins = [i for i, l in enumerate(self.lines) if l.startswith("# >>> satellite_remote >>>")]
        ends = [i for i, l in enumerate(self.lines) if l == "# <<< satellite_remote <<<"]
        self.assertEqual((len(begins), len(ends)), (1, 1))
        self.assertLess(begins[0], ends[0])
        self.assertLess(next(i for i, l in enumerate(self.lines) if l.startswith("TOOL = ")), begins[0])
        self.assertLess(next(i for i, l in enumerate(self.lines) if l.startswith("SETUPTOOLS_WHEEL = ")), begins[0])
        self.assertEqual(ept.TOOL, "enterprise_pypy_tool")

    def test_session_short_name_is_pypy(self):
        if not hasattr(ept, "_sr_session_name"):
            self.skipTest("the block is the stand-in: sync_remote.py has not filled it")
        self.assertTrue(ept._sr_session_name(ept.TOOL).startswith("pypy-"))

    def test_block_is_what_sync_remote_writes(self):
        sys.path.insert(0, TOOLKIT)
        try:
            import sync_remote
        except ImportError:
            self.skipTest("no sync_remote.py")
        finally:
            sys.path.remove(TOOLKIT)
        if not os.path.exists(sync_remote.SOURCE):
            self.skipTest("no satellite_remote.py beside sync_remote.py")
        self.assertEqual(sync_remote.synced(self.text, sync_remote.block()), self.text,
                         "the block differs from satellite_remote.py: run ./sync_remote.py")
        self.assertIn("tools/pypy/enterprise_pypy_tool.py", sync_remote.TOOLS)

    def test_version_still_asks_where_only_help_and_tool_version_do_not(self):
        """The Child contract: --version is a real option here (= --pypy-version) and must still ask."""
        self.assertEqual(ept.build_parser().parse_args(["build", "--version", "8.0.0"]).pypy_version, "8.0.0")
        with mock.patch.dict(os.environ), mock.patch.object(ept, "_sr_tty", return_value=True):
            os.environ.pop("SATELLITE_TOOLKIT_WHERE", None)
            self.assertTrue(ept._sr_wants_question(["status", "--version", "8.0.0"]))
            self.assertTrue(ept._sr_wants_question(["build", "--pypy-version", "8.0.0"]))
            for quiet in ("-h", "--help", "--tool-version"):
                self.assertFalse(ept._sr_wants_question(["build", quiet]))

    def test_where_to_run_is_the_first_statement_of_main(self):
        tree = ast.parse(self.text)
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        self.assertIsInstance(first, ast.Assign)
        self.assertEqual(first.targets[0].id, "argv")
        self.assertEqual(first.value.func.id, "where_to_run")
        args = [ast.dump(a) for a in first.value.args]
        self.assertIn("sys", args[0])
        self.assertIn("__file__", args[1])
        self.assertIn("TOOL", args[2])


# ------------------------------------------------------------- the release list

INDEX = """<html><body>
<a href="pypy2.7-v7.3.23-src.tar.gz">pypy2.7-v7.3.23-src.tar.gz</a>
<a href="pypy2.7-v7.3.22-src.tar.bz2">pypy2.7-v7.3.22-src.tar.bz2</a>
<a href="pypy2.7-v7.3.22-linux64.tar.bz2">pypy2.7-v7.3.22-linux64.tar.bz2</a>
<a href="pypy2.7-v7.3.23-linux64.tar.gz">pypy2.7-v7.3.23-linux64.tar.gz</a>
<a href="pypy2.7-v7.3.23-aarch64.tar.gz">x</a>
<a href="pypy3.11-v7.3.23-src.tar.gz">pypy3.11-v7.3.23-src.tar.gz</a>
<a href="pypy2.7-v8.0.0-src.tar.gz">pypy2.7-v8.0.0-src.tar.gz</a>
<a href="pypy2.7-v8.0.0-src.tar.bz2">pypy2.7-v8.0.0-src.tar.bz2</a>
<a href="pypy2.7-v8.0.0-linux64.tar.gz">pypy2.7-v8.0.0-linux64.tar.gz</a>
<a href="pypy2.7-v8.0.0-linux32.tar.gz">pypy2.7-v8.0.0-linux32.tar.gz</a>
<a href="pypy3.11-v8.0.0-src.tar.gz">pypy3.11-v8.0.0-src.tar.gz</a>
<a href="pypy3.12-v8.0.0-src.tar.gz">pypy3.12-v8.0.0-src.tar.gz</a>
<a href="pypy3.12-v8.0.0-linux64.tar.gz">pypy3.12-v8.0.0-linux64.tar.gz</a>
<a href="pypy2.7-v8.0.1rc1-src.tar.gz">pypy2.7-v8.0.1rc1-src.tar.gz</a>
<a href="pypy3.12-v8.0.1rc1-src.tar.gz">pypy3.12-v8.0.1rc1-src.tar.gz</a>
<a href="pypy3.12-v8.0.0-src.tar.gz.asc">signature</a>
<a href="pypy2.7-v8.0.0-win64.zip">pypy2.7-v8.0.0-win64.zip</a>
</body></html>"""

VERSIONS_JSON = json.dumps([
    {"pypy_version": "8.0.0", "python_version": "3.12.14", "stable": True, "latest_pypy": True, "date": "2026-09-19",
     "files": [{"filename": "pypy3.12-v8.0.0-linux64.tar.gz", "arch": "x64", "platform": "linux"}]},
    {"pypy_version": "8.0.0", "python_version": "3.11.16", "stable": True, "latest_pypy": True, "date": "2026-09-19",
     "files": []},
    {"pypy_version": "8.0.0", "python_version": "2.7.18", "stable": True, "latest_pypy": True, "date": "2026-09-19",
     "files": [{"filename": "pypy2.7-v8.0.0-linux64.tar.gz", "arch": "x64", "platform": "linux"},
               {"filename": "pypy2.7-v8.0.0-aarch64.tar.gz", "arch": "aarch64", "platform": "linux"}]},
    {"pypy_version": "7.3.23", "python_version": "2.7.18", "stable": True, "latest_pypy": False, "date": "2026-05-26",
     "files": [{"filename": "pypy2.7-v7.3.23-linux64.tar.gz", "arch": "x64", "platform": "linux"}]},
    {"pypy_version": "8.0.1rc1", "python_version": "3.12.14", "stable": False, "latest_pypy": False, "date": "",
     "files": []},
    {"pypy_version": "nightly", "python_version": "3.12", "stable": False, "latest_pypy": False, "files": []},
])

NOTES_800 = ("======================================================================\n"
             "PyPy v8.0.0: release of python 2.7, 3.11,3.12 beta released 2026-09-XX\n"
             "======================================================================\n\nIt is our first release of "
             "Python 3.12, which may still have some bugs so we are calling it \"beta\" quality.\n")
NOTES_802 = ("===============================================================\n"
             "PyPy v8.0.2: release of python 2.7 and 3.12 released 2026-XX-XX\n"
             "===============================================================\n\nIn 8.0.0, 3.11 beta was ...\n")

TAGS = "\n".join("deadbeef\trefs/tags/release-%s" % t for t in (
    "pypy2.7-v7.3.23", "pypy3.11-v7.3.23", "pypy2.7-v8.0.0", "pypy3.11-v8.0.0", "pypy3.12-v8.0.0",
    "pypy3.12-v8.0.0rc1", "pypy3.9-v7.3.16"))


def catalog(index=INDEX, notes=None, versions=VERSIONS_JSON):
    flags, _ = ept.parse_versions_json(versions)
    return ept.Catalog(ept.parse_index(index), flags, "test index", notes)


class Index(Quiet):
    def test_parse_index_sources_binaries_and_both_compressions(self):
        rels = ept.parse_index(INDEX)
        self.assertEqual(rels["7.3.22"]["src"]["2.7"], "pypy2.7-v7.3.22-src.tar.bz2")
        self.assertEqual(rels["8.0.0"]["src"]["2.7"], "pypy2.7-v8.0.0-src.tar.gz")     # .tar.gz preferred
        self.assertEqual(sorted(rels["8.0.0"]["src"]), ["2.7", "3.11", "3.12"])
        self.assertEqual(rels["8.0.0"]["bin"]["2.7"]["linux32"], "pypy2.7-v8.0.0-linux32.tar.gz")
        self.assertNotIn("win64", json.dumps(rels))
        self.assertNotIn(".asc", json.dumps(rels))

    def test_gzip_encoded_listing_is_decoded(self):
        body = gzip.compress(INDEX.encode())
        self.assertEqual(ept.parse_index(ept.decode_body(body).decode()), ept.parse_index(INDEX))
        self.assertEqual(ept.decode_body(body, "gzip"), INDEX.encode())
        self.assertEqual(ept.decode_body(b"plain"), b"plain")

    def test_versions_json_flags_and_binaries_never_sources(self):
        flags, rels = ept.parse_versions_json(VERSIONS_JSON)
        self.assertTrue(flags[("8.0.0", "3.12")]["stable"])
        self.assertFalse(flags[("8.0.1rc1", "3.12")]["stable"])
        self.assertNotIn(("nightly", "3.12"), flags)
        self.assertEqual(rels["8.0.0"]["bin"]["2.7"]["aarch64"], "pypy2.7-v8.0.0-aarch64.tar.gz")
        self.assertEqual(rels["8.0.0"]["src"], {})
        with self.assertRaises(ept.ToolError):
            ept.parse_versions_json("<html>blocked</html>")

    def test_git_tags_checksums_and_release_order(self):
        tags = ept.parse_git_tags(TAGS)
        self.assertEqual(tags["8.0.0"], set(["2.7", "3.11", "3.12"]))
        self.assertIn("8.0.0rc1", tags)
        sums = ept.parse_checksums("<tt>%s  pypy2.7-v8.0.0-src.tar.gz</tt>\n%s pypy3.12-v8.0.0-linux64.tar.gz" % ("a" * 64, "b" * 64))
        self.assertEqual(sums["pypy2.7-v8.0.0-src.tar.gz"], "a" * 64)
        self.assertEqual(sums["pypy3.12-v8.0.0-linux64.tar.gz"], "b" * 64)
        order = sorted(["7.3.23", "8.0.0rc2", "8.0.0", "8.0.0rc1", "nightly", "7.3.9"], key=ept.release_key)
        self.assertEqual(order, ["nightly", "7.3.9", "7.3.23", "8.0.0rc1", "8.0.0rc2", "8.0.0"])

    def test_notes_title_says_which_series_is_beta(self):
        self.assertEqual(ept.parse_notes_beta(NOTES_800)[0], set(["3.12"]))
        self.assertEqual(ept.parse_notes_beta(NOTES_802)[0], set())
        self.assertIn("v8.0.2", ept.parse_notes_beta(NOTES_802)[1])
        self.assertEqual(ept.parse_notes_beta("no title here"), (set(), ""))

    def test_pick_release_auto_latest_and_named(self):
        cat = catalog()
        self.assertEqual(ept.pick_release(cat, "2.7", "auto")[0], "8.0.0")
        self.assertEqual(ept.pick_release(cat, "2.7", "latest")[0], "8.0.1rc1")
        self.assertEqual(ept.pick_release(cat, "2.7", "7.3.22")[0], "7.3.22")
        with self.assertRaises(ept.ToolError):
            ept.pick_release(cat, "2.7", "7.3.1")
        with self.assertRaises(ept.UsageError):
            ept.pick_release(cat, "2.7", "eight")

    def test_pypy3_auto_skips_the_series_the_notes_call_beta(self):
        cat = catalog(notes=lambda rel: NOTES_800)
        rel, series, why = ept.pick_pypy3(cat, "auto", "auto")
        self.assertEqual((rel, series), ("8.0.0", "3.11"))
        self.assertTrue(any("3.12" in w and "beta" in w for w in why))
        self.assertTrue(any("--series 3.12" in w for w in why))

    def test_pypy3_auto_takes_312_once_nobody_calls_it_beta(self):
        index = INDEX + '\n<a href="pypy2.7-v8.0.2-src.tar.gz">x</a><a href="pypy3.12-v8.0.2-src.tar.gz">x</a>'
        cat = catalog(index, notes=lambda rel: NOTES_802 if rel == "8.0.2" else NOTES_800)
        self.assertEqual(ept.pick_pypy3(cat, "auto", "auto")[:2], ("8.0.2", "3.12"))

    def test_pypy3_latest_and_named_series(self):
        cat = catalog(notes=lambda rel: NOTES_800)
        self.assertEqual(ept.pick_pypy3(cat, "auto", "latest")[:2], ("8.0.0", "3.12"))
        rel, series, why = ept.pick_pypy3(cat, "auto", "3.12")
        self.assertEqual((rel, series), ("8.0.0", "3.12"))
        self.assertIn("NOTE", why[0])                           # asked for, and told it is beta
        self.assertEqual(ept.pick_pypy3(cat, "7.3.23", "auto")[:2], ("7.3.23", "3.11"))
        with self.assertRaises(ept.UsageError):
            ept.pick_pypy3(cat, "auto", "4.0")

    def test_beta_falls_back_to_the_table_then_versions_json(self):
        def unreachable(rel):
            raise ept.ToolError("no network")
        cat = catalog(notes=unreachable)
        is_beta, why = cat.beta("8.0.0", "3.12")
        self.assertTrue(is_beta)
        self.assertIn("when this tool was written", why)
        self.assertTrue(cat.beta("8.0.1rc1", "3.12")[0])        # stable: false in versions.json
        self.assertFalse(cat.beta("8.0.0", "3.11")[0])

    def test_every_series_beta_takes_the_newest_and_says_so(self):
        cat = catalog(index='<a href="pypy3.12-v9.0.0-src.tar.gz">x</a>', notes=lambda rel: "release of python 3.12 beta")
        rel, series, why = ept.pick_pypy3(cat, "auto", "auto")
        self.assertEqual((rel, series), ("9.0.0", "3.12"))
        self.assertIn("every pypy3 series is called beta", why[-1])

    def test_load_catalog_falls_back_to_versions_json_and_git_tags(self):
        def get(url):
            if url == ept.PYPY_BASE or url.startswith(ept.PYPY_BASE):
                raise ept.ToolError("cannot fetch %s: 403" % url)
            if url == ept.PYPY_VERSIONS_URLS[1]:
                return VERSIONS_JSON.encode()
            raise ept.ToolError("no")
        wd = os.path.join(self.tmp, "w")
        cat = ept.load_catalog({"offline": False}, wd, get=get, ls_remote=lambda: TAGS)
        self.assertIn("git tags", cat.origin)
        self.assertTrue(cat.has_src("8.0.0", "3.12"))
        self.assertEqual(cat.src_name("8.0.0", "2.7"), "")
        cands, git = ept.source_candidates(cat, "8.0.0", "2.7")
        self.assertEqual([c[1] for c in cands], ["pypy2.7-v8.0.0-src.tar.gz", "pypy2.7-v8.0.0-src.tar.bz2",
                                                 "pypy2.7-v8.0.0-src.github.tar.gz"])
        self.assertEqual(git, (ept.PYPY_GIT, "release-pypy2.7-v8.0.0"))
        self.assertEqual(cat.bin_name("8.0.0", "2.7", "linux64"), "pypy2.7-v8.0.0-linux64.tar.gz")

    def test_load_catalog_caches_and_offline_reads_the_cache(self):
        wd = os.path.join(self.tmp, "w")
        got = []

        def get(url):
            got.append(url)
            if url == ept.PYPY_BASE:
                return gzip.compress(INDEX.encode())
            if url in ept.PYPY_VERSIONS_URLS:
                return VERSIONS_JSON.encode()
            raise ept.ToolError("no")
        cat = ept.load_catalog({}, wd, get=lambda u: ept.decode_body(get(u)))
        self.assertEqual(cat.origin, "the downloads.python.org index")
        self.assertTrue(os.path.isfile(os.path.join(wd, "cache", "index.html")))
        cat2 = ept.load_catalog({"offline": True}, wd, get=lambda u: self.fail("network used offline"))
        self.assertIn("cached", cat2.origin)
        self.assertEqual(ept.pick_release(cat2, "2.7", "auto")[0], "8.0.0")
        with self.assertRaises(ept.ToolError):
            ept.load_catalog({"offline": True}, os.path.join(self.tmp, "empty"), get=get)

    def test_a_named_release_goes_ahead_without_a_list(self):
        def nothing(*a):
            raise ept.ToolError("offline")
        opts = {"offline": True, "pypy_version": "8.0.0", "target": "pypy2", "series": "auto"}
        cat = ept.get_catalog(opts, os.path.join(self.tmp, "w"))
        self.assertEqual(ept.pick_release(cat, "2.7", "8.0.0")[0], "8.0.0")
        with self.assertRaises(ept.ToolError):
            ept.get_catalog(dict(opts, pypy_version="auto"), os.path.join(self.tmp, "w"))


class Names(Quiet):
    def test_binary_tag_per_architecture(self):
        self.assertEqual(ept.arch_tag("x86_64"), "linux64")
        self.assertEqual(ept.arch_tag("aarch64"), "aarch64")
        self.assertEqual(ept.arch_tag("i686"), "linux32")
        self.assertEqual(ept.arch_tag("s390x"), "s390x")
        self.assertEqual(ept.arch_tag("riscv64"), "")

    def test_prebuilt_host_by_arch_and_glibc(self):
        cat = catalog()
        self.assertEqual(ept.prebuilt_host_choice(cat, "linux64", (2, 39)), ("8.0.0", "pypy2.7-v8.0.0-linux64.tar.gz"))
        self.assertEqual(ept.prebuilt_host_choice(cat, "linux64", (2, 17)), ("7.3.23", "pypy2.7-v7.3.23-linux64.tar.gz"))
        self.assertEqual(ept.prebuilt_host_choice(cat, "aarch64", (2, 28))[1], "pypy2.7-v7.3.23-aarch64.tar.gz")
        with self.assertRaises(ept.ToolError):
            ept.prebuilt_host_choice(cat, "", (2, 39))
        with self.assertRaises(ept.ToolError):
            ept.prebuilt_host_choice(cat, "s390x", (2, 39))

    def test_source_names_from_the_index(self):
        cands, git = ept.source_candidates(catalog(), "7.3.22", "2.7")
        self.assertEqual(cands[0][:2], (ept.PYPY_BASE + "pypy2.7-v7.3.22-src.tar.bz2", "pypy2.7-v7.3.22-src.tar.bz2"))
        self.assertIn("release-pypy2.7-v7.3.22", cands[-1][0])

    def test_ctx_names(self):
        c2 = ept.Ctx(dict(ept.DEFAULTS), "pypy2", "/w", "/o")
        c2.release, c2.series = "8.0.0", "2.7"
        self.assertEqual((c2.stem, c2.exe_c, c2.lib_c, c2.final_exe, c2.current_name, c2.prefix),
                         ("pypy2.7-v8.0.0", "pypy-c", "libpypy-c.so", "pypy", "pypy2-current", "/o/pypy2.7-v8.0.0"))
        self.assertFalse(c2.needs_setuptools)
        c3 = ept.Ctx(dict(ept.DEFAULTS, prefix="/elsewhere"), "pypy3", "/w", "/o")
        c3.release, c3.series = "8.0.0", "3.12"
        self.assertEqual((c3.exe_c, c3.lib_c, c3.final_exe, c3.current_name, c3.prefix),
                         ("pypy3.12-c", "libpypy3.12-c.so", "pypy3.12", "pypy3-current", "/elsewhere"))
        self.assertTrue(c3.needs_setuptools)
        c3.series = "3.11"
        self.assertFalse(c3.needs_setuptools)
        self.assertEqual(c3.src_dir, "/w/src/pypy3.11-v8.0.0-src")
        self.assertEqual(c3.usession, "/w/usession/pypy3.11-v8.0.0")


# ------------------------------------------------------------------- the host

PY2 = {"major": 2, "version": "2.7.18", "impl": "cpython", "pypy": "", "pycparser": "2.21", "cffi": "1.15.1",
       "curses": "_curses"}


class Host(Quiet):
    def test_a_cpython3_is_refused_with_the_reason(self):
        msg = ept.host_problem({"major": 3, "version": "3.12.3", "impl": "cpython", "pycparser": ""}, "/usr/bin/python3")
        self.assertIn("cannot run it", msg)
        self.assertIn("cpython27", msg)
        self.assertIn("HOST_MODE=cpython", msg)
        self.assertIn("PyPy3", ept.host_problem({"major": 3, "version": "3.11", "impl": "pypy"}, "/x/pypy3"))
        self.assertIn("pycparser", ept.host_problem(dict(PY2, pycparser=""), "/x/python2.7"))
        self.assertEqual(ept.host_problem(PY2, "/x/python2.7"), "")
        self.assertIn("does not run", ept.host_problem(None, "/x"))

    def test_the_real_python3_is_probed_and_refused(self):
        pr = ept.probe_interpreter(PY3)
        self.assertEqual(pr["major"], 3)
        self.assertEqual(pr["impl"], "cpython")
        with self.assertRaises(ept.ToolError) as cm:
            ept.plan_host({"host": PY3}, "pypy2", self.tmp, self.tmp)
        self.assertIn("a Python 3 cannot run it", str(cm.exception))
        self.assertIsNone(ept.probe_interpreter(os.path.join(self.tmp, "nothing")))

    def test_auto_is_cpython27_for_pypy2(self):
        h = ept.plan_host({"host": "auto"}, "pypy2", "/o", "/w")
        self.assertEqual(h["mode"], "cpython27")
        self.assertEqual(h["path"], "/w/host/cpython-2.7.18/prefix/bin/python2.7")
        self.assertIn("choice J", h["why"])
        self.assertEqual(ept.plan_host({"host": "cpython"}, "pypy2", "/o", "/w")["mode"], "cpython27")

    def test_auto_for_pypy3_is_the_real_path_of_pypy2_current(self):
        opt = os.path.join(self.tmp, "opt")
        exe = put(opt, "pypy2.7-v8.0.0/bin/pypy", "#!/bin/sh\n", 0o755)
        os.symlink("pypy2.7-v8.0.0", os.path.join(opt, "pypy2-current"))
        seen = []

        def probe(p):
            seen.append(p)
            return dict(PY2, impl="pypy", pypy="8.0.0", path=p)
        h = ept.plan_host({"host": "auto"}, "pypy3", opt, "/w", probe=probe)
        self.assertEqual(h["mode"], "built")
        self.assertEqual(h["path"], os.path.realpath(exe))
        self.assertEqual(seen, [os.path.realpath(exe)])
        h = ept.plan_host({"host": "auto"}, "pypy3", opt, "/w", probe=lambda p: None)
        self.assertEqual(h["mode"], "cpython27")
        self.assertIn("does not run", h["why"])

    def test_auto_for_pypy3_without_pypy2_current_is_the_pypy2_choice(self):
        h = ept.plan_host({"host": "auto"}, "pypy3", os.path.join(self.tmp, "none"), "/w")
        self.assertEqual(h["mode"], "cpython27")
        self.assertIn("step 6 makes it", h["why"])

    def test_built_and_system_say_what_to_do(self):
        with self.assertRaises(ept.ToolError) as cm:
            ept.plan_host({"host": "built"}, "pypy3", os.path.join(self.tmp, "none"), "/w")
        self.assertIn("build --target pypy2", str(cm.exception))
        with self.assertRaises(ept.ToolError) as cm:
            ept.plan_host({"host": "system"}, "pypy2", "/o", "/w", system_hosts=[])
        self.assertIn("ship no Python 2", str(cm.exception))
        h = ept.plan_host({"host": "system"}, "pypy2", "/o", "/w", probe=lambda p: dict(PY2, path=p),
                          system_hosts=["/usr/bin/python2.7"])
        self.assertEqual(h["mode"], "system")
        h = ept.plan_host({"host": "prebuilt"}, "pypy2", "/o", "/w")
        self.assertEqual((h["mode"], h["path"]), ("prebuilt", ""))
        with self.assertRaises(ept.UsageError):
            ept.plan_host({"host": "/no/such/python"}, "pypy2", "/o", "/w")

    def test_a_fake_python2_path_is_accepted(self):
        host = script(self.tmp, "py2/python2.7", ept.re.sub(r"exec .*", "exit 1", FAKE_HOST))
        h = ept.plan_host({"host": host}, "pypy2", "/o", "/w")
        self.assertEqual((h["mode"], h["major"], h["pycparser"]), ("path", 2, "2.21"))

    def test_host_argv_and_low_memory(self):
        cpy = {"path": "/h/python2.7", "impl": "cpython"}
        pypy = {"path": "/h/pypy", "impl": "pypy"}
        self.assertEqual(ept.host_argv(cpy, True), ["/h/python2.7", "-E", "-s"])
        self.assertEqual(ept.host_argv(pypy, True), ["/h/pypy", "-E", "-s", "--jit", "loop_longevity=300"])
        self.assertEqual(ept.host_env_extra(pypy, True), {"PYPY_GC_MAX_DELTA": "200MB"})
        self.assertEqual(ept.host_env_extra(cpy, True), {})

    def test_system_hosts_include_the_sh_probe_path(self):
        opt = os.path.join(self.tmp, "opt")
        p = put(opt, "pypy2/pypy2/bin/pypy", "#!/bin/sh\n", 0o755)
        self.assertIn(p, ept.find_system_hosts(opt, dirs=[]))


class CPython27(Quiet):
    def ci(self, kind, v):
        return {"path": "/opt/cc/bin/" + kind, "kind": kind, "version": v, "banner": kind}

    def test_cflags_per_gcc_major(self):
        self.assertEqual(ept.cpy27_cflags(self.ci("gcc", (13, 3, 0))), ["-O2", "-std=gnu17"])
        for major in (14, 15, 16):
            self.assertEqual(ept.cpy27_cflags(self.ci("gcc", (major, 1, 0))), ["-O2", "-std=gnu17", "-fpermissive"])
        self.assertEqual(ept.cpy27_cflags(self.ci("gcc", (7, 5, 0))), ["-O2", "-std=gnu11"])
        clang = ept.cpy27_cflags(self.ci("clang", (18, 1, 3)))
        self.assertIn("-std=gnu17", clang)
        self.assertNotIn("-fpermissive", clang)
        self.assertIn("-Wno-error=implicit-function-declaration", clang)

    def test_configure_line(self):
        args = ept.cpy27_configure_args("/s", "/w/host/prefix", self.ci("gcc", (16, 1, 0)))
        self.assertEqual(args[0], "/s/configure")
        for want in ("--prefix=/w/host/prefix", "--enable-unicode=ucs4", "--with-system-ffi", "--with-ensurepip=install",
                     "CC=/opt/cc/bin/gcc", "CFLAGS=-O2 -std=gnu17 -fpermissive"):
            self.assertIn(want, args)
        for never in ("--enable-optimizations", "--with-lto", "--enable-shared"):
            self.assertFalse(any(a.startswith(never) for a in args))

    def test_module_report(self):
        text = ("running build\n...\nPython build finished successfully!\n"
                "The necessary bits to build these modules were not found:\n"
                "_bsddb             _hashlib           bsddb185\ndl                 nis\n"
                "To find the necessary bits, look in setup.py in detect_modules() for the module's name.\n\n"
                "Failed to build these modules:\n_ctypes\n\n")
        rep = ept.cpy27_module_report(text)
        self.assertEqual(rep["missing"], ["_bsddb", "_hashlib", "bsddb185", "dl", "nis"])
        self.assertEqual(rep["failed"], ["_ctypes"])
        self.assertEqual(ept.cpy27_module_report("nothing"), {"missing": [], "failed": []})

    def test_pinned_files(self):
        self.assertEqual(ept.CPY27_SOURCES[0][2], "b62c0e7937551d0cc02b8fd5cb0f544f9405bafc9a54d3808ed4594812edef43")
        self.assertEqual(ept.HOST_PINS, ("pycparser==2.21", "cffi==1.15.1"))
        for name, url, sha in ept.HOST_FILES.values():
            self.assertTrue(url.endswith("/" + name))
            self.assertEqual(len(sha), 64)

    def test_the_host_must_have_curses_and_one_built_without_it_is_built_again(self):
        self.assertIn("_curses", ept.CPY27_NEEDED)
        self.assertIn("_minimal_curses", ept.HOST_PROBE)
        msg = ept.host_problem(dict(PY2, curses=""), "/usr/local/bin/python2.7")
        self.assertIn("_minimal_curses", msg)
        self.assertIn("--host cpython27", msg)
        self.assertEqual(ept.host_problem(dict(PY2, impl="pypy", curses="_minimal_curses"), "/x/pypy"), "")
        wd = os.path.join(self.tmp, "w")
        state = ept.State(os.path.join(wd, "state.json"))
        ci = self.ci("gcc", (14, 2, 0))
        state.d["hosts"]["cpython27"] = {"fingerprint": ept.cpy27_fingerprint(ci, ""), "built": "then"}
        with mock.patch.object(ept, "cpy27_check", return_value=("2.7.18", ["_curses"])), \
                mock.patch.object(ept, "fetch_any", side_effect=ept.ToolError("rebuilding")):
            with self.assertRaises(ept.ToolError) as cm:
                ept.build_cpython27(wd, ci, "", 2, state, offline=True)
        self.assertIn("rebuilding", str(cm.exception))
        self.assertIn("has no _curses", self.out.getvalue())

    def test_a_built_host_is_reused_while_the_compiler_is_the_same(self):
        wd = os.path.join(self.tmp, "w")
        state = ept.State(os.path.join(wd, "state.json"))
        ci = self.ci("gcc", (14, 2, 0))
        state.d["hosts"]["cpython27"] = {"fingerprint": ept.cpy27_fingerprint(ci, ""), "built": "then"}
        with mock.patch.object(ept, "cpy27_check", return_value=("2.7.18", [])), \
                mock.patch.object(ept, "fetch_any", side_effect=AssertionError("would rebuild")):
            self.assertEqual(ept.build_cpython27(wd, ci, "", 2, state), ept.cpy27_python(wd))


# ------------------------------------------------------------ environment, argv

class TranslateSetup(Quiet):
    def ctx(self, **opts):
        c = ept.Ctx(dict(ept.DEFAULTS, **opts), "pypy2", "/w", "/o")
        c.release, c.series, c.cc, c.jobs = "8.0.0", "2.7", FAKE_CC, 6
        c.binutils = "/o/binutils-2.46"
        c.host = {"path": "/w/host/cpython-2.7.18/prefix/bin/python2.7", "impl": "cpython", "mode": "cpython27"}
        return c

    def test_the_shell_never_reaches_the_build(self):
        with mock.patch.dict(os.environ, {"CC": "/home/m/opt/clang-current/bin/clang", "CFLAGS": "-flto=thin",
                                          "LDFLAGS": "-fuse-ld=lld", "LD_LIBRARY_PATH": "/home/m/opt/lib",
                                          "PYTHONPATH": "/x", "PYPY_USESSION_DIR": "/tmp", "https_proxy": "http://p:3128"}):
            env = ept.scrubbed_env()
            self.assertEqual(env["PATH"].split(":")[0], "/usr/bin")
            for k in ("CC", "CFLAGS", "LDFLAGS", "LD_LIBRARY_PATH", "PYTHONPATH", "PYPY_USESSION_DIR"):
                self.assertNotIn(k, env)
            self.assertEqual(env["https_proxy"], "http://p:3128")
            traps = " ".join(ept.env_traps())
            self.assertIn("CC=/home/m/opt/clang-current/bin/clang", traps)
            tenv = ept.translate_env(self.ctx())
        self.assertEqual(tenv["CC"], "/usr/bin/gcc")
        self.assertEqual((tenv["CFLAGS"], tenv["LDFLAGS"]), ("", ""))
        self.assertNotIn("LD_LIBRARY_PATH", tenv)
        self.assertNotIn("PYTHONPATH", tenv)

    def test_usession_tmp_and_binutils_first(self):
        env = ept.translate_env(self.ctx(extra_cflags="-std=gnu17"))
        self.assertEqual(env["PYPY_USESSION_DIR"], "/w/usession/pypy2.7-v8.0.0")
        self.assertEqual(env["PYPY_USESSION_BASENAME"], "pypy2.7-v8.0.0")
        self.assertEqual(env["PYPY_USESSION_KEEP"], "1")
        self.assertEqual(env["TMPDIR"], "/w/tmp")
        self.assertEqual(env["CFLAGS"], "-std=gnu17")
        self.assertEqual(env["PATH"].split(":")[:2], ["/o/binutils-2.46/bin", "/usr/bin"])
        self.assertEqual(env["LC_ALL"], "C")

    def test_rpython_argv(self):
        argv = ept.rpython_argv(self.ctx())
        self.assertEqual(argv[:3], ["/w/host/cpython-2.7.18/prefix/bin/python2.7", "-E", "-s"])
        self.assertEqual(argv[3], "/w/src/pypy2.7-v8.0.0-src/rpython/bin/rpython")
        for want in ("--batch", "--opt=jit", "--cc=/usr/bin/gcc", "--make-jobs=6"):
            self.assertIn(want, argv)
        self.assertEqual(argv[-1], "targetpypystandalone.py")
        self.assertNotIn("--lto", argv)
        self.assertIn("--lto", ept.rpython_argv(self.ctx(lto=True)))
        self.assertLess(argv.index("--batch"), argv.index("targetpypystandalone.py"))

    def test_package_env_and_argv(self):
        c = self.ctx()
        env = ept.package_env(c)
        for k in ("PYPY_NO_EMBED_DEPENDENCIES", "PYPY_NO_REBUILD_EXTENSIONS", "PYPY_NO_MAKE_PORTABLE",
                  "PYPY_PACKAGE_WITHOUTTK", "PIP_NO_INDEX"):
            self.assertEqual(env[k], "1")
        self.assertEqual(env["PIP_FIND_LINKS"], "/w/downloads")
        self.assertNotIn("PYPY_PACKAGE_NOKEEPDEBUG", env)
        self.assertNotIn("PYPY_EMBED_DEPENDENCIES", env)
        env = ept.package_env(self.ctx(with_tk=True, strip=True, embed_deps=True))
        self.assertNotIn("PYPY_PACKAGE_WITHOUTTK", env)
        self.assertEqual(env["PYPY_PACKAGE_NOKEEPDEBUG"], "1")
        self.assertEqual((env["PYPY_EMBED_DEPENDENCIES"], env["PYPY_REBUILD_EXTENSIONS"]), ("1", "1"))
        self.assertNotIn("PYPY_NO_EMBED_DEPENDENCIES", env)
        argv = ept.package_argv(c, ["/h/py"], ["gdbm", "curses"])
        self.assertEqual(argv, ["/h/py", "/w/src/pypy2.7-v8.0.0-src/pypy/tool/release/package.py", "--archive-name",
                                "pypy2.7-v8.0.0", "--builddir", "/w/package/pypy2.7-v8.0.0", "--without-gdbm",
                                "--without-curses"])

    def test_cffi_env(self):
        env = ept.cffi_env(self.ctx())
        self.assertEqual(env["PYTHONPATH"], "/w/src/pypy2.7-v8.0.0-src")
        self.assertEqual(env["LDSHARED"], "/usr/bin/gcc -pthread -shared")
        self.assertEqual(env["PIP_NO_INDEX"], "1")

    def test_a_curl_only_machine_fetches_with_curl_in_cffi_and_package(self):
        with mock.patch.object(ept, "which", lambda name, path=None: "/usr/bin/curl" if name == "curl" else None):
            c = self.ctx(embed_deps=True)
            self.assertEqual(ept.package_env(c)["USE_CURL"], "1")
            self.assertEqual(ept.cffi_env(c)["USE_CURL"], "1")
            self.assertNotIn("USE_CURL", ept.package_env(self.ctx()))        # nothing is downloaded then
        with mock.patch.object(ept, "which", lambda name, path=None: "/usr/bin/" + name):
            self.assertNotIn("USE_CURL", ept.package_env(self.ctx(embed_deps=True)))
            self.assertNotIn("USE_CURL", ept.cffi_env(self.ctx(embed_deps=True)))

    def test_jobs_follow_memory(self):
        self.assertEqual(ept.default_jobs({"cpus": 24, "mem_avail_gb": 30}), 20)
        self.assertEqual(ept.default_jobs({"cpus": 4, "mem_avail_gb": 30}), 4)
        self.assertEqual(ept.default_jobs({"cpus": 8, "mem_avail_gb": 1}), 1)

    def test_find_cffi_script_wherever_it_is(self):
        src = os.path.join(self.tmp, "s")
        self.assertEqual(ept.find_cffi_script(src), "")
        old = put(src, "pypy/tool/build_cffi_imports.py")
        self.assertEqual(ept.find_cffi_script(src), old)
        new = put(src, "lib_pypy/pypy_tools/build_cffi_imports.py")
        self.assertEqual(ept.find_cffi_script(src), new)
        other = os.path.join(self.tmp, "t")
        moved = put(other, "somewhere/else/build_cffi_imports.py")
        self.assertEqual(ept.find_cffi_script(other), moved)


# ---------------------------------------------------- reading RPython's output

def legacy_log():
    if not os.path.isfile(LEGACY_LOG):
        raise unittest.SkipTest("no legacy/python_log-2026-08-29.txt")
    with open(LEGACY_LOG, encoding="utf-8", errors="replace") as f:
        return f.read()


class TranslationOutput(Quiet):
    def test_phases_from_the_real_log(self):
        text = legacy_log()
        w = ept.TranslationWatcher("pypy2")
        said = []
        for line in text.splitlines():
            said += w.feed(line, 10)
        self.assertEqual(w.phase, "Annotating&simplifying")
        self.assertTrue(any("phase 1/9: Annotating&simplifying" in s for s in said))
        self.assertTrue(any("[translation:ERROR] KeyboardInterrupt" in s for s in said))
        self.assertFalse(any("discarded-qualifiers" in s for s in said))       # clang's warnings are not errors
        self.assertFalse(any(set(s.strip()) <= set(".+*%#") for s in said))   # nor the mandelbrot
        self.assertEqual(w.timings["annotate"], 56.1)
        self.assertIn("phase 1/9", w.status(10))

    def test_the_real_log_is_an_interruption(self):
        text = legacy_log()
        self.assertEqual(ept.classify_translation(1, text), "interrupted")
        errs = ept.translation_errors(text)
        self.assertTrue(any("[translation:ERROR] KeyboardInterrupt" in e for e in errs))
        self.assertTrue(any("terminalwriter.py" in e for e in errs))           # the frames above it
        self.assertEqual(ept.failed_phase(text), "Annotating&simplifying")

    def test_phases_only_move_forward_and_paths_are_recorded(self):
        w = ept.TranslationWatcher("x")
        self.assertTrue(w.feed("[translation:info] RTyping...", 1))
        self.assertEqual(w.feed("[translation:info] Annotating&simplifying...", 2), [])
        self.assertEqual(w.phase_i, 2)
        w.feed("[translation:info] usession directory: /w/usession/k/usession-k-0", 3)
        w.feed("[translation:info] written: /w/usession/k/usession-k-0/testing_1/testing_1.c", 3)
        w.feed("[translation:info] created: /w/src/pypy/goal/pypy-c", 3)
        self.assertEqual(w.usession, "/w/usession/k/usession-k-0")
        self.assertTrue(w.c_file.endswith("testing_1.c"))
        self.assertTrue(w.created.endswith("pypy-c"))
        self.assertEqual(w.feed("[platform:WARNING] warning: unknown warning option", 4), [])
        self.assertTrue(w.feed("[platform:Error] implement.c:12: error: 'x' undeclared", 5))

    def test_oom_and_errors(self):
        self.assertEqual(ept.classify_translation(-9, ""), "oom")
        self.assertEqual(ept.classify_translation(137, ""), "oom")
        self.assertEqual(ept.classify_translation(1, "[translation:ERROR] MemoryError"), "oom")
        self.assertEqual(ept.classify_translation(1, "make: *** [Makefile:12] Killed"), "oom")
        self.assertEqual(ept.classify_translation(-2, ""), "interrupted")
        self.assertEqual(ept.classify_translation(1, "[translation:ERROR] AnnotatorError"), "error")
        self.assertEqual(ept.classify_translation(0, "whatever"), "ok")

    def test_c_errors_and_the_c23_hint(self):
        text = ("[translation:info] Compiling c source...\n"
                "[platform:Error] rpython_rlib.c:42:5: error: too many arguments to function 'f'\n"
                "make: *** [Makefile:300: rpython_rlib.o] Error 1\n")
        errs = ept.translation_errors(text)
        self.assertIn("too many arguments", errs[0])
        self.assertTrue(ept.C23_RE.search(text))
        self.assertEqual(ept.failed_phase(text), "Compiling c source")


# --------------------------------------------------------------- cffi modules

class Cffi(Quiet):
    def test_keys_per_flavour(self):
        k27, k311, k312 = ept.cffi_keys("2.7"), ept.cffi_keys("3.11"), ept.cffi_keys("3.12")
        self.assertIn("gdbm", k27)
        self.assertNotIn("lzma", k27)
        self.assertIn("_gdbm", k311)
        self.assertNotIn("gdbm", k311)
        for k in ("lzma", "_blake2", "_sha3"):
            self.assertIn(k, k311)
        self.assertIn("_posixshmem", k311)          # the 3.11 build_cffi_imports.py lists it too
        self.assertIn("_posixshmem", k312)

    def test_keys_are_cut_to_what_the_release_script_lists(self):
        """package.py makes a --without-KEY of each key its release lists: an unknown one stops it."""
        src = os.path.join(self.tmp, "src")
        put(src, "lib_pypy/pypy_tools/build_cffi_imports.py", 'import sys\ncffi_build_scripts = [\n'
            '    ("_ctypes._ctypes_cffi",\n     "_ctypes/_ctypes_build.py" if sys.platform == "darwin" else None),\n'
            '    ("_ssl", "_ssl_build.py"),\n    ("lzma", "_lzma_build.py"),\n    ("xx", None),\n'
            '    ("_posixshmem", "_posixshmem_build.py" if sys.platform != "win32" else None),\n]\n'
            'cffi_dependencies = {"_ssl": ("u", "d", [])}\n')
        self.assertEqual(ept.release_cffi_keys(src), {"_ctypes._ctypes_cffi", "_ssl", "lzma", "xx", "_posixshmem"})
        self.assertEqual(ept.cffi_keys("3.11", src), ["_ssl", "lzma", "_posixshmem"])
        old = os.path.join(self.tmp, "old")                  # an older release: the dict form, no _posixshmem
        put(old, "pypy/tool/build_cffi_imports.py", 'cffi_build_scripts = {\n    "_ssl": "_ssl_build.py",\n'
            '    "sqlite3": "_sqlite3_build.py",\n    "lzma": "_lzma_build.py",\n}\n')
        self.assertEqual(ept.cffi_keys("3.10", old), ["_ssl", "sqlite3", "lzma"])
        self.assertEqual(ept.cffi_keys("3.11", os.path.join(self.tmp, "none")), ept.cffi_keys("3.11"))
        table = {"_ssl": "ok", "lzma": "ok", "_posixshmem": "ImportError"}
        rows, req, other = ept.judge_cffi(table, "3.10", keys=ept.cffi_keys("3.10", old))
        self.assertEqual([r[0] for r in rows], ["_ssl", "sqlite3", "lzma"])
        self.assertEqual((req, other), (["sqlite3"], []))           # never a --without-_posixshmem here

    def test_levels_and_the_judgement(self):
        self.assertEqual(ept.cffi_level("_ssl", "2.7"), "required")
        self.assertEqual(ept.cffi_level("lzma", "3.12"), "required")
        self.assertEqual(ept.cffi_level("curses", "2.7"), "wanted")
        self.assertEqual(ept.cffi_level("_tkinter", "2.7"), "skipped")
        self.assertEqual(ept.cffi_level("_tkinter", "2.7", with_tk=True), "required")
        self.assertEqual(ept.cffi_level("audioop", "3.11"), "optional")
        table = dict((k, "ok") for k in ept.cffi_keys("2.7"))
        table.update(gdbm="ImportError: libgdbm.so.6", _tkinter="ImportError: no tk", _ssl="ImportError: x")
        rows, req, other = ept.judge_cffi(table, "2.7")
        self.assertEqual(req, ["_ssl"])
        self.assertEqual(sorted(other), ["_tkinter", "gdbm"])


# ------------------------------------------------------------- the interpreter

class FindExe(Quiet):
    def test_pypy27_layout(self):
        root = os.path.join(self.tmp, "p2")
        put(root, "bin/pypy", "", 0o755)
        os.symlink("pypy", os.path.join(root, "bin", "python"))
        self.assertEqual(ept.find_exe(root), (os.path.join(root, "bin", "pypy"), "2.7"))

    def test_pypy3N_layout_where_the_only_file_is_pypy3N(self):
        root = os.path.join(self.tmp, "p3")
        put(root, "bin/pypy3.12", "", 0o755)
        put(root, "bin/libpypy3.12-c.so", "")
        for l in ("pypy", "pypy3", "python", "python3"):
            os.symlink("pypy3.12", os.path.join(root, "bin", l))
        exe = os.path.join(root, "bin", "pypy3.12")
        self.assertEqual(ept.find_exe(root), (exe, "3.12"))
        self.assertEqual(ept.find_exe(root, "3.12"), (exe, "3.12"))
        # the .sh's `find -type f -name pypy3` would have found nothing here:
        self.assertFalse(os.path.isfile(os.path.join(root, "bin", "pypy3")) and
                         not os.path.islink(os.path.join(root, "bin", "pypy3")))

    def test_nothing_and_links_only(self):
        self.assertEqual(ept.find_exe(os.path.join(self.tmp, "none")), ("", ""))
        root = os.path.join(self.tmp, "legacy")
        put(root, "real/pypy3", "", 0o755)
        os.makedirs(os.path.join(root, "bin"))
        os.symlink(os.path.join(root, "real", "pypy3"), os.path.join(root, "bin", "pypy3"))
        self.assertEqual(ept.find_exe(root)[0], os.path.join(root, "bin", "pypy3"))


class Verify(Quiet):
    def prefix(self, layout="2.7", **cfg):
        root = os.path.join(self.tmp, "prefix-%d" % len(os.listdir(self.tmp)))
        exe = "pypy" if layout == "2.7" else "pypy" + layout
        version = [2, 7, 18] if layout == "2.7" else [int(x) for x in layout.split(".")] + [0]
        interp = fake_interp(self.tmp, "i%d" % len(os.listdir(self.tmp)), **dict({"version": version}, **cfg))
        os.makedirs(os.path.join(root, "bin"))
        shutil.copy2(interp, os.path.join(root, "bin", exe))
        return root

    def test_a_good_pypy2(self):
        good, s = ept.verify_interpreter(self.prefix(), "2.7", "8.0.0")
        self.assertTrue(good, s["failures"])
        self.assertEqual((s["version"], s["pypy"], s["jit"], s["ssl_context"]), ("2.7.18", "8.0.0", True, "ok"))
        self.assertTrue(s["pip"].startswith("pip 24.0"))

    def test_a_good_pypy3(self):
        good, s = ept.verify_interpreter(self.prefix("3.11"), "3.11", "8.0.0")
        self.assertTrue(good, s["failures"])
        self.assertEqual(s["exe"].rsplit("/", 1)[1], "pypy3.11")

    def test_a_missing_core_module_fails_and_a_nice_one_warns(self):
        good, s = ept.verify_interpreter(self.prefix(missing=["sqlite3", "gdbm"]), "2.7", "8.0.0")
        self.assertFalse(good)
        self.assertTrue(any("REQUIRED modules missing: sqlite3" in f for f in s["failures"]))
        self.assertTrue(any("gdbm" in w for w in s["warnings"]))
        self.assertEqual(s["missing"]["core"], ["sqlite3"])

    def test_an_ssl_crash_by_signal_is_told_apart(self):
        good, s = ept.verify_interpreter(self.prefix(ssl="crash"), "2.7", "8.0.0")
        self.assertFalse(good)
        self.assertEqual(s["ssl_context"], "crash")
        self.assertTrue(any("CRASHED (signal %d)" % signal.SIGSEGV in f for f in s["failures"]))

    def test_an_ssl_failure_is_not_a_crash(self):
        good, s = ept.verify_interpreter(self.prefix(ssl="fail"), "2.7", "8.0.0")
        self.assertFalse(good)
        self.assertEqual(s["ssl_context"], "fail")
        self.assertTrue(any("cannot create a default context" in f for f in s["failures"]))

    def test_no_jit_wrong_release_wrong_language_no_pip(self):
        self.assertFalse(ept.verify_interpreter(self.prefix(jit=False), "2.7", "8.0.0")[0])
        good, s = ept.verify_interpreter(self.prefix(pypy=[7, 3, 23]), "2.7", "8.0.0")
        self.assertTrue(any("7.3.23" in f for f in s["failures"]))
        good, s = ept.verify_interpreter(self.prefix("3.12"), "3.11", None)
        self.assertTrue(any("not 3.11" in f for f in s["failures"]))
        good, s = ept.verify_interpreter(self.prefix(pip=False), "2.7", "8.0.0")
        self.assertFalse(good)
        good, s = ept.verify_interpreter(self.prefix(pip=False), "2.7", "8.0.0", expect_pip=False)
        self.assertTrue(good, s["failures"])

    def test_no_interpreter(self):
        good, s = ept.verify_interpreter(os.path.join(self.tmp, "empty"))
        self.assertFalse(good)
        self.assertIn("no interpreter", s["failures"][0])

    def test_ssl_outcome(self):
        self.assertEqual(ept.ssl_outcome(0), ("ok", 0))
        self.assertEqual(ept.ssl_outcome(-11), ("crash", 11))
        self.assertEqual(ept.ssl_outcome(139), ("crash", 11))
        self.assertEqual(ept.ssl_outcome(1), ("fail", 0))


# --------------------------------------------------------- install, -current

class Install(Quiet):
    def stage(self, name="stage"):
        st = os.path.join(self.tmp, name)
        put(st, "bin/pypy", "#!/bin/sh\n", 0o755)
        put(st, "bin/pip", "#!%s/bin/pypy\nimport pip\n" % st, 0o755)
        return st

    def test_one_rename_marker_and_relocated_scripts(self):
        st, prefix = self.stage(), os.path.join(self.tmp, "opt", "pypy2.7-v8.0.0")
        self.assertEqual(ept.install_tree(st, prefix, marker={"tool": ept.TOOL, "state": "unverified"}), "")
        self.assertFalse(os.path.exists(st))
        self.assertEqual(ept.read_json(os.path.join(prefix, ept.MARKER))["state"], "unverified")
        with open(os.path.join(prefix, "bin", "pip")) as f:
            self.assertEqual(f.readline(), "#!%s/bin/pypy\n" % prefix)
        self.assertTrue(os.access(os.path.join(prefix, "bin", "pip"), os.X_OK))

    def test_never_installed_over(self):
        prefix = os.path.join(self.tmp, "opt", "p")
        os.makedirs(prefix)
        with self.assertRaises(ept.ToolError) as cm:
            ept.install_tree(self.stage(), prefix)
        self.assertIn("--rebuild", str(cm.exception))
        with self.assertRaises(ept.ToolError) as cm:
            ept.install_tree(self.stage("s2"), prefix, rebuild=True)
        self.assertIn("not a build of this tool's", str(cm.exception))

    def test_rebuild_sets_aside_and_puts_back_on_failure(self):
        prefix = os.path.join(self.tmp, "opt", "p")
        put(prefix, ept.MARKER, json.dumps({"tool": ept.TOOL}))
        put(prefix, "old-file", "old")
        with self.assertRaises(ept.ToolError):
            ept.install_tree(os.path.join(self.tmp, "no-stage"), prefix, rebuild=True, busy=lambda p: [])
        self.assertTrue(os.path.isfile(os.path.join(prefix, "old-file")))
        old = ept.install_tree(self.stage(), prefix, rebuild=True, busy=lambda p: [])
        self.assertTrue(old.startswith(prefix + ".old-"))
        self.assertTrue(os.path.isfile(os.path.join(old, "old-file")))
        self.assertTrue(os.path.isfile(os.path.join(prefix, "bin", "pypy")))

    def test_a_prefix_in_use_is_not_replaced(self):
        prefix = os.path.join(self.tmp, "opt", "p")
        put(prefix, ept.MARKER, json.dumps({"tool": ept.TOOL}))
        with self.assertRaises(ept.ToolError) as cm:
            ept.install_tree(self.stage(), prefix, rebuild=True, busy=lambda p: [(4242, "pypy server.py")])
        self.assertIn("pid 4242", str(cm.exception))
        self.assertTrue(os.path.isdir(prefix))

    def test_busy_processes_sees_this_test_runner(self):
        hits = ept.busy_processes([os.path.dirname(os.path.realpath(sys.executable))])
        self.assertIsInstance(hits, list)

    def test_current_link_is_relative_atomic_and_never_a_directory(self):
        opt = os.path.join(self.tmp, "opt")
        a, b = os.path.join(opt, "pypy2.7-v7.3.23"), os.path.join(opt, "pypy2.7-v8.0.0")
        os.makedirs(a)
        os.makedirs(b)
        link = ept.link_current(opt, "pypy2-current", a)
        self.assertEqual(os.readlink(link), "pypy2.7-v7.3.23")
        ept.link_current(opt, "pypy2-current", b)
        self.assertEqual(os.readlink(link), "pypy2.7-v8.0.0")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.assertEqual(os.readlink(ept.link_current(opt, "pypy2-current", elsewhere)), elsewhere)
        os.makedirs(os.path.join(opt, "pypy3-current"))
        with self.assertRaises(ept.ToolError):
            ept.link_current(opt, "pypy3-current", b)
        self.assertEqual([n for n in os.listdir(opt) if ".new-" in n], [])


class ByHand(Quiet):
    def src(self):
        s = os.path.join(self.tmp, "src")
        put(s, "lib-python/2.7/os.py", "")
        put(s, "lib-python/2.7/os.pyc", "")
        put(s, "lib-python/3/os.py", "")
        put(s, "lib-python/3/site-packages/README.txt", "")
        put(s, "lib_pypy/__init__.py", "")
        put(s, "lib_pypy/_ssl_cffi.c", "")
        put(s, "lib_pypy/_ssl.py", "")
        put(s, "lib_pypy/x.o", "")
        put(s, "include/pypy3.12/Python.h", "")
        put(s, "include/Python.h", "")
        put(s, "site-packages/README", "")
        put(s, "LICENSE", "")
        put(s, "pypy/goal/pypy-c", "", 0o755)
        put(s, "pypy/goal/libpypy-c.so", "")
        put(s, "pypy/goal/pypy3.12-c", "", 0o755)
        put(s, "pypy/goal/libpypy3.12-c.so", "")
        return s

    def test_pypy27(self):
        s = self.src()
        tree = os.path.join(self.tmp, "tree2")
        exe = ept.assemble_by_hand(s, os.path.join(s, "pypy", "goal"), tree, "2.7")
        self.assertEqual(exe, os.path.join(tree, "bin", "pypy"))
        self.assertTrue(os.path.isfile(os.path.join(tree, "bin", "libpypy-c.so")))
        self.assertEqual(os.readlink(os.path.join(tree, "bin", "python2")), "pypy")
        self.assertTrue(os.path.isfile(os.path.join(tree, "lib-python", "2.7", "os.py")))
        self.assertFalse(os.path.exists(os.path.join(tree, "lib-python", "2.7", "os.pyc")))
        self.assertTrue(os.path.isfile(os.path.join(tree, "lib_pypy", "_ssl.py")))
        self.assertFalse(os.path.exists(os.path.join(tree, "lib_pypy", "_ssl_cffi.c")))
        self.assertFalse(os.path.exists(os.path.join(tree, "lib_pypy", "x.o")))
        self.assertTrue(os.path.isfile(os.path.join(tree, "site-packages", "README")))
        self.assertEqual(ept.find_exe(tree), (exe, "2.7"))

    def test_pypy3N(self):
        s = self.src()
        tree = os.path.join(self.tmp, "tree3")
        exe = ept.assemble_by_hand(s, os.path.join(s, "pypy", "goal"), tree, "3.12")
        self.assertEqual(exe, os.path.join(tree, "bin", "pypy3.12"))
        self.assertEqual(os.readlink(os.path.join(tree, "bin", "pypy3")), "pypy3.12")
        lib = os.path.join(tree, "lib", "pypy3.12")
        self.assertTrue(os.path.isfile(os.path.join(lib, "os.py")))
        self.assertTrue(os.path.isfile(os.path.join(lib, "_ssl.py")))
        self.assertFalse(os.path.exists(os.path.join(lib, "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(lib, "site-packages", "README.txt")))
        self.assertTrue(os.path.isfile(os.path.join(tree, "include", "pypy3.12", "Python.h")))
        self.assertEqual(ept.find_exe(tree), (exe, "3.12"))


# ------------------------------------------------------------- doctor, status

MACHINE = {"pkg_manager": "dnf", "os_id": "almalinux", "os_like": "rhel centos fedora", "os_version": "10.2",
           "arch": "x86_64", "tools": dict((t, "/usr/bin/" + t) for t in ept.TOOLS),
           "headers": dict((k, True) for k in ept.HEADERS)}


class Doctor(Quiet):
    def test_the_contract_shape(self):
        m = dict(MACHINE, headers=dict(MACHINE["headers"], gdbm=False, zlib=False, tk=False))
        rows = ept.requirements({}, m, ["pypy2"], FAKE_CC, True)
        rep = ept.doctor_report(rows, m, euid=1000)
        self.assertEqual(sorted(rep), ["enable_repos", "install_command", "missing", "ok", "package_manager", "tool"])
        self.assertEqual(rep["tool"], "enterprise_pypy_tool")
        self.assertFalse(rep["ok"])                                       # zlib is required
        for item in rep["missing"]:
            self.assertEqual(sorted(item), ["level", "package", "what"])
            self.assertIn(item["level"], ("required", "wanted", "optional"))
        self.assertEqual(rep["package_manager"], "dnf")
        self.assertEqual(rep["enable_repos"], ["crb"])                    # gdbm-devel is in CRB
        self.assertEqual(rep["install_command"], "sudo dnf install -y zlib-ng-compat-devel gdbm-devel")
        self.assertNotIn("tk-devel", rep["install_command"])              # optional: not installed for you
        json.dumps(rep)

    def test_ncurses_is_required_by_every_translation_term_h_too(self):
        for headers in (dict(MACHINE["headers"], ncurses=False), dict(MACHINE["headers"], ncurses_term=False)):
            m = dict(MACHINE, headers=headers)
            for targets in (["pypy2"], ["pypy3"], ["host"]):
                rows = ept.requirements({}, m, targets, FAKE_CC, True)
                self.assertEqual([r[3] for r in rows if r[0] == "ncurses"], ["required"])
                rep = ept.doctor_report(rows, m, euid=1000)
                self.assertFalse(rep["ok"])
                self.assertIn("ncurses-devel", rep["install_command"])
        self.assertEqual(ept.HEADERS["ncurses_term"], "term.h")
        self.assertTrue(ept.doctor_report(ept.requirements({}, MACHINE, ["pypy2"], FAKE_CC, True), MACHINE, 1000)["ok"])

    def test_root_is_a_required_problem(self):
        rows = ept.requirements({}, MACHINE, ["pypy2"], FAKE_CC, True)
        rep = ept.doctor_report(rows, MACHINE, euid=0)
        self.assertFalse(rep["ok"])
        self.assertIn("root", rep["missing"][0]["what"])
        self.assertTrue(ept.doctor_report(rows, MACHINE, euid=1000)["ok"])

    def test_package_tables_for_every_manager(self):
        for key in ("libffi", "openssl", "libbz2", "expat", "ncurses", "gdbm", "sqlite", "liblzma", "zlib", "tk",
                    "pkgconf", "gcc", "make", "patch"):
            for pm in ("dnf", "apt-get", "zypper", "pacman"):
                self.assertTrue(ept.PACKAGES[key][pm], (key, pm))
        self.assertEqual(ept.package_name("libffi", "apt-get", {}), "libffi-dev")
        self.assertEqual(ept.package_name("openssl", "zypper", {}), "libopenssl-devel")
        self.assertEqual(ept.package_name("pkgconf", "pacman", {}), "pkgconf")
        self.assertEqual(ept.package_name("zlib", "dnf", dict(MACHINE, os_version="9.6")), "zlib-devel")
        self.assertEqual(ept.install_command(["a", "b"], "pacman"), "sudo pacman -S --needed --noconfirm a b")

    def test_one_package_at_a_time_keeps_the_whole_command_head(self):
        for pm, head in (("pacman", ["sudo", "pacman", "-S", "--needed", "--noconfirm"]),
                         ("dnf", ["sudo", "dnf", "install", "-y"]), ("zypper", ["sudo", "zypper", "install", "-y"])):
            calls = []

            def run(cmd, **kw):
                calls.append(list(cmd))
                return mock.Mock(returncode=1 if len(calls) == 1 else 0)       # the whole list is refused
            rep = {"package_manager": pm, "install_command": ept.install_command(["gdbm", "tk"], pm),
                   "enable_repos": []}
            with mock.patch.object(ept, "run", run), mock.patch.object(ept, "refuse_root", lambda *a, **k: None):
                ept.install_deps(rep)
            self.assertEqual(calls, [head + ["gdbm", "tk"], head + ["gdbm"], head + ["tk"]])

    def test_lzma_required_only_for_pypy3_and_tk_with_tk(self):
        m = dict(MACHINE, headers=dict(MACHINE["headers"], liblzma=False, tk=False))
        lv = dict((r[0], r[3]) for r in ept.requirements({}, m, ["pypy2"], FAKE_CC, True))
        self.assertEqual((lv["liblzma"], lv["tk"]), ("optional", "optional"))
        lv = dict((r[0], r[3]) for r in ept.requirements({"with_tk": True}, m, ["pypy2", "pypy3"], FAKE_CC, True))
        self.assertEqual((lv["liblzma"], lv["tk"]), ("required", "required"))
        rows = ept.requirements({}, dict(m, pkg_manager="apt-get"), ["pypy2"], FAKE_CC, True)
        self.assertIn("dpkg-dev", [r[0] for r in rows])
        rows = ept.requirements({"host": "prebuilt"}, m, ["pypy2"], FAKE_CC, True, host_mode="prebuilt")
        self.assertNotIn("readline", [r[0] for r in rows])

    def test_a_compiler_that_does_not_work_is_missing(self):
        rows = ept.requirements({}, MACHINE, ["pypy2"], FAKE_CC, "cannot run programs")
        self.assertFalse(dict((r[0], r[2]) for r in rows)["gcc"])

    def test_refuse_root(self):
        with self.assertRaises(ept.ToolError):
            ept.refuse_root("build", euid=0)
        ept.refuse_root("build", euid=1000)

    def test_reachability_reports_every_place(self):
        rows = ept.reachability(probe=lambda u: (u.startswith("https://files"), "HTTP 404"),
                                git=lambda: (_ for _ in ()).throw(ept.ToolError("fatal: no")))
        self.assertEqual(len(rows), 5)
        self.assertTrue(rows[2][2])
        self.assertFalse(rows[-1][2])


class Status(Quiet):
    def test_the_contract_shape_with_installed_legacy_and_builds(self):
        opt, wd = os.path.join(self.tmp, "opt"), os.path.join(self.tmp, "w")
        good = os.path.join(opt, "pypy2.7-v8.0.0")
        os.makedirs(os.path.join(good, "bin"))
        shutil.copy2(fake_interp(self.tmp, "a"), os.path.join(good, "bin", "pypy"))
        put(good, ept.MARKER, json.dumps({"tool": ept.TOOL, "state": "verified", "release": "8.0.0", "series": "2.7",
                                          "compiler": "gcc (GCC) 14.2.0"}))
        os.symlink("pypy2.7-v8.0.0", os.path.join(opt, "pypy2-current"))
        legacy = os.path.join(opt, "pypy3", "pypy3", "bin")           # the HANDOFF's ~/opt/pypy3, nested
        os.makedirs(legacy)
        shutil.copy2(fake_interp(self.tmp, "b", version=[3, 11, 9], pypy=[7, 3, 17], ssl="crash"),
                     os.path.join(legacy, "pypy3.11"))
        os.makedirs(os.path.join(opt, "pypy2.7-v8.0.0.old-20260101-000000"))
        os.makedirs(os.path.join(opt, "gcc-15.1.0"))
        put(wd, "state.json", json.dumps({"tool": ept.TOOL, "builds": {
            "pypy3.11-v8.0.0": {"status": "running", "stage": "translate", "error": None},
            "pypy2.7-v8.0.0": {"status": "done", "stage": "link", "error": None}}}))
        d = ept.status_data(opt, wd)
        self.assertEqual(sorted(d), ["builds", "current", "installed", "opt_root", "tool"])
        self.assertEqual(d["current"], {"pypy2-current": os.path.realpath(good), "pypy3-current": None})
        self.assertEqual(len(d["installed"]), 2)
        for e in d["installed"]:
            self.assertEqual(sorted(e), ["built_by_tool", "compiler", "for", "name", "prefix", "runs", "version"])
        mine = next(e for e in d["installed"] if e["prefix"] == os.path.realpath(good))
        self.assertEqual((mine["name"], mine["version"], mine["runs"], mine["built_by_tool"], mine["compiler"],
                          mine["for"]), ("pypy2", "8.0.0", True, True, "gcc (GCC) 14.2.0", "python2.7"))
        old = next(e for e in d["installed"] if e["prefix"] != os.path.realpath(good))
        self.assertEqual((old["name"], old["version"], old["runs"], old["built_by_tool"], old["compiler"], old["for"]),
                         ("pypy3", "7.3.17", False, False, None, "python3.11"))
        builds = dict((b["id"], b) for b in d["builds"])
        self.assertEqual(sorted(builds["pypy3.11-v8.0.0"]), ["error", "id", "stage", "status"])
        self.assertEqual(builds["pypy3.11-v8.0.0"]["status"], "killed")      # running, but nobody holds the lock
        self.assertEqual(builds["pypy2.7-v8.0.0"]["status"], "done")
        json.dumps(d)

    def test_installed_prefixes_are_real_paths_like_current(self):
        real, link = os.path.join(self.tmp, "realopt"), os.path.join(self.tmp, "optlink")
        put(real, "pypy2.7-v8.0.0/bin/pypy", "#!/bin/sh\n", 0o755)
        os.symlink("pypy2.7-v8.0.0", os.path.join(real, "pypy2-current"))
        os.symlink(real, link)
        d = ept.status_data(link, os.path.join(self.tmp, "w"), run_checks=False)
        self.assertEqual(d["opt_root"], link)
        self.assertEqual([e["prefix"] for e in d["installed"]], [d["current"]["pypy2-current"]])

    def test_empty(self):
        d = ept.status_data(os.path.join(self.tmp, "none"), os.path.join(self.tmp, "w"))
        self.assertEqual(d["installed"], [])
        self.assertEqual(d["builds"], [])
        self.assertEqual(d["current"], {"pypy2-current": None, "pypy3-current": None})


# ---------------------------------------------------------------- state, lock

class StateAndLock(Quiet):
    def test_state_round_trip_and_stamp(self):
        p = os.path.join(self.tmp, "w", "state.json")
        st = ept.State(p)
        st.set_stage("k", "translate", status="done", seconds=5)
        again = ept.State(p)
        self.assertTrue(again.done("k", "translate"))
        self.assertEqual(again.d["tool"], ept.TOOL)
        again.reset("k", ["translate"])
        self.assertFalse(ept.State(p).done("k", "translate"))
        put(self.tmp, "bad/state.json", "{not json")
        self.assertEqual(ept.State(os.path.join(self.tmp, "bad", "state.json")).d["builds"], {})

    def test_one_instance_per_work_directory(self):
        wd = os.path.join(self.tmp, "w")
        self.assertFalse(ept.lock_is_held(wd))
        ept.lock_dir(wd)
        self.assertTrue(ept.lock_is_held(wd))
        with self.assertRaises(ept.ToolError) as cm:
            ept.lock_dir(wd)
        self.assertIn("another", str(cm.exception))
        ept.unlock_all()
        self.assertFalse(ept.lock_is_held(wd))

    def test_run_stage_records_skips_and_fails(self):
        st = ept.State(os.path.join(self.tmp, "w", "state.json"))
        c = ept.Ctx(dict(ept.DEFAULTS), "pypy2", "/w", "/o")
        c.release, c.series = "8.0.0", "2.7"
        calls = []
        self.assertTrue(ept.run_stage(c, st, "extract", lambda: calls.append(1)))
        self.assertEqual(st.build(c.key)["status"], "partial")         # stages done, more to do
        self.assertFalse(ept.run_stage(c, st, "extract", lambda: calls.append(2)))
        self.assertTrue(ept.run_stage(c, st, "extract", lambda: calls.append(3), valid=lambda: False))
        self.assertTrue(ept.run_stage(c, st, "fetch", lambda: calls.append(4), always=True))
        self.assertTrue(ept.run_stage(c, st, "fetch", lambda: calls.append(5), always=True))
        self.assertEqual(calls, [1, 3, 4, 5])

        def boom():
            raise ept.ToolError("the C compiler died\nmore")
        with self.assertRaises(ept.ToolError):
            ept.run_stage(c, st, "translate", boom)
        self.assertEqual(st.stage(c.key, "translate")["status"], "failed")
        self.assertEqual(st.build(c.key)["error"], "the C compiler died")

        def interrupted():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            ept.run_stage(c, st, "cffi", interrupted)
        self.assertEqual(st.stage(c.key, "cffi")["status"], "interrupted")

    def test_a_stage_that_runs_again_makes_the_later_ones_run_again(self):
        st = ept.State(os.path.join(self.tmp, "state.json"))
        c = ept.Ctx(dict(ept.DEFAULTS), "pypy2", self.tmp, self.tmp)
        c.series, c.release = "2.7", "8.0.0"
        for n in ept.AFTER_HOST:
            st.set_stage(c.key, n, status="done")
        done = lambda: [n for n in ept.AFTER_HOST if st.done(c.key, n)]
        ept.run_stage(c, st, "smoke", lambda: None, valid=lambda: False)       # never undoes a translation
        self.assertEqual(done(), list(ept.AFTER_HOST))
        ept.run_stage(c, st, "package", lambda: None, valid=lambda: False)
        self.assertEqual(done(), ["extract", "smoke", "translate", "setuptools", "cffi", "package"])
        ept.run_stage(c, st, "translate", lambda: None, valid=lambda: False)
        self.assertEqual(done(), ["extract", "smoke", "translate"])
        ept.run_stage(c, st, "fetch", lambda: None, always=True)              # the always-stages undo nothing
        self.assertEqual(done(), ["extract", "smoke", "translate"])

    def test_fingerprint_diff(self):
        self.assertEqual(ept.fingerprint_diff({"cc": "/a", "x": 1}, {"cc": "/b", "x": 1}), ["cc: /a -> /b"])


# -------------------------------------------------------- downloads, processes

class Downloads(Quiet):
    def test_download_through_part_with_sha_checked(self):
        src = put(self.tmp, "srv/f.tar.gz", "payload")
        sha = ept.sha256_file(src)
        dest = os.path.join(self.tmp, "dl", "f.tar.gz")
        ept.download("file://" + src, dest, sha)
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        with self.assertRaises(ept.ToolError):
            ept.download("file://" + src, os.path.join(self.tmp, "dl", "g"), "0" * 64)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dl", "g")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dl", "g.part")))

    def test_fetch_any_cache_then_fallbacks(self):
        dd = os.path.join(self.tmp, "dl")
        tried = []

        def fetch(url, dest, sha):
            tried.append(url)
            if "bad" in url:
                raise ept.ToolError("403")
            put(os.path.dirname(dest), os.path.basename(dest), "data")
        path, url, sha = ept.fetch_any([("https://bad/a", "a", ""), ("https://good/b", "b", "")], dd, fetch=fetch)
        self.assertEqual((os.path.basename(path), url), ("b", "https://good/b"))
        self.assertEqual(tried, ["https://bad/a", "https://good/b"])
        self.assertEqual(ept.fetch_any([("https://x/a", "a", ""), ("https://x/b", "b", "")], dd,
                                       fetch=lambda *a: self.fail("downloaded again"))[1], "https://x/b")
        with self.assertRaises(ept.ToolError):
            ept.fetch_any([("https://x/c", "c", "")], dd, offline=True)

    def test_extract_one_top_directory(self):
        top = put(self.tmp, "t/pypy-release-pypy2.7-v8.0.0/README.rst", "x")
        tb = os.path.join(self.tmp, "a.tar.gz")
        subprocess.check_call(["tar", "-czf", tb, "-C", os.path.join(self.tmp, "t"), "pypy-release-pypy2.7-v8.0.0"])
        dest = os.path.join(self.tmp, "src", "pypy2.7-v8.0.0-src")
        ept.extract(tb, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "README.rst")))
        self.assertTrue(top)
        self.assertEqual([n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".extract")], [])

    def test_run_logged_keeps_everything_and_rotates(self):
        log = os.path.join(self.tmp, "logs", "x.log")
        rc, secs = ept.run_logged(["sh", "-c", "echo one; echo 'f.c:1: error: bad' ; exit 3"], self.tmp,
                                  ept.scrubbed_env(), log)
        self.assertEqual(rc, 3)
        self.assertIn("one", ept.read_text(log))
        self.assertIn("    f.c:1: error: bad", self.out.getvalue())
        ept.run_logged(["true"], self.tmp, ept.scrubbed_env(), log)
        self.assertTrue(os.path.isfile(log + ".prev"))
        self.assertEqual(ept.error_summary(log + ".prev")[0].split(": ", 1)[1], "f.c:1: error: bad")

    def test_c_compiler_works(self):
        works, why = ept.c_compiler_works("/bin/false", scratch=self.tmp)
        self.assertFalse(works)
        if not os.access("/usr/bin/gcc", os.X_OK):
            self.skipTest("no gcc here")
        works, why = ept.c_compiler_works("/usr/bin/gcc", scratch=os.path.join(self.tmp, "s"))
        self.assertTrue(works, why)
        self.assertTrue(ept.have_header("stdio.h", "/usr/bin/gcc"))
        self.assertFalse(ept.have_header("no/such/header.h", "/usr/bin/gcc"))
        ci = ept.compiler_info("/usr/bin/gcc")
        self.assertIn(ci["kind"], ("gcc", "clang"))

    def test_resolve_cc_and_binutils_are_real_paths(self):
        opt = os.path.join(self.tmp, "opt")
        gcc = script(opt, "gcc-16.1.0/bin/gcc", 'case "$1" in --version) echo "gcc (GCC) 16.1.0"; '
                                              'echo "Copyright (C) 2026 Free Software Foundation, Inc.";; '
                                              '-dumpfullversion) echo 16.1.0;; esac\n')
        os.symlink("gcc-16.1.0", os.path.join(opt, "gcc-current"))
        ci = ept.resolve_cc("", opt)
        self.assertEqual((ci["path"], ci["version"]), (os.path.realpath(gcc), (16, 1, 0)))
        self.assertIn("step 4", ci["why"])
        script(opt, "binutils-2.46/bin/as", "")
        script(opt, "binutils-2.46/bin/ld", "")
        os.symlink("binutils-2.46", os.path.join(opt, "binutils-current"))
        self.assertEqual(ept.resolve_binutils("auto", opt)[0], os.path.join(os.path.realpath(opt), "binutils-2.46"))
        self.assertEqual(ept.resolve_binutils(os.path.join(opt, "binutils-2.46", "bin"), opt)[0],
                         os.path.join(os.path.realpath(opt), "binutils-2.46"))
        self.assertEqual(ept.resolve_binutils("system", opt)[0], "")
        with self.assertRaises(ept.UsageError):
            ept.resolve_binutils(self.tmp, opt)
        with self.assertRaises(ept.UsageError):
            ept.resolve_cc(os.path.join(self.tmp, "nothing"), opt)


# --------------------------------------------------------- options, profiles

class Options(Quiet):
    def args(self, **kw):
        d = dict((k, None) for k in ept.DEFAULTS)
        d.update(profile=None, from_=None)
        d.update(kw)
        return mock.Mock(**d)

    def test_targets(self):
        self.assertEqual(ept.parse_targets("both"), ["pypy2", "pypy3"])
        self.assertEqual(ept.parse_targets("pypy3,pypy2"), ["pypy2", "pypy3"])
        self.assertEqual(ept.parse_targets("host"), ["host"])
        for bad in ("cpython", "pypy2,host", ""):
            with self.assertRaises(ept.UsageError):
                ept.parse_targets(bad) if bad else ept.parse_targets(",")

    def test_check_options(self):
        o = dict(ept.DEFAULTS)
        self.assertEqual(ept.check_options(dict(o))["jobs"], 0)
        for bad in ({"pypy_version": "eight"}, {"series": "3"}, {"series": "2.7"}, {"resume": True, "restart": True},
                    {"jobs": -1}, {"extra_cflags": "'unclosed"}, {"prefix": "/x", "target": "both"}):
            with self.assertRaises(ept.UsageError):
                ept.check_options(dict(o, **bad))

    def test_profile_round_trip_and_the_command_line_wins(self):
        opts = ept.merge_options(self.args(target="pypy3", series="3.11", host="prebuilt", cc="~/opt/gcc/bin/gcc",
                                           link_current=False, save_profile="mine"))
        path = ept.profile_path("mine", must_exist=True)
        self.assertTrue(path.startswith(os.path.join(self.tmp, "home", ".config", ept.TOOL, "profiles")))
        loaded = ept.load_profile("mine")
        self.assertEqual((loaded["target"], loaded["series"], loaded["host"], loaded["link_current"]),
                         ("pypy3", "3.11", "prebuilt", False))
        self.assertEqual(loaded["cc"], os.path.join(self.tmp, "home", "opt", "gcc", "bin", "gcc"))
        again = ept.merge_options(self.args(profile="mine", series="3.12"))
        self.assertEqual((again["target"], again["series"]), ("pypy3", "3.12"))
        self.assertTrue(ept.merge_options(self.args(from_="compile"))["from_compile"])
        self.assertEqual([p["name"] for p in ept.list_profiles()], ["mine"])
        with self.assertRaises(ept.UsageError):
            ept.load_profile("nope")


# ----------------------------------------------------------- the main entry

class Main(Quiet):
    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ept.main(["--local"] + list(argv))
        return code, self.out.getvalue(), err.getvalue()

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.run_main("frobnicate")[0], 2)
        self.assertEqual(self.run_main("build", "--target", "nope", "--workdir", self.tmp)[0], 2)
        self.assertEqual(self.run_main("plan", "--series", "2")[0], 2)
        self.assertEqual(self.run_main()[0], 2)

    def test_yes_asks_nothing_not_even_the_wizard(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        def no_questions(prompt, default=""):
            raise AssertionError("asked: " + prompt)
        wd, built = os.path.join(self.tmp, "w"), []
        term = Tty()
        with mock.patch.object(ept.sys, "stdin", Tty()), mock.patch.object(ept.sys, "stdout", term), \
                mock.patch.object(ept, "ask", no_questions), \
                mock.patch.object(ept, "cmd_build", lambda args: built.append(args.yes) or 0):
            self.assertEqual(ept.main(["--local", "--yes", "--workdir", wd]), 2)     # no command: no wizard
            self.assertIn("--yes asks nothing", term.getvalue())
            self.assertEqual(ept.main(["--local", "-y", "--dry-run", "--workdir", wd]), 2)
            self.assertEqual(ept.main(["--local", "wizard", "--yes", "--workdir", wd]), 0)
            self.assertEqual(built, [True])                                       # `build`, no questions
            with self.assertRaises(AssertionError), mock.patch.object(ept, "refuse_root", lambda *a, **k: None):
                ept.main(["--local", "--workdir", wd])                            # without --yes it asks

    def test_status_json_is_alone_on_stdout(self):
        code, out, err = self.run_main("status", "--json", "--workdir", os.path.join(self.tmp, "w"),
                                       "--opt-root", os.path.join(self.tmp, "o"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["tool"], ept.TOOL)

    def test_doctor_json_is_alone_on_stdout(self):
        code, out, err = self.run_main("doctor", "--json", "--yes", "--workdir", os.path.join(self.tmp, "w"),
                                       "--opt-root", os.path.join(self.tmp, "o"))
        rep = json.loads(out)
        self.assertEqual(sorted(rep), ["enable_repos", "install_command", "missing", "ok", "package_manager", "tool"])
        self.assertEqual(code, 0 if rep["ok"] else 1)

    def test_detect_json(self):
        code, out, err = self.run_main("detect", "--json", "--workdir", os.path.join(self.tmp, "w"))
        d = json.loads(out)
        self.assertEqual(code, 0)
        for k in ("glibc", "arch_tag", "mem_total_gb", "swap_free_gb", "headers", "selinux"):
            self.assertIn(k, d)

    def test_plan_offline_from_the_cache_touches_nothing(self):
        wd, opt = os.path.join(self.tmp, "w"), os.path.join(self.tmp, "o")
        put(wd, "cache/index.html", INDEX)
        put(wd, "cache/versions.json", VERSIONS_JSON)
        put(wd, "cache/notes/release-v8.0.0.rst", NOTES_800)
        host = script(self.tmp, "py2/python2.7", ept.re.sub(r"exec .*", "exit 1", FAKE_HOST))
        before = sorted(os.listdir(wd))
        code, out, err = self.run_main("plan", "--offline", "--target", "both", "--workdir", wd, "--opt-root", opt,
                                       "--host", host)
        self.assertEqual(code, 0, out)
        self.assertIn("pypy2: PyPy 8.0.0 (Python 2.7)", out)
        self.assertIn("pypy3: PyPy 8.0.0 (Python 3.11)", out)
        self.assertIn("--batch --opt=jit", out)
        self.assertIn("nothing was touched", out)
        self.assertEqual(sorted(os.listdir(wd)), before)
        self.assertFalse(os.path.exists(opt))

    def test_clean_only_its_own_and_never_as_root(self):
        wd = os.path.join(self.tmp, "w")
        with mock.patch.object(ept.os, "geteuid", return_value=0):
            self.assertEqual(self.run_main("clean", "--workdir", wd)[0], 1)
        put(wd, "notes.txt", "mine")
        with mock.patch.object(ept.os, "geteuid", return_value=1000):
            self.assertEqual(self.run_main("clean", "--workdir", wd)[0], 1)      # no record of this tool
            self.assertTrue(os.path.isfile(os.path.join(wd, "notes.txt")))
            put(wd, "state.json", json.dumps({"tool": ept.TOOL, "builds": {"k": {"stages": {
                "fetch": {"status": "done"}, "translate": {"status": "done"}}}}, "hosts": {"cpython27": {}}}))
            for d in ("src/pypy2.7-v8.0.0-src/x", "usession/k/x", "package/k/x", "host/cpython-2.7.18/x",
                      "smoke/x", "tmp/x", "downloads/pypy.tar.gz", "logs/k/translate.log"):
                put(wd, d, "x")
            self.assertEqual(self.run_main("clean", "--workdir", wd)[0], 0)
            left = sorted(os.listdir(wd))
            self.assertEqual(left, [".lock", "downloads", "logs", "notes.txt", "src", "state.json"])
            st = ept.read_json(os.path.join(wd, "state.json"))
            self.assertEqual(list(st["builds"]["k"]["stages"]), ["fetch"])
            self.assertEqual(st["hosts"], {})
            self.assertEqual(self.run_main("clean", "--all", "--workdir", wd)[0], 0)
            self.assertEqual(sorted(os.listdir(wd)), [".lock", "notes.txt"])

    def test_build_refuses_root(self):
        with mock.patch.object(ept.os, "geteuid", return_value=0):
            code, out, err = self.run_main("build", "--workdir", os.path.join(self.tmp, "w"))
        self.assertEqual(code, 1)
        self.assertIn("as root", out + err)


class BothRun(Quiet):
    """`build --target both` with --host auto: pypy3 is translated by THIS run's pypy2 (PLAN step 7,
    "build pypy3 with that pypy2") -- never by an older pypy2-current, and not at all when it failed."""

    def go(self, fail_pypy2, *extra):
        wd, opt = os.path.join(self.tmp, "w"), os.path.join(self.tmp, "opt")
        put(opt, "pypy2.7-v7.3.23/bin/pypy", "#!/bin/sh\n", 0o755)        # an earlier release, still current
        os.symlink("pypy2.7-v7.3.23", os.path.join(opt, "pypy2-current"))
        calls = []

        def build_one(ctx, state, cat, machine):
            calls.append((ctx.target, ctx.host["mode"], ctx.host.get("path")))
            if ctx.target == "pypy2":
                if fail_pypy2:
                    raise ept.ToolError("the translation FAILED (fake), in RTyping")
                put(ctx.prefix, "bin/pypy", "#!/bin/sh\n", 0o755)

        def probe(p):
            return dict(PY2, impl="pypy", pypy="8.0.0", path=p) if p and os.path.isfile(p) else None
        machine = {"cpus": 4, "mem_total_gb": 32, "mem_avail_gb": 30, "swap_free_gb": 0}
        with mock.patch.object(ept, "refuse_root", lambda *a, **k: None), \
                mock.patch.object(ept, "preflight", lambda *a, **k: ({"ok": True}, machine, dict(FAKE_CC))), \
                mock.patch.object(ept, "get_catalog", lambda *a, **k: catalog(notes=lambda rel: NOTES_800)), \
                mock.patch.object(ept, "resource_checks", lambda *a, **k: ([], [])), \
                mock.patch.object(ept, "probe_interpreter", probe), \
                mock.patch.object(ept, "build_one", build_one):
            code = ept.main(["--local", "build", "--target", "both", "--yes", "--workdir", wd, "--opt-root", opt]
                            + list(extra))
        return code, calls, opt

    def test_a_failed_pypy2_stops_pypy3(self):
        code, calls, opt = self.go(True)
        self.assertEqual(code, 1)
        self.assertEqual([c[0] for c in calls], ["pypy2"])               # pypy3 never started
        self.assertIn("pypy3 was NOT started", self.out.getvalue())
        self.assertIn("--host cpython27 | prebuilt | PATH", self.out.getvalue())

    def test_pypy3_is_translated_by_the_pypy2_this_run_built_not_by_pypy2_current(self):
        code, calls, opt = self.go(False, "--no-link-current")
        self.assertEqual(code, 0, self.out.getvalue())
        mine = os.path.realpath(os.path.join(opt, "pypy2.7-v8.0.0", "bin", "pypy"))
        self.assertEqual(calls[1], ("pypy3", "built", mine))
        self.assertIn("this run's pypy2", self.out.getvalue())           # and the plan said so up front

    def test_a_named_host_does_not_chain(self):
        host = script(self.tmp, "py2/python2.7", ept.re.sub(r"exec .*", "exit 1", FAKE_HOST))
        code, calls, opt = self.go(True, "--host", host)
        self.assertEqual([c[0] for c in calls], ["pypy2", "pypy3"])     # pypy3 does not need that pypy2
        self.assertEqual(calls[1][1:], ("path", os.path.realpath(host)))


# ------------------------------------------------- whole builds, with fakes

class EndToEnd(Quiet):
    """Every stage of a real build, against the fakes above: nothing translated, nothing
    downloaded, nothing outside the scratch directory touched."""

    def make(self, series="2.7", **cfg):
        stem = "pypy%s-v8.0.0" % series
        exe_c = "pypy-c" if series == "2.7" else "pypy%s-c" % series
        version = [2, 7, 18] if series == "2.7" else [int(x) for x in series.split(".")] + [0]
        interp = fake_interp(self.tmp, "interp" + series, version=version)
        self.tarball = fake_source(self.tmp, stem, interp, exe_c, **cfg)
        self.host = script(self.tmp, "host/python2.7", FAKE_HOST)
        self.wd, self.opt = os.path.join(self.tmp, "w"), os.path.join(self.tmp, "opt")
        opts = dict(ept.DEFAULTS, source=self.tarball, offline=True, host=self.host,
                    workdir=self.wd, opt_root=self.opt, target="pypy2" if series == "2.7" else "pypy3")
        return self.ctx(opts, series)

    def ctx(self, opts, series):
        c = ept.Ctx(opts, opts["target"], self.wd, self.opt, {"pkg_manager": "apt-get"})
        c.release, c.series, c.cc, c.jobs = "8.0.0", series, dict(FAKE_CC), 2
        c.host = ept.plan_host(opts, c.target, self.opt, self.wd)
        c.src_cands, c.src_git = ept.source_candidates(ept.Catalog({}), "8.0.0", series)
        return c

    def build(self, c):
        st = ept.State(os.path.join(self.wd, "state.json"))
        ept.build_one(c, st, ept.Catalog({}), c.machine)
        return st

    def test_pypy2_from_source_to_pypy2_current(self):
        c = self.make()
        st = self.build(c)
        prefix = os.path.join(self.opt, "pypy2.7-v8.0.0")
        rec = ept.read_json(os.path.join(prefix, ept.MARKER))
        self.assertEqual((rec["state"], rec["release"], rec["series"], rec["packaged_by"]),
                         ("verified", "8.0.0", "2.7", "package.py"))
        self.assertEqual(rec["host"]["mode"], "path")
        self.assertIn("--batch", rec["rpython"])
        self.assertTrue(os.path.isfile(os.path.join(prefix, "BUILD-INFO.txt")))
        self.assertEqual(os.readlink(os.path.join(self.opt, "pypy2-current")), "pypy2.7-v8.0.0")
        with open(os.path.join(prefix, "bin", "pip")) as f:
            self.assertEqual(f.readline(), "#!%s/bin/pypy\n" % prefix)       # ensurepip ran in the stage
        b = st.build(c.key)
        self.assertEqual(b["status"], "done")
        self.assertEqual([n for n in ept.stage_plan(c) if b["stages"].get(n, {}).get("status") != "done"], [])
        self.assertEqual(b["timings"]["annotate"], 56.1)
        self.assertFalse(os.path.exists(c.usession))                       # 4 GB of C: gone after success
        self.assertFalse(os.path.exists(c.pkgdir))
        self.assertIn("phase 8/9: Compiling c source", self.out.getvalue())
        tlog = ept.read_text(os.path.join(c.logs, "translate.log"))
        self.assertIn("rpython --batch --opt=jit", tlog)
        self.assertIn("Ready in", ept.read_text(os.path.join(c.logs, "package.log")))   # kept, not /dev/null
        d = ept.status_data(self.opt, self.wd)
        e = d["installed"][0]
        self.assertEqual((e["runs"], e["built_by_tool"], e["for"]), (True, True, "python2.7"))
        self.assertEqual(d["builds"], [{"id": "pypy2.7-v8.0.0", "status": "done", "stage": "link", "error": None}])
        # the same build again: already there, verified -- nothing rebuilt
        self.out.truncate(0)
        self.build(self.ctx(c.opts, "2.7"))
        self.assertIn("already built and verified", self.out.getvalue())

    def test_pypy312_installs_setuptools_offline_first(self):
        whl = put(self.tmp, "w/downloads/setuptools-99.0.0-py3-none-any.whl", "wheel")
        fake = ("setuptools-99.0.0-py3-none-any.whl", "https://example.invalid/s.whl", ept.sha256_file(whl), "99.0.0")
        c = self.make("3.12")
        with mock.patch.object(ept, "SETUPTOOLS_WHEEL", fake):
            st = self.build(c)
        self.assertEqual(st.stage(c.key, "setuptools")["status"], "done")
        prefix = os.path.join(self.opt, "pypy3.12-v8.0.0")
        self.assertEqual(ept.find_exe(prefix)[0], os.path.join(prefix, "bin", "pypy3.12"))
        self.assertEqual(os.readlink(os.path.join(self.opt, "pypy3-current")), "pypy3.12-v8.0.0")
        self.assertIn("pip install --no-index", ept.read_text(os.path.join(c.logs, "setuptools.log")))

    def test_a_failed_translation_resumes_there_and_a_new_compiler_starts_over(self):
        c = self.make(fail="[platform:Error] x.c:1: error: boom\n[translation:ERROR] CompilationError", phases=8)
        with self.assertRaises(ept.ToolError) as cm:
            self.build(c)
        msg = str(cm.exception)
        self.assertIn("phase: Compiling c source", msg)
        self.assertIn("error: boom", msg)
        self.assertIn("--from compile", msg)
        st = ept.State(os.path.join(self.wd, "state.json"))
        self.assertEqual(st.build(c.key)["status"], "failed")
        self.assertEqual(st.build(c.key)["failed_phase"], "Compiling c source")
        cfgp = os.path.join(c.src_dir, "rpython", "bin", "fake.json")
        with open(cfgp) as f:
            cfg = json.load(f)
        cfg.pop("fail")
        put(c.src_dir, "rpython/bin/fake.json", json.dumps(cfg))
        put(c.src_dir, "SENTINEL", "the tree was kept")
        self.build(self.ctx(c.opts, "2.7"))                                  # resumes: the tree is not extracted again
        self.assertTrue(os.path.isfile(os.path.join(c.src_dir, "SENTINEL")))
        self.assertIn("extract       done earlier", self.out.getvalue())
        # a different compiler: the record's inputs changed, so the tree is made again
        interp = fake_interp(self.tmp, "interp2.7", version=[2, 7, 18])
        os.unlink(self.tarball)
        fake_source(self.tmp, "pypy2.7-v8.0.0", interp, "pypy-c")
        c2 = self.ctx(dict(c.opts, rebuild=True), "2.7")
        c2.cc = dict(FAKE_CC, banner="gcc (GCC) 16.1.0", version=(16, 1, 0))
        self.build(c2)
        self.assertFalse(os.path.exists(os.path.join(c.src_dir, "SENTINEL")))
        self.assertIn("inputs changed", self.out.getvalue())
        self.assertEqual(ept.read_json(os.path.join(c2.prefix, ept.MARKER))["compiler"], "gcc (GCC) 16.1.0")
        self.assertEqual([n for n in os.listdir(self.opt) if ".old-" in n], [])

    def test_an_interrupted_translation_is_exit_130_material(self):
        c = self.make(fail="[translation:ERROR] KeyboardInterrupt\n[translation] batch mode", phases=1)
        with self.assertRaises(ept.Interrupted):
            self.build(c)
        st = ept.State(os.path.join(self.wd, "state.json"))
        self.assertEqual(st.stage(c.key, "translate")["status"], "interrupted")

    def test_no_curses_on_the_host_names_the_package(self):
        c = self.make(fail="[translation:ERROR] Skipped: no _curses or _minimal_curses module", phases=0)
        with self.assertRaises(ept.ToolError) as cm:
            self.build(c)
        self.assertIn("ncurses: every translation needs", str(cm.exception))
        self.assertIn("libncurses-dev", str(cm.exception))                 # the machine is apt-get here

    def test_another_opt_root_verifies_its_own_stage(self):
        """The same work directory, a second --opt-root: its stage is verified before it is installed."""
        c = self.make()
        self.build(c)
        self.out.seek(0)
        self.out.truncate(0)
        self.opt = os.path.join(self.tmp, "opt2")
        c2 = self.ctx(dict(c.opts, opt_root=self.opt), "2.7")
        st = self.build(c2)
        text = self.out.getvalue()
        self.assertIn("was last built for %s" % c.prefix, text)
        self.assertIn("Verifying the staged interpreter", text)
        self.assertNotIn("verify-stage  done earlier", text)
        self.assertIn("translate     done earlier", text)                     # the translation is kept
        self.assertEqual(ept.read_json(os.path.join(c2.prefix, ept.MARKER))["state"], "verified")
        self.assertEqual(st.build(c2.key)["prefix"], c2.prefix)

    def test_out_of_memory_says_so(self):
        c = self.make(fail="Killed", rc=137, phases=2)
        with self.assertRaises(ept.ToolError) as cm:
            self.build(c)
        self.assertIn("OUT OF MEMORY", str(cm.exception))
        self.assertIn("--low-memory", str(cm.exception))

    def test_package_failure_falls_back_to_assembly_by_hand_loudly(self):
        c = self.make()
        st = ept.State(os.path.join(self.wd, "state.json"))
        orig = ept.do_extract

        def extract_then_break(ctx):
            orig(ctx)
            put(ctx.src_dir, "FAIL_PACKAGE", "x")
        with mock.patch.object(ept, "do_extract", extract_then_break):
            ept.build_one(c, st, ept.Catalog({}), c.machine)
        self.assertIn("package.py FAILED", self.out.getvalue())
        self.assertIn("!!!!", self.out.getvalue())
        rec = ept.read_json(os.path.join(c.prefix, ept.MARKER))
        self.assertEqual(rec["packaged_by"], "by hand (package.py failed)")
        self.assertEqual(rec["state"], "verified")

    def test_a_foreign_prefix_is_refused(self):
        c = self.make()
        os.makedirs(os.path.join(self.opt, "pypy2.7-v8.0.0"))
        with self.assertRaises(ept.ToolError) as cm:
            self.build(c)
        self.assertIn("not a build of this tool's", str(cm.exception))

    def test_smoke_translation_with_the_fakes(self):
        c = self.make()
        st = ept.State(os.path.join(self.wd, "state.json"))
        ept.run_stage(c, st, "fetch", lambda: ept.do_fetch(c, st, ept.Catalog({})), always=True)
        ept.run_stage(c, st, "host", lambda: ept.do_host(c, st, ept.Catalog({})), always=True)
        ept.do_extract(c)
        secs = ept.smoke_test(c.host, c.src_dir, c.cc, "", self.wd, logs=c.logs)
        self.assertGreaterEqual(secs, 0)
        self.assertIn("smoke test passed", self.out.getvalue())
        self.assertIn("--opt=2", ept.read_text(os.path.join(c.logs, "smoke.log")))
        ept.smoke_test(c.host, c.src_dir, c.cc, "", self.wd, jit=True, logs=c.logs)
        self.assertIn("its JIT works", self.out.getvalue())
        self.assertIn("--opt=jit", ept.read_text(os.path.join(c.logs, "smoke.log")))
        with open(os.path.join(self.wd, "smoke", "square.tlr"), "rb") as f:
            self.assertEqual(f.read(), b"square bytecode")              # written by the host from tlr.SQUARE


if __name__ == "__main__":
    unittest.main()
