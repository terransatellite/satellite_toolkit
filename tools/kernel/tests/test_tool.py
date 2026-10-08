#!/usr/bin/python3 -I
"""Unit tests for enterprise_linux_kernel_tool.py.

No network, no root, no kernel tree: everything that touches the machine takes
a `root=` argument and is pointed at a fake filesystem built in a temp dir.

    /usr/bin/python3 tests/test_tool.py
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "elkt", os.path.join(HERE, "..", "enterprise_linux_kernel_tool.py"))
elkt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(elkt)

# The tool talks a lot; the tests only care what it DOES.  Keep what it said in
# case a failing test wants to look.
SAID = []
elkt.out = lambda text="": SAID.append(text)


def put(root, path, text="", mode="w"):
    full = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, mode) as f:
        f.write(text)
    return full


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="elkt-test-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


RELEASES = json.dumps({
    "latest_stable": {"version": "7.2.6"},
    "releases": [
        {"moniker": "mainline", "version": "7.3-rc3", "iseol": False,
         "released": {"isodate": "2026-09-13"},
         "source": "https://git.kernel.org/torvalds/t/linux-7.3-rc3.tar.gz"},
        {"moniker": "stable", "version": "7.2.6", "iseol": False,
         "released": {"isodate": "2026-09-14"}, "source": "x"},
        {"moniker": "stable", "version": "7.1.13", "iseol": True,
         "released": {"isodate": "2026-09-02"}, "source": "x"},
        {"moniker": "longterm", "version": "6.18.52", "iseol": False,
         "released": {"isodate": "2026-09-14"}, "source": "x"},
        {"moniker": "linux-next", "version": "next-20260918", "iseol": False,
         "released": {"isodate": "2026-09-18"}, "source": None},
    ]})
SUMS = {7: {"7.2": "a" * 64, "7.2.4": "b" * 64, "7.2.6": "c" * 64, "7.1.13": "d" * 64},
        6: {"6.12": "e" * 64, "6.12.9": "f" * 64, "6.12.110": "0" * 64, "6.18.52": "1" * 64}}


def sums_for(major):
    if major not in SUMS:      # the real fetcher gets a 404 and raises the same
        raise elkt.ToolError("could not fetch v%d.x/sha256sums.asc" % major)
    return SUMS[major], "signed text"


class Versions(unittest.TestCase):
    def test_ordering(self):
        order = ["7.3-rc1", "7.3-rc3", "7.3", "7.3.1", "7.3.10", "7.10"]
        self.assertEqual(sorted(reversed(order), key=elkt.version_key), order)

    def test_numeric_not_lexical(self):
        self.assertGreater(elkt.version_key("6.12.110"), elkt.version_key("6.12.99"))

    def test_release_strings(self):
        self.assertEqual(elkt.series_of("6.12.0-211.53.1.el10_2.x86_64_v2"), "6.12")
        self.assertEqual(elkt.version_key("garbage"), (0, 0, 0, 0, 0))


class KernelOrg(unittest.TestCase):
    def setUp(self):
        self.latest, self.rels = elkt.parse_releases(RELEASES)

    def pick(self, sel):
        return elkt.resolve_kernel(sel, self.latest, self.rels, sums_for)

    def test_feed(self):
        self.assertEqual(self.latest, "7.2.6")
        self.assertTrue([r for r in self.rels if r["version"] == "7.1.13"][0]["eol"])

    def test_stable(self):
        t = self.pick("stable")
        self.assertEqual((t["version"], t["sha256"]), ("7.2.6", "c" * 64))
        self.assertTrue(t["url"].endswith("/v7.x/linux-7.2.6.tar.xz"))

    def test_longterm(self):
        self.assertEqual(self.pick("longterm")["version"], "6.18.52")

    def test_series_means_its_newest(self):
        self.assertEqual(self.pick("6.12")["version"], "6.12.110")

    def test_exact_old_release(self):
        self.assertEqual(self.pick("6.12.9")["sha256"], "f" * 64)

    def test_mainline_rc_has_no_checksum(self):
        t = self.pick("mainline")
        self.assertEqual(t["version"], "7.3-rc3")
        self.assertIsNone(t["sha256"])
        self.assertIn("git.kernel.org", t["url"])

    def test_eol_is_flagged(self):
        self.assertTrue(self.pick("7.1.13")["eol"])

    def test_unknown(self):
        for sel in ("6.12.999", "9.9", "7.0-rc1", "banana"):
            with self.assertRaises(elkt.ToolError):
                self.pick(sel)

    def test_sha256sums_parser(self):
        text = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
                + "a" * 64 + "  linux-7.2.6.tar.xz\n"
                + "b" * 64 + "  linux-7.2.6.tar.gz\n"
                + "c" * 64 + "  patch-7.2.6.xz\n"
                + "d" * 64 + "  linux-7.2.tar.xz\n-----BEGIN PGP SIGNATURE-----\n")
        self.assertEqual(elkt.parse_sha256sums(text), {"7.2.6": "a" * 64, "7.2": "d" * 64})


class Config(TempRoot):
    def cfg(self, text):
        return elkt.DotConfig(put(self.root, "c", text))

    def test_get(self):
        c = self.cfg('CONFIG_A=y\nCONFIG_B=m\n# CONFIG_C is not set\nCONFIG_S="x y"\nCONFIG_N=8192\n')
        self.assertEqual([c.get(s) for s in "ABCSNZ"], ["y", "m", "n", "x y", "8192", None])

    def test_set_appends_so_a_choice_is_won_by_the_last_line(self):
        # The legacy bug: DEBUG_INFO_NONE edited in place, ABOVE the DWARF line.
        c = self.cfg("# CONFIG_DEBUG_INFO_NONE is not set\n"
                     "CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y\n")
        c.set("DEBUG_INFO_NONE", "y")
        self.assertEqual(c.lines[-1], "CONFIG_DEBUG_INFO_NONE=y")
        self.assertEqual(len([l for l in c.lines if "DEBUG_INFO_NONE" in l]), 1)

    def test_set_does_not_touch_prefix_names(self):
        c = self.cfg("CONFIG_DRM_AMDGPU=y\nCONFIG_DRM_AMDGPU_SI=y\n")
        c.set("DRM_AMDGPU", "m")
        self.assertEqual((c.get("DRM_AMDGPU"), c.get("DRM_AMDGPU_SI")), ("m", "y"))

    def test_write_is_atomic_and_round_trips(self):
        c = self.cfg("CONFIG_A=y\n")
        c.set("LOCALVERSION", '"-elkt"')
        c.write()
        self.assertEqual(elkt.DotConfig(c.path).get("LOCALVERSION"), "-elkt")
        self.assertEqual([f for f in os.listdir(self.root) if ".tmp." in f], [])

    def test_validity(self):
        self.assertFalse(self.cfg("\n# a truncated fragment\n").valid())
        self.assertTrue(self.cfg("# CONFIG_X is not set\n").valid())


class Verify(TempRoot):
    def results(self, text, reqs, symbols):
        cfg = elkt.DotConfig(put(self.root, "c", text))
        return dict((r.sym, s) for r, _, s in elkt.verify_requests(cfg, reqs, symbols))

    def test_absent_symbol_is_a_note_not_a_crash(self):
        # B2: the legacy check() died silently right here.
        reqs = [elkt.Req("DRM_ACCEL_AMDXDNA", "m", "optional"),
                elkt.Req("DRM_XE", "m", "required")]
        self.assertEqual(self.results("CONFIG_A=y\n", reqs, set()),
                         {"DRM_ACCEL_AMDXDNA": "absent", "DRM_XE": "absent"})

    def test_present_but_dropped_is_graded_by_level(self):
        syms = {"R", "W", "O"}
        reqs = [elkt.Req("R", "y", "required"), elkt.Req("W", "y", "wanted"),
                elkt.Req("O", "y", "optional")]
        self.assertEqual(self.results("", reqs, syms), {"R": "fail", "W": "warn", "O": "note"})

    def test_module_asked_builtin_got_is_a_failure(self):
        # THE rule: a GPU driver that came out =y is wrong, not 'even better'.
        r = self.results("CONFIG_DRM_AMDGPU=y\n", [elkt.Req("DRM_AMDGPU", "m", "required")],
                         {"DRM_AMDGPU"})
        self.assertEqual(r["DRM_AMDGPU"], "fail")

    def test_at_least_module(self):
        r = elkt.Req("XFS_FS", "m|y", "required")
        self.assertTrue(r.met("y") and r.met("m"))
        self.assertFalse(r.met("n") or r.met(None))
        self.assertEqual((r.rendered("y"), r.rendered(None)), ("y", "m"))

    def test_off_and_empty_are_met_by_absence(self):
        self.assertTrue(elkt.Req("WERROR", "n", "required").met(None))
        self.assertTrue(elkt.Req("SYSTEM_REVOCATION_KEYS", ("str", ""), "required").met(None))
        self.assertFalse(elkt.Req("LOCALVERSION", ("str", "-x"), "required").met(None))


class Requests(unittest.TestCase):
    OPTS = dict(elkt.DEFAULTS, keep_symbols=[])
    MACHINE = {"firmware_compression": ["xz"],
               "storage": {"rootfs": "xfs", "fstypes": ["vfat", "xfs"], "layers": ["lvm"]}}

    def syms(self, vendors, **over):
        reqs = elkt.build_requests(dict(self.OPTS, **over), vendors, self.MACHINE)
        return dict((r.sym, r) for r in reqs), [r.sym for r in reqs]

    def test_gpu_drivers_are_modules(self):
        by, _ = self.syms({"amd", "nvidia", "intel"})
        for sym in ("DRM_AMDGPU", "DRM_NOUVEAU", "DRM_I915", "DRM_XE", "DRM_SIMPLEDRM"):
            self.assertEqual(by[sym].want, "m", sym)

    def test_this_machines_disks_are_required(self):
        by, _ = self.syms({"amd"})
        for sym in ("XFS_FS", "VFAT_FS", "BLK_DEV_DM"):
            self.assertEqual((by[sym].want, by[sym].level), ("m|y", "required"), sym)

    def test_firmware_decompressor_matches_what_is_on_disk(self):
        by, _ = self.syms({"amd"})
        self.assertEqual(by["FW_LOADER_COMPRESS_XZ"].level, "required")
        self.assertEqual(by["FW_LOADER_COMPRESS_ZSTD"].level, "wanted")

    def test_choice_members_are_cleared_before_the_winner_is_set(self):
        _, order = self.syms(set(), preempt="full", hz="1000")
        self.assertGreater(order.index("PREEMPT"), order.index("PREEMPT_LAZY"))
        self.assertGreater(order.index("HZ_1000"), order.index("HZ_250"))
        self.assertGreater(order.index("DEBUG_INFO_NONE"),
                           order.index("DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT"))

    def test_btf_mode_keeps_btf(self):
        by, _ = self.syms(set(), debug_info="btf")
        self.assertEqual((by["DEBUG_INFO_BTF"].want, by["DEBUG_INFO_NONE"].want), ("y", "n"))

    def test_p2p_dependency_precedes_it(self):
        _, order = self.syms({"amd"})
        self.assertLess(order.index("DMABUF_MOVE_NOTIFY"), order.index("HSA_AMD_P2P"))

    def test_gpu_selection(self):
        self.assertEqual(elkt.resolve_gpu("auto", ["amd"])[0], {"amd"})
        self.assertEqual(elkt.resolve_gpu("auto", [])[0], {"amd", "nvidia", "intel"})
        self.assertEqual(elkt.resolve_gpu("nv, intel", [])[0], {"nvidia", "intel"})
        self.assertEqual(elkt.resolve_gpu("none", ["amd"])[0], set())
        with self.assertRaises(elkt.ToolError):
            elkt.resolve_gpu("matrox", [])


class BaseConfig(TempRoot):
    HDR = "#\n# Automatically generated file; DO NOT EDIT.\n# Linux/x86 %s Kernel Configuration\n#\nCONFIG_X=y\n"

    def test_b1_running_custom_kernel_finds_its_build_tree(self):
        # No /boot/config-<rel>, no /proc/config.gz: exactly siege3 on 7.2.4-gpu.
        put(self.root, "boot/config-6.12.0-211.el10", self.HDR % "6.12.0")
        mine = put(self.root, "lib/modules/7.2.4-gpu/build/.config", self.HDR % "7.2.4")
        kind, path, _ = elkt.find_base_config("/nonexistent", "auto", "7.2.4-gpu", self.root, "7.2")
        self.assertEqual((kind, path), ("file", mine))

    def test_same_series_distro_config_beats_a_foreign_running_one(self):
        close = put(self.root, "boot/config-6.12.0-211.el10", self.HDR % "6.12.0")
        put(self.root, "lib/modules/7.2.4-gpu/build/.config", self.HDR % "7.2.4")
        _, path, label = elkt.find_base_config("/nonexistent", "auto", "7.2.4-gpu", self.root, "6.12")
        self.assertEqual(path, close)
        self.assertIn("--config-base running overrides", label)

    def test_a_full_build_is_not_seeded_from_this_tools_own_trimmed_config(self):
        # install writes /boot/config-<rel> for EVERY kernel, trimmed ones too.  Running
        # the distro's 6.12, the only 7.2 config in /boot was the trimmed one -- and the
        # same-series rule returned it before the want_full guard below it was reached.
        mods = lambda n: "".join("CONFIG_M%d=m\n" % i for i in range(n))
        full = put(self.root, "boot/config-6.12.0-211.el10", self.HDR % "6.12.0" + mods(400))
        trimmed = put(self.root, "boot/config-7.2.6-gpu", self.HDR % "7.2.6" + mods(40))
        args = ("/nonexistent", "auto", "6.12.0-211.el10", self.root, "7.2")
        self.assertEqual(elkt.find_base_config(*args, want_full=True)[1], full)
        self.assertEqual(elkt.find_base_config(*args, want_full=False)[1], trimmed)   # --modules trimmed: fine

    def test_running_means_running(self):
        put(self.root, "boot/config-6.12.0-211.el10", self.HDR % "6.12.0")
        mine = put(self.root, "lib/modules/7.2.4-gpu/build/.config", self.HDR % "7.2.4")
        self.assertEqual(elkt.find_base_config("/nonexistent", "running", "7.2.4-gpu",
                                               self.root, "6.12")[1], mine)

    def test_never_falls_back_to_defconfig_unasked(self):
        with self.assertRaises(elkt.ToolError):
            elkt.find_base_config("/nonexistent", "auto", "7.2.4-gpu", self.root)
        self.assertEqual(elkt.find_base_config("/nonexistent", "defconfig")[0], "defconfig")

    def test_b4_a_tree_config_is_adopted_only_if_valid(self):
        tree = os.path.join(self.root, "tree")
        put(tree, ".config", "# nothing but a comment\n")
        distro = put(self.root, "boot/config-7.2.4-gpu", self.HDR % "7.2.4")
        self.assertEqual(elkt.find_base_config(tree, "auto", "7.2.4-gpu", self.root)[1], distro)
        put(tree, ".config", self.HDR % "7.2.6")
        self.assertEqual(elkt.find_base_config(tree, "auto", "7.2.4-gpu", self.root)[1],
                         os.path.join(tree, ".config"))


class Clean(unittest.TestCase):
    def d(self, mode, objs, status, stage="", serving=()):
        return elkt.decide_clean(mode, objs, status, stage, list(serving))[0]

    def test_b5_a_failed_install_never_cleans_a_finished_build(self):
        for status, stage in (("built", "done"), ("installing", "install"),
                              ("installed", "done"), ("configured", "done")):
            self.assertFalse(self.d("auto", True, status, stage), status)

    def test_a_failure_we_saw_retries_incrementally(self):
        self.assertFalse(self.d("auto", True, "failed", "build"))

    def test_a_run_that_never_recorded_an_end_is_cleaned(self):
        self.assertTrue(self.d("auto", True, "building", "build"))

    def test_nothing_to_clean(self):
        self.assertFalse(self.d("auto", False, "building", "build"))

    def test_unrecorded_objects_are_left_alone(self):
        self.assertFalse(self.d("auto", True, ""))

    def test_b6_a_tree_in_service_is_protected(self):
        self.assertFalse(self.d("auto", True, "building", "build", ["7.2.4-gpu"]))
        with self.assertRaises(elkt.ToolError):
            self.d("always", True, "built", "done", ["7.2.4-gpu"])
        self.assertTrue(self.d("force", True, "built", "done", ["7.2.4-gpu"]))

    def test_never_and_bad_mode(self):
        self.assertFalse(self.d("never", True, "building"))
        with self.assertRaises(elkt.ToolError):
            self.d("sometimes", True, "")


class Machine(TempRoot):
    def pci(self, addr, cls, vendor, device, driver=None):
        d = "sys/bus/pci/devices/" + addr
        put(self.root, d + "/class", cls + "\n")
        put(self.root, d + "/vendor", vendor + "\n")
        put(self.root, d + "/device", device + "\n")
        if driver:
            os.symlink("/sys/bus/pci/drivers/" + driver, os.path.join(self.root, d, "driver"))

    def test_pci_by_class_and_vendor_id(self):
        self.pci("0000:04:00.0", "0x030000", "0x1002", "0x73e3", "amdgpu")
        self.pci("0000:04:00.1", "0x040300", "0x1002", "0xab28")      # its HDMI audio
        self.pci("0000:00:1f.2", "0x010601", "0x8086", "0x8d02")      # Intel SATA: not a GPU
        self.pci("0000:0a:00.0", "0x030000", "0x102b", "0x0534")      # Matrox BMC
        self.pci("0000:c5:00.1", "0x118000", "0x1022", "0x1502")      # not class 1200
        self.pci("0000:c6:00.1", "0x120000", "0x1022", "0x17f0")      # AMD NPU
        devs = elkt.scan_pci(self.root)
        self.assertEqual([d["addr"] for d in devs],
                         ["0000:04:00.0", "0000:0a:00.0", "0000:c6:00.1"])
        self.assertEqual(devs[0]["driver"], "amdgpu")
        self.assertEqual(elkt.gpu_vendors_present(devs), {"amd"})

    def test_an_intel_host_with_no_intel_gpu_is_not_intel(self):
        self.pci("0000:00:1f.2", "0x010601", "0x8086", "0x8d02")
        self.assertEqual(elkt.gpu_vendors_present(elkt.scan_pci(self.root)), set())

    def test_storage_stack_walks_lvm_and_raid(self):
        put(self.root, "proc/mounts",
            "sysfs /sys sysfs rw 0 0\n"
            "dm-0 / xfs rw 0 0\n"                       # not /dev-backed: ignored
            "/dev/dm-0 / xfs rw 0 0\n"
            "/dev/sde1 /boot/efi vfat rw 0 0\n"
            "/dev/sdz1 /run/media/u/STICK exfat rw 0 0\n")
        put(self.root, "sys/class/block/dm-0/dm/uuid", "LVM-abcdef\n")
        put(self.root, "sys/class/block/dm-0/slaves/md0/x")
        put(self.root, "sys/class/block/md0/md/level", "raid1\n")
        put(self.root, "sys/class/block/md0/slaves/sde3/x")
        st = elkt.storage_stack(self.root)
        self.assertEqual(st["rootfs"], "xfs")
        self.assertEqual(st["fstypes"], ["vfat", "xfs"])
        self.assertEqual(st["layers"], ["lvm", "md:raid1"])

    def test_secure_boot(self):
        self.assertEqual(elkt.secure_boot(self.root), "n/a")
        var = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
        put(self.root, var, b"\x06\x00\x00\x00\x01", "wb")
        self.assertEqual(elkt.secure_boot(self.root), "on")
        put(self.root, var, b"\x06\x00\x00\x00\x00", "wb")
        self.assertEqual(elkt.secure_boot(self.root), "off")

    def test_in_service(self):
        tree = os.path.join(self.root, "src/linux-7.2.4")
        os.makedirs(tree)
        os.makedirs(os.path.join(self.root, "lib/modules/7.2.4-gpu"))
        os.symlink(tree, os.path.join(self.root, "lib/modules/7.2.4-gpu/build"))
        self.assertEqual(elkt.in_service(tree, self.root), ["7.2.4-gpu"])
        self.assertEqual(elkt.in_service(self.root, self.root), [])

    def test_module_coverage_normalises_hyphens(self):
        tree = os.path.join(self.root, "tree")
        put(tree, "modules.order", "drivers/gpu/drm/virtio/virtio-gpu.o\nfs/xfs/xfs.o\n")
        put(tree, "modules.builtin", "kernel/drivers/ata/ahci.ko\n")
        put(self.root, "proc/modules",
            "virtio_gpu 1 0 - Live 0x0\nxfs 1 0 - Live 0x0\nahci 1 0 - Live 0x0\n"
            "zfs 1 0 - Live 0x0\nsep5 1 0 - Live 0x0\n")
        put(self.root, "sys/module/sep5/taint", "OE\n")
        cov = elkt.module_coverage(tree, self.root)
        self.assertEqual((cov["missing"], cov["out_of_tree"]), (["zfs"], ["sep5"]))


class Dkms(unittest.TestCase):
    STATUS = ("amdgpu/6.19.14-2364437.el10, 6.12.0-211.50.1.el10_2.x86_64_v2, x86_64_v2: "
              "installed (Original modules exist)\n"
              "amdgpu/6.19.14-2364437.el10, 6.12.0-211.53.1.el10_2.x86_64_v2, x86_64_v2: installed\n"
              "nvidia, 570.86, 6.8.0-45-generic, x86_64: installed\n")

    def test_status_both_formats(self):
        mods = elkt.parse_dkms_status(self.STATUS)
        self.assertEqual(len(mods[("amdgpu", "6.19.14-2364437.el10")]), 2)
        self.assertEqual(mods[("nvidia", "570.86")], [("6.8.0-45-generic", "installed")])

    def test_cap_follows_dkms_own_rule(self):
        lim = elkt.parse_dkms_conf('PACKAGE_NAME="amdgpu"\nBUILD_EXCLUSIVE_KERNEL_MAX="6.18"\n')
        self.assertEqual(lim, {"BUILD_EXCLUSIVE_KERNEL_MAX": "6.18"})
        self.assertFalse(elkt.dkms_will_build(lim, "7.2.6-elkt")[0])
        self.assertTrue(elkt.dkms_will_build(lim, "6.12.110-elkt")[0])
        self.assertFalse(elkt.dkms_will_build(lim, "6.18.52")[0])   # 6.18.52 > "6.18"
        self.assertTrue(elkt.dkms_will_build({}, "7.2.6")[0])


class BuildLog(TempRoot):
    def test_first_real_errors_not_file_names(self):
        log = put(self.root, "build.log",
                  "  HDRINST usr/include/linux/can/error.h\n"
                  "  CC      kernel/fork.o\n"
                  "drivers/x.c:10:5: warning: unused variable 'y'\n"
                  "drivers/x.c:12:5: error: 'foo' undeclared\n"
                  "make[3]: *** [scripts/Makefile.build:289: drivers/x.o] Error 1\n")
        hits = elkt.error_summary(log)
        self.assertEqual(len(hits), 2)
        self.assertIn("'foo' undeclared", hits[0])

    def test_step_lines(self):
        self.assertEqual(elkt.STEP.match("  CC [M]  drivers/gpu/drm/amd/amdgpu/amdgpu_drv.o").group(1), "CC [M]")
        self.assertIsNone(elkt.STEP.match("Kernel: arch/x86/boot/bzImage is ready  (#1)"))


class Options(TempRoot):
    def test_profile_round_trip_and_cli_wins(self):
        path = os.path.join(self.root, "fleet.ini")
        elkt.save_profile(path, dict(elkt.DEFAULTS, kernel="6.12", modules="trimmed", hz="1000"))
        args = elkt.build_parser().parse_args(["build", "--profile", path, "--hz", "250"])
        opts = elkt.merge_options(args)
        self.assertEqual((opts["kernel"], opts["modules"], opts["hz"]), ("6.12", "trimmed", "250"))

    def test_bad_values_are_refused(self):
        for argv in (["build", "--preempt", "sometimes"], ["build", "--jobs", "many"],
                     ["build", "--localversion", "-a b"], ["build", "--debug-info", "maybe"]):
            with self.assertRaises(elkt.ToolError):
                elkt.merge_options(elkt.build_parser().parse_args(argv))

    def test_state_survives_and_separates_trees(self):
        path = os.path.join(self.root, "state.ini")
        s = elkt.State(path)
        s.update("/src/linux-7.2.6", status="built", release="7.2.6-elkt")
        s.update("/src/linux-6.12.110", status="failed", stage="build")
        again = elkt.State(path)
        self.assertEqual(again.get("/src/linux-7.2.6")["release"], "7.2.6-elkt")
        self.assertEqual(again.get("/src/linux-6.12.110")["stage"], "build")
        self.assertEqual(again.get("/src/other"), {})

    def test_scrubbed_env_is_an_allow_list(self):
        os.environ["CC"] = "/somewhere/clang"
        os.environ["LD_RUN_PATH"] = "/x"
        os.environ["LOCALVERSION"] = "-gpu"
        env = elkt.scrubbed_env()
        for leaked in ("CC", "LD_RUN_PATH", "LOCALVERSION", "PYTHONPATH"):
            self.assertNotIn(leaked, env)
        self.assertTrue(env["PATH"].startswith("/usr/bin:"))


class ReviewFindings(TempRoot):
    """One test per bug a fresh-eyes review PROVED in the first version."""

    def test_localversion_value_starting_with_a_dash(self):
        argv = elkt.normalize_argv(["build", "--localversion", "-gpu", "--hz", "250"])
        self.assertEqual(argv, ["build", "--localversion=-gpu", "--hz", "250"])
        opts = elkt.merge_options(elkt.build_parser().parse_args(argv))
        self.assertEqual(opts["localversion"], "-gpu")

    def test_only_argv0_can_be_the_command(self):
        self.assertEqual(elkt.normalize_argv(["--workdir", "build"])[0], "wizard")
        self.assertEqual(elkt.normalize_argv(["--kernel", "6.12"])[:2], ["wizard", "--kernel"])
        self.assertEqual(elkt.normalize_argv(["status"]), ["status"])
        self.assertEqual(elkt.normalize_argv([]), ["wizard"])
        self.assertEqual(elkt.normalize_argv(["--version"]), ["--version"])

    def test_a_second_clean_restores_the_config_it_just_saved(self):
        tree = os.path.join(self.root, "tree")
        real = put(tree, ".config", "CONFIG_GEN=1\n")

        class FakeMake(object):
            def call(self, targets, **kw):
                if os.path.exists(real):
                    os.unlink(real)          # what mrproper does
        elkt.clean_tree(FakeMake(), tree)
        self.assertIn("CONFIG_GEN=1", elkt.read_text(real))
        put(tree, ".config", "CONFIG_GEN=2\n")
        elkt.clean_tree(FakeMake(), tree)
        self.assertIn("CONFIG_GEN=2", elkt.read_text(real))       # was: GEN=1 resurrected
        self.assertIn("CONFIG_GEN=1", elkt.read_text(real + ".before-clean"))

    def test_an_interrupted_mrproper_does_not_lose_the_config(self):
        tree = os.path.join(self.root, "tree")
        real = put(tree, ".config", "CONFIG_MINE=y\n")

        class DyingMake(object):
            def call(self, targets, **kw):
                os.unlink(real)
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            elkt.clean_tree(DyingMake(), tree)
        self.assertIn("CONFIG_MINE=y", elkt.read_text(real))

    def test_checksums_outside_the_signed_block_are_ignored(self):
        text = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
                + "a" * 64 + "  linux-7.2.6.tar.xz\n"
                + "-----BEGIN PGP SIGNATURE-----\n\niQEz...\n-----END PGP SIGNATURE-----\n"
                + "f" * 64 + "  linux-7.2.6.tar.xz\n")
        self.assertEqual(elkt.parse_sha256sums(text), {"7.2.6": "a" * 64})

    def test_checksums_BEFORE_the_signed_block_are_refused(self):
        # gpg skips whatever precedes the first real armor line and still says GOODSIG;
        # an unanchored regex started at the FAKE header, and "first wins" took the evil sum.
        genuine = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
                   + "a" * 64 + "  linux-7.2.6.tar.xz\n"
                   + "-----BEGIN PGP SIGNATURE-----\n\niQEz...\n-----END PGP SIGNATURE-----\n")
        evil = "x-----BEGIN PGP SIGNED MESSAGE-----\n\n" + "e" * 64 + "  linux-7.2.6.tar.xz\n" + genuine
        with self.assertRaises(elkt.ToolError):
            elkt.parse_sha256sums(evil)
        with self.assertRaises(elkt.ToolError):
            elkt.parse_sha256sums("e" * 64 + "  linux-7.2.6.tar.xz\n" + genuine)
        self.assertEqual(elkt.parse_sha256sums("\n" + genuine), {"7.2.6": "a" * 64})
        plain = "b" * 64 + "  linux-7.2.6.tar.xz\n"                 # an unsigned list is still a list
        self.assertEqual(elkt.parse_sha256sums(plain), {"7.2.6": "b" * 64})

    def test_a_missing_cc_is_an_error_not_the_system_gcc(self):
        for cc in ("/nonexistent/bin/gcc", "gcc-that-is-not-installed-99"):
            with self.assertRaises(elkt.ToolError):
                elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=cc), {})

    def test_two_instances_do_not_overwrite_each_others_trees(self):
        path = os.path.join(self.root, "state.ini")
        one = elkt.State(path)
        one.update("/src/A", status="building")
        two = elkt.State(path)                   # starts later; snapshot says A=building
        one.update("/src/A", status="built")
        two.update("/src/B", status="built")     # was: rewrote A=building
        self.assertEqual(elkt.State(path).get("/src/A")["status"], "built")

    def test_dying_while_configuring_is_not_a_reason_to_clean(self):
        self.assertFalse(elkt.decide_clean("auto", True, "configuring", "configure", [])[0])

    def test_a_trimmed_config_in_boot_is_not_the_base_for_a_full_build(self):
        hdr = "# Linux/x86 %s Kernel Configuration\n"
        distro = put(self.root, "boot/config-6.12.0-211.el10",
                     hdr % "6.12.0" + "".join("CONFIG_M%d=m\n" % i for i in range(2000)))
        put(self.root, "boot/config-6.12.9-elkt",       # installed by this tool, trimmed
            hdr % "6.12.9" + "".join("CONFIG_M%d=m\n" % i for i in range(90)))
        put(self.root, "lib/modules/7.2.4-gpu/build/.config", hdr % "7.2.4" + "CONFIG_X=y\n")
        path = elkt.find_base_config("/nonexistent", "auto", "7.2.4-gpu", self.root, "6.12")[1]
        self.assertEqual(path, distro)

    def test_running_may_be_this_very_tree(self):
        tree = os.path.join(self.root, "src/linux-7.2.4")
        own = put(tree, ".config", "# Linux/x86 7.2.4 Kernel Configuration\nCONFIG_X=y\n")
        os.makedirs(os.path.join(self.root, "lib/modules/7.2.4-gpu"))
        os.symlink(tree, os.path.join(self.root, "lib/modules/7.2.4-gpu/build"))
        path = elkt.find_base_config(tree, "running", "7.2.4-gpu", self.root)[1]
        self.assertEqual(os.path.realpath(path), os.path.realpath(own))

    def test_held_config_recovery(self):
        tree = os.path.join(self.root, "tree")
        put(tree, ".config", "STAGING COPY\n")
        put(tree, elkt.HOLD, "CONFIG_REAL=y\n")
        elkt.recover_held_config(tree)
        self.assertIn("CONFIG_REAL", elkt.read_text(os.path.join(tree, ".config")))
        # ...and when the killed run had no .config of its own:
        put(tree, ".config", "STAGING COPY\n")
        put(tree, elkt.HOLD_NONE, "")
        elkt.recover_held_config(tree)
        self.assertFalse(os.path.exists(os.path.join(tree, ".config")))
        self.assertFalse(os.path.exists(os.path.join(tree, elkt.HOLD_NONE)))

    def test_typed_answers_are_validated_like_flags(self):
        with self.assertRaises(elkt.ToolError):
            elkt.validate_options(dict(elkt.DEFAULTS, localversion='-my "kernel" $(x)'))

    def test_a_profile_holds_what_was_asked_not_what_it_resolved_to(self):
        path = os.path.join(self.root, "fleet.ini")
        opts = elkt.merge_options(elkt.build_parser().parse_args(["build"]))
        self.assertGreater(opts["jobs"], 0)
        elkt.save_profile(path, opts)
        saved = elkt.load_profile(path)
        self.assertEqual((saved["jobs"], saved["workdir"]), ("0", "~/src"))

    def test_trimmed_keeps_the_parent_of_the_drm_drivers(self):
        reqs = elkt.build_requests(dict(elkt.DEFAULTS, keep_symbols=[], modules="trimmed"),
                                   set(), {"storage": {}})
        order = [r.sym for r in reqs]
        self.assertLess(order.index("DRM"), order.index("DRM_SIMPLEDRM"))


class Install(TempRoot):
    """install_kernel() against a fake sudo.  The real thing needs a password."""

    def setUp(self):
        TempRoot.setUp(self)
        self.old = put(self.root, "boot/vmlinuz-old", "k")
        self.new = put(self.root, "boot/vmlinuz-new", "k")
        self.default = self.old
        self.calls = []
        self.fail_on = None
        self.saved = (elkt.sudo, elkt.SudoKeepalive, elkt.install_preflight,
                      elkt.post_install_checks)
        test = self

        class P(object):
            def __init__(self, out_="", rc=0):
                self.stdout, self.returncode = out_, rc

        def fake_sudo(cmd, check=True, capture=False, dry=False):
            test.calls.append(list(cmd))
            if cmd[:2] == ["grubby", "--default-kernel"]:
                # stderr is merged in, and grubby is noisy: junk before AND after
                return P("grep: /boot/grub2/grubenv: warning\n%s\n/boot\n" % test.default)
            if cmd[:2] == ["grubby", "--set-default"]:
                test.default = cmd[2]
                return P()
            if cmd[0] == "make" and cmd[-1] == "install":
                test.default = test.new          # installkernel moves it FIRST...
                if test.fail_on == "install":
                    raise elkt.ToolError("dracut: no space left on device")   # ...then dies
            return P()

        class NoKeepalive(object):
            def start(self): pass
            def stop(self): pass
        elkt.sudo, elkt.SudoKeepalive = fake_sudo, NoKeepalive
        elkt.install_preflight = lambda *a, **k: None
        elkt.post_install_checks = lambda *a, **k: None
        self.tree = os.path.join(self.root, "tree")
        put(self.tree, ".config", "CONFIG_X=y\n")
        self.state = elkt.State(os.path.join(self.root, "state.ini"))
        self.mk = elkt.Make(self.tree, {"args": [], "extra_path": None}, 4)
        self.machine = {"boot": {"grubby": True}, "running": "x", "secure_boot": "off"}

    def tearDown(self):
        (elkt.sudo, elkt.SudoKeepalive, elkt.install_preflight,
         elkt.post_install_checks) = self.saved
        TempRoot.tearDown(self)

    def go(self, **over):
        opts = dict(dry_run=False, set_default=False, yes=False, allow_unsigned=False)
        opts.update(over)
        elkt.install_kernel(self.tree, self.mk, self.machine, set(), "7.2.6-elkt", opts, self.state)

    def test_default_entry_is_put_back(self):
        self.go()
        self.assertEqual(self.default, self.old)
        self.assertEqual(self.state.get(self.tree)["status"], "installed")

    def test_default_entry_is_put_back_even_when_make_install_fails(self):
        self.fail_on = "install"
        with self.assertRaises(elkt.ToolError):
            self.go()
        self.assertEqual(self.default, self.old)     # was: left on the new, initramfs-less kernel

    def test_a_failed_install_puts_the_default_back_even_with_set_default(self):
        # --set-default wants the new kernel as default once it is INSTALLED.  The
        # guard used to read `not set_default`, so this -- the path the first real
        # install took -- was the one failure that skipped the restore.
        self.fail_on = "install"
        with self.assertRaises(elkt.ToolError):
            self.go(set_default=True)
        self.assertEqual(self.default, self.old)

    def test_set_default_really_sets_it(self):
        self.go(set_default=True)
        self.assertIn(["grubby", "--set-default", "/boot/vmlinuz-7.2.6-elkt"], self.calls)
        self.assertEqual(self.default, "/boot/vmlinuz-7.2.6-elkt")

    def test_dry_run_runs_nothing_and_records_nothing(self):
        real_sudo = self.saved[0]
        elkt.sudo = lambda cmd, check=True, capture=False, dry=False: (
            self.assertTrue(dry, "sudo called for real in a dry run: %r" % (cmd,)) or
            real_sudo(cmd, check, capture, dry))
        self.go(dry_run=True)
        self.assertEqual(self.state.get(self.tree), {})


ARCH = elkt.TRIPLE_ARCH.get(os.uname().machine, os.uname().machine)
GCC13 = "gcc (GCC) 13.2.0"
CLANG18 = "Ubuntu clang version 18.1.3 (1ubuntu1)"
DPCPP = "Intel(R) oneAPI DPC++/C++ Compiler 2026.1.1 (2026.1.1.20260724)"


def fake_tool(path, banner, triple=None, first_line="", installed=""):
    """An executable that answers --version / -v / -dumpmachine like the real
    thing and 'compiles' anything else successfully."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\ncase \"$1\" in\n"
                "  --version|-v) %secho '%s'; %s;;\n"
                "  -dumpmachine) echo '%s';;\n"
                "  -print-prog-name=*) echo \"${1#-print-prog-name=}\";;\n"
                "  -c) echo '%s';;\n"
                "  *) cat >/dev/null 2>&1; exit 0;;\nesac\n"
                % ("echo '%s'; " % first_line if first_line else "", banner,
                   "echo 'InstalledDir: %s'" % installed if installed else ":",
                   triple or ARCH + "-pc-linux-gnu", banner))
    os.chmod(path, 0o755)
    return path


