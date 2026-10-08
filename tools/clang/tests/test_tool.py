#!/usr/bin/python3 -I
"""Tests for enterprise_clang_tool.py.  stdlib unittest; no network, no root, no
LLVM tree (a twelve-line fake one), no compiler beyond what `doctor` would ask for."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import enterprise_clang_tool as ect          # noqa: E402

TOP = """cmake_minimum_required(VERSION 3.20.0)
set(LLVM_ALL_PROJECTS "bolt;clang;clang-tools-extra;cross-project-tests;lld;lldb;mlir;polly")
set(LLVM_EXTRA_PROJECTS "flang" "libc" "compiler-rt")
set(LLVM_SUPPORTED_RUNTIMES "libc;libunwind;libcxxabi;libcxx;compiler-rt;openmp;llvm-libgcc;offload;flang-rt;libclc;libsycl")
set(LLVM_ALL_TARGETS
  AArch64
  X86
  RISCV
  )
set(LLVM_ALL_EXPERIMENTAL_TARGETS
  ARC
  Xtensa )
  set(LLVM_MINIMUM_PYTHON_VERSION 3.8)
  set(LLVM_MINIMUM_PYTHON_VERSION 3.0)
"""
VERSION = ("if(NOT DEFINED LLVM_VERSION_MAJOR)\n  set(LLVM_VERSION_MAJOR 24)\nendif()\n"
           "  set(LLVM_VERSION_MINOR 0)\n  set(LLVM_VERSION_PATCH 0)\n  set(LLVM_VERSION_SUFFIX git)\n")


def put(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ect-test-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_tree(self, top=TOP, leave_out=()):
        src = os.path.join(self.root, "llvm-project")
        put(src, "llvm/CMakeLists.txt", top)
        put(src, "cmake/Modules/LLVMVersion.cmake", VERSION)
        put(src, "llvm/cmake/modules/CheckCompilerVersion.cmake", "set(GCC_MIN 7.4)\nset(CLANG_MIN 5.0)\n")
        for d in ("bolt clang clang-tools-extra cross-project-tests lld lldb mlir polly flang libc libunwind "
                  "libcxxabi libcxx compiler-rt openmp llvm-libgcc offload flang-rt libclc libsycl").split():
            if d not in leave_out:
                put(src, d + "/CMakeLists.txt")
        for t in ("AArch64", "X86", "RISCV", "ARC", "Xtensa"):
            put(src, "llvm/lib/Target/%s/CMakeLists.txt" % t)
        put(src, "libc/utils/hdrgen/main.py")
        return ect.Tree(src)

    MACHINE = {"arch": "x86_64", "mem_total_gb": 64.0, "mem_avail_gb": 32.0, "cpus": 24}
    PY = {"path": "/usr/bin/python3", "yaml": True}

    def select(self, tree=None, machine=None, python=None, dropped=None, **over):
        return ect.select_components(tree or self.fake_tree(), dict(self.MACHINE, **(machine or {})),
                                     self.PY if python is None else python, dict(ect.DEFAULTS, **over), dropped)


class ReadingTheTree(TempRoot):
    def test_the_three_ways_cmake_writes_a_list(self):
        self.assertEqual(ect.cmake_set(TOP, "LLVM_ALL_PROJECTS")[:2], ["bolt", "clang"])          # "a;b"
        self.assertEqual(ect.cmake_set(TOP, "LLVM_EXTRA_PROJECTS"), ["flang", "libc", "compiler-rt"])  # "a" "b"
        self.assertEqual(ect.cmake_set(TOP, "LLVM_ALL_TARGETS"), ["AArch64", "X86", "RISCV"])     # one per line
        self.assertIsNone(ect.cmake_set(TOP, "LLVM_NO_SUCH_THING"))
        cached = 'set(LLVM_TARGETS_TO_BUILD "all" CACHE STRING "semicolon list")\n'
        self.assertEqual(ect.cmake_set(cached, "LLVM_TARGETS_TO_BUILD"), ["all"])                 # not the docstring

    def test_a_tree_says_what_it_is_and_what_it_needs(self):
        t = self.fake_tree()
        self.assertEqual((t.version, t.major), ("24.0.0git", 24))
        self.assertEqual(t.cmake_min, (3, 20, 0))
        self.assertEqual(t.python_min, (3, 8))                   # the larger of the two branches
        self.assertEqual(t.cc_min, {"gcc": (7, 4), "clang": (5, 0)})
        self.assertIn("flang", t.projects)                       # from LLVM_EXTRA_PROJECTS
        self.assertNotIn("libc", t.projects)                     # ...but libc is a RUNTIME, not a project
        self.assertEqual(t.experimental, ["ARC", "Xtensa"])

    def test_only_what_is_really_in_the_tree(self):
        t = self.fake_tree(leave_out=("libsycl", "bolt"))        # an older release: declared or not, no directory
        self.assertNotIn("libsycl", t.runtimes)
        self.assertNotIn("bolt", t.projects)

    def test_experimental_targets_of_a_tree_that_does_not_list_them(self):
        t = self.fake_tree(top=TOP.replace("set(LLVM_ALL_EXPERIMENTAL_TARGETS\n  ARC\n  Xtensa )\n", ""))
        self.assertEqual(t.experimental, ["ARC", "Xtensa"])      # on disk, and not in LLVM_ALL_TARGETS

    def test_not_a_tree(self):
        with self.assertRaises(ect.ToolError):
            ect.Tree(self.root)

    TOP17 = TOP.replace('set(LLVM_ALL_PROJECTS "bolt;clang;clang-tools-extra;cross-project-tests;lld;lldb;mlir;polly")',
                        'set(LLVM_ALL_PROJECTS "bolt;clang;clang-tools-extra;compiler-rt;cross-project-tests;libc;libclc;lld;lldb;mlir;openmp;polly;pstl")') \
        .replace('set(LLVM_SUPPORTED_RUNTIMES "libc;libunwind;libcxxabi;libcxx;compiler-rt;openmp;llvm-libgcc;offload;flang-rt;libclc;libsycl")',
                 'set(LLVM_SUPPORTED_RUNTIMES "libc;libunwind;libcxxabi;pstl;libcxx;compiler-rt;openmp;llvm-libgcc")')

    def test_a_runtime_is_not_a_project(self):
        # LLVM 17-22 still list compiler-rt, openmp, libc and pstl among the projects.  Named
        # in both lists they are built twice, and the configure dies on the duplicate target.
        t = self.fake_tree(top=self.TOP17)
        put(t.src, "pstl/CMakeLists.txt")
        t = ect.Tree(t.src)
        for r in ("compiler-rt", "openmp", "libc", "pstl"):
            self.assertIn(r, t.runtimes)
            self.assertNotIn(r, t.projects)
        self.assertIn("libclc", t.projects)                      # a project only, in 17: left where it is
        self.assertFalse(set(t.projects) & set(t.runtimes))

    def test_llvm_17_to_20_need_arcmt_off_with_the_analyzer(self):
        # "Cannot disable static analyzer while enabling ARCMT": the stage1 configure of every
        # release before 21 died on MINIMAL.  From 21 the variable is a deprecated alias.
        t = self.fake_tree()
        self.assertEqual(t.major, 24)
        self.assertNotIn("-DCLANG_ENABLE_ARCMT=OFF", ect.minimal_args(t))
        put(t.src, "cmake/Modules/LLVMVersion.cmake", VERSION.replace("MAJOR 24", "MAJOR 20"))
        old = ect.Tree(t.src)
        self.assertEqual(old.major, 20)
        self.assertIn("-DCLANG_ENABLE_ARCMT=OFF", ect.minimal_args(old))
        self.assertIn("-DCLANG_ENABLE_STATIC_ANALYZER=OFF", ect.minimal_args(old))

    def test_a_cpu_name_the_tree_does_not_know_is_not_used(self):
        t = self.fake_tree()
        self.assertTrue(ect.tree_knows_cpu(t.src, "yongfeng"))         # no table in the tree: trusted
        put(t.src, "llvm/lib/TargetParser/X86TargetParser.cpp", '{"haswell", CK_Haswell},\n{"znver5", CK_ZNVER5},\n')
        self.assertTrue(ect.tree_knows_cpu(t.src, "haswell"))
        self.assertFalse(ect.tree_knows_cpu(t.src, "yongfeng"))          # gcc 14's name for a Zhaoxin: -mtune=native instead
        self.assertTrue(ect.tree_knows_cpu(t.src, ""))


class Everything(TempRoot):
    def test_the_default_is_everything_the_tree_declares(self):
        projects, runtimes, targets, experimental, skipped = self.select()
        self.assertEqual(sorted(projects), sorted("bolt clang clang-tools-extra lld lldb mlir polly flang".split()))
        self.assertEqual(sorted(runtimes), sorted("libc libunwind libcxxabi libcxx compiler-rt openmp offload "
                                                  "flang-rt libclc libsycl".split()))
        self.assertEqual((targets, experimental), ("all", ["ARC", "Xtensa"]))
        self.assertEqual(sorted(n for n, _ in skipped), ["cross-project-tests", "llvm-libgcc"])   # each with its reason
        self.assertTrue(all(why for _, why in skipped))

    def test_what_goes_takes_its_dependents_with_it(self):
        projects, runtimes, _, _, skipped = self.select(without="mlir,openmp")
        for gone in ("mlir", "flang"):
            self.assertNotIn(gone, projects)
        for gone in ("flang-rt", "openmp", "offload", "libsycl"):
            self.assertNotIn(gone, runtimes)
        self.assertIn(("flang-rt", "needs mlir (--without)"), skipped)

    def test_core_components_cannot_be_left_out(self):
        for core in ("clang", "libcxx", "compiler-rt"):
            with self.assertRaises(ect.ToolError):
                self.select(without=core)

    def test_the_machine_decides_too(self):
        projects, runtimes, _, _, skipped = self.select(machine={"arch": "s390x"})
        self.assertNotIn("bolt", projects)
        self.assertNotIn("offload", runtimes)
        _, runtimes, _, _, skipped = self.select(python={"path": "/usr/bin/python3", "yaml": False})
        self.assertNotIn("libc", runtimes)
        self.assertIn("PyYAML", dict(skipped)["libc"])
        projects, _, _, _, _ = self.select(machine={"mem_total_gb": 4.0})
        self.assertNotIn("flang", projects)

    def test_what_an_earlier_attempt_dropped_stays_dropped(self):
        projects, runtimes, _, experimental, skipped = self.select(
            dropped={"libsycl": "error: no member named x", "target:Xtensa": "error: y", "flang": "error: z"})
        self.assertNotIn("libsycl", runtimes)
        self.assertNotIn("flang-rt", runtimes)                   # flang's dependent
        self.assertEqual(experimental, ["ARC"])
        self.assertIn("no member named x", dict(skipped)["libsycl"])

    def test_targets(self):
        self.assertEqual(self.select(targets="host")[2:4], ("Native", []))
        self.assertEqual(self.select(targets="x86,aarch64,arc")[2:4], ("X86;AArch64", ["ARC"]))
        self.assertEqual(self.select(experimental_targets=False)[3], [])
        with self.assertRaises(ect.ToolError):
            self.select(targets="Z80")

    ALL_TARGETS_CACHE = """set(LLVM_ENABLE_PROJECTS "clang" CACHE STRING "")
