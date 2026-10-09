#!/usr/bin/python3 -I
"""Tests for enterprise_gcc_tool.py.  stdlib unittest; no network (fake mirrors, file://),
no GCC is compiled (fake source trees whose Makefiles the real `make` runs), and they pass
when run as root: the tool refuses root, so the tests that drive `build` say they are uid
1000.  The end-to-end binutils run assembles and links through wrappers around the system's
own binutils."""

import ast
import gzip
import io
import json
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_FILE = os.path.abspath(os.path.join(HERE, "..", "enterprise_gcc_tool.py"))
TOOLKIT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.dirname(TOOL_FILE))
import enterprise_gcc_tool as egt          # noqa: E402

REAL_OUT = egt.out                         # TempRoot replaces it; Captured puts it back



def _gnu_as():
    try:
        return "GNU" in subprocess.run(["/usr/bin/as", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       universal_newlines=True).stdout
    except OSError:
        return False


GNU = os.path.isfile("/usr/bin/gcc") and os.path.isfile("/usr/bin/g++") and _gnu_as() and bool(shutil.which("make"))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


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


def fake_gcc(root, rel="bin/gcc", version="14.2.1", banner="gcc (GCC) 14.2.1 20250110"):
    """A script that answers like gcc does to --version and -dumpfullversion."""
    return script(root, rel, 'case "$1" in\n  --version) echo "%s"; echo "Copyright (C) 2024 Free Software '
                  'Foundation, Inc.";;\n  -dumpfullversion|-dumpversion) echo %s;;\n  *) exit 0;;\nesac\n'
                  % (banner, version))


def machine(**over):
    tools = dict((t, "/usr/bin/" + t) for t in egt.BUILD_TOOLS)
    m = {"os": "Test Linux 1", "os_id": "fedora", "os_like": "", "os_version": "43", "arch": "x86_64", "cpus": 8,
         "cpu_model": "Test CPU", "cpu_level": "x86-64-v3", "mem_total_gb": 32.0, "mem_avail_gb": 24.0,
         "pkg_manager": "dnf", "host_compilers": [], "tools": tools,
         "binutils": [{"dir": "/usr/bin", "version": (2, 45), "banner": "GNU assembler 2.45", "system": True, "gold": True}],
         "headers": {"zlib": True, "zstd": True, "libc": True, "multilib": False, "gmp": False, "mpfr": False,
                     "mpc": False, "isl": False}}
    m.update(over)
    return m


def opts(**over):
    return egt.check_options(dict(egt.DEFAULTS, **over))


HOST = {"path": "/usr/bin/gcc", "cxx": "/usr/bin/g++", "kind": "gcc", "version": (14, 2, 1),
        "banner": "gcc (GCC) 14.2.1", "origin": "system", "system": True}


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="egt-test-")
        self.printed = []
        patches = [mock.patch.object(egt, "out", lambda text="": self.printed.append(text)),
                   mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.root, "config"),
                                                "SATELLITE_TOOLKIT_WHERE": "local"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        while egt._LOCKS:                      # the tool holds its locks until it exits; a test does not
            egt._LOCKS.pop().close()
        shutil.rmtree(self.root, ignore_errors=True)

    def said(self):
        return "\n".join(self.printed)

    @staticmethod
    def release_locks():
        """A build holds its directory's lock until its process exits; two builds in one
        test process are two processes in real life."""
        while egt._LOCKS:
            egt._LOCKS.pop().close()


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
        self.assertEqual(egt.PLATFORM_PYTHON, "/usr/bin/python3")


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
        tool_line = next(i for i, l in enumerate(self.lines) if l.startswith("TOOL = "))
        self.assertLess(tool_line, begins[0])
        self.assertEqual(egt.TOOL, "enterprise_gcc_tool")

    def test_session_short_name_is_gcc(self):
        if not hasattr(egt, "_sr_session_name"):
            self.skipTest("the block is the stand-in: sync_remote.py has not filled it")
        self.assertTrue(egt._sr_session_name(egt.TOOL).startswith("gcc-"))

    def test_block_is_what_sync_remote_writes(self):
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
        self.assertIsInstance(first, ast.Assign)
        self.assertEqual(first.targets[0].id, "argv")
        self.assertEqual(first.value.func.id, "where_to_run")
        args = [ast.dump(a) for a in first.value.args]
        self.assertIn("__file__", args[1])
        self.assertIn("TOOL", args[2])
        self.assertIn("sys", args[0])                          # list(sys.argv[1:] if argv is None else argv)


# ------------------------------------------------------------------ listings, mirrors

RELEASES = ('<a href="gcc-9.5.0/">gcc-9.5.0/</a>\n<a href="gcc-16.2.0/">gcc-16.2.0/</a>\n'
            '<a href="gcc-15.1.0/">gcc-15.1.0/</a> <a href="gcc-16.1.0/">gcc-16.1.0/</a>\n<a href="gcc-3.4.6/">x</a>')
SNAPS = ('<a href="LATEST-17/">LATEST-17/</a> <a href="17-20260816/">17-20260816/</a> <a href="17-20260823/">'
         '17-20260823/</a> <a href="16-20260820/">16-20260820/</a> <a href="9-20230101/">9-20230101/</a>')
BINUTILS = ("binutils-2.43.tar.xz binutils-2.43.tar.xz.sig binutils-2.44.tar.xz binutils-with-gold-2.44.tar.xz "
            "binutils-2.46.tar.xz binutils-with-gold-2.46.tar.xz binutils-with-gold-2.46.tar.xz.sig "
            "binutils-2.47.tar.xz binutils-2.45.1.tar.xz binutils-with-gold-2.45.1.tar.xz")


class Listings(unittest.TestCase):
    def test_releases_sort_numerically_newest_first(self):
        self.assertEqual(egt.parse_gcc_releases(RELEASES), ["16.2.0", "16.1.0", "15.1.0", "9.5.0", "3.4.6"])

    def test_gzip_bodies_are_undone_with_or_without_the_header(self):
        z = gzip.compress(RELEASES.encode())
        self.assertEqual(egt.decode_body(z, "gzip").decode(), RELEASES)          # Content-Encoding: gzip
        self.assertEqual(egt.decode_body(z, "").decode(), RELEASES)              # a proxy dropped the header
        self.assertEqual(egt.decode_body(b"plain", ""), b"plain")
        with self.assertRaises(egt.ToolError):
            egt.decode_body(b"\x1f\x8bnot really gzip", "gzip")
        # undecoded, the gzip trap: binary that matches nothing and looks like "no versions"
        self.assertEqual(egt.parse_gcc_releases(z.decode("latin-1")), [])

    def test_snapshots_by_series_and_auto_is_the_highest(self):
        self.assertEqual(egt.parse_snapshots(SNAPS, "17"), ["17-20260823", "17-20260816"])
        self.assertEqual(egt.parse_snapshots(SNAPS, "auto")[0], "17-20260823")
        self.assertEqual(egt.parse_snapshots(SNAPS, "16"), ["16-20260820"])
        self.assertNotIn("LATEST-17", " ".join(egt.parse_snapshots(SNAPS)))

    def test_binutils_with_and_without_gold(self):
        found = egt.parse_binutils(BINUTILS)
        self.assertEqual(found["2.44"], {"plain": True, "gold": True})
        self.assertEqual(found["2.47"], {"plain": True, "gold": False})
        self.assertEqual(found["2.43"], {"plain": True, "gold": False})
        self.assertEqual(egt.binutils_pick(found, gold=True), "2.46")     # 2.47 has no gold tarball: skipped
        self.assertEqual(egt.binutils_pick(found, gold=False), "2.47")
        self.assertEqual(egt.binutils_pick(egt.parse_binutils("binutils-2.43.tar.xz"), gold=True), "2.43")  # gold inside
        self.assertEqual(egt.binutils_pick(egt.parse_binutils("binutils-2.47.tar.xz"), gold=True), "")

    def test_first_listing_falls_through_dead_and_useless_mirrors(self):
        calls = []

        def get(url):
            calls.append(url)
            if "one" in url:
                raise egt.ToolError("cannot fetch %s: 403" % url)
            if "two" in url:
                return b"<html>a proxy's block page</html>"    # answers, but with nothing usable
            return RELEASES.encode()
        found, mirror = egt.first_listing(("https://one", "https://two", "https://three"), egt.parse_gcc_releases, get)
        self.assertEqual((found[0], mirror), ("16.2.0", "https://three"))
        self.assertEqual(calls, ["https://one/", "https://two/", "https://three/"])
        with self.assertRaises(egt.ToolError) as e:
            egt.first_listing(("https://one", "https://two"), egt.parse_gcc_releases, get)
        self.assertIn("https://one", str(e.exception))
        self.assertIn("lists nothing", str(e.exception))

    def test_the_newest_release_is_confirmed_with_a_head(self):
        get = lambda url: RELEASES.encode()
        v, _ = egt.latest_gcc_release(get, exists=lambda url: "16.2.0" not in url)
        self.assertEqual(v, "16.1.0")                          # 16.2.0's directory, no tarball yet
        with mock.patch.object(egt, "out"):
            v, _ = egt.latest_gcc_release(get, exists=lambda url: False)
        self.assertEqual(v, "16.2.0")                          # nothing confirms: the newest, with a note
        self.assertEqual(egt.latest_snapshot("auto", lambda url: SNAPS.encode())[0], "17-20260823")
        self.assertEqual(egt.latest_snapshot("16", lambda url: SNAPS.encode())[0], "16-20260820")

    def test_released_majors(self):
        self.assertEqual(egt.released_majors(lambda url: RELEASES.encode()), set([16, 15, 9, 3]))

        def down(url):
            raise egt.ToolError("down")
        self.assertEqual(egt.released_majors(down), set())

    def test_versions_report_keeps_each_source_apart(self):
        def get(url):
            if "snapshots" in url:
                raise egt.ToolError("cannot fetch %s: 403" % url)
            if "binutils" in url:
                return BINUTILS.encode()
            if "BASE-VER" in url:
                return b"17.0.0\n"
            return gzip.compress(RELEASES.encode())
        rep = dict((t.split()[1] if t.startswith("GCC") else t.split()[0], (lines, err))
                   for t, lines, err in egt.versions_report(3, get))
        self.assertIn("16.2.0  16.1.0  15.1.0", rep["releases"][0][0])
        self.assertIsNone(rep["snapshots,"][0])
        self.assertIn("403", rep["snapshots,"][1])
        self.assertEqual(rep["trunk"][0], ["BASE-VER 17.0.0"])
        self.assertTrue(any(l.startswith("2.46") and "with-gold" in l for l in rep["binutils"][0]))
        self.assertTrue(any(l.startswith("2.47") and "NO gold" in l for l in rep["binutils"][0]))