class Toolchains(TempRoot):
    def test_version_banners(self):
        for banner, want in (
                ("gcc (GCC) 14.3.1 20251022 (Red Hat 14.3.1-4)", (14, 3, 1)),
                ("gcc (crosstool-NG 1.26.0) 13.2.0", (13, 2, 0)),
                ("gcc (Ubuntu 13.2.0-23ubuntu4) 13.2.0", (13, 2, 0)),
                (CLANG18, (18, 1, 3)), (DPCPP, (2026, 1, 1)),
                ("GNU ld version 2.41-63.el10.alma.1", (2, 41)),
                ("GNU ld (GNU Binutils) 2.46", (2, 46)),
                ("GNU bash, version 5.2.26(1)-release (x86_64-redhat-linux-gnu)", (5, 2, 26)),
                ("kmod version 31", (31,)), ("v1.31", (1, 31)), ("3.9.x", (3, 9)),
                ("rustc 1.92.0 (ded5c06cf 2025-12-08) (Red Hat 1.92.0-1.el10)", (1, 92, 0))):
            self.assertEqual(elkt.numver(banner), want, banner)

    def test_version_comparison_pads(self):
        self.assertTrue(elkt.ver_ge((8, 1), (8, 1, 0)))       # a bare tuple compare says False
        self.assertTrue(elkt.ver_ge((31,), (13,)))
        self.assertFalse(elkt.ver_ge((1, 6, 95), (1, 7)))
        self.assertTrue(elkt.ver_ge((1, 7, 1), (1, 6, 95)))

    def test_scan_wants_executable_files_once(self):
        a, b = os.path.join(self.root, "a"), os.path.join(self.root, "b")
        fake_tool(a + "/clang-21", CLANG18)
        os.symlink("clang-21", a + "/clang")
        os.symlink(a + "/clang-21", os.path.join(self.root, "alias-of-clang"))
        os.makedirs(b + "/gcc")                       # PATH held ~/opt/gcc-17/lib: a DIRECTORY named gcc
        put(b, "gcc-12", "not executable")
        fake_tool(b + "/x86_64-linux-gnu-gcc", GCC13)  # a cross-style name: not looked for
        fake_tool(b + "/python3.11", "CPython 3.11.2")
        cc, py, cxx = elkt.scan_tool_dirs([a, b])
        self.assertEqual(cc, [a + "/clang"])          # the plain name, and only once
        self.assertEqual(py, [b + "/python3.11"])

    def test_intel_dpcpp_is_recognised_and_refused(self):
        icx = fake_tool(self.root + "/opt/intel/oneapi/compiler/2026.1/bin/icx", DPCPP)
        ci = elkt.compiler_info(icx)
        self.assertEqual((ci["kind"], ci["usable"]), ("intel", "no"))   # was: filed as gcc
        self.assertIn("removed in Linux 6.2", ci["why"])
        with self.assertRaises(elkt.ToolError):
            elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=icx), {})

    def test_a_deprecation_warning_is_not_the_banner(self):
        dp = fake_tool(self.root + "/bin/dpcpp", DPCPP,
                       first_line="icpx: warning: use of dpcpp is deprecated")
        self.assertEqual(elkt.compiler_info(dp)["version"], DPCPP)

    def test_vendor_forks_and_cross_compilers(self):
        rocm = fake_tool(self.root + "/opt/rocm/llvm/bin/clang", "AMD clang version 23.0.0git")
        self.assertEqual(elkt.compiler_info(rocm)["usable"], "fork")
        cross = fake_tool(self.root + "/x/gcc", GCC13, triple="sparc64-unknown-linux-gnu")
        ci = elkt.compiler_info(cross)
        self.assertEqual(ci["usable"], "no")
        self.assertIn("sparc64", ci["why"])

    def test_one_compiler_under_two_names_is_listed_once_usable_ones_first(self):
        d = self.root + "/bin"
        fake_tool(d + "/amdclang", "AMD clang version 23.0.0git", installed="/opt/rocm/llvm/bin")
        fake_tool(d + "/clang", "AMD clang version 23.0.0git", installed="/opt/rocm/llvm/bin")
        fake_tool(d + "/icx", DPCPP)
        fake_tool(d + "/gcc-13", GCC13)
        found = elkt.find_toolchains([d])
        self.assertEqual([os.path.basename(f["path"]) for f in found], ["gcc-13", "clang", "icx"])

    def test_llvm_argument_forms(self):
        d = self.root + "/llvm/bin"
        fake_tool(d + "/clang", CLANG18)
        self.assertIsNone(elkt.llvm_companions(d + "/clang"))            # no tools beside it
        for tool in elkt.LLVM_TOOLS:
            fake_tool(os.path.join(d, tool), "LLD 18")
            fake_tool(os.path.join(d, tool + "-18"), "LLD 18")
        self.assertEqual(elkt.llvm_companions(d + "/clang"), ("LLVM=" + d + "/", None))
        fake_tool(d + "/clang-18", CLANG18)
        self.assertEqual(elkt.llvm_companions(d + "/clang-18"), ("LLVM=-18", d))

    def toolset(self):
        d = self.root + "/opt/rh/gcc-toolset-15/root/usr/bin"
        fake_tool(d + "/gcc", "gcc (GCC) 15.2.1 20260123 (Red Hat 15.2.1-7)")
        fake_tool(d + "/ld", "GNU ld version 2.44-8.el10")
        fake_tool(d + "/as", "GNU assembler version 2.44-8.el10")
        return d

    def test_a_toolset_gcc_gets_its_own_binutils_and_an_absolute_cc(self):
        d = self.toolset()
        tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc"), {})
        self.assertEqual(tc["extra_path"], d)
        self.assertIn("CC=" + d + "/gcc", tc["args"])      # never a bare CC=gcc with PATH changed
        self.assertTrue([a for a in tc["args"] if a.startswith("HOSTCC=")])
        self.assertIn("vendor-supported", tc["origin"])

    def test_contradictory_choices_are_refused(self):
        d = self.toolset()
        bad = (dict(cc=d + "/gcc", binutils="llvm"),             # LLVM binutils need clang
               dict(cc=d + "/gcc", binutils=self.root),           # no ld/as in that directory
               dict(cc=d + "/gcc", toolchain="llvm"),             # says llvm, is gcc
               dict(cc=d + "/gcc", python=d + "/gcc"))            # not a python
        for over in bad:
            with self.assertRaises(elkt.ToolError):
                elkt.resolve_toolchain(dict(elkt.DEFAULTS, **over), {})

    def test_gcc_with_lld(self):
        d = self.toolset()
        lld = fake_tool(self.root + "/llvm/bin/ld.lld", "LLD 18.1.3")
        fake_tool(self.root + "/llvm/bin/clang", CLANG18)
        machine = {"toolchains": [elkt.compiler_info(self.root + "/llvm/bin/clang")]}
        real_which = elkt.which
        elkt.which = lambda name, path=None: None if name == "ld.lld" else real_which(name, path)
        try:
            tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc", linker="lld"), machine)
        finally:
            elkt.which = real_which
        self.assertIn("LD=" + lld, tc["args"])

    def test_binutils_hard_links_are_one_set(self):
        a, b = self.root + "/binutils/bin", self.root + "/binutils/x86_64-pc-linux-gnu/bin"
        for tool in ("ld", "as"):
            fake_tool(os.path.join(a, tool), "GNU ld (GNU Binutils) 2.46")
        os.makedirs(b)
        for tool in ("ld", "as"):
            os.link(os.path.join(a, tool), os.path.join(b, tool))
        fake_tool(self.root + "/lld-as-ld/ld", "LLD 18.1.3 (compatible with GNU linkers)")
        fake_tool(self.root + "/lld-as-ld/as", "x")
        sets = elkt.find_binutils([b, a, self.root + "/lld-as-ld"])
        self.assertEqual([(s_["dir"], s_["numver"]) for s_ in sets], [(a, (2, 46))])

    def test_pythons(self):
        d = self.root + "/bin"
        fake_tool(d + "/python3.11", "CPython 3.11.2")
        fake_tool(d + "/pypy3", "PyPy 3.10.14")
        fake_tool(d + "/pypy", "PyPy 2.7.18")             # a Python 2: not offered
        got = [(y["impl"], y["version"]) for y in elkt.find_pythons([d])]
        self.assertEqual(got, [("CPython", "3.11.2"), ("PyPy", "3.10.14")])

    def test_dkms_state_is_read_from_disk(self):
        base = "var/lib/dkms/amdgpu"
        for k in ("6.12.0-211.50.1.el10", "6.12.0-211.53.1.el10"):
            os.makedirs(os.path.join(self.root, base, "6.19.14", k, "x86_64"))
        os.makedirs(os.path.join(self.root, base, "original_module"))
        os.symlink("6.19.14/6.12.0-211.53.1.el10/x86_64",
                   os.path.join(self.root, base, "kernel-6.12.0-211.53.1.el10-x86_64"))
        put(self.root, "usr/src/amdgpu-6.19.14/dkms.conf", 'BUILD_EXCLUSIVE_KERNEL_MAX="6.18"\n')
        mods = elkt.dkms_modules(self.root)
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["kernels"], [("6.12.0-211.50.1.el10", "built"),
                                              ("6.12.0-211.53.1.el10", "installed")])
        self.assertEqual(mods[0]["limits"], {"BUILD_EXCLUSIVE_KERNEL_MAX": "6.18"})

    TABLE = ("Intro text\n\n"
             "====================== ===============  ========================================\n"
             "        Program        Minimal version       Command to check the version\n"
             "====================== ===============  ========================================\n"
             "bash                   4.2              bash --version\n"
             "GNU C                  %s              gcc --version\n"
             "Clang/LLVM (optional)  17.0.1           clang --version\n"
             "bindgen (optional)     0.71.1           elkt-no-such-bindgen --version\n"
             "e2fsprogs              99.0             e2fsck -V\n"
             "====================== ===============  ========================================\n"
             "after\n")

    def test_a_tree_states_its_own_requirements(self):
        rows = elkt.parse_requirements(self.TABLE % "8.1 ")
        self.assertEqual(rows[1], ("GNU C", "8.1", "gcc --version"))
        self.assertEqual(len(rows), 5)

    def test_the_chosen_compiler_is_what_gets_checked(self):
        d = self.toolset()
        tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc"), {})
        tree = os.path.join(self.root, "tree")
        put(tree, "Documentation/process/changes.rst", self.TABLE % "8.1 ")
        self.assertEqual(elkt.check_tree_requirements(tree, tc), 0)   # e2fsprogs 99.0: runtime, ignored
        put(tree, "Documentation/process/changes.rst", self.TABLE % "16.0")
        self.assertEqual(elkt.check_tree_requirements(tree, tc), 1)   # 15.2.1 < 16.0

    def test_pahole_is_only_demanded_of_a_build_that_runs_it(self):
        # changes.rst never marks pahole optional, but --debug-info off never calls it:
        # RHEL 8 / Debian 12 / Ubuntu 24.04 were refused over a tool the build would not use.
        d = self.toolset()
        tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc"), {})
        tree = os.path.join(self.root, "tree")
        table = (self.TABLE % "8.1 ").replace(
            "e2fsprogs ", "pahole                 99.0             elkt-no-such-pahole --version\ne2fsprogs ")
        put(tree, "Documentation/process/changes.rst", table)
        self.assertEqual(elkt.check_tree_requirements(tree, tc), 1)
        self.assertEqual(elkt.check_tree_requirements(tree, tc, need_pahole=False), 0)

    def test_a_compressor_the_config_needs_must_exist(self):
        cfg = elkt.DotConfig(put(self.root, "c", "CONFIG_KERNEL_LZ4=y\nCONFIG_MODULE_COMPRESS_XZ=y\n"
                                                  "# CONFIG_KERNEL_ZSTD is not set\n"))
        self.assertEqual(elkt.check_compressors(cfg, "/nonexistent"), 2)
        fake_tool(self.root + "/bin/lz4", "lz4 1.9")
        fake_tool(self.root + "/bin/xz", "xz 5.4")
        self.assertEqual(elkt.check_compressors(cfg, self.root + "/bin"), 0)

    def test_wizard_asks_for_the_compiler_then_the_tools(self):
        d = self.toolset()
        other = self.root + "/opt/binutils-246/bin"
        for tool in ("ld", "as"):
            fake_tool(os.path.join(other, tool), "GNU ld (GNU Binutils) 2.46")
        fake_tool(self.root + "/intel/bin/icx", DPCPP)
        dirs = [d, other, self.root + "/intel/bin"]
        machine = {"toolchains": elkt.find_toolchains(dirs), "binutils": elkt.find_binutils(dirs),
                   "pythons": []}
        answers = ["9", "/nonexistent/gcc", self.root + "/intel/bin/icx",   # three refusals...
                   "1",                                                     # ...then the toolset gcc
                   "y", "2",                                                # change tools: binutils #2
                   "default", "zero", "12"]                                 # linker (if asked), jobs
        asked = []
        real_ask, real_which = elkt.ask, elkt.which
        elkt.which = lambda name, path=None: None if name in ("ld.lld", "ccache") else real_which(name, path)

        def scripted(prompt, default=""):
            asked.append(prompt)
            return answers.pop(0)
        elkt.ask = scripted
        opts = dict(elkt.DEFAULTS, jobs=4)
        try:
            elkt.wizard_toolchain(opts, machine)
        finally:
            elkt.ask, elkt.which = real_ask, real_which
        self.assertEqual(opts["cc"], d + "/gcc")
        self.assertEqual(opts["jobs"], 12)
        self.assertIn(opts["binutils"], (d, other))
        self.assertEqual(asked.count("compiler"), 4)
        listing = "\n".join(SAID)
        self.assertIn("cannot build a kernel", listing)