set(LLVM_RUNTIME_TARGETS
  amdgcn-amd-amdhsa-llvm
  nvptx64-nvidia-cuda
  spirv64-unknown-unknown
  CACHE STRING "")
set(RUNTIMES_amdgcn-amd-amdhsa-llvm_LLVM_ENABLE_RUNTIMES "libclc" CACHE STRING "")
set(RUNTIMES_nvptx64-nvidia-cuda_LLVM_ENABLE_RUNTIMES "libclc" CACHE STRING "")
set(RUNTIMES_spirv64-unknown-unknown_LIBCLC_USE_SPIRV_BACKEND ON CACHE BOOL "")
set(RUNTIMES_spirv64-unknown-unknown_LLVM_ENABLE_RUNTIMES "libclc" CACHE STRING "")
"""

    def test_libclc_is_a_device_library_not_a_host_runtime(self):
        # The first real build dropped it: "Unknown libclc target architecture: x86_64".
        # It was never broken -- it was in the wrong list.
        t = self.fake_tree()
        runtimes = ["libcxx", "compiler-rt", "libclc"]
        everything = ["X86", "AArch64", "AMDGPU", "NVPTX", "SPIRV"]
        self.assertEqual(ect.device_runtimes(t, runtimes, "all", everything), (runtimes, [], []))   # an older tree: as found
        put(t.src, "libclc/cmake/caches/all-targets.cmake", self.ALL_TARGETS_CACHE)
        host, args, triples = ect.device_runtimes(t, runtimes, "all", everything)
        self.assertEqual(host, ["libcxx", "compiler-rt"])                   # OUT of the host's list...
        self.assertEqual(triples, ["amdgcn-amd-amdhsa-llvm", "nvptx64-nvidia-cuda", "spirv64-unknown-unknown"])
        self.assertEqual(args[0], "-DLLVM_RUNTIME_TARGETS=default;" + ";".join(triples))   # ...'default' keeps the host's
        self.assertIn("-DRUNTIMES_amdgcn-amd-amdhsa-llvm_LLVM_ENABLE_RUNTIMES=libclc", args)
        self.assertIn("-DRUNTIMES_spirv64-unknown-unknown_LIBCLC_USE_SPIRV_BACKEND=ON", args)     # per-triple settings too
        self.assertFalse(any("LLVM_ENABLE_PROJECTS" in a for a in args))    # the cache's own project list is not ours
        # Only for the backends that are being built:
        self.assertEqual(ect.device_runtimes(t, runtimes, "X86;AMDGPU", everything)[2], ["amdgcn-amd-amdhsa-llvm"])
        # No device target wanted, or libclc dropped: the list is said to be empty, so that a list an
        # earlier attempt left in the cache does not come back.
        self.assertEqual(ect.device_runtimes(t, runtimes, "Native", everything),
                         (["libcxx", "compiler-rt"], ["-DLLVM_RUNTIME_TARGETS=default"], []))
        self.assertEqual(ect.device_runtimes(t, ["libcxx"], "all", everything), (["libcxx"], ["-DLLVM_RUNTIME_TARGETS=default"], []))
        # A release whose libclc refuses the host triple but ships no cache yet (23.1.x): the built-in list.
        src2 = os.path.join(self.root, "llvm-project-2")
        shutil.copytree(t.src, src2)
        os.unlink(os.path.join(src2, "libclc/cmake/caches/all-targets.cmake"))
        put(src2, "libclc/CMakeLists.txt", 'message(FATAL_ERROR "Unknown libclc target architecture: ${X}")\n')
        t2 = ect.Tree(src2)
        self.assertTrue(t2.libclc_device_only)
        host, args, triples = ect.device_runtimes(t2, runtimes, "all", everything)
        self.assertEqual(host, ["libcxx", "compiler-rt"])
        self.assertEqual(len(triples), 6)
        self.assertIn("amdgcn-amd-amdhsa-llvm", triples)
        self.assertIn("-DRUNTIMES_spirv64-unknown-unknown_LIBCLC_USE_SPIRV_BACKEND=ON", args)
        self.assertEqual(ect.device_runtimes(t2, runtimes, "X86", everything)[2], [])

    def test_a_failure_in_a_per_target_runtimes_build_is_still_blamed_correctly(self):
        log = ("FAILED: libclc/CMakeFiles/clc.dir/generic/lib/math/sin.cl.bc\n/b/stage3/bin/clang -target amdgcn ...\n"
               "sin.cl:3:1: error: nope\nninja: build stopped: subcommand failed.\n"
               "FAILED: runtimes/runtimes-amdgcn-amd-amdhsa-llvm-stamps/runtimes-amdgcn-amd-amdhsa-llvm-build\n"
               "cd /b/stage3/runtimes/runtimes-amdgcn-amd-amdhsa-llvm-bins && cmake --build .\n")
        blamed, _ = ect.blame(log, "/src/llvm-project", ["libclc", "libcxx"], ["libclc"])
        self.assertEqual(list(blamed), ["libclc"])

    def test_asking_for_something_the_tree_does_not_have(self):
        with self.assertRaises(ect.ToolError):
            self.select(projects="clang,lld,circt")
        p, r, _, _, _ = self.select(projects="clang,lld,lldb", runtimes="compiler-rt,libcxx,libcxxabi,libunwind")
        self.assertEqual(sorted(p), ["clang", "lld", "lldb"])
        self.assertEqual(len(r), 4)


FLANG_FAIL = """[4101/9000] Building CXX object tools/mlir/lib/IR/CMakeFiles/obj.MLIRIR.dir/Builders.cpp.o
FAILED: [code=1] tools/flang/lib/Semantics/CMakeFiles/FortranSemantics.dir/check-omp.cpp.o
/b/stage2/bin/clang++ -DFLANG -I/src/llvm-project/flang/include -c /src/llvm-project/flang/lib/Semantics/check-omp.cpp
/src/llvm-project/flang/lib/Semantics/check-omp.cpp:12:3: error: no member named 'foo' in 'Fortran::parser::X'
1 error generated.
[4102/9000] Building CXX object lib/Support/CMakeFiles/LLVMSupport.dir/APInt.cpp.o
ninja: build stopped: cannot make progress due to previous errors.
"""
RUNTIME_FAIL = """[12/800] Building CXX object libsycl/src/CMakeFiles/sycl.dir/platform.cpp.o
FAILED: libsycl/src/CMakeFiles/sycl.dir/platform.cpp.o
/b/stage3/bin/clang++ -I/src/llvm-project/libsycl/include -c /src/llvm-project/libsycl/src/platform.cpp
/src/llvm-project/libsycl/src/platform.cpp:40:10: fatal error: 'OffloadAPI.h' file not found
ninja: build stopped: subcommand failed.
FAILED: runtimes/runtimes-stamps/runtimes-build /b/stage3/runtimes/runtimes-stamps/runtimes-build
cd /b/stage3/runtimes/runtimes-bins && /usr/bin/cmake --build .
ninja: build stopped: cannot make progress due to previous errors.
"""
CORE_FAIL = """FAILED: lib/Support/CMakeFiles/LLVMSupport.dir/APInt.cpp.o
/b/stage2/bin/clang++ -c /src/llvm-project/llvm/lib/Support/APInt.cpp
/src/llvm-project/llvm/lib/Support/APInt.cpp:1:1: error: nope
"""
XTENSA_FAIL = """FAILED: lib/Target/Xtensa/CMakeFiles/LLVMXtensaCodeGen.dir/XtensaISelLowering.cpp.o
/b/stage2/bin/clang++ -c /src/llvm-project/llvm/lib/Target/Xtensa/XtensaISelLowering.cpp
XtensaISelLowering.cpp:5:1: error: use of undeclared identifier 'x'
"""
CMAKE_FAIL = """-- Performing Test HAVE_X - Success
CMake Error at /usr/share/cmake/Modules/FindPackageHandleStandardArgs.cmake:233 (message):
  Could NOT find LevelZero (missing: LevelZero_LIBRARY)
Call Stack (most recent call first):
  /usr/share/cmake/Modules/FindPackageHandleStandardArgs.cmake:603 (_FPHSA_FAILURE_MESSAGE)
  /src/llvm-project/libsycl/cmake/Modules/FindLevelZero.cmake:20 (find_package_handle_standard_args)
  /src/llvm-project/libsycl/CMakeLists.txt:77 (find_package)