class Downloads(TempRoot):
    def xz(self, name, payload=b"x" * 1000):
        return put(self.root, "srv/" + name, "") and self._write(os.path.join(self.root, "srv", name), lzma.compress(payload))

    def _write(self, path, data):
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_download_goes_through_part_then_rename(self):
        src = self.xz("a.tar.xz")
        dest = os.path.join(self.root, "dl", "a.tar.xz")
        os.makedirs(os.path.dirname(dest))
        egt.download("file://" + src, dest)
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        with self.assertRaises(egt.ToolError):
            egt.download("file://" + os.path.join(self.root, "nothing-here"), dest + "2")
        self.assertFalse(os.path.exists(dest + "2"))

    def test_mirror_fallback(self):
        dest = os.path.join(self.root, "t.tar.xz")
        tried = []

        def fetch(url, d):
            tried.append(url)
            with open(d + ".part", "w") as f:
                f.write("half")
            if "bad" in url:
                raise egt.ToolError("download failed: %s\n  HTTP Error 404" % url)
            os.replace(d + ".part", d)
        used = egt.download_from(["https://bad/1", "https://good/2"], dest, fetch)
        self.assertEqual(used, "https://good/2")
        self.assertEqual(tried, ["https://bad/1", "https://good/2"])
        self.assertIn("that mirror failed, trying the next one", self.said())
        self.assertFalse(os.path.exists(dest + ".part"))
        os.unlink(dest)
        with self.assertRaises(egt.ToolError) as e:
            egt.download_from(["https://bad/1", "https://bad/2"], dest, fetch)
        self.assertIn("https://bad/2", str(e.exception))
        self.assertFalse(os.path.exists(dest))

    def test_a_transient_failure_is_retried_on_the_same_mirror_a_404_is_not(self):
        """build_gcc_universal.sh:240: curl -fL --retry 3 --retry-delay 5 on every mirror."""
        dest = os.path.join(self.root, "t.tar.xz")
        tried = []
        blips = {"https://flaky/1": 2, "https://dead/1": 99}

        def fetch(url, d):
            tried.append(url)
            with open(d + ".part", "w") as f:
                f.write("half")
            if "404" in url:
                raise egt.DownloadError("download failed: %s\n  HTTP Error 404: Not Found" % url)
            if blips.get(url, 0) > 0:
                blips[url] -= 1
                raise egt.DownloadError("download failed: %s\n  [Errno 104] Connection reset by peer" % url, True)
            os.replace(d + ".part", d)
        with mock.patch.object(egt, "DOWNLOAD_RETRY_DELAY", 0), mock.patch.object(egt.time, "sleep") as slept:
            self.assertEqual(egt.download_from(["https://flaky/1", "https://good/2"], dest, fetch), "https://flaky/1")
            self.assertEqual(tried, ["https://flaky/1"] * 3)                 # two blips, then it came
            self.assertEqual(slept.call_count, 2)
            self.assertIn("trying it again in 0s (1 of 3)", self.said())
            os.unlink(dest)
            tried[:] = []
            self.assertEqual(egt.download_from(["https://404/1", "https://good/2"], dest, fetch), "https://good/2")
            self.assertEqual(tried, ["https://404/1", "https://good/2"])     # a 404 is final at once
            os.unlink(dest)
            tried[:] = []
            self.assertEqual(egt.download_from(["https://dead/1", "https://good/2"], dest, fetch), "https://good/2")
            self.assertEqual(tried, ["https://dead/1"] * 4 + ["https://good/2"])   # 1 + --retry 3, then the next
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_what_counts_as_transient(self):
        import socket
        import urllib.error
        import http.client
        yes = [urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),
               urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
               urllib.error.URLError(socket.timeout("timed out")), socket.timeout("timed out"),
               ConnectionResetError(104, "Connection reset by peer"), http.client.IncompleteRead(b"x", 10),
               http.client.RemoteDisconnected("closed")]
        no = [urllib.error.HTTPError("u", 404, "Not Found", {}, None),
              urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
              urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
              urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
              urllib.error.URLError(FileNotFoundError(2, "No such file")), OSError(28, "No space left on device"),
              ValueError("unknown url type")]
        for e in yes:
            self.assertTrue(egt.transient_error(e), repr(e))
        for e in no:
            self.assertFalse(egt.transient_error(e), repr(e))

    def test_download_tells_a_503_and_a_short_body_from_a_404_over_http(self):
        """The real download() against a server on 127.0.0.1 (no outside network)."""
        import http.server
        import threading
        hits = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits[self.path] = hits.get(self.path, 0) + 1
                if self.path == "/busy" and hits[self.path] == 1:
                    self.send_error(503)
                elif self.path == "/short" and hits[self.path] == 1:
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"x" * 10)
                    self.close_connection = True
                elif self.path in ("/busy", "/short"):
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"whole")
                else:
                    self.send_error(404)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        dest = os.path.join(self.root, "f")
        with mock.patch.object(egt, "DOWNLOAD_RETRY_DELAY", 0), \
                mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            for path in ("/busy", "/short"):
                hits.clear()
                with self.assertRaises(egt.DownloadError) as e:
                    egt.download(base + path, dest)
                self.assertTrue(e.exception.transient, path)
                hits.clear()
                self.assertEqual(egt.download_from([base + path], dest), base + path)
                self.assertEqual(hits, {path: 2})                       # failed once, retried, whole
                self.assertEqual(egt.read_text(dest), "whole")
                os.unlink(dest)
            hits.clear()
            with self.assertRaises(egt.DownloadError) as e:
                egt.download(base + "/gone", dest)
            self.assertFalse(e.exception.transient)
            with self.assertRaises(egt.ToolError):
                egt.download_from([base + "/gone"], dest)
        self.assertEqual(hits, {"/gone": 2})                            # once each: never retried
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_a_file_that_fails_its_check_is_deleted_and_the_next_mirror_tried(self):
        dest = os.path.join(self.root, "gmp.tar.bz2")

        def fetch(url, d):
            with open(d, "w") as f:
                f.write("<html>404</html>" if "first" in url else "the real thing")
        used = egt.download_from(["https://first/x", "https://second/x"], dest, fetch,
                                 check=lambda p: (egt.read_text(p) == "the real thing", "sha512 does not match"))
        self.assertEqual(used, "https://second/x")
        self.assertEqual(egt.read_text(dest), "the real thing")

    def test_xz_integrity(self):
        good = self.xz("g.tar.xz")
        self.assertTrue(egt.xz_ok(good)[0])
        data = read_bytes(good)
        bad = self._write(os.path.join(self.root, "b.tar.xz"), data[:len(data) // 2])
        self.assertFalse(egt.xz_ok(bad)[0])

    def test_fetch_tarball_checks_sha512_and_refetches_a_corrupt_one_once(self):
        payload = lzma.compress(b"gcc" * 100)
        digest = __import__("hashlib").sha512(payload).hexdigest()
        dest = os.path.join(self.root, "gcc-16.2.0.tar.xz")
        self._write(dest, b"not xz at all")                    # a corrupt one left from before
        fetched = []

        def fetch(url, d):
            fetched.append(url)
            self._write(d, payload)
        get = lambda url: ("%s  gcc-16.2.0.tar.xz\n" % digest).encode()
        egt.fetch_tarball(["https://m/gcc-16.2.0.tar.xz"], dest, ["https://m"], get, fetch)
        self.assertEqual(fetched, ["https://m/gcc-16.2.0.tar.xz"])     # once
        self.assertEqual(read_bytes(dest), payload)
        self.assertIn("sha512 matches", self.said())
        wrong = lambda url: ("%s  gcc-16.2.0.tar.xz\n" % ("0" * 128)).encode()
        with self.assertRaises(egt.ToolError) as e:
            egt.fetch_tarball(["https://m/gcc-16.2.0.tar.xz"], dest, ["https://m"], wrong, fetch)
        self.assertIn("twice", str(e.exception))
        self.assertFalse(os.path.exists(dest))

    def test_no_published_sum_says_so(self):
        dest = os.path.join(self.root, "binutils-2.46.tar.xz")
        egt.fetch_tarball(["https://m/x"], dest, [], None, lambda url, d: self._write(d, lzma.compress(b"b")))
        self.assertIn("integrity rests on TLS", self.said())

    def test_parse_sums(self):
        sums = egt.parse_sums("%s  gmp-6.2.1.tar.bz2\n%s *isl-0.24.tar.bz2\nnoise\n" % ("a" * 128, "B" * 128))
        self.assertEqual(sums, {"gmp-6.2.1.tar.bz2": "a" * 128, "isl-0.24.tar.bz2": "b" * 128})


# ------------------------------------------------------------------ choosing the source

class Options(TempRoot):
    def test_track_and_version_are_checked(self):
        for over, word in (({"track": "nightly"}, "--track"), ({"mode": "fast"}, "--mode"),
                           ({"gcc_version": "16.2"}, "16.2.0"), ({"track": "snapshot", "gcc_version": "16.2.0"}, "17-2026"),
                           ({"track": "git", "gcc_version": "16.2.0"}, "--git-ref"), ({"series": "trunk"}, "--series"),
                           ({"languages": "c, c++"}, "--languages"), ({"resume": True, "restart": True}, "opposite"),
                           ({"build_id": "../x"}, "NAME"), ({"binutils_version": "two"}, "2.46"),
                           ({"with_arch": "x86-64 v3"}, "one word"), ({"prereqs": "both"}, "--prereqs"),
                           ({"component": "llvm"}, "--component"), ({"configure_extra": "'unclosed"}, "configure-extra")):
            with self.assertRaises(egt.UsageError, msg=str(over)) as e:
                opts(**over)
            self.assertIn(word, str(e.exception))
        self.assertEqual(opts(track="snapshot", gcc_version="17-20260823")["gcc_version"], "17-20260823")
        self.assertEqual(opts(component="binutils", gcc_version="junk-ignored")["component"], "binutils")

    def test_release_and_snapshot_urls(self):
        s = egt.gcc_source(opts(gcc_version="16.2.0"), self.root)
        self.assertEqual(s["urls"], ["https://ftp.gnu.org/gnu/gcc/gcc-16.2.0/gcc-16.2.0.tar.xz",
                                     "https://mirrors.kernel.org/gnu/gcc/gcc-16.2.0/gcc-16.2.0.tar.xz",
                                     "https://ftpmirror.gnu.org/gcc/gcc-16.2.0/gcc-16.2.0.tar.xz"])
        self.assertEqual((s["id"], s["dir"]), ("16.2.0", os.path.join(self.root, "src", "gcc-16.2.0")))
        s = egt.gcc_source(opts(track="snapshot", gcc_version="17-20260823"), self.root)
        self.assertEqual(s["urls"][0], "https://gcc.gnu.org/pub/gcc/snapshots/17-20260823/gcc-17-20260823.tar.xz")
        self.assertEqual(s["sum_dirs"][1], "https://mirrors.kernel.org/sourceware/gcc/snapshots/17-20260823")

    def test_auto_discovers_and_offline_falls_back_to_what_is_here(self):
        s = egt.gcc_source(opts(), self.root, get=lambda url: RELEASES.encode(), exists=lambda url: True)
        self.assertEqual(s["id"], "16.2.0")

        def down(url):
            raise egt.ToolError("cannot fetch %s: Tunnel connection failed: 403" % url)
        with self.assertRaises(egt.ToolError) as e:
            egt.gcc_source(opts(), self.root, get=down)
        self.assertIn("--gcc-version 16.2.0", str(e.exception))
        put(self.root, "src/gcc-15.2.0/configure")
        put(self.root, "src/gcc-9.5.0/configure")
        self.assertEqual(egt.gcc_source(opts(), self.root, get=down)["id"], "15.2.0")
        put(self.root, "src/gcc-17-20260823/configure")
        self.assertEqual(egt.gcc_source(opts(track="snapshot", series="17"), self.root, get=down)["id"], "17-20260823")

    def test_git_track_is_named_after_the_ref_until_cloned_then_after_the_tree(self):
        s = egt.gcc_source(opts(track="git", git_ref="releases/gcc-16"), self.root)
        self.assertEqual(s["dir"], os.path.join(self.root, "src", "gcc-git-releases_gcc-16"))
        self.assertEqual(s["id"], "git-releases_gcc-16")
        put(s["dir"], "gcc/BASE-VER", "16.2.1\n")
        self.assertEqual(egt.git_id(s["dir"]), "16.2.1-git-unknown")
        self.assertTrue(egt.is_sha("4f2a9c1"))
        self.assertFalse(egt.is_sha("master"))

    def test_binutils_tarball_names(self):
        b = egt.binutils_source(opts(component="binutils", binutils_version="2.46"), self.root)
        self.assertTrue(b["dir"].endswith("/binutils-with-gold-2.46"))
        self.assertEqual(b["urls"][0], "https://ftp.gnu.org/gnu/binutils/binutils-with-gold-2.46.tar.xz")
        self.assertEqual(b["urls"][1], "https://sourceware.org/pub/binutils/releases/binutils-with-gold-2.46.tar.xz")
        self.assertEqual(len(b["urls"]), 4)
        self.assertTrue(egt.binutils_source(opts(component="binutils", binutils_version="2.43"), self.root)["dir"]
                        .endswith("/binutils-2.43"))                    # gold still inside before 2.44
        self.assertTrue(egt.binutils_source(opts(component="binutils", binutils_version="2.46", gold=False),
                                            self.root)["dir"].endswith("/binutils-2.46"))
        b = egt.binutils_source(opts(component="binutils"), self.root, get=lambda url: BINUTILS.encode())
        self.assertEqual(b["id"], "2.46")

    def test_trees_say_what_they_are(self):
        rel = os.path.join(self.root, "rel")
        put(rel, "gcc/BASE-VER", "16.2.0\n")
        put(rel, "gcc/DEV-PHASE", "\n")
        self.assertEqual(egt.local_gcc_id(rel), "16.2.0")
        snap = os.path.join(self.root, "snap")
        put(snap, "gcc/BASE-VER", "17.0.0\n")
        put(snap, "gcc/DEV-PHASE", "experimental\n")
        put(snap, "gcc/DATESTAMP", "20260823\n")
        self.assertEqual(egt.local_gcc_id(snap), "17-20260823")
        with self.assertRaises(egt.ToolError):
            egt.local_gcc_id(self.root)
        bu = os.path.join(self.root, "bu")
        put(bu, "bfd/version.m4", "m4_define([BFD_VERSION], [2.46])\n")
        self.assertEqual(egt.binutils_tree_version(bu), "2.46")


# ------------------------------------------------------------------ configure and make

class Lines(unittest.TestCase):
    def test_mode_table(self):
        got = dict((k, (v["target"], v["config"], v["disk"])) for k, v in egt.MODES.items())
        self.assertEqual(got, {"profiled-lto": ("profiledbootstrap", "bootstrap-lto", 30),
                               "lto": ("bootstrap", "bootstrap-lto", 30),
                               "plain": ("bootstrap", "", 15), "quick": ("", "", 10)})
        self.assertEqual(egt.disk_needed("gcc", "plain", "git"), 20)
        self.assertEqual(egt.disk_needed("gcc", "quick", "release"), 10)
        self.assertEqual(egt.disk_needed("binutils", "profiled-lto", "git"), egt.BINUTILS_DISK)

    def test_make_lines(self):
        boot = "-march=native -mtune=native"
        self.assertEqual(egt.gcc_make_args("profiled-lto", 8, boot),
                         ["make", "-j8", "BUILD_CONFIG=bootstrap-lto", "BOOT_CFLAGS=-O2 -g " + boot,
                          "BOOT_CXXFLAGS=-O2 -g " + boot, "profiledbootstrap"])
        self.assertEqual(egt.gcc_make_args("lto", 2, ""), ["make", "-j2", "BUILD_CONFIG=bootstrap-lto", "bootstrap"])
        self.assertEqual(egt.gcc_make_args("plain", 4, ""), ["make", "-j4", "bootstrap"])
        self.assertEqual(egt.gcc_make_args("quick", 4, ""), ["make", "-j4"])

    def test_the_sh_configure_line_in_its_order(self):
        a = egt.gcc_configure_args("/p", "c,c++,lto", "native", "", "/usr/bin/gcc", "/usr/bin/g++", False, "profiled-lto")
        self.assertEqual(a, ["--prefix=/p", "--enable-languages=c,c++,lto", "--enable-checking=release",
                             "--enable-threads=posix", "--enable-__cxa_atexit", "--enable-lto", "--enable-plugin",
                             "--enable-default-pie", "--with-system-zlib", "--with-linker-hash-style=gnu",
                             "--disable-werror", "--with-tune=native", "CC=/usr/bin/gcc", "CXX=/usr/bin/g++",
                             "--disable-multilib"])

    def test_configure_variations(self):
        a = egt.gcc_configure_args("/p", "c,c++,jit", "", "x86-64-v3", "/cc", "/cxx", True, "quick",
                                   pair="/opt/binutils-2.46", extra="--with-gmp=/x --program-suffix='-16'")
        self.assertIn("--with-arch=x86-64-v3", a)
        self.assertNotIn("--disable-multilib", a)
        self.assertIn("--disable-bootstrap", a)
        self.assertFalse(any(x.startswith("--with-tune") for x in a))
        self.assertIn("--with-as=/opt/binutils-2.46/bin/as", a)
        self.assertIn("--with-ld=/opt/binutils-2.46/bin/ld", a)
        self.assertIn("--enable-host-shared", a)
        self.assertEqual(a[-2:], ["--with-gmp=/x", "--program-suffix=-16"])
        b = egt.gcc_configure_args("/p", "c", "", "", "/cc", "/cxx", False, "plain")
        self.assertFalse(any(x.startswith(("--with-arch", "--with-as", "--enable-host-shared", "--disable-bootstrap"))
                             for x in b))

    def test_binutils_configure_is_reboot_txt(self):
        a = egt.binutils_configure_args("/home/u/opt/binutils-2.46", True, "-g -O3 -march=native", "/usr/bin/gcc", "/usr/bin/g++")
        self.assertEqual(a, ["--prefix=/home/u/opt/binutils-2.46", "--enable-plugins", "--enable-lto", "--enable-gold",
                             "CC=/usr/bin/gcc", "CXX=/usr/bin/g++", "CFLAGS=-g -O3 -march=native",
                             "CXXFLAGS=-g -O3 -march=native"])
        self.assertNotIn("--enable-gold", egt.binutils_configure_args("/p", False, "-g -O3", "/c", "/cx"))
        self.assertFalse(any("gold=default" in x for x in a))

    def test_binutils_tune(self):
        self.assertEqual(egt.binutils_cflags("native", "x86_64"), "-g -O3 -march=native")
        self.assertEqual(egt.binutils_cflags("portable", "x86_64"), "-g -O3")
        self.assertEqual(egt.binutils_cflags("x86-64-v3", "x86_64"), "-g -O3 -march=x86-64-v3")
        self.assertEqual(egt.binutils_cflags("-mcpu=neoverse-v2", "aarch64"), "-g -O3 -mcpu=neoverse-v2")
        self.assertEqual(egt.binutils_cflags("native", "ppc64le"), "-g -O3 -mcpu=native")

    def test_with_tune_default_per_architecture(self):
        self.assertEqual(egt.with_tune_value("auto", "x86_64"), "native")
        self.assertEqual(egt.with_tune_value("auto", "aarch64"), "native")
        self.assertEqual(egt.with_tune_value("auto", "s390x"), "")
        self.assertEqual(egt.with_tune_value("none", "x86_64"), "")
        self.assertEqual(egt.with_tune_value("znver4", "x86_64"), "znver4")

    def test_languages_are_checked_against_the_tree(self):
        src = tempfile.mkdtemp(prefix="egt-test-")
        self.addCleanup(shutil.rmtree, src)
        for d, name in (("c", "c"), ("cp", "c++"), ("fortran", "fortran"), ("lto", "lto")):
            put(src, "gcc/%s/config-lang.in" % d, '# x\nlanguage="%s"\n' % name)
        egt.check_languages("c,c++,lto,fortran", src)
        egt.check_languages("all", src)
        with self.assertRaises(egt.ToolError) as e:
            egt.check_languages("c,rust", src)
        self.assertIn("rust", str(e.exception))
        egt.check_languages("c,rust", os.path.join(src, "nowhere"))      # a tree without the files: not judged


# ------------------------------------------------------------------ the environment

class Environment(TempRoot):
    TRAPS = {"LANGUAGES": "c,c++,lto", "CC": "clang", "CXX": "clang++", "CFLAGS": "-O0", "LD_RUN_PATH": "/x/lib",
             "LD_LIBRARY_PATH": "/x/lib", "MAKEFLAGS": "-j99", "LIBRARY_PATH": "/x", "CPATH": "/x/include",
             "CFLAGS_FOR_TARGET": "-O0", "AS": "/x/as", "DESTDIR": "/oops", "https_proxy": "http://proxy:3128"}

    def test_the_build_environment_is_an_allow_list(self):
        with mock.patch.dict(os.environ, self.TRAPS):
            env = egt.scrubbed_env(["/pair/bin"])
        for var in self.TRAPS:
            if var != "https_proxy":
                self.assertNotIn(var, env)
        self.assertEqual(env["https_proxy"], "http://proxy:3128")
        self.assertEqual(env["PATH"], "/pair/bin:/usr/bin:/usr/sbin:/bin:/sbin")
        self.assertNotIn("/usr/local/bin", env["PATH"])
        self.assertEqual(env["LC_ALL"], "C")

    def test_env_traps_names_them(self):
        found = " ".join(egt.env_traps(dict(self.TRAPS, HOME="/home/u")))
        for var in ("LANGUAGES=", "CC=clang", "LD_RUN_PATH=", "MAKEFLAGS=", "CFLAGS_FOR_TARGET=", "AS="):
            self.assertIn(var, found)
        self.assertNotIn("HOME", found)
        self.assertNotIn("https_proxy", found)

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_languages_never_reaches_make(self):
        # HANDOFF trap 6: an exported LANGUAGES beats gcc/Makefile's own and kills stage 1.
        put(self.root, "Makefile", "all:\n\t@echo \"lang=[$(LANGUAGES)] cc=[$${CC-unset}] flags=[$(MAKEFLAGS)]\"\n")
        log = os.path.join(self.root, "logs", "m.log")
        with mock.patch.dict(os.environ, self.TRAPS):
            rc, _ = egt.run_logged(["make", "-s"], self.root, egt.scrubbed_env(), log, "make")
        self.assertEqual(rc, 0)
        self.assertIn("lang=[] cc=[unset]", egt.read_text(log))
        self.assertNotIn("-j99", egt.read_text(log))

    def test_run_logged_keeps_the_previous_log_and_echoes_errors(self):
        log = os.path.join(self.root, "logs", "x.log")
        egt.run_logged(["sh", "-c", "echo first"], self.root, egt.scrubbed_env(), log, "x")
        rc, _ = egt.run_logged(["sh", "-c", "echo 'foo.c:1: error: bad'; echo 'warning: meh'; exit 3"],
                               self.root, egt.scrubbed_env(), log, "x")
        self.assertEqual(rc, 3)
        self.assertIn("first", egt.read_text(log + ".prev"))
        self.assertIn("foo.c:1: error: bad", self.said())
        self.assertNotIn("warning: meh", self.said())


class HostCompiler(TempRoot):
    def test_a_versioned_gcc_gets_its_own_gxx(self):
        cc = fake_gcc(self.root, "bin/gcc-14")
        cxx = fake_gcc(self.root, "bin/g++-14")
        fake_gcc(self.root, "bin/g++")                          # the .sh's ${HOST_CC%gcc}g++ would miss this one
        h = egt.pick_host(opts(host_cc=cc), machine())
        self.assertEqual((h["path"], h["cxx"], h["kind"], h["version"]), (cc, cxx, "gcc", (14, 2, 1)))
        self.assertEqual(egt.cxx_partner(os.path.join(self.root, "nowhere", "gcc-13"), "gcc"), "")    # not there
        d = os.path.join(self.root, "bin")
        fake_gcc(self.root, "bin/x86_64-linux-gnu-g++-13")
        self.assertEqual(egt.cxx_partner(os.path.join(d, "x86_64-linux-gnu-gcc-13"), "gcc"),
                         os.path.join(d, "x86_64-linux-gnu-g++-13"))

    def test_a_forced_host_must_be_a_compiler_with_a_partner(self):
        with self.assertRaises(egt.ToolError):
            egt.pick_host(opts(host_cc=script(self.root, "bin/notcc", "echo hello\n")), machine())
        lone = fake_gcc(self.root, "lone/gcc-15")
        with self.assertRaises(egt.ToolError) as e:
            egt.pick_host(opts(host_cc=lone), machine())
        self.assertIn("--host-cxx", str(e.exception))
        cxx = fake_gcc(self.root, "elsewhere/my-g++")
        self.assertEqual(egt.pick_host(opts(host_cc=lone, host_cxx=cxx), machine())["cxx"], cxx)

    def test_an_exported_cc_is_ignored_with_the_warning(self):
        w = egt.host_warnings(HOST, {"CC": "clang"})
        self.assertEqual(len(w), 1)
        self.assertIn("ignoring CC=clang inherited from your environment", w[0])
        self.assertEqual(egt.host_warnings(HOST, {"CC": "/usr/bin/gcc"}), [])
        self.assertEqual(egt.host_warnings(HOST, {}), [])
        clang = dict(HOST, kind="clang", path="/usr/bin/clang")
        self.assertIn("LLVMgold.so", egt.host_warnings(clang, {})[0])

    def test_host_identity_is_kind_and_version(self):
        self.assertEqual(egt.host_identity(HOST), "gcc 14.2.1")
        self.assertEqual(egt.host_identity(None), "none")


class Pairing(TempRoot):
    def binutils(self, name="binutils-2.46"):
        for t in ("as", "ld"):
            script(self.root, "opt/%s/bin/%s" % (name, t), "echo 'GNU %s 2.46'\n" % t)
        return os.path.join(self.root, "opt", name)

    def test_auto_takes_the_realpath_of_binutils_current(self):
        real = self.binutils()
        os.symlink(real, os.path.join(self.root, "opt", "binutils-current"))
        pair, why = egt.resolve_pair(opts(), os.path.join(self.root, "opt"))
        self.assertEqual(pair, os.path.realpath(real))       # the version, not the link: one fixed pair
        self.assertIn("binutils-current", why)
        a = egt.gcc_configure_args("/p", "c", "", "", "/cc", "/cxx", False, "plain", pair)
        self.assertIn("--with-ld=%s/bin/ld" % os.path.realpath(real), a)

    def test_auto_without_a_working_one_is_the_system(self):
        pair, why = egt.resolve_pair(opts(), os.path.join(self.root, "opt"))
        self.assertEqual(pair, "")
        self.assertIn("does not run", why)
        self.assertEqual(egt.resolve_pair(opts(binutils="system"), self.root)[0], "")
        self.assertEqual(egt.resolve_pair(opts(component="binutils"), self.root)[0], "")

    def test_an_explicit_directory(self):
        real = self.binutils("mine")
        self.assertEqual(egt.resolve_pair(opts(binutils=real + "/bin"), self.root)[0], os.path.realpath(real))
        with self.assertRaises(egt.ToolError):
            egt.resolve_pair(opts(binutils=os.path.join(self.root, "nothing")), self.root)


# ------------------------------------------------------------------ failures

class Failures(TempRoot):
    def log(self, text):
        return put(self.root, "build.log", text)

    def test_the_stage_comes_from_stage_current_then_the_log(self):
        obj = os.path.join(self.root, "obj")
        put(obj, "stage_current", "stage2\n")
        self.assertEqual(egt.gcc_stage_of(obj, "", "plain"), "stage2")
        os.unlink(os.path.join(obj, "stage_current"))
        put(obj, "stage_last", "stagefeedback\n")
        self.assertEqual(egt.gcc_stage_of(obj, "", "profiled-lto"), "stagefeedback")
        os.unlink(os.path.join(obj, "stage_last"))
        self.assertEqual(egt.gcc_stage_of(obj, "x\nmake[2]: Entering directory '/b/stage1-gcc'\n", "plain"), "stage1")
        self.assertEqual(egt.gcc_stage_of(obj, "nothing", "plain"), "")
        self.assertEqual(egt.gcc_stage_of(obj, "nothing", "quick"), "stage1")

    def test_stage_one_says_mode_will_not_help(self):
        obj = os.path.join(self.root, "obj")
        put(obj, "stage_current", "stage1\n")
        log = self.log("make[3]: *** No rule to make target 'c,c++,lto', needed by 'native'.  Stop.\n")
        msg = egt.failure_report("gcc", obj, log, "profiled-lto", "/usr/bin/gcc", "/o/binutils-2.46", 8)
        self.assertIn("STAGE 1", msg)
        self.assertIn("changing --mode will NOT help", msg)
        self.assertIn("/usr/bin/gcc", msg)
        self.assertIn("/o/binutils-2.46/bin", msg)            # the binutils is named too: it is on PATH in stage 1
        self.assertIn("No rule to make target", msg)

    def test_later_stages_point_at_the_mode(self):
        obj = os.path.join(self.root, "obj")
        put(obj, "stage_current", "stagefeedback\n")
        msg = egt.failure_report("gcc", obj, self.log("lto1: internal compiler error: Segmentation fault\n"),
                                 "profiled-lto", "/usr/bin/gcc")
        self.assertIn("after stage 1 (in stagefeedback)", msg)
        self.assertIn("--mode plain --restart", msg)
        self.assertNotIn("will NOT help", msg)

    def test_memory_and_disk(self):
        obj = os.path.join(self.root, "obj")
        os.makedirs(obj)
        msg = egt.failure_report("gcc", obj, self.log("g++: fatal error: Killed signal terminated program cc1plus\n"),
                                 "lto", "/usr/bin/gcc", jobs=16)
        self.assertIn("--jobs 8", msg)
        msg = egt.failure_report("binutils", obj, self.log("as: write error: No space left on device\n"), "", "/usr/bin/gcc")
        self.assertIn("disk filled up", msg)

    def test_the_first_errors_not_the_last(self):
        lines = ["checking...", "foo.c:1:1: warning: unused", "a.c:2:3: error: first one",
                 "make[3]: *** [Makefile:10: a.o] Error 1", "make: [x] Error 2 (ignored)"] + \
            ["z%d.c:1: error: later %d" % (i, i) for i in range(30)]
        hits = egt.error_summary(self.log("\n".join(lines) + "\n"), limit=3)
        self.assertEqual(len(hits), 3)
        self.assertIn("first one", hits[0])
        self.assertIn("Error 1", hits[1])
        self.assertFalse(any("warning" in h or "ignored" in h for h in hits))

    def test_a_missing_program_is_the_first_error(self):
        # binutils 2.42 from its release tarball, built out of tree here: bfd.info is remade,
        # and the cause is two lines before the first `***`.
        log = self.log("  MAKEINFO doc/bfd.info\n/src/missing: 81: makeinfo: not found\n"
                       "WARNING: 'makeinfo' is missing on your system.\n"
                       "make[3]: *** [Makefile:1792: doc/bfd.info] Error 127\n")
        hits = egt.error_summary(log)
        self.assertIn("makeinfo: not found", hits[0])
        self.assertIn("missing on your system", hits[1])
        self.assertFalse(egt.errorish("checking for libfoo... not found"))

    def test_the_failed_sub_configure_is_found(self):
        obj = os.path.join(self.root, "obj")
        put(obj, "config.log", "all fine\nconfigure: exit 0\n")
        put(obj, "x86_64-pc-linux-gnu/libgcc/config.log",
            "checking for suffix of object files... \nconftest.c:1: error: cannot compute suffix\n"
            "configure: error: cannot compute suffix of object files: cannot compile\nconfigure: exit 1\n")
        path, errs = egt.failed_config_log(obj)
        self.assertTrue(path.endswith("libgcc/config.log"))
        self.assertIn("cannot compute suffix", errs[0])
        msg = egt.failure_report("gcc", obj, self.log("configure: error: cannot compute suffix of object files\n"
                                                      "See `config.log' for more details\n"), "plain", "/usr/bin/gcc")
        self.assertIn("libgcc/config.log", msg)

    def test_dejagnu_totals(self):
        t = egt.check_summary("# of expected passes\t\t100\n# of unexpected failures\t2\n"
                              "# of expected passes\t\t50\n")
        self.assertEqual(t, {"expected passes": 150, "unexpected failures": 2})


# ------------------------------------------------------------------ install, -current

INSTALL_MAKEFILE = """prefix = %(prefix)s
install-strip:
\t@echo "strip: cannot strip"; exit 1
install:
\tmkdir -p $(DESTDIR)$(prefix)/bin $(DESTDIR)$(prefix)/lib64
\tprintf '#!/bin/sh\\necho gcc\\n' > $(DESTDIR)$(prefix)/bin/gcc
\tchmod +x $(DESTDIR)$(prefix)/bin/gcc
broken-install:
\tmkdir -p $(DESTDIR)$(prefix)/share
"""


@unittest.skipUnless(shutil.which("make"), "needs make")
class Install(TempRoot):
    def setup_obj(self, prefix, text=INSTALL_MAKEFILE):
        obj = os.path.join(self.root, "obj")
        put(obj, "Makefile", text % {"prefix": prefix})
        return obj

    def test_install_strip_else_install_into_staging_then_one_rename(self):
        prefix = os.path.join(self.root, "opt", "gcc-16.2.0")
        obj = self.setup_obj(prefix)
        rec = {"tool": egt.TOOL, "component": "gcc", "configure": ["--prefix=" + prefix], "verify": "unverified"}
        egt.do_install(prefix, obj, egt.scrubbed_env(), os.path.join(self.root, "logs", "install.log"), rec, "bin/gcc")
        self.assertTrue(os.access(os.path.join(prefix, "bin", "gcc"), os.X_OK))
        self.assertEqual(egt.read_json(os.path.join(prefix, egt.MARKER))["component"], "gcc")
        self.assertIn("configure : --prefix=", egt.read_text(os.path.join(prefix, "BUILD-INFO.txt")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "opt"))), ["gcc-16.2.0"])   # no staging left
        self.assertIn("plain `make install` tried", self.said())
        self.assertIn("cannot strip", egt.read_text(os.path.join(self.root, "logs", "install.log.prev")))

    def test_an_existing_prefix_is_never_installed_over(self):
        prefix = os.path.join(self.root, "opt", "gcc-16.2.0")
        put(prefix, "bin/gcc", "theirs")
        obj = self.setup_obj(prefix)
        with self.assertRaises(egt.ToolError) as e:
            egt.do_install(prefix, obj, egt.scrubbed_env(), os.path.join(self.root, "i.log"), {}, "bin/gcc")
        self.assertIn("Never install over", str(e.exception))
        self.assertEqual(egt.read_text(os.path.join(prefix, "bin", "gcc")), "theirs")

    def test_a_failed_or_empty_install_leaves_nothing(self):
        prefix = os.path.join(self.root, "opt", "gcc-x")
        obj = self.setup_obj(prefix, INSTALL_MAKEFILE.replace("install:\n", "install: missing-dep\n"))
        with self.assertRaises(egt.ToolError):
            egt.do_install(prefix, obj, egt.scrubbed_env(), os.path.join(self.root, "i.log"), {}, "bin/gcc")
        self.assertEqual(os.listdir(os.path.join(self.root, "opt")), [])
        obj = self.setup_obj(prefix, INSTALL_MAKEFILE.replace("install:\n", "install: broken-install\nunused:\n"))
        with self.assertRaises(egt.ToolError) as e:
            egt.do_install(prefix, obj, egt.scrubbed_env(), os.path.join(self.root, "i.log"), {}, "bin/gcc", strip=False)
        self.assertIn("put no bin/gcc", str(e.exception))
        self.assertEqual(os.listdir(os.path.join(self.root, "opt")), [])