class CxxCompilers(TempRoot):
    GXX13 = "g++ (GCC) 13.2.0"

    def test_two_installs_of_one_build_are_two_compilers(self):
        # Same commit, same banner, two prefixes.  Merging on the banner hid one.
        banner = "clang version 24.0.0git (https://github.com/llvm/llvm-project.git 3c2eaf39)"
        a, b = self.root + "/opt/clang-24-3/bin", self.root + "/opt/clang-24-4/bin"
        fake_tool(a + "/clang", banner, installed=a)
        fake_tool(b + "/clang", banner, installed=b)
        self.assertEqual(sorted(f["path"] for f in elkt.find_toolchains([a, b])),
                         [a + "/clang", b + "/clang"])

    def test_cxx_is_scanned_separately_from_c(self):
        d = self.root + "/bin"
        fake_tool(d + "/clang-21", CLANG18, installed=d)
        os.symlink("clang-21", d + "/clang")
        os.symlink("clang-21", d + "/clang++")            # the SAME binary, as on every distro
        fake_tool(d + "/g++", self.GXX13)
        fake_tool(d + "/c++", self.GXX13)                 # g++ again, under another name: not listed
        fake_tool(d + "/icpx", DPCPP, installed=d + "/intel")
        fake_tool(d + "/dpcpp", DPCPP, installed=d + "/intel")
        cc, _, cxx = elkt.scan_tool_dirs([d])
        self.assertEqual((cc, sorted(cxx)), ([d + "/clang"], sorted(
            [d + "/g++", d + "/icpx", d + "/dpcpp", d + "/clang++"])))
        found = [(os.path.basename(x["path"]), x["usable"]) for x in elkt.find_cxx([d])]
        self.assertEqual(found, [("g++", "yes"), ("clang++", "yes"), ("icpx", "no")])

    def gcc_with_gxx(self, name="gcc-13"):
        d = self.root + "/opt/" + name + "/bin"
        fake_tool(d + "/gcc", GCC13)
        fake_tool(d + "/g++", self.GXX13)
        return d

    def test_a_private_gcc_builds_its_plugins_with_its_own_gxx(self):
        d = self.gcc_with_gxx()
        tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc"), {})
        self.assertIn("HOSTCXX=" + d + "/g++", tc["args"])
        self.assertEqual(elkt.cxx_partner(d + "/gcc"), d + "/g++")

    def test_cxx_can_be_chosen_and_intel_cannot(self):
        d = self.gcc_with_gxx()
        other = fake_tool(self.root + "/other/bin/g++", "g++ (GCC) 12.1.0")
        tc = elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc", cxx=other), {})
        self.assertIn("HOSTCXX=" + other, tc["args"])
        self.assertIn("matching one is", tc["cxx_note"])          # chosen, but told
        icpx = fake_tool(self.root + "/opt/intel/oneapi/compiler/2026.1/bin/icpx", DPCPP)
        for bad in (icpx, "/nonexistent/g++"):
            with self.assertRaises(elkt.ToolError):
                elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=d + "/gcc", cxx=bad), {})