-- Configuring incomplete, errors occurred!
FAILED: runtimes/runtimes-stamps/runtimes-configure /b/stage3/runtimes/runtimes-stamps/runtimes-configure
cd /b/stage3/runtimes/runtimes-bins && /usr/bin/cmake ...
"""
OOM_FAIL = """FAILED: bin/clang-24
: && /b/stage2/bin/clang++ -flto=thin -o bin/clang-24 tools/clang/tools/driver/CMakeFiles/clang.dir/driver.cpp.o
clang++: error: unable to execute command: Killed
clang++: error: linker command failed due to signal (use -v to see invocation)
"""
RUNTIMES = "libc libunwind libcxxabi libcxx compiler-rt openmp offload flang-rt libclc libsycl".split()


class WhoBrokeTheBuild(unittest.TestCase):
    def blame(self, text, optional=("flang", "mlir", "libsycl", "libc", "offload")):
        return ect.blame(text, "/src/llvm-project", RUNTIMES, optional)

    def test_a_project(self):
        blamed, oom = self.blame(FLANG_FAIL)
        self.assertEqual(list(blamed), ["flang"])                     # and ninja 1.12's "[code=1]" is not an output
        self.assertIn("no member named 'foo'", blamed["flang"])
        self.assertFalse(oom)

    def test_a_runtime_is_named_by_the_inner_ninja_not_by_the_stamp(self):
        blamed, oom = self.blame(RUNTIME_FAIL)
        self.assertEqual(list(blamed), ["libsycl"])
        self.assertIn("OffloadAPI.h", blamed["libsycl"])
        self.assertFalse(oom)                                    # the silent outer block is the stamp, not a kill

    def test_core_is_core(self):
        self.assertEqual(list(self.blame(CORE_FAIL)[0]), ["llvm"])
        self.assertEqual(ect.component_of("tools/clang/lib/Sema/x.o", RUNTIMES), "clang")
        self.assertEqual(ect.component_of("tools/clang/tools/extra/clang-tidy/x.o", RUNTIMES), "clang-tools-extra")
        self.assertEqual(ect.component_of("libcxx/src/x.o", RUNTIMES), "libcxx")      # not 'libc'
        self.assertEqual(ect.component_of("libc/src/x.o", RUNTIMES), "libc")

    def test_an_experimental_target(self):
        blamed, oom = self.blame(XTENSA_FAIL)
        self.assertEqual(list(blamed), ["target:Xtensa"])
        self.assertFalse(oom)

    def test_a_configure_error_is_traced_through_its_call_stack(self):
        blamed, oom = self.blame(CMAKE_FAIL)
        self.assertEqual(list(blamed), ["libsycl"])                   # not cmake's own module, not the stamp
        self.assertIn("LevelZero", blamed["libsycl"])
        self.assertFalse(oom)

    def test_an_install_that_exits_0_is_not_thereby_an_install(self):
        # Real, from trunk 3c2eaf39: a typo in orc-rt's header list.  LLVM runs the runtimes'
        # install script through execute_process() and ignores the result, so `ninja install`
        # exited 0 and this tool said "everything was built" -- twice.
        log = ("-- Installing: /stage/opt/x/bin/sycl-ls\n-- Installing: /stage/opt/x/include/Compiler.h\n"
               "CMake Error at runtimes/runtimes-bins/orc-rt/include/cmake_install.cmake:46 (file):\n"
               "  file INSTALL cannot find\n"
               "  \"/src/llvm-project/orc-rt/include/orc-rt-c/CoreTyspe.h\":\n  No such file or directory.\n"
               "Call Stack (most recent call first):\n  runtimes/runtimes-bins/orc-rt/cmake_install.cmake:52 (include)\n\n"
               "-- Install configuration: \"Release\"\n-- Installing: /stage/opt/x/lib/clang/24/lib/amdgcn-amd-amdhsa-llvm/libclc.bc\n")
        errs = ect.install_errors(log, RUNTIMES + ["orc-rt"])
        self.assertEqual(list(errs), ["orc-rt"])
        self.assertIn("CoreTyspe.h", errs["orc-rt"])
        self.assertIn("No such file or directory", errs["orc-rt"])
        self.assertEqual(ect.install_errors("-- Installing: /x/bin/clang\n-- Up-to-date: /x/lib/a\n", RUNTIMES), {})
        per_target = "CMake Error at runtimes/runtimes-amdgcn-amd-amdhsa-llvm-bins/libclc/cmake_install.cmake:9 (file):\n  nope\n"
        self.assertEqual(list(ect.install_errors(per_target, RUNTIMES)), ["libclc"])
        core = "CMake Error at tools/clang/lib/Headers/cmake_install.cmake:12 (file):\n  file INSTALL cannot find x\n"
        self.assertEqual(list(ect.install_errors(core, RUNTIMES)), ["clang"])          # core: the caller stops

    def test_killed_for_memory_is_nobodys_fault(self):
        blamed, oom = self.blame(OOM_FAIL)
        self.assertTrue(oom)

    def test_a_failure_nobody_can_be_blamed_for_is_fatal(self):
        blamed, oom = self.blame("FAILED: runtimes/runtimes-stamps/runtimes-build\ncd /b && cmake --build .\n")
        self.assertEqual(list(blamed), ["llvm"])
        self.assertFalse(oom)
        self.assertEqual(self.blame("[1/2] fine\n")[0], {})

    def test_a_kill_without_a_word_is_a_kill(self):
        # clang runs cc1 in-process: when the kernel kills it, ninja shows the command and
        # nothing else.  ninja 1.12 adds the exit code; 1.11 does not.
        silent = "FAILED: tools/flang/lib/Semantics/CMakeFiles/x.dir/a.cpp.o\n/b/stage2/bin/clang++ -c a.cpp\n[9/90] next\n"
        self.assertTrue(self.blame(silent)[1])
        coded = "FAILED: [code=137] lib/Support/CMakeFiles/x.dir/a.cpp.o\n/b/stage2/bin/clang++ -c a.cpp\nclang++: note: x\n"
        blamed, oom = self.blame(coded)
        self.assertTrue(oom)                                     # by the code alone: the body says nothing of memory
        self.assertEqual(list(blamed), ["llvm"])                 # the code is not part of the output's name
        self.assertFalse(self.blame(FLANG_FAIL)[1])             # a real error always says something
        self.assertEqual(ect.failed_blocks(coded)[0][3], 137)
        # ninja 1.12 gave a code, and it is not a kill: silence with [code=1] is a plain failure.
        self.assertFalse(self.blame("FAILED: [code=1] runtimes/runtimes-stamps/runtimes-build\ncd /b && cmake --build .\n")[1])
        self.assertFalse(self.blame("FAILED: [code=1] tools/mlir/x.o\n/b/clang++ -c x.cpp\n")[1])

    def test_a_full_disk_is_nobodys_fault(self):
        self.assertTrue(ect.disk_full("a.cpp:1:1: fatal error: error writing a.o: No space left on device\n"))
        self.assertFalse(ect.disk_full(FLANG_FAIL))

    def test_error_summary_shows_the_cmake_message(self):
        log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        log.write("-- Configuring\nCMake Error at clang/CMakeLists.txt:415 (message):\n"
                  "  Cannot disable static analyzer while enabling ARCMT or Z3\nCall Stack (most recent call first):\n"
                  "  x.cmake:1\n\n-- Configuring incomplete, errors occurred!\n")
        log.close()
        try:
            hits = ect.error_summary(log.name)
        finally:
            os.unlink(log.name)
        self.assertTrue(any("Cannot disable static analyzer" in h for h in hits), hits)


class Resuming(unittest.TestCase):
    PIPE = ["stage1", "stage2-instrumented", "train", "stage2", "stage3", "install", "verify"]

    def todo(self, done, present, selected=None):
        return ect.stages_to_run(self.PIPE, selected or self.PIPE, lambda n: n in done, lambda n: n in present)

    def test_a_fresh_build_runs_everything_in_order(self):
        self.assertEqual(self.todo([], []), self.PIPE)

    def test_a_failed_stage3_resumes_at_stage3(self):
        done = self.PIPE[:4]
        self.assertEqual(self.todo(done, ["train", "stage2"]), ["stage3", "install", "verify"])    # stage1 is GONE: fine

    def test_but_not_without_the_compiler_it_is_built_with(self):
        done = self.PIPE[:4]
        # stage2 was tidied away (or deleted by hand): it is rebuilt, and so is what IT needs.
        self.assertEqual(self.todo(done, ["train"]), ["stage1", "stage2", "stage3", "install", "verify"])

    def test_run_2_alone(self):
        self.assertEqual(self.todo(self.PIPE[:4], ["train", "stage2"], ["stage3", "install", "verify"]),
                         ["stage3", "install", "verify"])

    def test_a_training_makes_what_was_built_from_the_profile_stale(self):
        # The profile is gone but stage2 is done and on disk: train runs, and stage2
        # (and stage3, when selected) must run after it, or stage3 has no compiler.
        self.assertEqual(ect.with_profile_users(["train", "stage3", "install", "verify"], self.PIPE, self.PIPE),
                         ["train", "stage2", "stage3", "install", "verify"])
        run1 = ["stage1", "stage2-instrumented", "train", "stage2"]
        self.assertEqual(ect.with_profile_users(["train"], run1, self.PIPE), ["train", "stage2"])
        self.assertEqual(ect.with_profile_users(["stage3"], self.PIPE, self.PIPE), ["stage3"])      # no training: nothing added

    def test_without_pgo_there_is_no_training_to_need(self):
        pipe = ["stage1", "stage2", "stage3", "install", "verify"]
        self.assertEqual(ect.stages_to_run(pipe, pipe, lambda n: n in ("stage1", "stage2"), lambda n: n == "stage2"),
                         ["stage3", "install", "verify"])


class SmallThings(TempRoot):
    def test_jobs_come_from_available_memory(self):
        m = {"cpus": 24, "mem_avail_gb": 22.0, "mem_total_gb": 62.0}
        j = ect.plan_jobs(dict(ect.DEFAULTS), m, "clang")
        self.assertEqual((j["jobs"], j["compile"], j["link"], j["lto_link"], j["flang"]), (24, 22, 5, 1, 5))
        self.assertEqual(ect.plan_jobs(dict(ect.DEFAULTS), m, "gcc")["stage1_compile"], 13)       # gcc needs more per job
        tiny = ect.plan_jobs(dict(ect.DEFAULTS), {"cpus": 4, "mem_avail_gb": 1.0, "mem_total_gb": 2.0}, "gcc")
        self.assertTrue(all(v >= 1 for v in tiny.values()))
        self.assertEqual(ect.plan_jobs(dict(ect.DEFAULTS, link_jobs=3), m, "clang")["lto_link"], 3)

    def test_the_cpu_level_is_read_from_the_flags_the_kernel_lists(self):
        v2 = "fpu cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3 pni".split()
        v3 = v2 + "avx avx2 bmi1 bmi2 f16c fma abm movbe xsave".split()
        v4 = v3 + "avx512f avx512bw avx512cd avx512dq avx512vl".split()
        self.assertEqual(ect.cpu_level(v4), "x86-64-v4")
        self.assertEqual(ect.cpu_level(v3), "x86-64-v3")
        self.assertEqual(ect.cpu_level(v2), "x86-64-v2")
        self.assertEqual(ect.cpu_level([f for f in v3 if f != "fma"]), "x86-64-v2")     # one v3 bit short: v2
        self.assertEqual(ect.cpu_level(["sse2"]), "")
        self.assertEqual(ect.cpu_level([f for f in v4 if f != "popcnt"]), "")          # no v2, so no v4 either
        info = ect.cpu_info("processor\t: 0\nmodel name\t: Intel(R) Xeon(R) CPU E5-2670 v3 @ 2.30GHz\n"
                            "flags\t\t: " + " ".join(v3) + "\nbugs\t: none\n")
        self.assertEqual(info["model"], "Intel(R) Xeon(R) CPU E5-2670 v3 @ 2.30GHz")
        self.assertEqual(ect.cpu_level(info["flags"]), "x86-64-v3")
        m = {"arch": "x86_64", "cpu_flags": v3, "cpu_model": "Xeon", "cpu_tune": "haswell"}
        self.assertEqual(ect.auto_tune(m), (["-march=x86-64-v3", "-mtune=haswell"], "Xeon is x86-64-v3, which the compiler calls haswell"))
        self.assertEqual(ect.auto_tune(dict(m, cpu_tune=""))[0], ["-march=x86-64-v3", "-mtune=native"])
        self.assertEqual(ect.auto_tune({"arch": "aarch64"})[0], [])
        self.assertEqual(ect.auto_tune({"arch": "x86_64", "cpu_flags": ["sse2"]})[0], [])
        self.assertEqual(ect.tune_flags("auto", {}, m), ["-march=x86-64-v3", "-mtune=haswell"])
        self.assertEqual(ect.tune_flags("auto", {}), [])                                # nothing known: portable
        self.assertEqual(ect.merge_options(type("A", (), {})())["tune"], "auto")
        clang = put(self.root, "clang", "#!/bin/sh\necho ' \"-cc1\" \"-target-cpu\" \"haswell\"' >&2\n")
        os.chmod(clang, 0o755)
        self.assertEqual(ect.native_cpu_name({"kind": "clang", "path": clang}), "haswell")
        gcc = put(self.root, "gcc", "#!/bin/sh\necho '  -mtune=                     znver3'\n")
        os.chmod(gcc, 0o755)
        self.assertEqual(ect.native_cpu_name({"kind": "gcc", "path": gcc}), "znver3")
        self.assertEqual(ect.native_cpu_name(None), "")

    def test_tuning(self):
        self.assertEqual(ect.tune_flags("portable", {}), [])
        self.assertEqual(ect.tune_flags("native", {}), ["-march=native"])
        self.assertEqual(ect.tune_flags("x86-64-v3", {}), ["-march=x86-64-v3"])
        self.assertEqual(ect.tune_flags("-march=x86-64-v3 -mtune=haswell", {}), ["-march=x86-64-v3", "-mtune=haswell"])
        self.assertEqual(ect.tune_flags("portable", {"gcc_prefix": "/opt/rh/gcc-toolset-15/root/usr"}),
                         ["--gcc-toolchain=/opt/rh/gcc-toolset-15/root/usr"])

    def test_the_stage0_compiler(self):
        def mk(kind, ver, path, origin, rpath=""):
            return {"kind": kind, "version": ver, "path": path, "cxx": path + "++", "origin": origin,
                    "system": origin == "system", "rpath": rpath, "gcc_prefix": "", "lld": True}
        sys_gcc, sys_clang = mk("gcc", (8, 5, 0), "/usr/bin/gcc", "system"), mk("clang", (19, 1), "/usr/bin/clang", "system")
        toolset = mk("gcc", (15, 2), "/opt/rh/gcc-toolset-15/root/usr/bin/gcc", "toolset")
        mine = mk("clang", (24, 0), "/home/me/opt/clang-24/bin/clang", "private")
        my_gcc = mk("gcc", (17, 0), "/home/me/opt/gcc-17/bin/gcc", "private", rpath="/home/me/opt/gcc-17/lib64")
        mins = {"gcc": (7, 4), "clang": (5, 0)}
        everything = [my_gcc, mine, toolset, sys_gcc, sys_clang]
        self.assertEqual(ect.pick_host_compiler(everything, mins)["path"], "/usr/bin/clang")     # the system's, clang first
        self.assertEqual(ect.pick_host_compiler([mine, toolset, sys_gcc], mins)["path"], "/usr/bin/gcc")
        # A tree that has outgrown the system compiler: the toolset, not a refusal...
        newer = {"gcc": (12, 0), "clang": (20, 0)}
        self.assertEqual(ect.pick_host_compiler([my_gcc, toolset, sys_gcc], newer)["path"], toolset["path"])
        # ...and a private gcc that would bake an rpath into the result is never picked FOR you.
        self.assertIsNone(ect.pick_host_compiler([my_gcc, sys_gcc], newer))
        self.assertEqual(ect.pick_host_compiler([my_gcc, mine, sys_gcc], newer)["path"], mine["path"])
        no_partner = dict(sys_clang, cxx="")
        self.assertEqual(ect.pick_host_compiler([no_partner, sys_gcc], mins)["path"], "/usr/bin/gcc")

    def test_what_a_person_choosing_a_compiler_is_told(self):
        mins = {"gcc": (7, 4), "clang": (5, 0)}
        base = {"kind": "gcc", "version": (17, 0), "path": "/x/bin/gcc", "cxx": "/x/bin/g++", "origin": "private",
                "rpath": "/x/lib64", "gcc_prefix": "/x", "lld": False}
        tags, good = ect.compiler_tags(base, mins, check=False)
        self.assertTrue(good)
        self.assertTrue(any("NOT PORTABLE" in t and "/x/lib64" in t for t in tags))
        tags, good = ect.compiler_tags(dict(base, version=(4, 8, 5), rpath=""), mins, check=False)
        self.assertFalse(good)
        self.assertTrue(any("too old" in t for t in tags))
        self.assertFalse(ect.compiler_tags(dict(base, cxx=""), mins, check=False)[1])
        toolset = dict(base, origin="toolset", rpath="", gcc_prefix="/opt/rh/gcc-toolset-15/root/usr")
        self.assertTrue(any("--gcc-toolchain=/opt/rh/gcc-toolset-15/root/usr" in t for t in ect.compiler_tags(toolset, mins, False)[0]))

    def test_the_stage0_identity_survives_a_package_rebuild(self):
        # The record used to keep the full banner: a dnf upgrade of 21.1.8-1 to 21.1.8-2 made
        # a finished run 1 impossible to continue -- and Enter would have deleted it.
        self.assertEqual(ect.host_identity({"kind": "clang", "version": (21, 1, 8)}), "clang 21.1.8")
        self.assertEqual(ect.host_identity("clang version 21.1.8 (AlmaLinux OS Foundation 21.1.8-1.el10.alma.1)"), "clang 21.1.8")
        self.assertEqual(ect.host_identity("clang version 21.1.8 (AlmaLinux OS Foundation 21.1.8-2.el10.alma.1)"), "clang 21.1.8")
        self.assertEqual(ect.host_identity("gcc (GCC) 14.2.1 20250110 (Red Hat 14.2.1-7)"), "gcc 14.2.1")
        old = {"source": "23.1.3", "host": "clang version 21.1.8 (AlmaLinux OS Foundation 21.1.8-1.el10.alma.1)",
               "flags": ["-march=x86-64-v3"], "pgo": True, "rtti": True}                         # as recorded before --link was in it
        new = {"source": "23.1.3", "host": "clang 21.1.8", "flags": ["-march=x86-64-v3"], "pgo": True, "rtti": True, "link": "static"}
        self.assertEqual(ect.fingerprint_norm(old), ect.fingerprint_norm(new))
        self.assertNotEqual(ect.fingerprint_norm(dict(old, host="clang version 22.1.0")), ect.fingerprint_norm(new))
        self.assertNotEqual(ect.fingerprint_norm(dict(new, link="dylib")), ect.fingerprint_norm(new))
        # A record from before --link was compared: its own options say what it was.
        st = ect.State(os.path.join(self.root, "state.json"))
        st.d["fingerprint"], st.d["options"] = dict(old), {"link": "dylib"}
        self.assertEqual(ect.recorded_fingerprint(st)["link"], "dylib")
        # The gcc installation flag changed its spelling once; the host identity covers the compiler.
        gcc_old = dict(old, host="gcc (GCC) 14.2.1 20250110", flags=["-march=x86-64-v3", "--gcc-toolchain=/opt/rh/gcc-toolset-14/root/usr"])
        gcc_new = dict(new, host="gcc 14.2.1", flags=["-march=x86-64-v3", "--gcc-install-dir=/opt/rh/gcc-toolset-14/root/usr/lib/gcc/x86_64-redhat-linux/14"])
        self.assertEqual(ect.fingerprint_norm(gcc_old), ect.fingerprint_norm(gcc_new))

    def test_packages_and_repositories_fit_the_machine(self):
        py312 = {"path": "/usr/bin/python3.12"}
        self.assertEqual(ect.python_pkg("python3-pyyaml", py312, "dnf"), "python3.12-pyyaml")
        self.assertEqual(ect.python_pkg("python3-devel", py312, "yum"), "python3.12-devel")
        self.assertEqual(ect.python_pkg("python3-PyYAML", {"path": "/usr/bin/python3.11"}, "zypper"), "python311-PyYAML")
        self.assertEqual(ect.python_pkg("python3-pyyaml", {"path": "/usr/bin/python3"}, "dnf"), "python3-pyyaml")
        self.assertEqual(ect.python_pkg("python3-yaml", py312, "apt-get"), "python3-yaml")
        self.assertEqual(ect.preferred_pkg_managers({"ID": "ubuntu", "ID_LIKE": "debian"}), ["apt-get"])
        self.assertEqual(ect.preferred_pkg_managers({"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"}), ["zypper"])
        self.assertEqual(ect.preferred_pkg_managers({"ID": "almalinux", "ID_LIKE": "rhel centos fedora"}), ["dnf", "yum"])
        self.assertEqual(ect.preferred_pkg_managers({"ID": "arch"}), ["pacman"])
        self.assertEqual(ect.preferred_pkg_managers({"ID": "nixos"}), [])
        self.assertEqual(ect.crb_hint({"os_id": "almalinux", "os_like": "rhel centos fedora", "os_version": "8.10"}),
                         "sudo dnf config-manager --set-enabled powertools")
        self.assertEqual(ect.crb_hint({"os_id": "rocky", "os_like": "rhel centos fedora", "os_version": "10.0"}),
                         "sudo dnf config-manager --set-enabled crb")
        self.assertEqual(ect.crb_hint({"os_id": "rhel", "os_like": "fedora", "os_version": "9.4", "arch": "x86_64"}),
                         "sudo subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms")
        self.assertEqual(ect.crb_hint({"os_id": "fedora", "os_like": "", "os_version": "42"}), "")
        self.assertEqual(ect.crb_hint({"os_id": "amzn", "os_like": "fedora", "os_version": "2023"}), "")

    def test_a_gcc_is_pinned_by_its_installation_not_by_where_it_was_found(self):
        inst = os.path.join(self.root, "opt/gcc-17/lib/gcc/x86_64-pc-linux-gnu/17.0.0")
        os.makedirs(inst)
        so = put(self.root, "opt/gcc-17/lib64/libstdc++.so", "\x7fELF")
        gxx = put(self.root, "opt/gcc-17/bin/g++", '#!/bin/sh\ncase "$1" in -print-search-dirs) echo "install: %s/";; *) echo %s;; esac\n' % (inst, so))
        os.chmod(gxx, 0o755)
        self.assertEqual(ect.gcc_install_dir(gxx), inst)
        link = os.path.join(self.root, "usr-local-bin")                  # found through a symlink: the same answer
        os.makedirs(link)
        os.symlink(gxx, os.path.join(link, "g++-17"))
        self.assertEqual(ect.gcc_install_dir(os.path.join(link, "g++-17")), inst)
        self.assertEqual(ect.gcc_runtime(os.path.join(link, "g++-17")),
                         (os.path.join(self.root, "opt/gcc-17"), os.path.join(self.root, "opt/gcc-17/lib64")))
        self.assertEqual(ect.tune_flags("portable", {"gcc_install": "/i/lib/gcc/x/14"}), ["--gcc-install-dir=/i/lib/gcc/x/14"])
        self.assertEqual(ect.tune_flags("portable", {"gcc_prefix": "/p"}), ["--gcc-toolchain=/p"])

    def test_what_build_info_says_was_built(self):
        prefix = os.path.join(self.root, "prefix")
        put(prefix, "BUILD-INFO.txt", "Built by x\nprojects : clang lld mlir\nruntimes : libcxx compiler-rt\n"
                                      "targets  : all ARC Xtensa\nNOT built: flang-rt -- error: x\n")
        built = ect.built_from_info(prefix)
        self.assertEqual(built["projects"], ["clang", "lld", "mlir"])
        self.assertEqual(built["runtimes"], ["libcxx", "compiler-rt"])
        self.assertEqual((built["targets"], built["experimental"]), ("all", ["ARC", "Xtensa"]))
        self.assertEqual(built["dropped"], ["flang-rt"])
        self.assertEqual(ect.built_from_info(os.path.join(self.root, "nowhere")), {})

    def test_a_killed_build_is_not_running(self):
        d = os.path.join(self.root, "b")
        os.makedirs(d)
        self.assertFalse(ect.lock_is_held(d))                         # no lock file at all
        put(d, ".lock")
        self.assertFalse(ect.lock_is_held(d))                         # nobody holds it
        held = len(ect._LOCKS)
        try:
            ect.lock_dir(d)
            self.assertTrue(ect.lock_is_held(d))
        finally:
            for f in ect._LOCKS[held:]:
                f.close()
            del ect._LOCKS[held:]
        self.assertFalse(ect.lock_is_held(d))

    def test_where_a_tool_comes_from(self):
        self.assertEqual(ect.origin_of("/usr/bin/gcc"), "system")
        self.assertEqual(ect.origin_of("/usr/bin/gcc-13"), "system, versioned")
        self.assertEqual(ect.origin_of("/opt/rh/gcc-toolset-15/root/usr/bin/gcc"), "toolset")
        self.assertEqual(ect.origin_of("/usr/lib/llvm-18/bin/clang"), "distro LLVM package")
        self.assertEqual(ect.origin_of("/home/me/opt/clang-24/bin/clang"), "private")

    def test_names_are_matched_files_are_checked(self):
        a, b = os.path.join(self.root, "a"), os.path.join(self.root, "b")
        for d, names in ((a, ("gcc", "gcc-13", "clang", "gcc-ar", "clang-format", "clang++")), (b, ("python3",))):
            for n in names:
                os.chmod(put(d, n, "#!/bin/sh\n"), 0o755)
        os.makedirs(os.path.join(b, "gcc"))                      # ~/opt/gcc-17/lib holds a DIRECTORY named gcc
        put(b, "clang", "not executable")
        os.symlink(os.path.join(a, "gcc"), os.path.join(b, "gcc-99"))      # an alias of one already seen
        found = ect.scan_names([a, b], ect.COMPILER_NAME)
        self.assertEqual([os.path.basename(f) for f in found], ["gcc", "clang", "gcc-13"])
        self.assertEqual(ect.scan_names([a, b], ect.PYTHON_NAME), [os.path.join(b, "python3")])

    def fake_binutils(self, where, banner):
        for t in ("as", "ld"):
            os.chmod(put(self.root, where + "/" + t, "#!/bin/sh\necho '%s'\n" % banner), 0o755)
        return os.path.join(self.root, where)

    def test_binutils_sets(self):
        mine = self.fake_binutils("opt/binutils-246/bin", "GNU assembler (GNU Binutils) 2.46")
        self.fake_binutils("opt/binutils-246/x86_64-pc-linux-gnu/bin", "GNU assembler (GNU Binutils) 2.46")   # the same set again
        ts = self.fake_binutils("ts/root/usr/bin", "GNU assembler version 2.44-8.el10")
        self.fake_binutils("llvm/bin", "LLVM (http://llvm.org/): assembler")                                   # not GNU
        os.chmod(put(self.root, "half/bin/as", "#!/bin/sh\necho 'GNU assembler 2.40'\n"), 0o755)              # as without ld
        sets = ect.find_binutils([mine, os.path.join(self.root, "opt/binutils-246/x86_64-pc-linux-gnu/bin"), ts,
                                  os.path.join(self.root, "llvm/bin"), os.path.join(self.root, "half/bin")])
        self.assertEqual(sorted((b["version"], b["dir"]) for b in sets), sorted([((2, 46), mine), ((2, 44), ts)]))

    def test_a_compiler_is_paired_with_the_assembler_it_was_built_against(self):
        sets = [{"dir": "/usr/bin", "version": (2, 41), "system": True}, {"dir": "/ts/bin", "version": (2, 44), "system": False},
                {"dir": "/mine/bin", "version": (2, 46), "system": False}]
        gcc = {"kind": "gcc", "cxx": "/home/me/opt/gcc-17/bin/g++", "path": "/home/me/opt/gcc-17/bin/gcc", "system": False, "rpath": ""}
        tried = []
        real = ect.compiler_works

        def fake(cxx, flags=(), extra_path=None):
            tried.append(list(extra_path or []))
            return (True, "runs") if "/ts/bin" in (extra_path or []) or "/mine/bin" in (extra_path or []) \
                else (False, "Error: unknown pseudo-op: `.base64'")
        ect.compiler_works = fake
        try:
            # As found: broken.  Then the other sets, NEWEST first; the first that works is the one.
            self.assertEqual(ect.pick_binutils(gcc, sets)[:2], ("/mine/bin", True))
            self.assertEqual([t[0] for t in tried], ["/home/me/opt/gcc-17/bin", "/mine/bin"])
            self.assertEqual(ect.pick_binutils(gcc, sets[:1]), ("", False, "Error: unknown pseudo-op: `.base64'"))
            # clang assembles by itself: a broken clang is not "fixed" by trying assemblers.
            del tried[:]
            self.assertEqual(ect.pick_binutils(dict(gcc, kind="clang"), sets)[:2], ("", False))
            self.assertEqual(len(tried), 1)
            ect.compiler_works = lambda cxx, flags=(), extra_path=None: (True, "runs")
            self.assertEqual(ect.pick_binutils(gcc, sets)[:2], ("", True))            # works as found: nothing to add
            with self.assertRaises(ect.ToolError):
                ect.pick_binutils(gcc, sets, asked=os.path.join(self.root, "no-binutils-here"))
            chosen = self.fake_binutils("chosen/bin", "GNU assembler (GNU Binutils) 2.45")
            self.assertEqual(ect.pick_binutils(gcc, sets, asked=chosen)[:2], (chosen, True))
        finally:
            ect.compiler_works = real
        self.assertEqual(ect.host_path(gcc, "/mine/bin"), ["/mine/bin", "/home/me/opt/gcc-17/bin"])
        self.assertEqual(ect.host_path({"system": True, "path": "/usr/bin/gcc"}), [])

    def test_the_probe_has_the_data_that_needs_a_matching_assembler(self):
        self.assertIn("static const unsigned char blob[] = {0,1,2,", ect.PROBE_CXX)   # a hello-world passed; LLVM did not

    def test_a_gcc_that_brings_its_own_libstdcxx(self):
        def fake_gxx(where, so, elf):
            put(self.root, so, "\x7fELF" if elf else "/* GNU ld script */ INPUT ( /usr/lib64/libstdc++.so.6 -lstdc++_nonshared )")
            path = put(self.root, where + "/bin/g++", "#!/bin/sh\necho %s\n" % os.path.join(self.root, so))
            os.chmod(path, 0o755)
            return path
        mine = fake_gxx("opt/gcc-17", "opt/gcc-17/lib64/libstdc++.so", True)
        self.assertEqual(ect.gcc_runtime(mine), (os.path.join(self.root, "opt/gcc-17"),
                                                 os.path.join(self.root, "opt/gcc-17/lib64")))
        # A Red Hat toolset's libstdc++.so is a linker SCRIPT onto the system's: headers yes, rpath no.
        ts = fake_gxx("ts/root/usr", "ts/root/usr/lib/gcc/x/15/libstdc++.so", False)
        self.assertEqual(ect.gcc_runtime(ts), (os.path.join(self.root, "ts/root/usr"), ""))
        self.assertEqual(ect.gcc_runtime("/usr/bin/g++")[0], "")             # the system's needs nothing said

    def test_the_build_python_is_the_one_with_yaml(self):
        mk = lambda path, ver, yaml, impl="cpython": {"path": path, "version": ver, "yaml": yaml, "impl": impl, "devel": True}
        pys = [mk("/usr/local/bin/python3", (3, 14, 7), False), mk("/usr/bin/python3", (3, 12, 1), True),
               mk("/usr/bin/pypy3", (3, 10, 0), True, "pypy")]
        self.assertEqual(ect.pick_python(pys, (3, 8))["path"], "/usr/bin/python3")
        self.assertEqual(ect.pick_python(pys[:1], (3, 8))["path"], "/usr/local/bin/python3")      # better than none
        self.assertIsNone(ect.pick_python([mk("/usr/bin/python3", (3, 6, 8), True)], (3, 8)))      # RHEL 8's platform python
        with self.assertRaises(ect.ToolError):
            ect.pick_python(pys, (3, 8), forced="/nonexistent/python3")
        me = ect.pick_python([], (3, 6), forced=sys.executable)                                   # --python: asked, not guessed
        self.assertEqual(me["path"], sys.executable)
        with self.assertRaises(ect.ToolError):
            ect.pick_python([], (99, 0), forced=sys.executable)

    def test_releases(self):
        raw = json.dumps([
            {"tag_name": "llvmorg-22.1.0-rc3", "prerelease": True, "assets": []},
            {"tag_name": "llvmorg-21.1.8", "prerelease": False, "assets": [
                {"name": "llvm-project-21.1.8.src.tar.xz", "browser_download_url": "https://x/21.1.8.tar.xz",
                 "digest": "sha256:" + "ab" * 32}]},
            {"tag_name": "llvmorg-22.1.0", "prerelease": False, "assets": []},
            {"tag_name": "something-else", "prerelease": False, "assets": []}])
        rels = ect.parse_releases(raw)
        self.assertEqual([r["version"] for r in rels], ["22.1.0", "21.1.8"])        # newest first, no rc
        self.assertEqual(rels[1]["sha256"], "ab" * 32)
        self.assertIn("llvmorg-22.1.0/llvm-project-22.1.0.src.tar.xz", rels[0]["url"])   # no asset listed: the known URL
        with self.assertRaises(ect.ToolError):
            ect.parse_releases("<html>blocked by the proxy</html>")

    def test_the_environment_is_an_allow_list(self):
        old = dict(os.environ)
        os.environ.update(CC="/opt/x/clang", CXX="/opt/x/clang++", LD_LIBRARY_PATH="/opt/x/lib", LLVM="/opt/x/bin/",
                          CMAKE_PREFIX_PATH="/opt/x", https_proxy="http://proxy:3128")
        try:
            env = ect.scrubbed_env(["/tools/bin"])
            for leaked in ("CC", "CXX", "LD_LIBRARY_PATH", "LLVM", "CMAKE_PREFIX_PATH"):
                self.assertNotIn(leaked, env)
            self.assertEqual(env["https_proxy"], "http://proxy:3128")             # git clone behind a proxy
            self.assertTrue(env["PATH"].startswith("/tools/bin:/usr/bin"))
            self.assertIn("CC=/opt/x/clang", ect.env_traps())                      # ...and the user is TOLD
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_configure_is_verified_in_build_ninja(self):
        bdir = os.path.join(self.root, "b")
        # As cmake's Ninja generator really lays it out: the compiler in the RULES file,
        # the flags on the build edges.  Looking in build.ninja alone stopped a good build.
        put(bdir, "CMakeFiles/rules.ninja", "rule CXX_COMPILER__x\n  command = /b/stage1/bin/clang++ $DEFINES $FLAGS -c $in\n")
        put(bdir, "build.ninja", "build x.o: CXX_COMPILER__x x.cpp\n  FLAGS = -O3 -fprofile-use=/b/clang.profdata\n")
        self.assertTrue(ect.verify_configure(bdir, {"compiler": "/b/stage1/bin/clang++", "profile": "clang.profdata"}))
        self.assertFalse(ect.verify_configure(bdir, {"LTO": "-flto=thin"}))        # asked for, silently not there
        self.assertFalse(ect.verify_configure(os.path.join(self.root, "nowhere"), {"x": "y"}))

    @unittest.skipUnless(shutil.which("tar"), "needs tar")
    def test_a_tree_with_the_final_name_is_a_whole_tree(self):
        src = os.path.join(self.root, "pack", "llvm-project-1.0.src")
        put(src, "llvm/CMakeLists.txt", "x")
        tb = os.path.join(self.root, "t.tar")
        ect.run(["tar", "-cf", tb, "-C", os.path.join(self.root, "pack"), "llvm-project-1.0.src"])
        dest = os.path.join(self.root, "out", "llvm-project-1.0.src")
        ect.extract(tb, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "llvm", "CMakeLists.txt")))
        self.assertEqual(os.listdir(os.path.dirname(dest)), ["llvm-project-1.0.src"])  # no temp dir left behind
        two = os.path.join(self.root, "two.tar")
        put(self.root, "pack/other/x", "y")
        ect.run(["tar", "-cf", two, "-C", os.path.join(self.root, "pack"), "llvm-project-1.0.src", "other"])
        with self.assertRaises(ect.ToolError):
            ect.extract(two, os.path.join(self.root, "out2", "t"))

    def test_the_record_survives_and_one_instance_per_build(self):
        path = os.path.join(self.root, "b", "state.json")
        os.makedirs(os.path.dirname(path))
        st = ect.State(path)
        st.set_stage("stage1", status="done", seconds=700)
        st.d["dropped"]["libsycl"] = "error: x"
        st.save()
        again = ect.State(path)
        self.assertTrue(again.done("stage1"))
        self.assertFalse(again.done("stage2"))
        self.assertEqual(again.d["dropped"], {"libsycl": "error: x"})
        held = len(ect._LOCKS)
        try:
            ect.lock_dir(os.path.dirname(path))
            with self.assertRaises(ect.ToolError):
                ect.lock_dir(os.path.dirname(path))
        finally:
            for f in ect._LOCKS[held:]:
                f.close()
            del ect._LOCKS[held:]

    def test_a_profile_holds_the_choices_and_the_command_line_wins(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.root
        try:
            self.assertEqual(ect.list_profiles(), [])
            opts = dict(ect.DEFAULTS, source="/src/llvm-project", host_cc="/home/me/opt/clang-24/bin/clang",
                        tune="x86-64-v3", pgo=False, without="flang,lldb", jobs=12, restart=True)
            ect.save_profile("siege3", opts)
            got = ect.load_profile("siege3")
            self.assertEqual((got["host_cc"], got["tune"], got["pgo"], got["jobs"], got["without"]),
                             ("/home/me/opt/clang-24/bin/clang", "x86-64-v3", False, 12, "flang,lldb"))
            self.assertNotIn("restart", got)                                # belongs to ONE run, not to a profile
            profs = ect.list_profiles()
            self.assertEqual([p["name"] for p in profs], ["siege3"])
            self.assertIn("pgo off", profs[0]["summary"])

            class A(object):
                profile, tune = "siege3", "native"
            merged = ect.merge_options(A())
            self.assertEqual(merged["tune"], "native")                      # the command line
            self.assertEqual(merged["host_cc"], "/home/me/opt/clang-24/bin/clang")   # the profile
            self.assertEqual(merged["lto"], "thin")                         # the default
            put(self.root, ect.TOOL + "/profiles/notes.txt", "not a profile")
            self.assertEqual(len(ect.list_profiles()), 1)
            with self.assertRaises(ect.ToolError):
                ect.load_profile("no-such-profile")
            # --source, --ref and --version choose the same thing: the command line's choice
            # replaces the profile's, it does not clash with it.
            ect.save_profile("pinned", dict(ect.DEFAULTS, version="22.1.8", workdir="./wd"))

            class B(object):
                profile, source = "pinned", "/x/llvm-project"
            merged = ect.merge_options(B())
            self.assertEqual((merged["source"], merged["version"]), ("/x/llvm-project", "latest"))
            self.assertTrue(os.path.isabs(ect.load_profile("pinned")["workdir"]))    # replayed from any directory
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    def test_with_no_terminal_there_is_nothing_to_ask(self):
        class A(object):
            pass
        saved = sys.stdin
        try:
            sys.stdin = open(os.devnull)                            # whatever the test runner's stdin is
            self.assertEqual(ect.cmd_wizard(A()), 2)
        finally:
            sys.stdin.close()
            sys.stdin = saved
        a = A()
        a.host_linker = "ld.exe"
        with self.assertRaises(ect.ToolError):
            ect.merge_options(a)

    def test_options(self):
        class A(object):
            pass
        a = A()
        a.lto = "fat"
        with self.assertRaises(ect.ToolError):
            ect.merge_options(a)
        a.lto, a.source, a.ref = "thin", "/x", "main"
        with self.assertRaises(ect.ToolError):                                       # two ways to say "which source"
            ect.merge_options(a)
        a.ref = None
        self.assertEqual(ect.merge_options(a)["source"], "/x")
        self.assertTrue(ect.merge_options(A())["pgo"])                               # PGO, ThinLTO, everything: the defaults
        self.assertTrue(ect.merge_options(A())["bolt"])
        self.assertTrue(ect.merge_options(A())["rtti"])
        self.assertIsNone(ect.merge_options(A())["activate"])                       # ask; default yes
        a = A()
        a.activate = False                                                           # --no-activate
        self.assertFalse(ect.merge_options(a)["activate"])
        self.assertEqual(ect.merge_options(A())["projects"], "all")


class Bolt(TempRoot):
    def test_bolt_sits_between_stage3_and_install(self):
        pipe = ["stage1", "stage2-instrumented", "train", "stage2", "stage3", "bolt", "install", "verify"]
        todo = ect.stages_to_run(pipe, pipe, lambda n: n in pipe[:5], lambda n: n in ("train", "stage2", "stage3"))
        self.assertEqual(todo, ["bolt", "install", "verify"])
        # 'done', but the binaries no longer carry the mark (stage3 relinked them): again, before install
        todo = ect.stages_to_run(pipe, ["install", "verify"], lambda n: n in pipe[:6], lambda n: n != "bolt")
        self.assertEqual(todo, ["bolt", "install", "verify"])

    def test_when_bolt_is_possible(self):
        opts, m = dict(ect.DEFAULTS), {"arch": "x86_64"}
        self.assertEqual(ect.bolt_possible(opts, m), (True, ""))
        self.assertFalse(ect.bolt_possible(dict(opts, bolt=False), m)[0])
        self.assertFalse(ect.bolt_possible(dict(opts, without="lldb,bolt"), m)[0])
        self.assertFalse(ect.bolt_possible(dict(opts, projects="clang,lld"), m)[0])
        self.assertFalse(ect.bolt_possible(dict(opts, link="dylib"), m)[0])
        self.assertIn("ppc64le", ect.bolt_possible(opts, {"arch": "ppc64le"})[1])

    def test_the_hook_names_the_two_executables_and_nothing_else(self):
        hook = ect.BOLT_HOOK % (ect.TOOL, " ".join(ect.BOLT_TARGETS))
        self.assertIn("foreach(t clang lld)", hook)
        self.assertIn('"LINKER:--emit-relocs" "LINKER:-z,now"', hook)
        self.assertIn("cmake_language(DEFER", hook)
        self.assertNotIn("CMAKE_EXE_LINKER_FLAGS", hook)

    def test_the_profiles_are_folded_while_the_training_runs(self):
        raw, outd = os.path.join(self.root, "raw"), os.path.join(self.root, "bolt")
        os.makedirs(raw)
        os.makedirs(outd)
        tool = put(self.root, "merge-fdata", "#!/bin/sh\ncat \"$@\"\n")
        os.chmod(tool, 0o755)
        old = time.time() - 120
        for i in range(3):
            os.utime(put(raw, "clang.%d.fdata" % i, "1 a 1 b 0 %d\n" % i), (old, old))
        young = put(raw, "clang.9.fdata", "young\n")
        m = ect.FdataMerger(tool, raw, outd, ("clang", "lld"), dict(os.environ))
        m.fold()
        self.assertEqual(m.counts["clang"], 3)
        self.assertTrue(os.path.exists(young))                     # still being written, for all we know
        self.assertEqual(os.listdir(raw), ["clang.9.fdata"])
        m.fold(final=True)                                         # after ninja: nothing is still writing
        self.assertEqual(m.counts, {"clang": 4, "lld": 0})
        self.assertEqual(os.listdir(raw), [])
        with open(os.path.join(outd, "clang.fdata")) as f:
            self.assertEqual(f.read().count("\n"), 4)             # the merged file is an input to the next merge
        self.assertFalse(os.path.exists(os.path.join(outd, "lld.fdata")))
        self.assertEqual(m.errors, [])

    def test_bolts_mark_is_read_from_the_binary(self):
        readelf = put(self.root, "readelf", "#!/bin/sh\necho '  [ 1] .text'\n"
                                            "case \"$2\" in *bolted*) echo '  [ 2] .note.bolt_info';; esac\n")
        os.chmod(readelf, 0o755)
        self.assertTrue(ect.has_bolt_mark(put(self.root, "bolted"), readelf))
        self.assertFalse(ect.has_bolt_mark(put(self.root, "plain"), readelf))
        self.assertFalse(ect.has_bolt_mark(os.path.join(self.root, "missing"), readelf))
        self.assertFalse(ect.has_relocs(put(self.root, "bolted"), readelf))
        self.assertEqual(ect.bolt_stats("x\nBOLT-INFO: 4123 out of 91022 functions in the binary (4.5%) have "
                                        "non-empty execution profile\n"), "4123 of 91022 functions profiled (4.5%)")
        self.assertEqual(ect.bolt_stats("nothing"), "")


class ResumeOrStartOver(TempRoot):
    """A build directory with a past: shown, then resumed, deleted or left alone."""

    FP = {"source": "23.1.3", "host": "clang 21.1.8", "flags": ["-march=x86-64-v3"], "pgo": True, "rtti": True, "link": "static"}

    def record(self, **stages):
        """A state like the real one left by stage1 done + stage2-instrumented interrupted."""
        root = os.path.join(self.root, "build", "23.1.3")
        os.makedirs(root)
        st = ect.State(os.path.join(root, "state.json"))
        st.d["fingerprint"], st.d["prefix"] = dict(self.FP), os.path.join(self.root, "opt", "clang-23.1.3")
        st.d["stages"]["stage1"] = {"status": "done", "started": "2026-10-08T00:14:48-0400",
                                    "finished": "2026-10-08T00:21:21-0400", "seconds": 392, "error": ""}
        st.d["stages"]["stage2-instrumented"] = {"status": "interrupted", "started": "2026-10-08T00:21:21-0400",
                                                 "finished": "2026-10-08T00:21:40-0400", "error": ""}
        st.d["stages"].update(stages)
        st.save()
        put(root, "stage1/bin/clang", "x" * 3000)
        put(root, "stage2-instrumented/CMakeCache.txt", "y" * 1000)
        put(root, "logs/stage1.build.log", "z" * 100)
        put(root, ".lock")
        return st, root

    def opts(self, **over):
        return dict(ect.DEFAULTS, **over)

    @staticmethod
    def answers(*given):
        """An `ask` that hands out these answers in turn, and remembers what it was asked."""
        asked, queue = [], list(given)

        def ask(question, default):
            asked.append((question, default))
            return queue.pop(0)
        ask.asked = asked
        return ask

    def never(self, question, default):
        self.fail("asked %r although there was nothing to ask" % question)

    def test_nothing_there_nothing_shown_nothing_asked(self):
        root = os.path.join(self.root, "empty")
        os.makedirs(root)
        st = ect.State(os.path.join(root, "state.json"))
        self.assertEqual(ect.previous_build_summary(st, root), [])
        self.assertEqual(ect.previous_build_summary(st, os.path.join(self.root, "not-even-there")), [])
        self.assertEqual(ect.resume_decision(self.opts(), st, True, self.never), "resume")
        self.assertEqual(ect.resume_decision(self.opts(restart=True), st, True, self.never), "restart")   # files without a record: --restart still wipes

    def test_the_record_is_shown_stages_times_size_prefix_dropped(self):
        st, root = self.record()
        st.d["dropped"]["libsycl"] = "error: no member named x"
        text = "\n".join(ect.previous_build_summary(st, root, fingerprint=self.FP))
        self.assertIn("started     : 2026-10-08 00:14, last activity 2026-10-08 00:21", text)
        self.assertRegex(text, r"stages      : stage1 +done +6m32s")
        self.assertRegex(text, r"\n +stage2-instrumented +interrupted")
        self.assertRegex(text, r"on disk     : 4\.\dK  \(logs, stage1, stage2-instrumented\)")   # not .lock, not state.json
        self.assertIn("prefix      : %s  (not installed yet)" % st.d["prefix"], text)
        self.assertIn("dropped     : libsycl -- error: no member named x", text)
        self.assertIn("options     : the same as this run's", text)
        self.assertNotIn("record      :", text)
        os.makedirs(st.d["prefix"])                                  # the prefix exists: install would refuse
        text = "\n".join(ect.previous_build_summary(st, root, fingerprint=self.FP))
        self.assertIn("(EXISTS -- it is never installed over", text)
        self.assertIn("--prefix NEW", text)
        mine = os.path.join(self.root, "opt", "elsewhere")             # --prefix given now: THAT one is checked
        self.assertIn("prefix      : %s  (not installed yet)" % mine,
                      "\n".join(ect.previous_build_summary(st, root, prefix=mine)))

    def test_a_record_that_says_running_was_killed(self):
        # This process holds the directory's lock, so nothing IS running: the tool died without a word.
        st, root = self.record(stage3={"status": "running", "started": "2026-10-09T10:00:00-0400", "finished": "", "error": ""})
        text = "\n".join(ect.previous_build_summary(st, root))
        self.assertRegex(text, r"stage3 +killed +\(recorded as running, and nothing is running now\)")
        self.assertIn("last activity 2026-10-09 10:00", text)

    def test_files_without_a_record_are_still_shown(self):
        root = os.path.join(self.root, "b")
        put(root, "stage1/bin/clang", "x")
        st = ect.State(os.path.join(root, "state.json"))
        lines = ect.previous_build_summary(st, root)
        self.assertTrue(any(l.startswith("record      : no stage recorded") and "--restart" in l for l in lines))
        self.assertTrue(any(l.startswith("on disk     :") and "(stage1)" in l for l in lines))
        self.assertFalse(any(l.startswith("started") for l in lines))
        self.assertEqual(ect.resume_decision(self.opts(), st, True, self.never), "resume")    # nothing was built: no question
        self.assertEqual(ect.resume_decision(self.opts(restart=True), st, True, self.never), "restart")

    def test_a_record_with_options_but_no_stage_is_simply_written_over(self):
        # `build --only source` leaves a record with a fingerprint and no stage; nothing
        # was built, so a run with other options is not a mismatch -- and no question is
        # asked about a build that was never shown.
        root = os.path.join(self.root, "b")
        os.makedirs(root)
        st = ect.State(os.path.join(root, "state.json"))
        st.d["fingerprint"] = dict(self.FP)
        st.save()
        other = dict(self.FP, pgo=False)
        self.assertEqual(ect.previous_build_summary(st, root, fingerprint=other), [])
        self.assertEqual(ect.resume_decision(self.opts(), st, False, self.never), "resume")
        self.assertEqual(ect.resume_decision(self.opts(resume=True), st, False, None), "resume")
        put(root, "logs/old.log", "x")                                 # with files on disk: shown, still no question
        text = "\n".join(ect.previous_build_summary(st, root, fingerprint=other))
        self.assertIn("nothing was built, so this run's count", text)
        self.assertNotIn("DIFFERENT", text)
        self.assertEqual(ect.resume_decision(self.opts(), st, False, self.never), "resume")

    def test_the_question_and_its_default(self):
        st, _ = self.record()
        for answer, meaning in (("", "resume"), ("   ", "resume"), ("r", "resume"), ("Resume", "resume"),
                                ("d", "restart"), ("DELETE", "restart"), ("restart", "restart"),   # the flag's name: NOT 'r'
                                ("q", "quit"), ("quit", "quit"), (None, "quit")):                  # None: the input ended
            ask = self.answers(answer)
            self.assertEqual(ect.resume_decision(self.opts(), st, True, ask), meaning, repr(answer))
            self.assertEqual(ask.asked, [("Resume it (r), delete everything and start over (d), or quit (q)?", "r")])
        ask = self.answers("x", "yes", "res", "d")                    # not an answer: asked again
        self.assertEqual(ect.resume_decision(self.opts(), st, True, ask), "restart")
        self.assertEqual(len(ask.asked), 4)

    def test_other_options_delete_or_quit_and_enter_deletes(self):
        st, root = self.record()
        now = dict(self.FP, source="24.0.0git-abc", rtti=False)
        text = "\n".join(ect.previous_build_summary(st, root, fingerprint=now))
        self.assertIn("options     : DIFFERENT from this run's -- it cannot be continued with these:", text)
        self.assertRegex(text, r"\n +rtti +then true, now false")
        self.assertRegex(text, r"\n +source +then \"23.1.3\", now \"24.0.0git-abc\"")
        self.assertNotIn("host ", text)                               # only what differs
        for answer, meaning in (("", "restart"), ("d", "restart"), ("restart", "restart"), ("q", "quit"), (None, "quit")):
            ask = self.answers(answer)
            self.assertEqual(ect.resume_decision(self.opts(), st, False, ask), meaning, repr(answer))
            self.assertEqual(ask.asked, [("Delete everything and start over (d), or quit (q)?", "d")])
        ask = self.answers("r", "resume", "q")                        # resume is not on offer
        self.assertEqual(ect.resume_decision(self.opts(), st, False, ask), "quit")
        self.assertEqual(len(ask.asked), 3)

    def test_with_nobody_to_ask_the_flags_decide_and_nothing_is_deleted_on_a_guess(self):
        st, _ = self.record()
        self.assertEqual(ect.resume_decision(self.opts(), st, True, None), "resume")           # no terminal, --yes
        self.assertEqual(ect.resume_decision(self.opts(), st, False, None), "quit")            # other options: an error, not a deletion
        self.assertEqual(ect.resume_decision(self.opts(restart=True), st, True, None), "restart")
        self.assertEqual(ect.resume_decision(self.opts(restart=True), st, False, None), "restart")
        self.assertEqual(ect.resume_decision(self.opts(resume=True), st, True, self.never), "resume")
        self.assertEqual(ect.resume_decision(self.opts(resume=True), st, False, self.never), "quit")
        self.assertEqual(ect.resume_decision(self.opts(restart=True), st, True, self.never), "restart")

    def test_the_two_flags_contradict_each_other(self):
        class A(object):
            resume = restart = True
        with self.assertRaises(ect.ToolError):
            ect.merge_options(A())
        self.assertFalse(ect.merge_options(type("A", (), {})())["resume"])
        self.assertNotIn("resume", ect.PROFILE_KEYS + ect.PROFILE_FLAGS)     # one run's answer, never a profile's

    def test_the_build_id_is_a_name_not_a_path(self):
        # The build directory is the one thing this tool deletes: it is always <workdir>/build/<name>.
        for bad in ("..", ".", "../..", "a/b", "/home/me", "23.1.3/"):
            class A(object):
                build_id = bad
            with self.assertRaises(ect.ToolError, msg=bad):
                ect.merge_options(A())

        class B(object):
            build_id = "23.1.3-second-try"
        self.assertEqual(ect.merge_options(B())["build_id"], "23.1.3-second-try")

    def test_a_start_over_is_planned_as_if_the_directory_were_already_empty(self):
        # Run 1 done, its products on disk, `build --run 2` answered d: the wipe comes
        # after Proceed, so the plan must not count what it removes -- cmd_build hands
        # stages_to_run a has_product that says no to everything.
        pipe = ["stage1", "stage2-instrumented", "train", "stage2", "stage3", "bolt", "install", "verify"]
        run2 = ["stage3", "bolt", "install", "verify"]
        fresh = ect.State(os.path.join(self.root, "state.json"), fresh=True)
        self.assertEqual(ect.stages_to_run(pipe, run2, fresh.done, lambda n: False), pipe)
        self.assertEqual(ect.stages_to_run(pipe, ["stage3"], fresh.done, lambda n: False), pipe[:5])
        on_disk = lambda n: n in ("train", "stage2")                 # what a resume would see
        self.assertEqual(ect.stages_to_run(pipe, run2, fresh.done, on_disk), run2)

    def test_a_record_from_before_rtti_was_a_choice(self):
        st = ect.State(os.path.join(self.root, "state.json"))
        self.assertIsNone(ect.recorded_fingerprint(st))
        st.d["fingerprint"] = {"source": "x", "host": "h", "flags": [], "pgo": True}
        self.assertEqual(ect.recorded_fingerprint(st)["rtti"], False)
        self.assertEqual(ect.fingerprint_diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}),
                         ["b       then 2, now 3", "c       then null, now 4"])
        self.assertEqual(ect.when("2026-10-08T00:14:48-0400"), "2026-10-08 00:14")
        self.assertEqual(ect.when(""), "?")
        # Across the autumn clock change the wall-clock text orders wrongly: 01:10 EST is AFTER 01:30 EDT.
        self.assertGreater(ect.stamp_key("2026-11-01T01:10:00-0500"), ect.stamp_key("2026-11-01T01:30:00-0400"))
        self.assertEqual(ect.stamp_key("garbage"), 0.0)
        st, root = self.record(stage3={"status": "done", "started": "2026-11-01T01:30:00-0400",
                                       "finished": "2026-11-01T01:10:00-0500", "seconds": 2400, "error": ""})
        self.assertIn("last activity 2026-11-01 01:10", "\n".join(ect.previous_build_summary(st, root)))

    def test_a_fresh_record_reads_nothing_and_writes_nothing_until_saved(self):
        st, root = self.record()
        fresh = ect.State(st.path, fresh=True)
        self.assertEqual(fresh.d, {"stages": {}, "dropped": {}})
        self.assertTrue(ect.State(st.path).done("stage1"))           # the file is still the old record
        put(root, "garbage.json", "[1, 2]")
        self.assertEqual(ect.State(os.path.join(root, "garbage.json")).d, {"stages": {}, "dropped": {}})

    def test_delete_everything_keeps_the_lock_and_follows_no_link(self):
        st, root = self.record()
        outside = put(self.root, "elsewhere/precious.txt", "keep me")
        os.symlink(os.path.dirname(outside), os.path.join(root, "stage1", "lib"))     # bolt/inst/lib is such a link
        os.symlink(os.path.dirname(outside), os.path.join(root, "link"))
        ect.wipe_build_dir(root)
        self.assertEqual(os.listdir(root), [".lock"])
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(ect.previous_build_summary(ect.State(st.path), root), [])
        ect.wipe_build_dir(root)                                       # again: nothing to do, no complaint
        ect.wipe_build_dir(os.path.join(self.root, "no-such-directory"))   # not even a directory: the same

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_a_wipe_cut_short_leaves_no_record_that_lies(self):
        st, root = self.record()
        locked = os.path.join(root, "stage1")
        os.chmod(locked, 0o500)                                         # rmtree cannot empty it
        try:
            with self.assertRaises(ect.ToolError):
                ect.wipe_build_dir(root)
        finally:
            os.chmod(locked, 0o700)
        self.assertFalse(os.path.exists(st.path))                       # the record went FIRST
        self.assertTrue(os.path.isdir(locked))                          # what could not go is still there
        self.assertIn("--restart removes it", "\n".join(ect.previous_build_summary(ect.State(st.path), root)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