class CurrentLink(TempRoot):
    def test_atomic_link_and_relink(self):
        opt = os.path.join(self.root, "opt")
        a, b = os.path.join(opt, "gcc-16.1.0"), os.path.join(opt, "gcc-16.2.0")
        os.makedirs(a)
        os.makedirs(b)
        self.assertTrue(egt.link_current(opt, "gcc", a))
        self.assertTrue(egt.link_current(opt, "gcc", b))
        cur = os.path.join(opt, "gcc-current")
        self.assertEqual(os.readlink(cur), b)
        self.assertFalse(os.path.lexists(cur + ".new"))

    def test_a_real_directory_is_left_alone(self):
        opt = os.path.join(self.root, "opt")
        put(opt, "binutils-current/bin/ld", "mine")
        self.assertFalse(egt.link_current(opt, "binutils", os.path.join(opt, "binutils-2.46")))
        self.assertEqual(egt.read_text(os.path.join(opt, "binutils-current", "bin", "ld")), "mine")
        self.assertIn("real directory", self.said())

    def test_no_link_current(self):
        st = egt.State(os.path.join(self.root, "state.json"))
        ctx = egt.Ctx(opts=opts(link_current=False), opt_root=self.root, component="gcc",
                      prefix=os.path.join(self.root, "gcc-1"), state=st)
        egt.stage_activate(ctx)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "gcc-current")))
        self.assertIs(st.d["linked"], False)

    def test_advice_goes_through_the_link(self):
        text = "\n".join(egt.advice("gcc", "/o/gcc-16.2.0", "/o", True))
        self.assertIn('export PATH="/o/gcc-current/bin:$PATH"', text)
        self.assertIn("/o/gcc-current/lib64:/o/gcc-current/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}", text)
        self.assertIn("g++ -Wl,-rpath,/o/gcc-current/lib64", text)
        self.assertIn("GLIBCXX", text)
        self.assertIn("/o/gcc-16.2.0/bin", "\n".join(egt.advice("gcc", "/o/gcc-16.2.0", "/o", False)))
        self.assertIn("gcc -B /o/binutils-current/bin/", "\n".join(egt.advice("binutils", "/o/b", "/o", True)))