class ReportedByTheUser(TempRoot):
    def test_a_status_line_never_wraps(self):
        long = "  [00:09:54]   7223 steps  CC [M] drivers/gpu/drm/amd/amdgpu/../display/dc/core/dc_resource.o"
        self.assertEqual(len(elkt.fit_line(long, 80)), 79)       # wrapped = a copy left per redraw
        self.assertEqual(elkt.fit_line("short", 80), "short")

    def test_localversion_gets_its_separator(self):
        opts = dict(elkt.DEFAULTS, localversion="machine-native-gpu")
        elkt.validate_options(opts)
        self.assertEqual(opts["localversion"], "-machine-native-gpu")   # was: 7.2.6machine-native-gpu
        for keep in ("-gpu", "+rt", "", "_x"):
            opts = dict(elkt.DEFAULTS, localversion=keep)
            elkt.validate_options(opts)
            self.assertEqual(opts["localversion"], keep)

    def test_the_probe_runs_what_this_dkms_runs(self):
        tree = os.path.join(self.root, "tree")
        new = put(self.root, "dkms3", 'if grep -q CONFIG_CC_IS_CLANG=y "$kernel_config"; then\n')
        old = put(self.root, "dkms2", "#!/bin/bash\n# knows nothing about clang\n")
        put(tree, ".config", "CONFIG_CC_IS_CLANG=y\n")
        self.assertEqual(elkt.dkms_make_args(tree, new), ["LLVM=1"])
        self.assertEqual(elkt.dkms_make_args(tree, old), [])
        put(tree, ".config", "CONFIG_CC_IS_GCC=y\n")
        self.assertEqual(elkt.dkms_make_args(tree, new), [])