# ------------------------------------------------------------------ doctor

class Doctor(TempRoot):
    KEYS = ["enable_repos", "install_command", "missing", "ok", "package_manager", "tool"]

    def report(self, m=None, euid=1000, works=True, **o):
        return egt.doctor_report(opts(**o), m or machine(), HOST, works, euid=euid)

    def test_the_contract_shape(self):
        r = self.report()
        self.assertEqual(sorted(r), self.KEYS)
        self.assertEqual((r["tool"], r["ok"], r["package_manager"], r["missing"], r["enable_repos"], r["install_command"]),
                         ("enterprise_gcc_tool", True, "dnf", [], [], ""))
        r = self.report(machine(tools=dict(machine()["tools"], flex="")))
        self.assertEqual(sorted(r["missing"][0]), ["level", "package", "what"])
        self.assertEqual(r["missing"][0]["level"], "required")
        self.assertIsNone(egt.doctor_report(opts(), machine(pkg_manager="", tools=dict(machine()["tools"], flex="")),
                                            HOST, True, euid=1000)["package_manager"])

    def test_packages_per_distribution(self):
        tools = dict(machine()["tools"], flex="", makeinfo="", xz="", gawk="")
        headers = dict(machine()["headers"], zlib=False)
        want = {"dnf": "sudo dnf install -y flex texinfo xz zlib-devel gawk",
                "apt-get": "sudo apt-get install -y flex texinfo xz-utils zlib1g-dev gawk",
                "zypper": "sudo zypper install -y flex texinfo xz zlib-devel gawk",
                "pacman": "sudo pacman -S --needed --noconfirm flex texinfo xz zlib gawk"}
        for pm, cmd in want.items():
            r = self.report(machine(pkg_manager=pm, tools=tools, headers=headers))
            self.assertEqual(r["install_command"], cmd, pm)
            self.assertFalse(r["ok"])
        gawk = [m for m in self.report(machine(tools=tools))["missing"] if m["package"] == "gawk"][0]
        self.assertEqual(gawk["level"], "wanted")                # an awk is there: gawk is only better
        gawk = [m for m in self.report(machine(tools=dict(tools, awk="")))["missing"] if m["package"] == "gawk"][0]
        self.assertEqual(gawk["level"], "required")

    def test_crb_on_enterprise_linux_only(self):
        tools = dict(machine()["tools"], makeinfo="")
        alma = machine(os_id="almalinux", os_like="rhel centos fedora", os_version="10.0", tools=tools)
        self.assertEqual(self.report(alma)["enable_repos"], ["crb"])
        self.assertEqual(self.report(machine(tools=tools))["enable_repos"], [])           # Fedora
        self.assertEqual(self.report(machine(pkg_manager="apt-get", os_id="ubuntu", tools=tools))["enable_repos"], [])
        self.assertEqual(self.report(dict(alma, tools=machine()["tools"]))["enable_repos"], [])  # nothing from CRB

    def test_the_real_repository_id_per_enterprise_linux(self):
        tools = dict(machine()["tools"], makeinfo="")
        for osid, like, ver, arch, repo in (
                ("almalinux", "rhel centos fedora", "10.0", "x86_64", "crb"),
                ("rocky", "rhel centos fedora", "9.6", "x86_64", "crb"),
                ("almalinux", "rhel centos fedora", "8.10", "x86_64", "powertools"),
                ("centos", "rhel fedora", "8", "x86_64", "powertools"),
                ("rhel", "fedora", "9.6", "x86_64", "codeready-builder-for-rhel-9-x86_64-rpms"),
                ("rhel", "fedora", "10.0", "aarch64", "codeready-builder-for-rhel-10-aarch64-rpms"),
                ("rhel", "fedora", "8.10", "x86_64", "codeready-builder-for-rhel-8-x86_64-rpms")):
            m = machine(os_id=osid, os_like=like, os_version=ver, arch=arch, tools=tools)
            self.assertEqual(self.report(m)["enable_repos"], [repo], (osid, ver))
        self.assertEqual(egt.enable_repo_command("codeready-builder-for-rhel-10-x86_64-rpms"),
                         ["sudo", "subscription-manager", "repos", "--enable", "codeready-builder-for-rhel-10-x86_64-rpms"])
        self.assertEqual(egt.enable_repo_command("powertools"), ["sudo", "dnf", "config-manager", "--set-enabled", "powertools"])
        self.assertEqual(egt.enable_repo_command(""), [])

    def test_install_deps_enables_that_repository_then_base_first_options_apart(self):
        """build_gcc_universal.sh:189-197: the base packages in one command, the 32-bit ones in a
        second that may fail with a warning.  The doctor contract keeps its ONE install_command."""
        tools = dict(machine()["tools"], flex="", makeinfo="", gdc="", runtest="", expect="", patch="")
        rhel = machine(os_id="rhel", os_like="fedora", os_version="9.6", tools=tools,
                       headers=dict(machine()["headers"], zstd=False))
        r = egt.doctor_report(opts(multilib=True, languages="c,c++,d", check=True), rhel, HOST, True, euid=1000)
        self.assertEqual(r["install_command"], "sudo dnf install -y flex texinfo libzstd-devel patch "
                         "glibc-devel.i686 libstdc++-devel.i686 gcc-gdc dejagnu expect")
        steps = egt.install_steps(r)
        self.assertEqual([p for _, p, _ in steps], [["flex", "texinfo"], ["glibc-devel.i686", "libstdc++-devel.i686"],
                                                    ["gcc-gdc"], ["dejagnu", "expect"], ["libzstd-devel", "patch"]])
        ran = []

        def runner(argv):
            ran.append(" ".join(argv))
            return 1 if "glibc-devel.i686" in argv or "gcc-gdc" in argv else 0
        egt.install_deps(r, runner)
        self.assertEqual(ran, ["sudo subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms",
                               "sudo dnf install -y flex texinfo",
                               "sudo dnf install -y glibc-devel.i686 libstdc++-devel.i686",
                               "sudo dnf install -y gcc-gdc",
                               "sudo dnf install -y dejagnu expect",
                               "sudo dnf install -y libzstd-devel patch"])
        self.assertIn("32-bit headers unavailable; multilib may fail", self.said())
        self.assertIn("--languages d will be refused", self.said())
        ran[:] = []
        apt = egt.doctor_report(opts(multilib=True), machine(pkg_manager="apt-get", os_id="ubuntu", tools=tools),
                                HOST, True, euid=1000)
        egt.install_deps(apt, runner)
        self.assertEqual(ran, ["sudo apt-get update", "sudo apt-get install -y flex texinfo",
                               "sudo apt-get install -y gcc-multilib g++-multilib", "sudo apt-get install -y patch"])
        ran[:] = []
        egt.install_deps(egt.doctor_report(opts(), machine(), HOST, True, euid=1000), runner)
        self.assertEqual(ran, [])
        self.assertIn("nothing to install", self.said())

    def test_what_the_options_add(self):
        def pk(**o):
            return self.report(**o)["install_command"]
        self.assertIn("glibc-devel.i686 libstdc++-devel.i686", pk(multilib=True))
        self.assertIn("gcc-multilib g++-multilib", self.report(machine(pkg_manager="apt-get"), multilib=True)["install_command"])
        m = machine(tools=dict(machine()["tools"], gdc="", gnatmake="", runtest="", expect="", git=""))
        self.assertIn("gcc-gdc", egt.doctor_report(opts(languages="c,d"), m, HOST, True, euid=1000)["install_command"])
        self.assertIn("gcc-gnat", egt.doctor_report(opts(languages="c,ada"), m, HOST, True, euid=1000)["install_command"])
        self.assertIn("dejagnu expect", egt.doctor_report(opts(check=True), m, HOST, True, euid=1000)["install_command"])
        self.assertIn("git", egt.doctor_report(opts(track="git"), m, HOST, True, euid=1000)["install_command"])
        self.assertEqual(egt.doctor_report(opts(), m, HOST, True, euid=1000)["install_command"], "")
        r = self.report(prereqs="system")
        self.assertEqual(r["install_command"], "sudo dnf install -y gmp-devel mpfr-devel libmpc-devel")
        self.assertIn({"what": "ISL headers: Graphite loop optimisations", "package": "isl-devel", "level": "optional"},
                      r["missing"])

    def test_root_and_a_broken_compiler(self):
        r = self.report(euid=0)
        self.assertFalse(r["ok"])
        self.assertIn("root", r["missing"][0]["what"])
        self.assertEqual(r["missing"][0]["package"], "")
        r = self.report(works="unknown pseudo-op: `.base64'")
        self.assertIn(".base64", r["missing"][0]["what"])
        self.assertEqual(r["install_command"], "sudo dnf install -y gcc-c++")
        r = egt.doctor_report(opts(), machine(), None, None, euid=1000)
        self.assertEqual(r["install_command"], "sudo dnf install -y gcc gcc-c++")

    def test_binutils_needs_less(self):
        m = machine(tools=dict(machine()["tools"], gawk="", awk="", bzip2="", wget="", curl="", patch=""))
        self.assertTrue(egt.doctor_report(opts(component="binutils"), m, HOST, True, euid=1000)["ok"])
        self.assertFalse(egt.doctor_report(opts(component="gcc"), m, HOST, True, euid=1000)["ok"])