class Profiles(TempRoot):
    def setUp(self):
        TempRoot.setUp(self)
        self.old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.root, "config")

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_xdg
        TempRoot.tearDown(self)

    def test_a_bare_name_goes_where_it_will_be_found_again(self):
        elkt.save_profile("machine-native", dict(elkt.DEFAULTS, kernel="7.2.6", install=True))
        path = os.path.join(elkt.profiles_dir(), "machine-native.ini")
        self.assertTrue(os.path.isfile(path))                      # was: ./machine-native, lost
        opts = elkt.merge_options(elkt.build_parser().parse_args(
            ["build", "--profile", "machine-native"]))
        self.assertEqual(opts["kernel"], "7.2.6")
        self.assertTrue(opts["install"])                           # so a reuse asks nothing

    def test_the_list_is_newest_first_and_finds_the_old_strays(self):
        elkt.save_profile("older", dict(elkt.DEFAULTS, localversion="-a"))
        os.utime(elkt.profile_path("older"), (1, 1))
        elkt.save_profile("newer", dict(elkt.DEFAULTS, localversion="-b"))
        stray = os.path.join(self.root, "cwd")
        put(stray, "xeon-haswell-all-gpu",                         # bare name, no extension
            elkt.PROFILE_HEADER + ".\n[build]\nkernel = stable\ncpu = native\n")
        put(stray, "notes.ini", "[other]\nx = 1\n")               # somebody else's .ini
        put(stray, "README", "not a profile\n")
        os.utime(os.path.join(stray, "xeon-haswell-all-gpu"), (2, 2))
        names = [pr["name"] for pr in elkt.list_profiles([stray])]
        self.assertEqual(names, ["newer", "xeon-haswell-all-gpu", "older"])

    def test_choosing(self):
        elkt.save_profile("only", dict(elkt.DEFAULTS))
        profs = elkt.list_profiles([])
        real_ask = elkt.ask
        for answers, want in ((["", ""], ("only", True)), (["only", "e"], ("only", False)),
                              (["7", "n"], None)):
            elkt.ask = lambda prompt, default="", a=answers: (a.pop(0) or default)
            try:
                got = elkt.choose_profile(profs)
            finally:
                elkt.ask = real_ask
            self.assertEqual(got and (got["name"], got["as_is"]), want)
        self.assertIsNone(elkt.choose_profile([]))


class SecureBoot(TempRoot):
    def reqs(self, **over):
        r = elkt.build_requests(dict(elkt.DEFAULTS, keep_symbols=[], **over), set(), {"storage": {}})
        return dict((x.sym, x) for x in r)

    def test_it_is_on_unless_turned_off(self):
        self.assertEqual(elkt.DEFAULTS["secure_boot"], "on")
        on, off = self.reqs(), self.reqs(secure_boot="off")
        for sym in ("EFI_STUB", "MODULE_SIG", "MODULE_SIG_ALL"):
            self.assertEqual((on[sym].want, on[sym].level), ("y", "required"), sym)
            self.assertNotIn(sym, off)
        self.assertEqual(on["INTEGRITY_MACHINE_KEYRING"].want, "y")     # DKMS's MOK-signed modules
        self.assertEqual(on["MODULE_SIG_FORCE"].want, "n")              # ...and unsigned ones with SB off

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_the_key_is_made_once_private_and_reused(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.root
        try:
            self.assertIsNone(elkt.signing_key(create=False))
            k = elkt.signing_key()
            self.assertEqual(os.stat(k["key"]).st_mode & 0o077, 0)      # nobody else can read it
            self.assertIn("kernel signing key", elkt.key_cn(k))
            before = os.path.getmtime(k["key"])
            self.assertEqual(elkt.signing_key()["key"], k["key"])
            self.assertEqual(os.path.getmtime(k["key"]), before)        # not regenerated
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_an_existing_private_key_is_never_overwritten(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.root
        try:
            k = elkt.signing_key()
            def der_of(path):
                with open(path, "rb") as f:
                    return f.read()
            key, der = elkt.read_text(k["key"]), der_of(k["der"])
            os.unlink(k["der"])                     # moved to a USB stick to enrol it elsewhere
            again = elkt.signing_key(create=False)
            self.assertEqual(elkt.read_text(again["key"]), key)         # was: a NEW key over the old one
            self.assertEqual(der_of(again["der"]), der)                 # the same certificate, derived again
            os.unlink(k["crt"])
            with self.assertRaises(elkt.ToolError):
                elkt.signing_key()
            self.assertEqual(elkt.read_text(k["key"]), key)
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    def test_a_signer_is_recognised_by_certificate_not_by_name(self):
        pem = "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"
        self.assertEqual(len(elkt._cert_digests(pem + pem.replace("QUJD", "QUJE"))), 2)
        self.assertEqual(elkt._cert_digests("no certificate here"), set())
        img = put(self.root, "bzImage", "not signed")
        # An unparsable certificate used to give CN '' -- and '' is "in" any pesign output.
        k = {"crt": put(self.root, "k.crt", "garbage"), "key": "", "der": ""}
        self.assertFalse(elkt.image_signed_by(img, k))
        self.assertFalse(elkt.image_signed_by(img, None))
        self.assertFalse(elkt.image_signed_by(img, {"crt": put(self.root, "ok.crt", pem), "key": "", "der": ""}))


KBUILD_IN_MINIATURE = """# what matters about kbuild's top Makefile, in twenty lines
ifneq ($(sub_make_done),1)
export sub_make_done := 1
PHONY := __all show
__all show: __sub-make
	@:
__sub-make:
	@$(MAKE) --no-print-directory -f $(CURDIR)/Makefile $(MAKECMDGOALS)
else
CC = gcc
HOSTCC = gcc
ifneq ($(LLVM),)
CC = $(LLVM)clang
endif
show:
	@echo "CC=$(CC) HOSTCC=$(HOSTCC) LLVM=$(LLVM) PATH1=$(firstword $(subst :, ,$(PATH)))"
endif
"""


class ToolchainPin(TempRoot):
    def tc(self, args, extra=None, **kw):
        cc = put(self.root, "cc/clang", "x")
        return dict({"args": args, "extra_path": extra, "path": cc,
                     "version": "clang version 24.0.0git"}, **kw)

    def make(self, tree, *argv):
        env = dict(os.environ, LC_ALL="C")
        env.pop("MAKEFLAGS", None)
        o = elkt.run(["make", "-C", tree, "show"] + list(argv), env=env).stdout
        return next(l for l in o.splitlines() if l.startswith("CC="))     # not make's "Leaving directory"

    def test_the_system_toolchain_needs_no_pin(self):
        self.assertEqual(elkt.pin_text(self.tc([])), "")
        self.assertEqual(elkt.pin_text(self.tc(["HOSTLDFLAGS=-no-pie", "PYTHON3=/x/python3"])), "")

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_the_pin_beats_whatever_the_caller_passes(self):
        tree = os.path.join(self.root, "tree")
        put(tree, "Makefile", KBUILD_IN_MINIATURE)
        self.assertIn("CC=gcc ", self.make(tree))                         # unpinned: gcc
        self.assertIn("CC=1clang", self.make(tree, "LLVM=1"))             # what dkms 3.x does
        self.assertTrue(elkt.pin_toolchain(tree, self.tc(["LLVM=/opt/clang24/bin/", "HOSTLDFLAGS=-no-pie"])))
        for caller in ([], ["LLVM=1"], ["CC=cc"], ["LLVM=1", "CC=cc", "HOSTCC=cc"]):
            self.assertIn("CC=/opt/clang24/bin/clang ", self.make(tree, *caller), caller)
        env_off = elkt.run(["make", "-C", tree, "show", "LLVM=1"],
                           env=dict(os.environ, ELKT_NO_PIN="1", LC_ALL="C")).stdout
        self.assertIn("CC=1clang", env_off)                               # the escape hatch

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_a_gcc_toolset_pin_carries_cc_hostcc_and_path(self):
        tree = os.path.join(self.root, "tree")
        put(tree, "Makefile", KBUILD_IN_MINIATURE)
        elkt.pin_toolchain(tree, self.tc(["CC=/ts/bin/gcc", "HOSTCC=/usr/bin/gcc"], extra="/ts/bin"))
        self.assertEqual(self.make(tree, "CC=cc"),
                         "CC=/ts/bin/gcc HOSTCC=/usr/bin/gcc LLVM= PATH1=/ts/bin")

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_a_callers_value_with_a_blank_in_it_survives_the_pin(self):
        # make hands `CC=ccache gcc` down as `CC=ccache\\ gcc`: two words to filter-out.
        # The second one became a GOAL of the sub-make -- exit 0, nothing built.
        tree = os.path.join(self.root, "tree")
        put(tree, "Makefile", KBUILD_IN_MINIATURE.replace("PATH1=$(firstword $(subst :, ,$(PATH)))",
                                                          "FOO=[$(FOO)]"))
        elkt.pin_toolchain(tree, self.tc(["LLVM=/opt/clang24/bin/"]))
        env = dict(os.environ, LC_ALL="C"); env.pop("MAKEFLAGS", None)
        for caller, foo in ((["FOO=hello CC=1 world"], "[hello CC=1 world]"),
                            (["CC=/usr/bin/ccache /usr/bin/gcc"], "[]"),
                            (["FOO=a b", "LLVM=1"], "[a b]")):
            o = elkt.run(["make", "-C", tree, "show"] + caller, env=env).stdout
            self.assertNotIn("Nothing to be done", o, caller)
            self.assertIn("CC=/opt/clang24/bin/clang ", o, caller)
            self.assertIn("FOO=" + foo, o, caller)

    def test_the_tools_own_makes_stay_out_of_the_pin(self):
        self.assertEqual(elkt.scrubbed_env()["ELKT_NO_PIN"], "1")       # they name their toolchain, ccache and all

    def test_one_instance_per_tree(self):
        tree = os.path.join(self.root, "tree")
        os.makedirs(tree)
        held = list(elkt._TREE_LOCKS)
        try:
            elkt.lock_tree(tree)
            with self.assertRaises(elkt.ToolError):     # was: read 'building' as a kill -9, ran mrproper
                elkt.lock_tree(tree)
            os.environ["ELKT_TREE_LOCKED"] = "1"        # the sign hook, under `package`'s own lock
            elkt.lock_tree(tree)
        finally:
            os.environ.pop("ELKT_TREE_LOCKED", None)
            for f in elkt._TREE_LOCKS[len(held):]:
                f.close()
            del elkt._TREE_LOCKS[len(held):]

    def test_a_tree_serving_installed_kernels_is_not_turned_into_another_kernel(self):
        tree = os.path.join(self.root, "tree")
        put(tree, ".config", 'CONFIG_LOCALVERSION="-server"\nCONFIG_CC_VERSION_TEXT="clang version 24"\n')
        same = elkt.serving_conflict(tree, ["7.2.6-server"], "-server", "clang version 24")
        self.assertEqual(same, "")                                      # rebuilding THAT kernel is fine
        self.assertEqual(elkt.serving_conflict(tree, [], "-other", "gcc 14"), "")   # nobody depends on it
        why = elkt.serving_conflict(tree, ["7.2.6-server"], "-desktop", "gcc (GCC) 14.2")
        self.assertIn("7.2.6-server", why)
        self.assertIn("LOCALVERSION -server -> -desktop", why)
        self.assertIn("compiler", why)
        self.assertIn("--workdir", why)

    def test_a_pin_follows_the_compiler_and_never_touches_a_foreign_file(self):
        tree = os.path.join(self.root, "tree")
        os.makedirs(tree)
        pin = os.path.join(tree, elkt.PIN_FILE)
        elkt.pin_toolchain(tree, self.tc(["LLVM=/a/"]))
        self.assertIn("override LLVM := /a/", elkt.read_text(pin))
        elkt.pin_toolchain(tree, self.tc(["CC=ccache /b/gcc"], ccache=True))
        self.assertIn("override CC := " + self.root + "/cc/clang", elkt.read_text(pin))   # no 'ccache '
        self.assertFalse(elkt.pin_toolchain(tree, self.tc([])))           # back to the system compiler
        self.assertFalse(os.path.exists(pin))                             # ...and the stale pin is gone
        put(tree, elkt.PIN_FILE, "# mine\ninclude Makefile\n")
        self.assertFalse(elkt.pin_toolchain(tree, self.tc(["LLVM=/a/"])))
        self.assertEqual(elkt.read_text(pin), "# mine\ninclude Makefile\n")

    def test_with_secure_boot_even_the_system_toolchain_gets_a_wrapper_that_signs(self):
        text = elkt.pin_text(self.tc([]), sign=True)
        self.assertIn("elkt-all: __all", text)
        self.assertIn(" sign --source $(CURDIR)", text)
        self.assertNotIn("override", text)                  # nothing to pin
        self.assertLess(text.index("include Makefile"), text.index(".DEFAULT_GOAL"))

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_only_a_goalless_in_tree_make_signs(self):
        tree = os.path.join(self.root, "tree")
        put(tree, "Makefile", "__all:\n\t@echo built\nmodules_install:\n\t@echo installed\n")
        text = elkt.pin_text(self.tc([]), sign=True)
        text = re.sub(r"\t@.* sign --source .*", "\t@echo SIGNED", text)
        text = re.sub(r"ifneq \(\$\(wildcard [^)]*enterprise[^)]*\),\)", "ifneq (x,)", text)
        put(tree, elkt.PIN_FILE, text)
        env = dict(os.environ, LC_ALL="C"); env.pop("MAKEFLAGS", None); env.pop("ELKT_NO_SIGN", None)
        run = lambda *a, **e: elkt.run(["make", "-C", tree] + list(a), env=dict(env, **e)).stdout
        self.assertIn("SIGNED", run())                                   # rpm's %build step
        self.assertNotIn("SIGNED", run("modules_install"))               # a goal: untouched
        self.assertNotIn("SIGNED", run("M=/tmp/some-module"))            # DKMS: untouched
        self.assertNotIn("SIGNED", run(ELKT_NO_SIGN="1"))                # the tool's own builds

    def test_install_txt_describes_the_tree_not_the_command_line(self):
        cfg = elkt.DotConfig(put(self.root, "c", "CONFIG_DRM_AMDGPU=m\nCONFIG_XFS_FS=m\nCONFIG_EFI=y\n"
                                 "CONFIG_X86_NATIVE_CPU=y\nCONFIG_PREEMPT_LAZY=y\nCONFIG_PREEMPT_DYNAMIC=y\n"
                                 "CONFIG_HZ=1000\n"))
        text = elkt.tree_summary(cfg)
        for want in ("2 modules", "GPU: amdgpu", "native", "preempt lazy", "HZ 1000", "BTF no"):
            self.assertIn(want, text)

    def test_it_switches_itself_off_where_the_compiler_is_missing(self):
        text = elkt.pin_text(dict(self.tc(["LLVM=/a/"]), path="/nonexistent/clang"))
        self.assertIn("ifneq ($(wildcard /nonexistent/clang),)", text)


class DiskPath(TempRoot):
    """The drivers between a disk and the CPU, on a fake sysfs laid out like the
    real one.  Her ASUS Z790 first: SATA behind an Intel VMD domain."""
    RUNNING = "6.12.0-211.61.1.el10_2.x86_64"
    SATA = "pci0000:00/0000:00:0e.0/pci10000:e0/10000:e0:17.0/ata1/host0/target0:0:0/0:0:0:0"

    def dev(self, path, driver=None, module=None, bus="pci"):
        d = os.path.join(self.root, "sys/devices", path)
        os.makedirs(d, exist_ok=True)
        if driver:
            drv = os.path.join(self.root, "sys/bus", bus, "drivers", driver)
            os.makedirs(drv, exist_ok=True)
            if module and not os.path.islink(os.path.join(drv, "module")):
                os.makedirs(os.path.join(self.root, "sys/module", module), exist_ok=True)
                os.symlink(os.path.join(self.root, "sys/module", module), os.path.join(drv, "module"))
            os.symlink(drv, os.path.join(d, "driver"))
        return d

    def block(self, name, under, partition=False):
        """sys/class/block/<name> -> <under>/<name>, as the kernel links them."""
        d = os.path.join(self.root, "sys/devices", under, name)
        os.makedirs(d, exist_ok=True)
        if partition:
            put(d, "partition", "1\n")
        os.makedirs(os.path.join(self.root, "sys/class/block"), exist_ok=True)
        os.symlink(d, os.path.join(self.root, "sys/class/block", name))
        return d

    def lvm(self, name, uuid, *slaves):
        d = self.block(name, "virtual/block")
        put(d, "dm/uuid", uuid + "\n")
        for s in slaves:
            os.makedirs(os.path.join(d, "slaves", s))
        return d

    def kernel_modules(self, dep="", builtin_modinfo=""):
        base = "lib/modules/%s/" % self.RUNNING
        put(self.root, base + "modules.dep", dep)
        put(self.root, base + "modules.builtin.modinfo", builtin_modinfo)

    def her_machine(self, vmd_module_link=True):
        self.dev("pci0000:00/0000:00:0e.0", "vmd", "vmd" if vmd_module_link else None)
        self.dev("pci0000:00/0000:00:0e.0/pci10000:e0/10000:e0:17.0", "ahci", "ahci")
        self.dev(self.SATA, "sd", "sd_mod", bus="scsi")
        self.block("sda", self.SATA + "/block")
        for p in ("sda1", "sda4", "sda5"):
            self.block(p, self.SATA + "/block/sda", partition=True)
        self.lvm("dm-0", "LVM-rootlv", "sda5")
        self.lvm("dm-1", "LVM-swaplv", "sda5")
        put(self.root, "proc/mounts", "/dev/dm-0 / xfs rw 0 0\n/dev/sda4 /boot xfs rw 0 0\n"
            "/dev/sda1 /boot/efi vfat rw 0 0\n/dev/sdc1 /run/media/jsadlier/BIOS vfat rw 0 0\n")
        put(self.root, "proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n"
            "/dev/dm-1                               partition\t3145724\t0\t-2\n")
        self.kernel_modules("kernel/drivers/scsi/sd_mod.ko.xz: kernel/drivers/scsi/scsi_common.ko.xz\n"
                            "kernel/drivers/ata/ahci.ko.xz: kernel/drivers/ata/libahci.ko.xz\n",
                            "vmd.version=0.6\x00vmd.file=drivers/pci/controller/vmd\x00vmd.license=GPL v2\x00")

    def tree(self):
        t = os.path.join(self.root, "linux-7.2.6")
        put(t, "drivers/scsi/Makefile", "sd_mod-objs\t:= sd.o\nobj-$(CONFIG_BLK_DEV_SD)\t+= sd_mod.o\n")
        put(t, "drivers/ata/Makefile", "obj-$(CONFIG_SATA_AHCI)\t\t+= ahci.o libahci.o\n"
                                       "obj-$(CONFIG_SATA_AHCI_PLATFORM) += ahci_platform.o libahci.o\n")
        put(t, "drivers/pci/controller/Makefile", "obj-$(CONFIG_VMD) += vmd.o\n")
        put(t, "drivers/nvme/host/Makefile", "obj-$(CONFIG_BLK_DEV_NVME)\t\t+= nvme.o\n")
        put(t, "drivers/usb/host/Makefile", "obj-$(CONFIG_USB_XHCI_HCD)\t+= xhci-hcd.o\n"
                                            "obj-$(CONFIG_USB_XHCI_PCI)\t+= xhci-pci.o \\\n\t\txhci-pci-renesas.o\n")
        put(t, "drivers/usb/storage/Makefile", "obj-$(CONFIG_USB_UAS)\t\t+= uas.o\n")
        return t

    def test_her_disks_sit_behind_vmd(self):
        self.her_machine()
        st = elkt.storage_stack(self.root, self.RUNNING)
        self.assertEqual((st["rootfs"], st["fstypes"], st["layers"]), ("xfs", ["vfat", "xfs"], ["lvm"]))
        self.assertEqual(st["disks"], ["sda"])                     # sdc is a USB stick under /run/media
        self.assertEqual([(d["module"], d["path"]) for d in st["drivers"]],
                         [("sd_mod", "drivers/scsi/sd_mod"), ("ahci", "drivers/ata/ahci"),
                          ("vmd", "drivers/pci/controller/vmd")])
        self.assertTrue(st["vmd"])

    def test_vmd_becomes_a_required_option_of_the_new_tree(self):
        self.her_machine(vmd_module_link=False)                # built in, as on EL: no module link
        machine = {"storage": elkt.storage_stack(self.root, self.RUNNING)}
        disk, unknown = elkt.disk_requests(self.tree(), machine)
        self.assertEqual([s for s, _, _ in disk], ["BLK_DEV_SD", "SATA_AHCI", "VMD"])
        self.assertEqual(unknown, [])
        reqs = dict((r.sym, r) for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[]), set(), dict(machine, firmware_compression=[]), disk))
        self.assertEqual((reqs["VMD"].want, reqs["VMD"].level), ("m|y", "required"))
        self.assertIn("invisible", reqs["VMD"].why)
        self.assertEqual(reqs["SATA_AHCI"].level, "required")

    def test_vmd_is_seen_by_its_domain_number_and_by_its_driver(self):
        behind = "pci0000:00/0000:00:0e.0/pci10000:e0/10000:e0:06.0/10000:e1:00.0"
        self.dev(behind, "nvme", "nvme")                            # VMD's own driver not bound
        self.block("nvme0n1", behind + "/nvme/nvme0")
        self.assertTrue(elkt.disk_chain("nvme0n1", self.root)[1])   # five hex digits: pci10000
        self.dev("pci0000:00/0000:00:17.0", "vmd")                  # a domain named the usual way
        self.dev("pci0000:00/0000:00:17.0/0000:01:00.0", "nvme", "nvme")
        self.block("nvme1n1", "pci0000:00/0000:00:17.0/0000:01:00.0/nvme/nvme1")
        self.assertTrue(elkt.disk_chain("nvme1n1", self.root)[1])
        self.dev("pci0000:00/0000:00:1d.0/0000:03:00.0", "nvme", "nvme")
        self.block("nvme2n1", "pci0000:00/0000:00:1d.0/0000:03:00.0/nvme/nvme2")
        self.assertFalse(elkt.disk_chain("nvme2n1", self.root)[1])

    def test_a_vmd_domain_is_required_even_when_its_driver_is_not_seen(self):
        machine = {"storage": {"drivers": [], "disks": ["sda"], "vmd": True}}
        self.assertEqual(elkt.disk_requests(self.tree(), machine)[0], [("VMD", "vmd", ["sda"])])

    def test_root_on_a_partition_of_an_rst_array_keeps_its_raid_level(self):
        md = self.block("md126", "virtual/block")
        put(md, "md/level", "raid1\n")
        for s in ("sdb", "sdc"):
            os.makedirs(os.path.join(md, "slaves", s))
        self.block("md126p3", "virtual/block/md126", partition=True)
        put(self.root, "proc/mounts", "/dev/md126p3 / ext4 rw 0 0\n")
        st = elkt.storage_stack(self.root, self.RUNNING)
        self.assertEqual(st["layers"], ["md:raid1"])                # was: md:? -- no level required
        self.assertEqual(st["disks"], ["sdb", "sdc"])

    def test_nvme_with_native_multipath(self):
        self.dev("pci0000:00/0000:00:1d.0", "pcieport")
        self.dev("pci0000:00/0000:00:1d.0/0000:03:00.0", "nvme", "nvme")
        path = self.dev("pci0000:00/0000:00:1d.0/0000:03:00.0/nvme/nvme0/nvme0c0n1")
        ns = self.block("nvme0n1", "virtual/nvme-subsystem/nvme-subsys0")
        os.makedirs(os.path.join(ns, "multipath"))
        os.symlink(path, os.path.join(ns, "multipath", "nvme0c0n1"))
        self.block("nvme0n1p2", "virtual/nvme-subsystem/nvme-subsys0/nvme0n1", partition=True)
        put(self.root, "proc/mounts", "/dev/nvme0n1p2 / xfs rw 0 0\n")
        st = elkt.storage_stack(self.root, self.RUNNING)
        self.assertEqual([d["module"] for d in st["drivers"]], ["nvme"])   # pcieport: plumbing
        self.assertFalse(st["vmd"])

    def test_usb_root_through_a_builtin_xhci(self):
        usb = "pci0000:00/0000:00:14.0"
        self.dev(usb, "xhci_hcd")                                  # built in: no module link
        self.dev(usb + "/usb2", "usb", bus="usb")
        self.dev(usb + "/usb2/2-1", "usb", bus="usb")
        self.dev(usb + "/usb2/2-1/2-1:1.0", "uas", "uas", bus="usb")
        disk = usb + "/usb2/2-1/2-1:1.0/host3/target3:0:0/3:0:0:0"
        self.dev(disk, "sd", "sd_mod", bus="scsi")
        self.block("sdf", disk + "/block")
        put(self.root, "proc/mounts", "/dev/sdf / ext4 rw 0 0\n")
        self.kernel_modules("kernel/drivers/usb/storage/uas.ko.zst:\n",
                            "xhci_pci.file=drivers/usb/host/xhci-pci\x00")
        st = elkt.storage_stack(self.root, self.RUNNING)
        self.assertEqual([d["module"] for d in st["drivers"]], ["sd_mod", "uas", "xhci_pci"])
        disk_reqs, _ = elkt.disk_requests(self.tree(), {"storage": st})
        self.assertIn("USB_XHCI_PCI", [s for s, _, _ in disk_reqs])

    def test_makefiles_are_read_like_localmodconfig_reads_them(self):
        t = self.tree()
        objs = elkt.makefile_objects(os.path.join(t, "drivers/usb/host/Makefile"))
        self.assertEqual(objs["xhci_pci_renesas"], ["USB_XHCI_PCI"])      # a continued line
        self.assertEqual(elkt.symbol_for_module(t, "libahci", "drivers/ata/libahci"),
                         ["SATA_AHCI", "SATA_AHCI_PLATFORM"])            # two: not an answer
        # moved since the running kernel was built: found by a search of drivers/
        self.assertEqual(elkt.symbol_for_module(t, "vmd", "drivers/pci/host/vmd"), ["VMD"])
        self.assertEqual(elkt.symbol_for_module(t, "zfs", "extra/zfs"), [])