class Captured(TempRoot):
    """main() as the main file runs it: what reaches the real stdout and stderr (out() is NOT
    replaced here)."""

    def run_main(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.object(egt, "out", REAL_OUT), \
                mock.patch.object(egt.sys, "stdout", o), mock.patch.object(egt.sys, "stderr", e):
            code = egt.main(list(argv))
        return code, o.getvalue(), e.getvalue()


class DoctorJson(Captured):
    def test_doctor_json_through_main(self):
        m = machine(tools=dict(machine()["tools"], flex=""))
        with mock.patch.object(egt, "detect_machine", return_value=m), \
                mock.patch.object(egt, "pick_host", return_value=dict(HOST)), \
                mock.patch.object(egt, "compiler_works", return_value=(True, "runs")), \
                mock.patch.object(egt.os, "geteuid", return_value=1000):
            code, stdout, stderr = self.run_main("doctor", "--json", "--opt-root", self.root)
        self.assertEqual(code, 1)
        r = json.loads(stdout)
        self.assertEqual(sorted(r), Doctor.KEYS)
        self.assertEqual(r["install_command"], "sudo dnf install -y flex")

    def test_with_json_an_error_goes_to_stderr_and_stdout_stays_empty(self):
        """PLAN.md section 3: with --json, stdout is the JSON and nothing else."""
        cases = [(("doctor", "--json", "--host-cc", os.path.join(self.root, "nonexistent")), 1, "--host-cc"),
                 (("doctor", "--json", "--profile", "nosuch"), 1, "nosuch"),
                 (("status", "--json", "--profile", "nosuch"), 1, "nosuch"),
                 (("doctor", "--json", "--mode", "bogus"), 2, "--mode")]
        for argv, want, word in cases:
            code, stdout, stderr = self.run_main(*(argv + ("--opt-root", os.path.join(self.root, "opt"),
                                                            "--workdir", os.path.join(self.root, "w"))))
            self.assertEqual(code, want, argv)
            self.assertEqual(stdout, "", argv)
            self.assertIn("ERROR:", stderr, argv)
            self.assertIn(word, stderr, argv)
        self.assertIsNone(egt.UI.stream)                         # main() leaves the process as it found it

    def test_without_json_messages_stay_on_stdout(self):
        code, stdout, stderr = self.run_main("status", "--profile", "nosuch", "--opt-root", self.root)
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", stdout)
        self.assertEqual(stderr, "")


# ------------------------------------------------------------------ status

STATUS_KEYS = ["builds", "components", "current", "installed", "opt_root", "tool", "workdir"]
INSTALLED_KEYS = ["built_by_tool", "compiler", "for", "name", "prefix", "runs", "version"]
BUILD_KEYS = ["error", "id", "stage", "status"]


class Status(Captured):
    def fake_opt(self):
        opt = os.path.join(self.root, "opt")
        pair = os.path.join(opt, "binutils-2.46")
        script(opt, "binutils-2.46/bin/ld", "echo 'GNU ld (GNU Binutils) 2.46'\n")
        put(opt, "binutils-2.46/" + egt.MARKER, json.dumps({"tool": egt.TOOL, "component": "binutils", "version": "2.46",
                                                            "host": "gcc (GCC) 14.2.1", "verify": "ok"}))
        os.symlink(pair, os.path.join(opt, "binutils-current"))
        fake_gcc(opt, "gcc-16.2.0/bin/gcc", "16.2.0")
        put(opt, "gcc-16.2.0/" + egt.MARKER, json.dumps({"tool": egt.TOOL, "mode": "profiled-lto", "version": "16.2.0",
                                                         "languages": "c,c++,lto", "binutils": pair, "verify": "ok",
                                                         "host": "gcc (GCC) 14.2.1 20250110"}))
        fake_gcc(opt, "gcc-17/bin/gcc", "17.0.0")                      # not ours: listed, never touched
        script(opt, "gcc-15.1.0/bin/gcc", "exit 1\n")                   # there, but it does not run
        put(opt, "gcc-17.0.0-broken/bin/README")                      # no bin/gcc at all: a leftover
        os.makedirs(os.path.join(opt, ".enterprise_gcc_tool-staging-gcc-16.3.0"))
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))
        return opt, pair

    def test_status_is_the_toolkit_contract(self):
        opt, pair = self.fake_opt()
        put(self.root, "work/build/gcc-16.3.0/state.json", json.dumps(
            {"tool": egt.TOOL, "component": "gcc", "id": "16.3.0", "prefix": "/x",
             "stages": {"source": {"status": "done"}, "build": {"status": "running"}}}))
        d = egt.status_data(opt, os.path.join(self.root, "work"))
        self.assertEqual(sorted(d), STATUS_KEYS)
        self.assertEqual((d["tool"], d["opt_root"]), (egt.TOOL, opt))
        self.assertEqual(d["current"], {"gcc-current": os.path.join(opt, "gcc-16.2.0"), "binutils-current": pair})
        for e in d["installed"]:
            self.assertTrue(set(INSTALLED_KEYS) <= set(e), e)
        by_prefix = dict((e["prefix"], e) for e in d["installed"])
        ours = by_prefix[os.path.join(opt, "gcc-16.2.0")]
        self.assertEqual(dict((k, ours[k]) for k in INSTALLED_KEYS),
                         {"name": "gcc", "version": "16.2.0", "prefix": os.path.join(opt, "gcc-16.2.0"), "runs": True,
                          "built_by_tool": True, "compiler": "gcc (GCC) 14.2.1 20250110", "for": None})
        self.assertEqual((ours["mode"], ours["paired_binutils"], ours["paired_binutils_present"], ours["verify"]),
                         ("profiled-lto", pair, True, "ok"))
        theirs = by_prefix[os.path.join(opt, "gcc-17")]
        self.assertEqual((theirs["name"], theirs["runs"], theirs["built_by_tool"], theirs["compiler"]),
                         ("gcc", True, False, None))                  # the menu's third state
        dead = by_prefix[os.path.join(opt, "gcc-15.1.0")]
        self.assertEqual((dead["runs"], dead["version"]), (False, None))
        b = by_prefix[pair]
        self.assertEqual((b["name"], b["version"], b["runs"], b["built_by_tool"], b["gold"]),
                         ("binutils", "2.46", True, True, False))
        self.assertNotIn(os.path.join(opt, "gcc-17.0.0-broken"), by_prefix)
        leftovers = [p["path"] for p in d["components"]["gcc"]["leftovers"]]
        self.assertIn(os.path.join(opt, "gcc-17.0.0-broken"), leftovers)
        self.assertIn(os.path.join(opt, ".enterprise_gcc_tool-staging-gcc-16.3.0"), leftovers)
        self.assertEqual(d["components"]["binutils"]["current"]["version"], "2.46")
        [build] = d["builds"]
        self.assertEqual(dict((k, build[k]) for k in BUILD_KEYS),
                         {"id": "gcc-16.3.0", "status": "killed", "stage": "build", "error": None})
        self.assertEqual(build["stages"], {"source": "done", "build": "killed"})
        self.printed[:] = []
        egt.main(["status", "--opt-root", opt, "--workdir", os.path.join(self.root, "work")])
        self.assertIn("DOES NOT RUN", self.said())
        self.assertIn("KILLED at build", self.said())

    def test_a_dangling_current_is_its_target_and_none_is_null(self):
        opt = os.path.join(self.root, "opt")
        os.makedirs(opt)
        os.symlink(os.path.join(opt, "gcc-16.2.0"), os.path.join(opt, "gcc-current"))   # removed since
        d = egt.status_data(opt, os.path.join(self.root, "work"))
        self.assertEqual(d["current"], {"gcc-current": os.path.join(opt, "gcc-16.2.0"), "binutils-current": None})
        self.assertEqual((d["installed"], d["builds"]), ([], []))

    def test_build_status_stage_and_error(self):
        def summary(stages, held=False, **rec):
            rec.setdefault("component", "gcc")
            return egt.build_summary(dict(rec, stages=stages), held)
        all_done = dict((n, {"status": "done"}) for n in egt.stage_names("gcc", opts()))
        self.assertEqual(summary(all_done), ("done", "activate", None))
        self.assertEqual(summary(dict(all_done, build={"status": "failed", "error": "make failed (exit 2)"})),
                         ("failed", "build", "make failed (exit 2)"))
        self.assertEqual(summary({"source": {"status": "done"}, "configure": {"status": "interrupted"}}),
                         ("interrupted", "configure", "interrupted"))
        self.assertEqual(summary({"source": {"status": "done"}, "build": {"status": "running"}}, held=True),
                         ("running", "build", None))
        self.assertEqual(summary({"source": {"status": "done"}}), ("incomplete", "prereqs", None))
        self.assertEqual(summary({}), ("new", "source", None))
        b = dict((n, {"status": "done"}) for n in egt.stage_names("binutils", opts(component="binutils")))
        self.assertEqual(summary(b, component="binutils", options={"component": "binutils"}), ("done", "activate", None))
        # --check in the record's options: the check stage is part of that build
        self.assertEqual(summary(all_done, options={"check": True}), ("incomplete", "check", None))

    def test_status_json_through_main_is_pure_json(self):
        opt, _ = self.fake_opt()
        code, stdout, stderr = self.run_main("status", "--json", "--opt-root", opt,
                                             "--workdir", os.path.join(self.root, "w"))
        self.assertEqual(code, 0)
        d = json.loads(stdout)
        self.assertEqual(sorted(d), STATUS_KEYS)
        self.assertEqual(sorted(d["current"]), ["binutils-current", "gcc-current"])
        self.assertTrue(all(set(INSTALLED_KEYS) <= set(e) for e in d["installed"]))
        self.assertEqual(sorted(d["components"]["gcc"]), ["current", "leftovers"])