class Nvidia(TempRoot):
    RUNNING = "6.12.0-211.61.1.el10_2.x86_64"
    GPU = {"addr": "0000:01:00.0", "kind": "display", "vendor": 0x10de, "device": 0x1f83,
           "driver": "nouveau", "name": "NVIDIA Corporation TU117 [GeForce GTX 1630]"}

    def state(self, dkms=()):
        return elkt.nvidia_state([self.GPU], list(dkms), self.RUNNING, self.root)

    def test_chip_names(self):
        for name, chip in (("NVIDIA Corporation TU117 [GeForce GTX 1630]", "tu117"),
                           ("GA107M [GeForce RTX 3050 Mobile]", "ga107"),
                           ("NVIDIA Corporation GA102GL [RTX A6000]", "ga102"),
                           ("GK208B [GeForce GT 710]", "gk208b"), ("", ""), ("Device 2d04", "")):
            self.assertEqual(elkt.nvidia_chip(name), chip, name)

    def test_her_gtx_1630_on_nouveau_with_gsp(self):
        put(self.root, "lib/firmware/nvidia/tu117/gsp/booter_load-570.144.bin.xz", "x")
        nv = self.state()
        self.assertEqual(nv["gpus"][0]["chip"], "tu117")
        out_ = elkt.nvidia_outlook(nv, "dnf")
        self.assertEqual([k for k, _ in out_], ["ok"])
        self.assertIn("GSP too", out_[0][1])

    def test_no_gsp_firmware_means_boot_clocks(self):
        put(self.root, "lib/firmware/nvidia/tu117/acr/ucode_ahesasc.bin.xz", "x")
        (kind, text), = elkt.nvidia_outlook(self.state(), "dnf")
        self.assertEqual(kind, "note")
        self.assertIn("boot clocks", text)

    def test_no_firmware_at_all_names_the_package(self):
        (kind, text), = elkt.nvidia_outlook(self.state(), "dnf")
        self.assertEqual(kind, "warn")
        self.assertIn("nvidia-gpu-firmware", text)

    def test_a_prebuilt_kmod_and_a_blacklisted_nouveau_leave_no_driver(self):
        put(self.root, "lib/modules/%s/extra/nvidia/nvidia.ko.xz" % self.RUNNING, "x")
        put(self.root, "usr/lib/modprobe.d/nvidia.conf", "# from the driver package\nblacklist nouveau\n")
        nv = self.state()
        self.assertEqual(len(nv["own_driver"]), 1)
        (kind, text), = elkt.nvidia_outlook(nv, "dnf", "7.2.6-elkt")
        self.assertEqual(kind, "warn")
        self.assertIn("no driver", text)
        self.assertIn("DKMS", text)

    def test_akmods_cannot_follow_a_tree_installed_kernel(self):
        put(self.root, "lib/modules/%s/extra/nvidia/nvidia.ko.xz" % self.RUNNING, "x")
        put(self.root, "usr/src/akmods/nvidia-kmod.latest", "x")
        kinds = elkt.nvidia_outlook(self.state(), "dnf")
        self.assertIn("kernel-devel", kinds[0][1])
        self.assertEqual(kinds[0][0], "note")                   # nouveau takes over: not blacklisted

    def test_dkms_follows(self):
        put(self.root, "lib/modules/%s/extra/nvidia.ko.xz" % self.RUNNING, "x")
        nv = self.state([{"name": "nvidia", "version": "580.95.05", "kernels": [], "limits": {}}])
        (kind, text), = elkt.nvidia_outlook(nv, "dnf")
        self.assertEqual(kind, "ok")
        self.assertIn("nvidia/580.95.05", text)

    def test_nouveau_switched_off_on_the_command_line_or_by_install(self):
        put(self.root, "proc/cmdline", "BOOT_IMAGE=x ro rd.driver.blacklist=nouveau,foo quiet\n")
        self.assertIn("rd.driver.blacklist", elkt.nouveau_blacklisted(self.root))
        put(self.root, "proc/cmdline", "BOOT_IMAGE=x ro quiet\n")
        self.assertEqual(elkt.nouveau_blacklisted(self.root), "")
        put(self.root, "etc/modprobe.d/x.conf", "install nouveau /bin/false\n")
        self.assertIn("install nouveau", elkt.nouveau_blacklisted(self.root))

    def test_no_nvidia_gpu_no_report(self):
        self.assertEqual(elkt.nvidia_state([dict(self.GPU, vendor=0x1002)], [], self.RUNNING, self.root), {})
        self.assertEqual(elkt.nvidia_outlook({}, "dnf"), [])


HER_FLAGS = set(("fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush "
                 "mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp lm constant_tsc pni pclmulqdq "
                 "ssse3 sdbg fma cx16 xtpr pdcm pcid sse4_1 sse4_2 x2apic movbe popcnt aes xsave avx "
                 "f16c rdrand lahf_lm abm 3dnowprefetch fsgsbase bmi1 avx2 smep bmi2 erms invpcid "
                 "rdseed adx smap clflushopt clwb sha_ni xsaveopt xsavec xgetbv1 xsaves avx_vnni "
                 "umip pku waitpkg gfni vaes vpclmulqdq rdpid movdiri movdir64b fsrm serialize").split())


class CpuTuning(TempRoot):
    def test_her_raptor_lake_is_v3_not_v4(self):
        self.assertEqual(elkt.x86_level(HER_FLAGS), "x86-64-v3")
        self.assertEqual(elkt.x86_level(HER_FLAGS | {"avx512f", "avx512bw", "avx512cd",
                                                      "avx512dq", "avx512vl"}), "x86-64-v4")
        self.assertEqual(elkt.x86_level(HER_FLAGS - {"movbe"}), "x86-64-v2")
        self.assertEqual(elkt.x86_level({"lm", "sse2"}), "x86-64")

    def test_hybrid_core_counts(self):
        self.assertEqual((elkt.cpulist_len("0-15\n"), elkt.cpulist_len("16-23,30")), (16, 9))
        put(self.root, "proc/cpuinfo", "vendor_id\t: GenuineIntel\nflags\t\t: %s\n" % " ".join(HER_FLAGS))
        put(self.root, "sys/devices/cpu_core/cpus", "0-15\n")
        put(self.root, "sys/devices/cpu_atom/cpus", "16-23\n")
        ci = elkt.cpu_info(self.root)
        self.assertEqual((ci["level"], ci["hybrid"]), ("x86-64-v3", [16, 8]))

    def test_the_compiler_says_what_a_cpu_name_implies(self):
        cc = os.path.join(self.root, "bin/cc")
        os.makedirs(os.path.dirname(cc))
        with open(cc, "w") as f:
            f.write("#!/bin/sh\ncase \"$1\" in\n  -march=nonsense) echo 'error: bad'; exit 1;;\n"
                    "  -march=x86-64-v2) echo '#define __SSE4_2__ 1'; echo '#define __POPCNT__ 1';"
                    " echo '#define __SSSE3__ 1';;\n"
                    "  *) for m in SSE4_2 POPCNT SSSE3 AVX2 BMI BMI2 FMA MOVBE; do "
                    "echo \"#define __${m}__ 1\"; done;;\nesac\n")
        os.chmod(cc, 0o755)
        self.assertEqual(elkt.march_level(cc, "raptorlake"), "x86-64-v3")
        self.assertEqual(elkt.march_level(cc, "x86-64-v2"), "x86-64-v2")
        self.assertIsNone(elkt.march_level(cc, "nonsense"))

    def test_flags_for_each_kind_of_choice(self):
        tree = os.path.join(self.root, "tree")
        put(tree, "arch/x86/Kconfig.cpu", "config X86_NATIVE_CPU\n\tbool \"native\"\n")
        old = os.path.join(self.root, "old")
        put(old, "arch/x86/Kconfig.cpu", "config GENERIC_CPU\n\tbool \"generic\"\n")
        self.assertEqual(elkt.cpu_flags("generic", tree, "x86_64"), (False, ""))
        self.assertEqual(elkt.cpu_flags("native", tree, "x86_64"), (True, ""))
        self.assertEqual(elkt.cpu_flags("native", old, "x86_64"), (False, "-march=native -mtune=native"))
        self.assertEqual(elkt.cpu_flags("x86-64-v3", tree, "x86_64"), (False, "-march=x86-64-v3"))
        self.assertEqual(elkt.cpu_flags("raptorlake", tree, "x86_64"),
                         (False, "-march=raptorlake -mtune=raptorlake"))

    def test_native_is_required_where_the_tree_has_it(self):
        by = lambda **o: dict((r.sym, r) for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[], **o), set(), {"storage": {}}))
        self.assertEqual(by(cpu="native")["X86_NATIVE_CPU"].level, "required")    # was: wanted
        self.assertEqual(by(cpu="native", _native_kconfig=False)["X86_NATIVE_CPU"].want, "n")
        self.assertEqual(by(cpu="x86-64-v3")["X86_NATIVE_CPU"].want, "n")

    def test_cpu_values(self):
        for good, want in (("x86-64-v3", "x86-64-v3"), ("Raptorlake", "raptorlake"), ("znver4", "znver4")):
            opts = dict(elkt.DEFAULTS, cpu=good)
            elkt.validate_options(opts)
            self.assertEqual(opts["cpu"], want)
        with self.assertRaises(elkt.ToolError):
            elkt.validate_options(dict(elkt.DEFAULTS, cpu="bad cpu!"))

    @unittest.skipUnless(shutil.which("make"), "needs make")
    def test_the_tree_remembers_its_cpu_flags(self):
        # A make run by hand -- or by rpmbuild -- must not rebuild the kernel generic.
        tree = os.path.join(self.root, "tree")
        put(tree, "Makefile", KBUILD_IN_MINIATURE.replace("PATH1=$(firstword $(subst :, ,$(PATH)))",
                                                          "KCFLAGS=[$(KCFLAGS)]"))
        elkt.pin_toolchain(tree, {"args": [], "extra_path": None, "path": "/usr/bin/gcc",
                                  "version": "gcc 14", "kcflags": "-march=x86-64-v3"})
        env = dict(os.environ, LC_ALL="C")
        env.pop("MAKEFLAGS", None)
        env.pop("KCFLAGS", None)
        show = lambda *a: elkt.run(["make", "-C", tree, "show"] + list(a), env=env).stdout
        self.assertIn("KCFLAGS=[-march=x86-64-v3]", show())                   # in kbuild's sub-make
        self.assertIn("KCFLAGS=[-O1]", show("KCFLAGS=-O1"))                     # a caller's own wins
        self.assertIn("KCFLAGS=[-march=x86-64-v3]", show("ELKT_NO_PIN=1"))      # not part of the pin

    def test_old_trees_get_llvm_1_and_the_clang_on_path(self):
        d = os.path.join(self.root, "clang/bin")
        for t in ("clang", "ld.lld"):
            fake_tool(os.path.join(d, t), CLANG18)
        tree = os.path.join(self.root, "linux")
        tc = lambda: {"args": ["LLVM=%s/" % d, "HOSTLDFLAGS=-no-pie"], "extra_path": None,
                      "path": d + "/clang"}
        put(tree, "Makefile", "VERSION = 5\nPATCHLEVEL = 15\nSUBLEVEL = 170\n")
        t = tc()
        elkt.fit_llvm_to_tree(t, tree)
        self.assertEqual((t["args"][0], t["extra_path"]), ("LLVM=1", d))
        put(tree, "Makefile", "VERSION = 6\nPATCHLEVEL = 12\n")
        t = tc()
        elkt.fit_llvm_to_tree(t, tree)
        self.assertEqual(t["args"][0], "LLVM=%s/" % d)                             # 5.18+: as it is
        put(tree, "Makefile", "VERSION = 5\nPATCHLEVEL = 4\n")
        with self.assertRaises(elkt.ToolError):
            elkt.fit_llvm_to_tree(tc(), tree)


class AnyMachine(TempRoot):
    def test_intel_gpus_only_where_they_exist(self):
        self.assertEqual(elkt.resolve_gpu("auto", [], "aarch64")[0], {"amd", "nvidia"})   # was: i915 REQUIRED
        self.assertEqual(elkt.resolve_gpu("all", [], "x86_64")[0], {"amd", "nvidia", "intel"})
        self.assertEqual(elkt.resolve_gpu("auto", ["nvidia"], "aarch64")[0], {"nvidia"})
        with self.assertRaises(elkt.ToolError):
            elkt.resolve_gpu("intel", [], "ppc64le")

    def test_firmware_decompression_is_needed_without_any_gpu(self):
        # Wi-Fi, the network card, Bluetooth: all load compressed firmware too.
        by = dict((r.sym, r) for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[]), set(), {"firmware_compression": ["xz"], "storage": {}}))
        self.assertEqual(by["FW_LOADER_COMPRESS_XZ"].level, "required")
        self.assertEqual(by["MICROCODE"].want, "y")

    def test_a_hybrid_cpu_asks_for_its_scheduler_support(self):
        reqs = lambda cpu: [r.sym for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[]), set(), {"storage": {}, "cpu": cpu})]
        self.assertIn("SCHED_MC_PRIO", reqs({"hybrid": [16, 8]}))
        self.assertNotIn("SCHED_MC_PRIO", reqs({"hybrid": None}))

    def test_debug_info_on_means_dwarf(self):
        base = lambda text: elkt.DotConfig(put(self.root, "base", text))
        by = lambda b: dict((r.sym, r) for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[], debug_info="on"), set(), {"storage": {}}, (), b))
        after_off = by(base("CONFIG_DEBUG_INFO_NONE=y\n"))           # an earlier `off` run's .config
        self.assertEqual(after_off["DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT"].level, "required")
        self.assertEqual(after_off["DEBUG_INFO_NONE"].want, "n")
        self.assertNotIn("DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT", by(base("CONFIG_DEBUG_INFO_DWARF5=y\n")))

    def test_a_file_that_cannot_be_executed_is_an_error_not_a_crash(self):
        bad = put(self.root, "opt/x/bin/gcc", "\x7fELF garbage, another architecture\n")
        os.chmod(bad, 0o755)
        with self.assertRaises(elkt.ToolError):
            elkt.run([bad, "--version"])
        self.assertIsNone(elkt.compiler_info(bad))           # was: OSError out of detect_machine()

    def test_a_saved_profile_beats_a_stray_of_the_same_name(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        cwd = os.getcwd()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.root, "config")
        try:
            elkt.save_profile("fleet", dict(elkt.DEFAULTS, kernel="6.12"))
            here = os.path.join(self.root, "here")
            put(here, "fleet", elkt.PROFILE_HEADER + ".\n[build]\nkernel = 5.15\n")
            os.chdir(here)
            self.assertEqual(elkt.load_profile("fleet")["kernel"], "6.12")    # was: the stray's 5.15
        finally:
            os.chdir(cwd)
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


class BuiltTrees(TempRoot):
    def test_a_tree_whose_build_failed_or_changed_is_not_installed(self):
        tree = os.path.join(self.root, "tree")
        cfg = put(tree, ".config", "CONFIG_A=y\n")
        sha = elkt.sha256_file(cfg)
        self.assertEqual(elkt.tree_is_stale(tree, {"status": "built", "config_sha": sha}), "")
        self.assertIn("FAILED", elkt.tree_is_stale(tree, {"status": "failed", "stage": "build"}))
        self.assertIn("never finished", elkt.tree_is_stale(tree, {"status": "building"}))
        put(tree, ".config", "CONFIG_A=m\n")
        self.assertIn("changed", elkt.tree_is_stale(tree, {"status": "built", "config_sha": sha}))
        self.assertEqual(elkt.tree_is_stale(tree, {}), "")          # no record: nothing to go on

    def test_install_takes_what_built_the_tree(self):
        args = elkt.build_parser().parse_args(["install", "--linker", "bfd"])
        opts = elkt.merge_options(args)
        rec = {"opt_cc": "/opt/clang24/bin/clang", "opt_toolchain": "llvm", "opt_cpu": "x86-64-v3",
               "opt_linker": "lld", "opt_binutils": "auto", "opt_python": "", "opt_cxx": "",
               "opt_llvm_prefix": "", "opt_ccache": "off"}
        elkt.adopt_tree_choices(opts, rec, self.root, {})
        self.assertEqual((opts["cc"], opts["toolchain"], opts["cpu"]),
                         ("/opt/clang24/bin/clang", "llvm", "x86-64-v3"))
        self.assertEqual(opts["linker"], "bfd")                    # given on this command line: kept

    def test_a_tree_with_no_record_gets_the_compiler_its_config_names(self):
        tree = os.path.join(self.root, "tree")
        put(tree, ".config", 'CONFIG_CC_VERSION_TEXT="clang version 24.0.0git"\n')
        machine = {"toolchains": [{"version": "gcc 14", "path": "/usr/bin/gcc", "real": "/usr/bin/gcc",
                                   "usable": "yes"},
                                  {"version": "clang version 24.0.0git", "path": "/opt/c/bin/clang",
                                   "real": "/opt/c/bin/clang-24", "usable": "yes"}]}
        opts = elkt.merge_options(elkt.build_parser().parse_args(["install"]))
        elkt.adopt_tree_choices(opts, {}, tree, machine)
        self.assertEqual(opts["cc"], "/opt/c/bin/clang")


class ReviewOfTheSecondPass(TempRoot):
    """One test per finding of the 2026-10-08 fresh-reader review (PLAN.md 9f)."""
    GCC = "/usr/bin/gcc"

    def tree(self, cc_text, kcflags=""):
        tree = os.path.join(self.root, "tree")
        put(tree, ".config", 'CONFIG_CC_VERSION_TEXT="%s"\nCONFIG_X=y\n' % cc_text)
        if kcflags:
            put(tree, elkt.PIN_FILE, "%s\nexport KCFLAGS := %s\ninclude Makefile\n" % (elkt.PIN_MARK, kcflags))
        return tree

    def opts(self, *argv):
        return elkt.merge_options(elkt.build_parser().parse_args(["install"] + list(argv)))

    @unittest.skipUnless(os.access("/usr/bin/gcc", os.X_OK), "needs /usr/bin/gcc")
    def test_f1_install_says_a_changed_compiler_package_refuses_it(self):
        tree = self.tree("gcc (GCC) 14.3.1 20251022 (Red Hat 14.3.1-3)")   # an errata update ago
        machine = {"arch": os.uname().machine, "toolchains": []}
        del SAID[:]
        tc = elkt.tree_toolchain(self.opts(), machine, tree, {}, strict=False)
        self.assertTrue(tc["path"])                                     # was: refused, no way past
        self.assertTrue([l for l in SAID if "fine for installing" in l])
        with self.assertRaises(elkt.ToolError):
            elkt.tree_toolchain(self.opts(), machine, tree, {}, strict=True)

    def test_f1_a_bare_compiler_name_is_recorded_by_its_path(self):
        rec = elkt.choice_record(dict(elkt.DEFAULTS, cc="clang"), {"path": "/opt/c/bin/clang", "cxx": ""})
        self.assertEqual(rec["opt_cc"], "/opt/c/bin/clang")
        self.assertEqual(elkt.choice_record(dict(elkt.DEFAULTS), {"path": "/usr/bin/gcc"})["opt_cc"], "")

    @unittest.skipUnless(os.access("/usr/bin/gcc", os.X_OK), "needs /usr/bin/gcc")
    def test_f3_package_refuses_other_cpu_flags_than_the_tree_was_built_with(self):
        out_ = elkt.run([self.GCC, "--version"]).stdout.splitlines()[0]
        tree = self.tree(out_)
        machine = {"arch": os.uname().machine, "toolchains": []}
        rec = {"opt_cpu": "generic", "kcflags": "", "opt_cc": "", "opt_toolchain": "gcc"}
        with self.assertRaises(elkt.ToolError):                         # would recompile it all for v4
            elkt.tree_toolchain(self.opts("--cpu", "x86-64-v4"), machine, tree, rec, strict=True)
        tc = elkt.tree_toolchain(self.opts(), machine, tree, rec, strict=True)
        self.assertEqual(tc["kcflags"], "")

    def test_f4_the_record_is_what_was_built_not_what_was_tried(self):
        tree = os.path.join(self.root, "tree")
        cfg = put(tree, ".config", "CONFIG_A=y\n")
        sha = elkt.sha256_file(cfg)
        # A re-run refused at configure leaves .config alone: still installable.
        self.assertEqual(elkt.tree_is_stale(tree, {"status": "failed", "stage": "configure",
                                                   "config_sha": sha}), "")
        put(tree, ".config", "CONFIG_A=m\n")                          # build --configure-only
        self.assertIn("changed", elkt.tree_is_stale(tree, {"status": "configured", "config_sha": sha}))
        self.assertIn("configured again", elkt.tree_is_stale(tree, {"status": "configured"}))

    def test_f2_a_package_says_which_cpu_it_needs(self):
        cfg = elkt.DotConfig(put(self.root, "c", "CONFIG_X86_NATIVE_CPU=n\n"))
        self.assertIn("-march=x86-64-v3 (x86-64-v3)", elkt.tree_summary(cfg, "-march=x86-64-v3", "x86-64-v3"))
        self.assertIn("native", elkt.tree_summary(cfg, "-march=native -mtune=native"))
        self.assertIn("CPU: generic", elkt.tree_summary(cfg))

    def test_f8_a_tree_without_a_record_keeps_its_cpu_flags(self):
        tree = self.tree("gcc 14", kcflags="-march=raptorlake -mtune=raptorlake")
        opts = self.opts()
        elkt.adopt_tree_choices(opts, {}, tree, {"toolchains": []})
        self.assertEqual(opts["cpu"], "raptorlake")                    # was: generic -> recompiled
        self.assertEqual(elkt.tree_kcflags(tree, {}), "-march=raptorlake -mtune=raptorlake")

    def test_f6_debug_info_before_5_18_is_its_own_switch(self):
        by = lambda mode: dict((r.sym, r) for r in elkt.build_requests(
            dict(elkt.DEFAULTS, keep_symbols=[], debug_info=mode), set(), {"storage": {}}))
        self.assertEqual(by("off")["DEBUG_INFO"].want, "n")
        self.assertEqual(by("on")["DEBUG_INFO"].want, "y")
        self.assertEqual(by("btf")["DEBUG_INFO"].want, "y")

    def test_f7_native_with_an_old_clang_is_refused_like_the_kernel_refuses_it(self):
        clang = fake_tool(self.root + "/llvm18/bin/clang", "clang version 18.1.8")
        with self.assertRaises(elkt.ToolError) as e:
            elkt.resolve_toolchain(dict(elkt.DEFAULTS, cc=clang, toolchain="llvm", cpu="native"), {})
        self.assertIn("19.1", str(e.exception))

    def test_f10_require_pgp_on_an_existing_tree_says_how_to_get_the_key(self):
        work = os.path.join(self.root, "src")
        put(work, "linux-7.2.6/Makefile", "VERSION = 7\n")
        real = elkt.pgp_check
        elkt.pgp_check = lambda text: ("nokey", "632D3A06589DA6B1")
        try:
            with self.assertRaises(elkt.ToolError) as e:
                elkt.fetch_source({"version": "7.2.6", "signed_text": "x", "tarball": "linux-7.2.6.tar.xz",
                                   "sha256": "a" * 64}, work, require_pgp=True)
        finally:
            elkt.pgp_check = real
        self.assertIn("gpg --locate-keys", str(e.exception))           # was: "move the tree away"

    def test_require_pgp_stops_a_fresh_download_without_the_key(self):
        work = os.path.join(self.root, "src")
        real = elkt.pgp_check
        elkt.pgp_check = lambda text: ("nokey", "632D3A06589DA6B1")
        try:
            with self.assertRaises(elkt.ToolError) as e:     # before any download is attempted
                elkt.fetch_source({"version": "7.2.6", "signed_text": "x", "tarball": "linux-7.2.6.tar.xz",
                                   "sha256": "a" * 64, "url": "http://127.0.0.1:9/none"}, work,
                                  require_pgp=True)
        finally:
            elkt.pgp_check = real
        self.assertIn("NOT verified", str(e.exception))

    def test_microcode_ctl_is_told_about_a_kernel_it_cannot_name(self):
        fake = os.path.join(self.root, "check_caveats")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\ncase "$*" in *6.12.0-211*) echo "ok_cfgs intel";; '
                    '*) echo "ok_cfgs"; echo "fail_cfgs intel";; esac\n')
        os.chmod(fake, 0o755)
        real = elkt.CHECK_CAVEATS
        elkt.CHECK_CAVEATS = fake
        try:
            intel = {"cpu": {"vendor": "GenuineIntel"}, "running": "6.12.0-211.61.1.el10_2.x86_64"}
            self.assertEqual(elkt.microcode_force_needed(intel, "7.2.6-elkt"),
                             "/lib/firmware/7.2.6-elkt/force-early-intel")
            self.assertEqual(elkt.microcode_force_needed(intel, "6.12.0-211.70.1.el10_2.x86_64"), "")
            self.assertEqual(elkt.microcode_force_needed(intel, "4.9.0-old"), "")     # no early load
            self.assertEqual(elkt.microcode_force_needed(dict(intel, cpu={"vendor": "AuthenticAMD"}),
                                                         "7.2.6-elkt"), "")
            self.assertEqual(elkt.microcode_force_needed(intel, "7.2.6-elkt", running="5.14.0-weird"), "")
        finally:
            elkt.CHECK_CAVEATS = real


if __name__ == "__main__":
    unittest.main(verbosity=1)