# ------------------------------------------------------------------ state, lock, resume

class Resume(TempRoot):
    def test_state_records_and_marks_itself(self):
        st = egt.State(os.path.join(self.root, "b", "state.json"))
        st.set_stage("configure", status="done")
        again = egt.State(os.path.join(self.root, "b", "state.json"))
        self.assertTrue(again.done("configure"))
        self.assertEqual(again.d["tool"], egt.TOOL)
        put(self.root, "bad/state.json", "{not json")
        self.assertEqual(egt.State(os.path.join(self.root, "bad", "state.json")).d["stages"], {})
        self.assertEqual(egt.State(os.path.join(self.root, "b", "state.json"), fresh=True).d["stages"], {})

    def test_one_instance_per_build_directory(self):
        d = os.path.join(self.root, "build", "gcc-16.2.0")
        self.assertFalse(egt.lock_is_held(d))
        egt.lock_dir(d)
        self.assertTrue(egt.lock_is_held(d))
        with self.assertRaises(egt.ToolError) as e:
            egt.lock_dir(d)
        self.assertIn("another enterprise_gcc_tool is working", str(e.exception))
        egt._LOCKS.pop().close()
        self.assertFalse(egt.lock_is_held(d))

    def test_resume_decision(self):
        st = egt.State(os.path.join(self.root, "state.json"))
        o = opts()
        self.assertEqual(egt.resume_decision(o, st, False), "resume")          # nothing was built
        st.d["stages"]["build"] = {"status": "failed"}
        self.assertEqual(egt.resume_decision(o, st, True), "resume")           # nobody to ask, compatible
        self.assertEqual(egt.resume_decision(o, st, False), "quit")            # never deleted on a guess
        self.assertEqual(egt.resume_decision(opts(restart=True), st, False), "restart")
        answers = iter(["x", ""])
        self.assertEqual(egt.resume_decision(o, st, True, lambda q, d: next(answers)), "resume")
        self.assertEqual(egt.resume_decision(o, st, False, lambda q, d: ""), "restart")   # Enter = delete there
        self.assertEqual(egt.resume_decision(o, st, True, lambda q, d: "restart"), "restart")
        self.assertEqual(egt.resume_decision(o, st, True, lambda q, d: None), "quit")

    def test_stages_to_run(self):
        p = egt.stage_names("gcc", opts())
        self.assertEqual(p, ["source", "prereqs", "configure", "build", "install", "verify", "activate"])
        done = set(p)
        self.assertEqual(egt.stages_to_run(p, lambda n: n in done, lambda n: True), [])
        done = set(["source", "prereqs", "configure"])
        self.assertEqual(egt.stages_to_run(p, lambda n: n in done, lambda n: True),
                         ["build", "install", "verify", "activate"])     # make re-run; configure kept
        # the build tree was cleaned after `build` was done, before install: configure again
        done = set(["source", "prereqs", "configure", "build"])
        self.assertEqual(egt.stages_to_run(p, lambda n: n in done, lambda n: n not in ("configure", "build"))[0],
                         "configure")
        self.assertEqual(egt.stage_names("gcc", opts(prereqs="system", check=True)),
                         ["source", "configure", "build", "check", "install", "verify", "activate"])
        self.assertEqual(egt.stage_names("binutils", opts(component="binutils")),
                         ["source", "configure", "build", "install", "verify", "activate"])

    def ctx(self, **o):
        return egt.resolve(opts(gcc_version="16.2.0", workdir=self.root, opt_root=self.root, **o), machine(), host=HOST)

    def test_the_fingerprint_holds_what_reaches_configure_and_make(self):
        base = egt.fingerprint(self.ctx())
        self.assertEqual(base, egt.fingerprint(self.ctx(jobs=3, yes=True, verbose=True)))
        for change in ({"mode": "plain"}, {"languages": "c,c++"}, {"multilib": True}, {"with_arch": "x86-64-v3"},
                       {"boot_arch": "none"}, {"binutils": "system", "with_tune": "none"}, {"configure_extra": "--x"},
                       {"prereqs": "system"}, {"prefix": "/elsewhere"}):
            self.assertNotEqual(base, egt.fingerprint(self.ctx(**change)), change)
        diff = egt.fingerprint_diff(base, egt.fingerprint(self.ctx(mode="plain")))
        self.assertEqual(len(diff), 1)
        self.assertIn('then "profiled-lto", now "plain"', diff[0])

    def test_previous_build_summary(self):
        root = os.path.join(self.root, "b")
        st = egt.State(os.path.join(root, "state.json"))
        self.assertEqual(egt.previous_build_summary(st, root), [])
        st.d["fingerprint"] = {"mode": "lto"}
        st.set_stage("build", status="running", seconds=3700)
        put(root, "obj/Makefile")
        lines = "\n".join(egt.previous_build_summary(st, root, {"mode": "plain"}))
        self.assertIn("killed", lines)
        self.assertIn("1h01m40s", lines)
        self.assertIn('then "lto", now "plain"', lines)

    def test_wipe_keeps_the_lock_and_follows_no_link(self):
        root = os.path.join(self.root, "b")
        outside = os.path.join(self.root, "outside")
        put(outside, "keep.txt", "x")
        put(root, "obj/Makefile")
        put(root, "state.json", "{}")
        put(root, ".lock")
        os.symlink(outside, os.path.join(root, "link"))
        egt.wipe_build_dir(root)
        self.assertEqual(os.listdir(root), [".lock"])
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))


# ------------------------------------------------------------------ prerequisites

PREREQ_SCRIPT = """#! /bin/sh
# fake contrib/download_prerequisites: the archives must be here already
gmp='gmp-6.2.1.tar.bz2'
mpfr='mpfr-4.1.0.tar.bz2'
mpc='mpc-1.2.1.tar.gz'
isl='isl-0.24.tar.bz2'
gettext='gettext-0.22.tar.gz'
base_url='http://gcc.gnu.org/pub/gcc/infrastructure/'
for ar in $gmp $mpfr $mpc $isl $gettext; do
  [ -e "$ar" ] || { echo "error: Cannot download $ar from $base_url"; exit 1; }
  d="${ar%.tar.*}"; mkdir -p "$d"; ln -sf "$d" "${d%%-*}"
done
echo "All prerequisites downloaded successfully."
"""


UPSTREAM_LIKE = """#! /bin/sh
# download_prerequisites' own rules: unpack only what has no directory yet, link only what has
# no link yet (gcc/contrib/download_prerequisites: [ -e "${directory}/${package}" ] || tar -xf)
gmp='gmp-6.2.1.tar.bz2'
mpfr='mpfr-4.1.0.tar.bz2'
mpc='mpc-1.2.1.tar.gz'
isl='isl-0.24.tar.bz2'
for ar in $gmp $mpfr $mpc $isl; do
  [ -e "$ar" ] || { echo "error: Cannot download $ar"; exit 1; }
  d="${ar%.tar*}"
  [ -e "$d" ] || { mkdir "$d" && touch "$d/whole"; }
done
for ar in $gmp $mpfr $mpc $isl; do
  d="${ar%.tar*}"; [ -e "${d%%-*}" ] || ln -s "$d" "${d%%-*}"
done
exit 0
"""


class Prereqs(TempRoot):
    def tree(self):
        src = os.path.join(self.root, "gcc-16.2.0")
        put(src, "contrib/download_prerequisites", PREREQ_SCRIPT, 0o755)
        archives = [a for _, a in egt.parse_prereq_script(PREREQ_SCRIPT)]
        digest = lambda a: __import__("hashlib").sha512(("real " + a).encode()).hexdigest()
        put(src, "contrib/prerequisites.sha512", "".join("%s  %s\n" % (digest(a), a) for a in archives))
        return src

    def test_the_script_lists_five(self):
        self.assertEqual([n for n, _ in egt.parse_prereq_script(PREREQ_SCRIPT)], ["gmp", "mpfr", "mpc", "isl", "gettext"])

    def ctx(self, src):
        return egt.Ctx(src={"dir": src}, root=os.path.join(self.root, "b"), verbose=False,
                       log=lambda n: os.path.join(self.root, "b", "logs", n + ".log"))

    def test_fetched_from_the_mirrors_checked_then_unpacked_by_the_script(self):
        src = self.tree()
        put(src, "gmp-6.2.1.tar.bz2", "<html>Not Found</html>")     # curl -LO's legacy: an error page
        tried = []

        def fetch(url, dest):
            tried.append(url)
            put(os.path.dirname(dest), os.path.basename(dest),
                "corrupt" if url.startswith("https://gcc.gnu.org") and "mpfr" in url else "real " + os.path.basename(dest))
        egt.stage_prereqs(self.ctx(src), fetch)
        self.assertEqual(egt.prereqs_missing(src, ["gmp", "mpfr", "mpc", "isl", "gettext"]), [])
        self.assertIn("https://sourceware.org/pub/gcc/infrastructure/mpfr-4.1.0.tar.bz2", tried)
        self.assertIn("deleted", self.said())                         # the error page went first
        self.assertEqual(egt.read_text(os.path.join(src, "gmp-6.2.1.tar.bz2")), "real gmp-6.2.1.tar.bz2")
        tried[:] = []
        egt.stage_prereqs(self.ctx(src), fetch)                       # already there: nothing fetched
        self.assertEqual(tried, [])

    def test_an_unpack_cut_short_is_unpacked_again(self):
        """Upstream download_prerequisites unpacks an archive only when its directory is not
        there yet, and links only what is not linked: a half-unpacked gmp-6.2.1/ left by a ^C
        would be taken as whole.  The tool's mark says an unpack was under way."""
        src = self.tree()
        put(src, "contrib/download_prerequisites", UPSTREAM_LIKE, 0o755)
        fetch = lambda url, dest: put(os.path.dirname(dest), os.path.basename(dest), "real " + os.path.basename(dest))
        # the run that was killed: the archives are here, gmp is half unpacked, nothing linked yet
        egt.prefetch_prereqs(src, egt.parse_prereq_script(UPSTREAM_LIKE), {}, fetch)
        put(src, "gmp-6.2.1/half-of-it")
        put(src, egt.PREREQ_MARK, "x")
        egt.stage_prereqs(self.ctx(src), fetch)
        self.assertTrue(os.path.isfile(os.path.join(src, "gmp", "whole")))      # unpacked again, then linked
        self.assertFalse(os.path.exists(os.path.join(src, "gmp-6.2.1", "half-of-it")))
        self.assertIn("cut short", self.said())
        self.assertFalse(os.path.exists(os.path.join(src, egt.PREREQ_MARK)))   # whole now: the mark is gone
        self.assertTrue(os.path.isfile(os.path.join(src, "gmp-6.2.1.tar.bz2")))  # the archives are kept
        # a failed run leaves the mark, and the next run does not trust what it left
        put(src, "contrib/download_prerequisites", UPSTREAM_LIKE.replace("exit 0", "exit 3"), 0o755)
        egt.clear_prereq_trees(src, egt.parse_prereq_script(UPSTREAM_LIKE))
        with self.assertRaises(egt.ToolError):
            egt.stage_prereqs(self.ctx(src), fetch)
        self.assertTrue(os.path.exists(os.path.join(src, egt.PREREQ_MARK)))
        # with no mark, a tree whose links all resolve (the .sh's, say) is used as it is
        os.unlink(os.path.join(src, egt.PREREQ_MARK))
        put(src, "gmp-6.2.1/theirs")
        self.printed[:] = []
        egt.stage_prereqs(self.ctx(src), fetch)
        self.assertIn("already in the tree", self.said())
        self.assertTrue(os.path.isfile(os.path.join(src, "gmp-6.2.1", "theirs")))

    def test_clear_follows_no_link(self):
        src = self.tree()
        outside = os.path.join(self.root, "outside")
        put(outside, "keep")
        os.symlink(outside, os.path.join(src, "gmp"))
        put(src, "mpfr-4.1.0/x")
        put(src, "mpfr-4.1.0.tar.bz2", "archive")
        removed = egt.clear_prereq_trees(src, [("gmp", "gmp-6.2.1.tar.bz2"), ("mpfr", "mpfr-4.1.0.tar.bz2"),
                                               ("isl", "../../evil.tar.bz2")])
        self.assertEqual(removed, ["gmp", "mpfr-4.1.0"])
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep")))
        self.assertTrue(os.path.isfile(os.path.join(src, "mpfr-4.1.0.tar.bz2")))
        self.assertEqual(egt.prereq_package_dir("mpc-1.2.1.tar.gz"), "mpc-1.2.1")

    def test_failure_names_the_real_alternatives(self):
        src = self.tree()

        def fetch(url, dest):
            raise egt.ToolError("download failed: %s\n  403" % url)
        with self.assertRaises(egt.ToolError) as e:
            egt.stage_prereqs(self.ctx(src), fetch)
        msg = str(e.exception)
        self.assertIn("--prereqs system", msg)
        self.assertIn("libmpc-devel", msg)
        self.assertIn("libmpfr-dev", msg)
        self.assertNotIn("--with-system-gmp", msg)                     # the .sh's advice named no real option


# ------------------------------------------------------------------ profiles, CLI

class Profiles(TempRoot):
    def test_round_trip_and_what_is_never_stored(self):
        o = opts(component="gcc", track="snapshot", series="17", mode="lto", languages="c,c++,fortran", multilib=True,
                 with_arch="x86-64-v3", jobs=6, link_current=False, resume=True, yes=True, check=True)
        path = egt.save_profile("mine", o)
        text = egt.read_text(path)
        self.assertTrue(text.startswith(egt.PROFILE_HEADER))
        for never in ("resume", "restart", "yes", "install_deps"):
            self.assertNotIn("\n%s =" % never, text)
        v = egt.load_profile("mine")
        self.assertEqual((v["track"], v["series"], v["mode"], v["languages"], v["multilib"], v["with_arch"], v["jobs"],
                          v["link_current"], v["check"]),
                         ("snapshot", "17", "lto", "c,c++,fortran", True, "x86-64-v3", 6, False, True))
        self.assertEqual([p["name"] for p in egt.list_profiles()], ["mine"])
        put(egt.profiles_dir(), "stray.ini", "[build]\nmode = quick\n")          # no header: not one of ours
        self.assertEqual([p["name"] for p in egt.list_profiles()], ["mine"])

    def test_defaults_then_profile_then_command_line(self):
        egt.save_profile("p", opts(mode="plain", languages="c"))
        args = egt.build_parser().parse_args(["build", "--profile", "p", "--languages", "c,c++"])
        o = egt.merge_options(args)
        self.assertEqual((o["mode"], o["languages"], o["track"]), ("plain", "c,c++", "release"))

    def test_the_legacy_names_parse(self):
        a = egt.build_parser().parse_args(["build", "--version", "16.2.0", "--gcc-git", "https://x/gcc.git",
                                           "--tune", "portable", "-j", "3", "--no-link-current"])
        o = egt.merge_options(a)
        self.assertEqual((o["gcc_version"], o["git_url"], o["binutils_tune"], o["jobs"], o["link_current"]),
                         ("16.2.0", "https://x/gcc.git", "portable", 3, False))


class Main(TempRoot):
    def test_exit_codes(self):
        self.assertEqual(egt.main(["build", "--mode", "fast"]), 2)              # usage
        with mock.patch.object(egt.sys, "stderr", io.StringIO()):
            self.assertEqual(egt.main(["frobnicate"]), 2)                       # argparse
        with mock.patch.object(egt.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(egt.sys, "stdout", io.StringIO()):
            self.assertEqual(egt.main([]), 2)                                   # no command: the help
            self.assertEqual(egt.main(["wizard"]), 2)                           # nobody to ask
        with mock.patch.object(egt.os, "geteuid", return_value=0):
            self.assertEqual(egt.main(["build", "--gcc-version", "16.2.0"]), 1)
        self.assertIn("as root", self.said())

        def interrupted(args):
            raise KeyboardInterrupt()
        with mock.patch.dict(egt.COMMANDS, {"profiles": interrupted}):
            self.assertEqual(egt.main(["profiles"]), 130)

    def test_dev_banner(self):
        ctx = egt.resolve(opts(track="snapshot", gcc_version="16-20260820", workdir=self.root, opt_root=self.root),
                          machine(), host=HOST)
        self.assertIn("release branch", " ".join(egt.dev_banner(ctx, set([15, 16]))))
        ctx = egt.resolve(opts(track="snapshot", gcc_version="17-20260823", workdir=self.root, opt_root=self.root),
                          machine(), host=HOST)
        self.assertIn("GCC 17 has not shipped", " ".join(egt.dev_banner(ctx, set([15, 16]))))
        self.assertIn("DEVELOPMENT compiler", egt.dev_banner(ctx, set())[1])
        ctx = egt.resolve(opts(gcc_version="16.2.0", workdir=self.root, opt_root=self.root), machine(), host=HOST)
        self.assertEqual(egt.dev_banner(ctx, set()), [])
        ctx = egt.resolve(opts(track="git", git_ref="releases/gcc-16", workdir=self.root, opt_root=self.root),
                          machine(), host=HOST)
        self.assertIn("git releases/gcc-16 is not a release", " ".join(egt.dev_banner(ctx, set([15, 16]))))
        self.assertTrue(ctx.prefix.endswith("/gcc-<BASE-VER>-git-<sha>"))     # named once cloned
        ctx.id = "16.2.1-git-4f2a9c1"
        self.assertIn("GCC 16 release branch", " ".join(egt.dev_banner(ctx, set([15, 16]))))

    def test_ram_warnings(self):
        self.assertIn("may OOM", egt.ram_warnings(machine(mem_total_gb=3.0), opts(), 4)[0])
        self.assertIn("--jobs 6", egt.ram_warnings(machine(mem_avail_gb=9.0), opts(), 16)[0])
        self.assertEqual(egt.ram_warnings(machine(mem_avail_gb=9.0), opts(mode="plain"), 16), [])
        self.assertEqual(egt.ram_warnings(machine(mem_total_gb=2.0), opts(component="binutils"), 16), [])

    def test_every_command_takes_opt_root_and_yes(self):
        p = egt.build_parser()
        for cmd in egt.COMMANDS:
            extra = ["/some/prefix"] if cmd == "verify" else []
            a = p.parse_args([cmd, "--opt-root", "/x/opt", "--yes"] + extra)
            self.assertEqual((a.opt_root, a.yes), ("/x/opt", True), cmd)

    def test_dry_run_is_the_plan_and_builds_nothing(self):
        report = {"tool": egt.TOOL, "ok": True, "package_manager": "dnf", "missing": [], "enable_repos": [],
                  "install_command": ""}
        with mock.patch.object(egt, "detect_machine", return_value=machine()), \
                mock.patch.object(egt, "preflight", return_value=(report, dict(HOST), "(test)")), \
                mock.patch.object(egt, "run_stages") as stages:
            code = egt.main(["build", "--dry-run", "--gcc-version", "16.2.0", "--opt-root", self.root,
                             "--workdir", self.root])
        self.assertEqual(code, 0)
        self.assertFalse(stages.called)
        self.assertIn("==> Plan: gcc", self.said())
        self.assertIn("make -j8 BUILD_CONFIG=bootstrap-lto", self.said())
        self.assertFalse(os.path.exists(os.path.join(self.root, "build")))

    def test_the_wizard_asks_what_the_flags_say(self):
        answers = iter(["", "snapshot", "17", "", "plain", "c,c++,fortran", "n", "none", "x86-64-v3", "", "",
                        "a", "", "y", "wiz"])
        with mock.patch("builtins.input", lambda prompt="": next(answers)):
            o = egt.wizard(dict(egt.DEFAULTS), machine())
        self.assertEqual((o["component"], o["track"], o["series"], o["gcc_version"], o["mode"], o["languages"],
                          o["multilib"], o["boot_arch"], o["with_arch"], o["host_cc"], o["link_current"]),
                         ("gcc", "snapshot", "17", "auto", "plain", "c,c++,fortran", False, "none", "x86-64-v3", "", True))
        self.assertEqual(egt.load_profile("wiz")["mode"], "plain")
        self.assertIn("SIGILL", self.said())

    def test_default_march_is_read_from_the_compiler(self):
        out_ = "The following options are target specific:\n  -m32   [disabled]\n  -march=                     \tx86-64-v3\n" \
               "  -mtune=                     \tznver4\n"
        self.assertEqual(egt.default_march(out_), "x86-64-v3")
        self.assertEqual(egt.default_march("nothing"), "")


# ------------------------------------------------------------------ clean

class Clean(TempRoot):
    def build_dir(self, name="gcc-16.2.0", tool=egt.TOOL):
        root = os.path.join(self.root, "w", "build", name)
        put(root, "state.json", json.dumps({"tool": tool, "source": os.path.join(self.root, "w", "src", "gcc-16.2.0"),
                                            "tarball": os.path.join(self.root, "w", "src", "gcc-16.2.0.tar.xz"),
                                            "stages": {}}))
        put(root, "logs/build.log", "x")
        put(root, "obj/gcc/xgcc", "x")
        put(self.root, "w/src/gcc-16.2.0/configure")
        put(self.root, "w/src/gcc-16.2.0.tar.xz")
        return root

    def run_clean(self, *extra):
        try:
            with mock.patch.object(egt.os, "geteuid", return_value=1000):
                return egt.main(["clean", "--workdir", os.path.join(self.root, "w"), "--build-id", "gcc-16.2.0"] + list(extra))
        finally:
            self.release_locks()

    def test_clean_keeps_the_record_and_logs(self):
        root = self.build_dir()
        self.assertEqual(self.run_clean(), 0)
        self.assertEqual(sorted(os.listdir(root)), [".lock", "logs", "state.json"])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "w", "src", "gcc-16.2.0")))
        self.assertEqual(self.run_clean("--all", "--sources"), 0)
        self.assertEqual(os.listdir(root), [".lock"])
        self.assertEqual(os.listdir(os.path.join(self.root, "w", "src")), [])

    def test_clean_touches_only_its_own(self):
        root = self.build_dir(tool="somebody_else")
        self.assertEqual(self.run_clean(), 1)
        self.assertTrue(os.path.exists(os.path.join(root, "obj", "gcc", "xgcc")))
        with mock.patch.object(egt.os, "geteuid", return_value=0):
            self.assertEqual(egt.main(["clean", "--workdir", self.root, "--build-id", "x"]), 1)


# ------------------------------------------------------------------ verification, for real

def wrap(prefix, names, real_dir="/usr/bin"):
    for n in names:
        script(prefix, "bin/" + n, 'exec %s/%s "$@"\n' % (real_dir, n))


@unittest.skipUnless(GNU and os.path.isfile("/usr/bin/ld.gold"), "needs the system's gcc and GNU binutils with gold")
class VerifyForReal(TempRoot):
    def test_binutils_checks_pass_through_wrappers_of_the_systems(self):
        prefix = os.path.join(self.root, "opt", "binutils-2.46")
        wrap(prefix, ("as", "ld", "ld.bfd", "ar", "nm", "objdump", "readelf", "strip", "ld.gold"))
        c = egt.verify_binutils(prefix, "/usr/bin/gcc", {"gold": True}, self.root)
        self.assertEqual(c.failed(), [], self.said())
        self.assertEqual(len(c.results), 7)

    def test_binutils_without_plugin_or_gold_fails(self):
        prefix = os.path.join(self.root, "opt", "binutils-x")
        wrap(prefix, ("as", "ld.bfd", "ar", "nm", "objdump", "readelf", "strip"))
        script(prefix, "bin/ld", 'case "$1" in --help) echo "usage: ld";; *) exec /usr/bin/ld "$@";; esac\n')
        c = egt.verify_binutils(prefix, "/usr/bin/gcc", {"gold": True}, self.root)
        self.assertIn("ld.gold", c.failed())
        self.assertIn("ld supports plugins (-plugin)", c.failed())

    def test_gcc_checks_tell_the_systems_libstdcxx_from_the_prefixs(self):
        # bin/gcc and bin/g++ are the system's: the C++20 program builds and runs with the
        # LD_LIBRARY_PATH -- and the rpath-alone check says its libstdc++ is NOT the prefix's.
        prefix = os.path.join(self.root, "opt", "gcc-sys")
        wrap(prefix, ("gcc", "g++"))
        c = egt.verify_gcc(prefix, {"mode": "quick", "languages": "c,c++,lto"}, self.root)
        passed = [n for n, good in c.results if good]
        self.assertIn("C++20 compile + link + run (rpath)", passed)
        self.assertIn("C: compile, link, run", passed)
        self.assertIn("-O2 -flto: compile, link, run", passed)
        self.assertIn("finds its own assembler (.base64 data)", passed)
        self.assertEqual(c.failed(), ["the same program with the rpath alone"])
        self.assertIn("skipped (--mode quick", self.said())


# ------------------------------------------------------------------ whole builds, fake trees

CONFIGURE = """#!/usr/bin/python3
# a fake configure: records its arguments, writes the Makefile from Makefile.in
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
prefix = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--prefix=")][0]
open("configure.args", "w").write("\\n".join(sys.argv[1:]) + "\\n")
with open(os.path.join(here, "..", "configure.count"), "a") as f:
    f.write("x\\n")
text = open(os.path.join(here, "Makefile.in")).read().replace("@prefix@", prefix)
open("Makefile", "w").write(text.replace("@flag@", os.path.join(here, "..", "FAIL")))
"""
BINUTILS_MAKEFILE = """prefix = @prefix@
TOOLS = as ld ld.bfd ar nm objdump readelf strip ld.gold
all:
\tmkdir -p ld gas
\tprintf '#!/bin/sh\\necho ld-new\\n' > ld/ld-new && chmod +x ld/ld-new
install:
\tmkdir -p $(DESTDIR)$(prefix)/bin
\tfor t in $(TOOLS); do printf '#!/bin/sh\\nexec /usr/bin/%s "$$@"\\n' $$t > $(DESTDIR)$(prefix)/bin/$$t && chmod +x $(DESTDIR)$(prefix)/bin/$$t || exit 1; done
"""
GCC_MAKEFILE = """prefix = @prefix@
profiledbootstrap bootstrap all:
\t@echo "LANGUAGES=[$(LANGUAGES)] BUILD_CONFIG=[$(BUILD_CONFIG)] BOOT_CFLAGS=[$(BOOT_CFLAGS)] goal=[$@]" > make.vars
\techo stage1 > stage_last
\t@if [ -f @flag@ ]; then echo stage2 > stage_current; echo "../../gcc/tree.c:1: error: boom"; exit 2; fi
\trm -f stage_current
\tmkdir -p gcc && printf '#!/bin/sh\\n' > gcc/xgcc && chmod +x gcc/xgcc
install-strip install:
\tmkdir -p $(DESTDIR)$(prefix)/bin
\tprintf '#!/bin/sh\\nexec /usr/bin/gcc "$$@"\\n' > $(DESTDIR)$(prefix)/bin/gcc
\tprintf '#!/bin/sh\\nexec /usr/bin/g++ "$$@"\\n' > $(DESTDIR)$(prefix)/bin/g++
\tchmod +x $(DESTDIR)$(prefix)/bin/gcc $(DESTDIR)$(prefix)/bin/g++
"""
PREREQ_PRESENT = "gmp='gmp-6.2.1.tar.bz2'\nmpfr='mpfr-4.1.0.tar.bz2'\nmpc='mpc-1.2.1.tar.gz'\nisl='isl-0.24.tar.bz2'\n"


@unittest.skipUnless(GNU and os.path.isfile("/usr/bin/ld.gold"), "needs the system's gcc, make and GNU binutils")
class WholeBuilds(TempRoot):
    def setUp(self):
        TempRoot.setUp(self)
        for p in (mock.patch.object(egt.os, "geteuid", return_value=1000),
                  mock.patch.object(egt, "preflight", self.ok_preflight)):
            p.start()
            self.addCleanup(p.stop)
        self.opt, self.work = os.path.join(self.root, "opt"), os.path.join(self.root, "work")

    @staticmethod
    def ok_preflight(o, m):
        return ({"tool": egt.TOOL, "ok": True, "package_manager": "dnf", "missing": [], "enable_repos": [],
                 "install_command": ""}, egt.pick_host(o, m), "(test)")

    def build(self, *argv):
        try:
            return egt.main(["build", "--opt-root", self.opt, "--workdir", self.work, "--yes", "-j", "2"] + list(argv))
        finally:
            self.release_locks()

    def binutils_tree(self):
        src = os.path.join(self.root, "trees", "binutils-with-gold-2.46")
        put(src, "configure", CONFIGURE, 0o755)
        put(src, "Makefile.in", BINUTILS_MAKEFILE)
        put(src, "bfd/version.m4", "m4_define([BFD_VERSION], [2.46])\n")
        os.makedirs(os.path.join(src, "gold"))
        return src

    def gcc_tree(self):
        src = os.path.join(self.root, "trees", "gcc-16.2.0")
        put(src, "configure", CONFIGURE, 0o755)
        put(src, "Makefile.in", GCC_MAKEFILE)
        put(src, "gcc/BASE-VER", "16.2.0\n")
        for d, name in (("c", "c"), ("cp", "c++"), ("lto", "lto")):
            put(src, "gcc/%s/config-lang.in" % d, 'language="%s"\n' % name)
        put(src, "contrib/download_prerequisites", PREREQ_PRESENT, 0o755)
        for n in ("gmp", "mpfr", "mpc", "isl"):
            os.makedirs(os.path.join(src, n))
        return src

    def test_binutils_from_tree_to_binutils_current(self):
        src = self.binutils_tree()
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0, self.said())
        prefix = os.path.join(self.opt, "binutils-2.46")
        self.assertEqual(os.readlink(os.path.join(self.opt, "binutils-current")), prefix)
        rec = egt.read_json(os.path.join(prefix, egt.MARKER))
        self.assertEqual((rec["component"], rec["verify"], rec["gold"], rec["cflags"]),
                         ("binutils", "ok", True, "-g -O3 -march=native"))
        root = os.path.join(self.work, "build", "binutils-2.46")
        st = egt.State(os.path.join(root, "state.json"))
        self.assertTrue(all(st.done(n) for n in ("source", "configure", "build", "install", "verify", "activate")))
        self.assertIn("CFLAGS=-g -O3 -march=native", st.d["configure"])
        self.assertIn("--enable-gold", st.d["configure"])
        self.assertFalse(os.path.exists(os.path.join(root, "obj")))          # removed once verified
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0)
        self.assertIn("nothing to do", self.said())
        # ...and gcc now pairs with it, by realpath
        ctx = egt.resolve(opts(opt_root=self.opt, workdir=self.work, gcc_version="16.2.0"), egt.detect_machine())
        self.assertEqual(ctx.pair, os.path.realpath(prefix))

    def test_a_recorded_install_that_no_longer_runs_is_not_done(self):
        """PLAN.md section 3: done is asked of the machine.  The record says every stage is
        done, but the prefix was removed: build again (the build tree was removed after the
        verified install, so from configure), never "nothing to do" with a dangling -current."""
        src = self.binutils_tree()
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0, self.said())
        prefix = os.path.join(self.opt, "binutils-2.46")
        shutil.rmtree(prefix)
        self.printed[:] = []
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0, self.said())
        self.assertNotIn("nothing to do", self.said())
        self.assertIn("does not run", self.said())
        self.assertIn("configure -> build -> install -> verify -> activate", self.said())
        self.assertEqual(egt.read_text(os.path.join(self.root, "trees", "configure.count")).count("x"), 2)
        self.assertTrue(egt.runs(os.path.join(self.opt, "binutils-current", "bin", "ld")))
        self.assertEqual(egt.read_json(os.path.join(prefix, egt.MARKER))["verify"], "ok")
        # there but broken: refused (never installed over), and not reported as done either
        with open(os.path.join(prefix, "bin", "ld"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        self.printed[:] = []
        self.assertEqual(self.build("--component", "binutils", "--source", src), 1)
        self.assertNotIn("nothing to do", self.said())
        self.assertIn("already exists", self.said())
        # ...and while it runs, a second build has nothing to do
        shutil.rmtree(prefix)
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0, self.said())
        self.printed[:] = []
        self.assertEqual(self.build("--component", "binutils", "--source", src), 0)
        self.assertIn("nothing to do", self.said())

    def test_gcc_fails_resumes_without_reconfiguring_and_is_not_linked_when_verify_fails(self):
        src = self.gcc_tree()
        flag = put(self.root, "trees/FAIL", "")
        with mock.patch.dict(os.environ, {"LANGUAGES": "c,c++,lto", "CC": "clang", "MAKEFLAGS": "-j99"}):
            code = self.build("--source", src, "--binutils", "system", "--ignore-disk")
        self.assertEqual(code, 1)
        self.assertIn("after stage 1 (in stage2)", self.said())
        self.assertIn("ignoring CC=clang", self.said())
        root = os.path.join(self.work, "build", "gcc-16.2.0")
        st = egt.State(os.path.join(root, "state.json"))
        self.assertEqual(st.stage("build")["status"], "failed")
        self.assertTrue(st.done("configure"))
        made = egt.read_text(os.path.join(root, "obj", "make.vars"))
        self.assertIn("LANGUAGES=[]", made)                                  # trap 6, end to end
        self.assertIn("BUILD_CONFIG=[bootstrap-lto]", made)
        self.assertIn("BOOT_CFLAGS=[-O2 -g -march=native -mtune=native]", made)
        self.assertIn("goal=[profiledbootstrap]", made)
        args = egt.read_text(os.path.join(root, "obj", "configure.args")).split("\n")
        self.assertIn("CC=/usr/bin/gcc", args)
        self.assertIn("--enable-languages=c,c++,lto", args)
        os.unlink(flag)
        self.printed[:] = []
        code = self.build("--source", src, "--binutils", "system", "--ignore-disk")
        self.assertEqual(code, 1)                                            # verify: the system's gcc is no bootstrap
        self.assertEqual(egt.read_text(os.path.join(self.root, "trees", "configure.count")).count("x"), 1)
        st = egt.State(os.path.join(root, "state.json"))
        self.assertTrue(st.done("build") and st.done("install"))
        self.assertEqual(st.stage("verify")["status"], "failed")
        prefix = os.path.join(self.opt, "gcc-16.2.0")
        self.assertEqual(egt.read_json(os.path.join(prefix, egt.MARKER))["verify"], "failed")
        self.assertFalse(os.path.lexists(os.path.join(self.opt, "gcc-current")))
        self.assertIn("NOT linked as gcc-current", self.said())
        self.assertTrue(os.path.isdir(os.path.join(root, "obj")))            # kept: the build is not verified

    def test_other_options_are_not_resumed_without_restart(self):
        src = self.gcc_tree()
        put(self.root, "trees/FAIL", "")
        self.assertEqual(self.build("--source", src, "--binutils", "system", "--ignore-disk"), 1)
        self.printed[:] = []
        self.assertEqual(self.build("--source", src, "--binutils", "system", "--ignore-disk", "--mode", "plain"), 1)
        self.assertIn("was started with other options", self.said())
        self.assertIn('then "profiled-lto", now "plain"', self.said())

    def test_an_existing_prefix_stops_before_building(self):
        src = self.binutils_tree()
        put(self.opt, "binutils-2.46/bin/ld", "someone's")
        self.assertEqual(self.build("--component", "binutils", "--source", src), 1)
        self.assertIn("already exists", self.said())
        self.assertFalse(os.path.exists(os.path.join(self.root, "trees", "configure.count")))


if __name__ == "__main__":
    unittest.main()
