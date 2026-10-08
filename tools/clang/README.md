# enterprise_clang_tool.py

Builds a **complete** LLVM/clang toolchain from source: bootstrapped to a real
stage3, PGO + ThinLTO + BOLT optimised, every project, runtime and target the
source tree declares. Successor to `build_clang_local.sh` and `build_clang_universal.sh`.

One file. Standard library only. Runs under the Python your package manager
depends on (`/usr/bin/python3`; syntax held to 3.6 for RHEL 8). Never run it as
root: only `--install-deps` calls `sudo`.

```
./enterprise_clang_tool.py doctor                 # what is missing, and the command that installs it
./enterprise_clang_tool.py plan                   # resolve everything, print it, build nothing
./enterprise_clang_tool.py build                  # fetch, bootstrap, build everything, install, verify
./enterprise_clang_tool.py build --run 1          # ...or in two runs: the bootstrap compiler
./enterprise_clang_tool.py build --run 2          #    then everything, with it
./enterprise_clang_tool.py status                 # every build, stage by stage
./enterprise_clang_tool.py verify ~/opt/clang-X   # the post-install checks against any prefix
./enterprise_clang_tool.py versions | detect | clean
```

Expect several hours and about 95 GB of disk (75 without BOLT). `build` is **resumable**: a
failure, a `^C`, a dropped ssh session or a reboot costs the stage that was
running, not the build. Run it again with the same options -- it shows what it
finds and asks whether to resume it or delete everything and start over (see
[Resume, or start over](#resume-or-start-over)).

## The pipeline

```
run 1  bootstrap    host cc -> stage1 -> stage2-instrumented -> train -> stage2
run 2  everything   stage2  -> stage3 (EVERYTHING, PGO + ThinLTO) -> bolt -> install -> verify
```

| stage | built by | what | why |
|---|---|---|---|
| stage1 | the system's clang, else gcc | clang, lld, the profile runtime; native target | something that can build the rest; thrown away |
| stage2-instrumented | stage1 | clang + lld, `LLVM_BUILD_INSTRUMENTED=IR` | a compiler that records what it does |
| train | the instrumented clang (and lld, which runs instrumented but writes no profile: it exits without running exit handlers) | LLVM itself (Release), then a small `-O0 -g` pass | real, heavy C++ that is on every machine this runs on |
| stage2 | stage1 + the profile | a fast clang + lld | run 2 is long; this makes it shorter |
| stage3 | **stage2** | everything, PGO + ThinLTO | the product |
| bolt | stage3's llvm-bolt | clang and lld instrumented, build LLVM (lld once through a ThinLTO link), re-laid out by what ran | hot code together, cold code apart: on top of PGO + LTO, which decided what the code is, this decides where it sits |

The old scripts (LLVM's `PGO.cmake`) install a compiler that stage1 built --
strictly a stage2. Here the installed compiler was built by a clang that was
itself built by clang from the same source, and `verify` reads the proof out of
the binary's `.comment` section.

**Two runs** because that is what it takes: "everything" is best built by an
optimised compiler, and that compiler does not exist until run 1 is over. `build`
does both back to back; `--run 1` and `--run 2` split them (different nights,
or run 1 once and run 2 per configuration).

## Resume, or start over

When `build` finds an earlier run in the build directory it shows it before
the plan -- when it started and was last active, every stage with its status
and time, the size on disk and what is there, the prefix (and whether it
already exists), the components an earlier attempt dropped -- and asks:

```
==> Previous build in ~/src/enterprise_clang_tool/build/23.1.3
  started     : 2026-10-08 00:14, last activity 2026-10-08 00:21
  stages      : stage1               done         6m32s
                stage2-instrumented  interrupted
  on disk     : 3.3G  (logs, stage1, stage2-instrumented)
  prefix      : ~/opt/clang-23.1.3  (not installed yet)
  options     : the same as this run's

  Resume it (r), delete everything and start over (d), or quit (q)? [r]:
```

Enter resumes. `d` (or `delete`, or `restart`) deletes everything in the build
directory -- the stage trees, the logs, the record, the profiles; the source
tree, the built tools and the prefix are elsewhere and stay -- and that
deletion happens only **after** `Proceed?`, so a `n` (or the end of the input)
there leaves the directory as it was. A build that starts over is planned as
if the directory were already empty (`--run 2` answered `d` runs run 1 again),
and the disk check counts the space the deletion frees. A stage the record
calls `running` while nothing is running is shown as `killed`. The only
directory the tool ever deletes is `<workdir>/build/<build-id>`, and
`--build-id` must be a name, not a path.

| | |
|---|---|
| `--resume` | continue, without being asked |
| `--restart` | delete everything first, without being asked |
| no terminal, or `--yes` | resume; a build that can be continued is never deleted on a guess |
| the record was started with **other options** (source, stage0 compiler and version, tuning, PGO, RTTI, `--link`) | it cannot be continued: the differences are listed, the question is delete (`d`, the default) or quit (`q`). Without a terminal, or with `--resume`, that is an error, not a deletion. `--build-id NAME` builds beside it instead |
| the prefix already exists | said so in the summary: it is never installed over, so a run that reaches install needs `--prefix NEW` (the build directory does not care) |

## "Everything"

Is **read from the tree**, not listed here: `LLVM_ALL_PROJECTS` (+ flang),
`LLVM_SUPPORTED_RUNTIMES`, `LLVM_ALL_TARGETS` and the experimental targets. The
same tool therefore builds LLVM 17 and trunk, and a runtime added upstream next
month is built without a new version of this file. (Releases 17 to 22 still list
compiler-rt, openmp, libc and pstl among the *projects*: they are built once, as
runtimes. Releases 17 to 20 refuse to turn the static analyzer off while ARCMT
is on; the bootstrap stages turn ARCMT off there too. A release whose libclc is
device-only but ships no `all-targets.cmake` yet, such as 23.1.x, gets trunk's
list of six device triples built in.) On trunk (2026-08) that is:

- projects: bolt clang clang-tools-extra lld lldb mlir polly flang
- runtimes: libc libunwind libcxxabi libcxx compiler-rt openmp offload flang-rt libclc libsycl orc-rt
- targets: all 20, plus ARC CSKY DirectX M68k Xtensa
- also: `libLLVM.so`, `libclang-cpp.so`, the LLVM utilities (FileCheck & co), `LLVMgold.so` when binutils' `plugin-api.h` is installed

`libclc` is a **device** library (OpenCL's C library for GPUs and SPIR-V): it has
no host variant, so it is built once per device triple through
`LLVM_RUNTIME_TARGETS`, with the triples and per-triple settings read from the
tree's own `libclc/cmake/caches/all-targets.cmake` -- and only for the backends
being built (`--targets X86,AMDGPU` builds it for amdgcn alone).

Left out, always, with the reason printed in every plan:

| | |
|---|---|
| `llvm-libgcc` | replaces the system's libgcc; upstream makes it an explicit opt-in, and it cannot share a prefix with compiler-rt + libunwind |
| `cross-project-tests` | tests only, nothing to install (`--check` adds it) |

Left out **on this machine** when it cannot be built here: bolt off x86_64 /
aarch64 / riscv64, offload where it is unsupported, libc without PyYAML, flang
under 7 GB of RAM. `--without a,b` leaves out more (dependents go with them:
`--without mlir` also drops flang and flang-rt). `--projects`, `--runtimes`,
`--targets` name an exact set.

### A component that does not build

An **optional** component that fails is *dropped* -- recorded, with the error
that did it -- and the build carries on; stage3 runs `ninja -k 0`, so one
reconfigure deals with every broken component at once. The final report and
`BUILD-INFO.txt` in the prefix say what is missing and why. `--retry-dropped`
tries them again -- and then installs and verifies again, so on a finished
build it needs `--prefix NEW` (an installed prefix is never touched).

A **core** failure (llvm, clang, lld, compiler-rt, libunwind, libc++abi, libc++,
a non-experimental target) stops the build with the *first* errors from the
log, not the tail of 24 interleaved jobs. A job **killed for memory** is nobody's
fault: the stage is retried with half the jobs and one link at a time -- a kill
that leaves no message (clang runs its compiler in-process, so the kernel takes
the driver itself) is recognised by the silence: a job that printed nothing and
for which ninja reported no exit code, or exit code 137. A **full disk** stops the
build with nothing dropped.

## It will refuse to

install over an existing prefix (it installs through `DESTDIR` into a staging
directory beside the prefix and renames once -- the prefix is complete or
absent) · build in a directory another instance is working in · continue a build
directory with different options (it offers to delete it, or `--restart` /
`--build-id`) · delete an earlier run without a person saying so (`--restart`
is the only way without a terminal) · start
stage3 with less disk than the plan needs (`--ignore-disk`) · trust a configure:
after each one it looks in `rules.ninja` and `build.ninja` for the stage's
compiler, `-fprofile-use` and `-flto` -- a profile that was silently not picked
up costs the whole build and produces an ordinary compiler · run as root ·
activate what it built when a check in `verify` failed, when `verify` did not
run in this invocation, or when told `--no-activate` (otherwise it asks once,
next to Proceed, and the default is yes: the result is meant to be used) ·
replace a `clang-current` that is a real directory rather than a link.

## Choosing the tools

```
./enterprise_clang_tool.py                       # on a terminal, no command: the wizard -- saved profiles FIRST
./enterprise_clang_tool.py toolchains            # every compiler, python, cmake, ninja, binutils and linker; which would be used
./enterprise_clang_tool.py build --host-cc ~/opt/clang-24-4/bin/clang --save-profile siege3
./enterprise_clang_tool.py build --profile siege3
./enterprise_clang_tool.py profiles
```

| flag | default | |
|---|---|---|
| `--host-cc PATH` `--host-cxx PATH` | the system's clang, else its gcc, else a toolset | the stage0 compiler. `toolchains` lists every gcc and clang in `/usr/bin`, your `PATH`, `~/opt/*/bin`, `/opt/*/bin`, `/usr/local`, Red Hat toolsets and distro LLVM packages -- with what each one would mean |
| `--host-binutils auto\|DIR` | auto | see below |
| `--host-linker default\|lld\|gold\|bfd\|mold` | lld with a clang stage0 | stage1 only; later stages use their own lld |
| `--python PATH` | a CPython >= the tree's minimum **that has yaml** | your `PATH` may lead to PyPy or a pyenv build without it |
| `--cmake PATH` `--ninja PATH` `--provision cmake,ninja` | the machine's, if new enough for this tree | else pinned versions are built from source |
| `--profile NAME` `--save-profile NAME` | | the choices (never `--resume`, `--restart`, `--from`, `--yes`), in `~/.config/enterprise_clang_tool/profiles/`. Command line beats profile beats default |

The wizard asks: which LLVM, the stage0 compiler (a numbered list), the build's
python when there is a choice, tuning, PGO, LTO, what to leave out, the prefix,
whether to activate -- each with a default good enough to press Enter through,
and the first screen is the list of saved profiles: Enter reuses the newest
as it is.

**A compiler is more than its path**, and the tool says what comes with each:

- A *private clang* (the old script's "build N with N-1") is an easy stage0: it
  uses the system's libstdc++ and assembles by itself.
- A *toolset gcc* needs later stages to be told `--gcc-toolchain=<its prefix>`,
  or stage1's clang -- linked against the toolset's libstdc++ -- would build
  stage2 against the system's older headers. Done automatically.
- A *private gcc* brings its own `libstdc++.so.6`. Every binary of every stage
  gets an rpath to it, and **the finished toolchain needs that directory at run
  time**. Listed as `NOT PORTABLE`, never chosen for you.
- A gcc may have been **built against a newer assembler** than the one it finds
  in a clean environment. gcc >= 15 writes data as `.base64`, which binutils
  < 2.43 rejects -- but only in files that have such data, so a hello-world
  passes and LLVM fails a minute into stage1. The preflight probe contains such
  data; when a gcc fails it, the other binutils sets on the machine are tried,
  newest first, and the one that makes it work is put first in the build's PATH.

The default never changes: the **system's** compiler, because that is what
makes a build repeatable on the next machine.

## What it works out for itself

| | from | drives |
|---|---|---|
| stage0 compiler | `/usr/bin/clang`, `/usr/bin/gcc`, versioned ones, `/opt/rh/gcc-toolset-*`; then your PATH, `~/opt`, `/opt`, `/usr/local` and distro LLVM packages, **listed, and chosen only when no system compiler qualifies** | the system's clang first (faster, less memory), else gcc; only one that meets the tree's own `GCC_MIN` / `CLANG_MIN` and that can build and run a C++17 program -- a broken first choice is passed over, and the preflight says so |
| build python | `/usr/bin/python3*`, then the same places | CPython >= the tree's minimum, **the one that has yaml** first (user site-packages count) |
| cmake, ninja | the machine's, if new enough for *this tree* | otherwise pinned versions are **built from source** into `<workdir>/tools` (`--provision cmake,ninja` forces that) |
| jobs | CPUs and **available** memory | compile / link / LTO-link / flang pools; the OOM killer takes the largest process, which on a shared machine is somebody else's |
| optional libraries | headers present | lldb's python, libedit, curses, lzma, libxml2; zlib, zstd; LLVMgold.so -- each left out cleanly, and named by `doctor` with its package |
| your shell's exports | `CC CXX CFLAGS LDFLAGS LD_LIBRARY_PATH LLVM CMAKE_PREFIX_PATH ...` | reported, and **not** passed to the build (proxy and CA variables are) |

## Choices

| flag | default | |
|---|---|---|
| `--version X.Y.Z` / `--ref REF` / `--source DIR` | latest release | tarball (sha256 checked when GitHub publishes one) / shallow git clone / your tree, read-only |
| `--prefix` | `~/opt/clang-<version>` | must not exist |
| `--tune` | `auto` | the x86-64 level this CPU has (read from the CPUID bits in `/proc/cpuinfo`, the ones glibc's own v2/v3/v4 checks use) and `-mtune` for this CPU (what the stage0 compiler resolves `-march=native` to: `haswell` here). A level rather than the CPU's name because `-march=haswell` also licenses AES, RDRND, PCLMUL, INVPCID, which no v3 check verifies. `portable` builds for any machine of this architecture; `native`, an ISA level (`x86-64-v3`), or raw flags are the manual choices. Applies to the toolchain's own binaries, never to the runtime libraries |
| `--lto thin\|full\|off` | thin | stage3 |
| `--no-pgo` | PGO on | skips the instrumented stage and training |
| `--link static\|dylib` | static | static is the fastest compiler; dylib the smallest install |
| `--no-rtti` | RTTI on | upstream's default is off; on costs the compiler nothing and Mesa and friends need it |
| `--no-bolt` | BOLT on | skips the bolt stage. Needs `--link static` (the default) and the bolt project; x86_64, aarch64, riscv64 hosts |
| `--keep-stages` | off | intermediate trees are removed as soon as the next stage has used them (about 35 GB) |
| `--strip` | off | `install/strip` instead of `install`; `verify` then reads the compiler's provenance from the unstripped stage3 binary, since llvm-strip removes `.comment` (a standalone `verify PREFIX` of a stripped install skips that check) |

With LTO on, the static archives of LLVM / clang / MLIR in `lib/` hold **LLVM
bitcode**: link them with this toolchain's lld, or link `libLLVM.so` /
`libclang-cpp.so`. The runtime libraries (libc++, compiler-rt, libomp, ...) are
ordinary native code -- `verify` checks.

## Dependencies

`doctor` prints the one command for this machine. Required: a C++17 compiler,
CPython >= the tree's minimum, tar + xz (or git). cmake and ninja are built if
missing. On RHEL-likes ninja and several `-devel` packages live in CRB /
PowerTools: `sudo dnf config-manager --set-enabled crb`.

## Files

`stage2` is **kept** beside `stage3`: every object in stage3 was compiled by it
and depends on the headers inside it, so without it the tree cannot be
reconfigured and `--retry-dropped` or `--check` would cost a full rebuild.
`clean` removes the pair (about 30 GB).

`<workdir>` (default `~/src/enterprise_clang_tool`): `src/` downloaded sources,
`tools/` built cmake/ninja, `build/<id>/` with one directory per stage,
`logs/`, `clang.profdata`, `bolt/` (the BOLT profiles; the instrumented pair and
its training trees while the stage runs), and `state.json` -- the record `build`
resumes from. In `stage3/bin`, `clang-NN.prebolt` and `lld.prebolt` are the
binaries as linked, before BOLT laid them out.
In the prefix: `BUILD-INFO.txt`.

## Tests

```
/usr/bin/python3 tests/test_tool.py
```

79 tests, no network, no root, no LLVM tree (a twelve-line fake one): reading a
tree, component selection and its dependencies, **blaming a failed build on the
right component** from real ninja and cmake output, resuming, the
resume-or-start-over summary and question (with the answers injected), job
sizing, the environment allow-list, atomic extraction.

## What has been tested, and what has not (2026-09-20, AlmaLinux 10.2, x86_64, LLVM trunk 3c2eaf3920a8)

**Run for real**

- One complete build, 4 h 25 m on a machine shared with a 128-thread job: stage1
  17 m, instrumented 18 m, training 16 m (50 MB profile, 168,861 functions),
  stage2 16 m, stage3 3 h 33 m, install 36 s, **verify 23 / 23**. 8 projects, 10
  runtimes, 20 + 5 targets, 189 tools, 11 GB. Resume after a failure; the
  drop-and-continue path (it dropped libclc -- see below -- and finished).
- A second complete build with the fixes below: bootstrap 56 m, stage3 2 h 34 m to
  step 11,338 of 11,437, **cancelled by the user, resumed later** -- the same
  command continued from that step (possible only because stage2 is kept) and
  finished in 17 m: install, **verify 24 / 24, libclc for six device triples
  included**. An interrupted build really does cost the interrupted step.
- cmake 3.31.8 and ninja 1.12.1 built from source (`--provision`).
- A stage1 target built with a **private gcc 17** and run in a clean environment:
  the rpath to its libstdc++ and the automatic pairing with binutils 2.46.
- libclc for all six device triples, in a scratch build driven by exactly the
  arguments `device_runtimes()` generates; installed under
  `lib/clang/<ver>/lib/<triple>/`.
- The wizard, end to end under a pseudo-terminal, saving a profile.
- `verify` proves a stage3: the installed clang's `.comment` must name the
  **tree's own** clang version as its producer, not the system compiler's.
- **The resume-or-start-over prompt, 2026-10-08**: on the real 23.1.3 build
  directory (stage1 done, stage2-instrumented interrupted), `d` at the prompt
  and Enter at Proceed deleted the 3.3 GB and started stage1 over; the lock was
  kept. The other answers and the no-terminal paths: unit tests, and a dry run
  under a pseudo-terminal (twelve paths) that stopped at Proceed. A review of
  the change found that a start-over was planned against the directory before
  the wipe (`--run 2` + `d` would have deleted run 1 and then failed at stage3),
  that the disk check ignored what the wipe frees, that `--build-id ..` could
  point the wipe at the work directory, and that typing `restart` at the prompt
  resumed. All fixed before the feature was used for run 2 of anything.
- **A whole-file review before publication, 2026-10-08** (five readers, every
  finding re-checked by a second): LLVM 17-20 could not even configure stage1
  (the static analyzer cannot be turned off while ARCMT is on); releases 17-22
  listed runtimes among the projects, so stage3's configure died on a duplicate
  target; `--retry-dropped` on a finished build rebuilt stage3 but never
  reinstalled, then re-pointed `clang-current` (at a prefix that did not exist
  when `--prefix NEW` was given); `--prefix NEW` for the install of a stage3
  configured for the old prefix crashed; a dnf rebuild of the system compiler
  (21.1.8-1 to 21.1.8-2) made a finished run 1 impossible to resume; a
  memory-killed clang left no message and was blamed on its component; a full
  disk was blamed on whatever was compiling; `--strip` always failed `verify`;
  `/tmp` mounted `noexec` broke the compiler probe; the `-mtune` name came from
  the stage0 compiler and might be unknown to the tree's clang; a libclc that
  is device-only without the trunk cache file (23.1.x) was always dropped;
  `--from train` left stage2 and stage3 compiled with the old profile; a
  private gcc was pinned by where its symlink was found, not by its
  installation; `nohup build &` died with the terminal because ninja handles
  SIGHUP itself; Python 3.6 under `LC_ALL=C` would have crashed on the first
  non-ASCII byte of build output; and some twenty smaller ones (package names
  for the build's python on RHEL 8 and Leap 15, the CRB hint, a dnf found on
  Ubuntu, swig 3, `--from source`, `clean` after `latest` moved on, `status`
  showing a killed build as running, stage times over several attempts, the
  activation message...). All fixed; the unit tests grew from 67 to 80.
- `-vp-counters-per-site=6`: accepted by clang, and the counter section grows.
- **The bolt stage, 2026-10-07**, against stage3's clang and lld relinked with
  their relocations (the tree's source was gone; a link needs none), training on
  trunk dc488b83639c: 33 min end to end. clang's profile from 1,064 processes
  (196 MB merged, 11.3% of functions executed), lld's from 54 (135 MB, 12.4%);
  the raw directory was empty when the training ended. The laid-out clang is
  18 MB smaller than its input (BOLT drops the relocation table), splitting set
  15 MB of cold code apart, and it compiles `Verifier.cpp` in 7.3 s against
  8.0 s before: **1.09x** on top of PGO + ThinLTO. The configure hook, in a
  separate configure of clang + lld: `--emit-relocs` on exactly two of 121
  link lines.

**What those runs found, all fixed**

| found by | what was wrong |
|---|---|
| run 1, 18 min in | the configure verifier looked for the compiler in `build.ninja`; cmake's Ninja generator keeps it in `CMakeFiles/rules.ninja`. It stopped a good build |
| a unit test | the reason recorded for a CMake error was the tail of its header line, `(message):` |
| run 1, stage3 | **libclc was in the host's runtime list.** It is a device library: "Unknown libclc target architecture: x86_64". Dropped, correctly -- but it was never broken, it was misconfigured |
| looking at why a re-run would be slow | **stage2 was deleted after verify**, orphaning the 24 GB stage3 tree: every object depends on headers inside stage2 |
| counting warnings after a "fix" | `LLVM_VP_COUNTERS_PER_SITE` is honoured by LLVM only under `LLVM_LINK_LLVM_DYLIB` and ignored silently otherwise: 12,806 "running out of static counters" warnings with it set. The flag now goes in directly, and the verifier looks for it |
| reading an install log that "succeeded" | **`ninja install` exits 0 when a runtime's install script dies.** Trunk 3c2eaf39's orc-rt lists a header as `CoreTyspe.h`; LLVM runs the runtimes' install through `execute_process()` and ignores the result. Both builds said "everything was built" with orc-rt not installed. The install log is now read; such a component is dropped like one that failed to build, core ones stop. (The two prefixes built before this were corrected by hand, and say so in `BUILD-INFO.txt`) |
| choosing gcc 17 as stage0 | it was built against a newer assembler than the system's: `unknown pseudo-op: '.base64'` a minute into stage1, after a hello-world probe had passed. The probe now contains such data; binutils are paired automatically |
| a reviewer reading the bolt stage | **lld never wrote a BOLT profile**: it leaves through `_Exit()` (no cleanup on exit, by design), which skips the hook BOLT's runtime writes from. lld is instrumented with `-instrumentation-sleep-time=1 -instrumentation-no-counters-clear`: a watcher process writes the counters every second and once more when lld is gone. Also from that review: an install with `--strip` loses `.note.bolt_info`, so the mark looked for is `.bolt.org.text` too; RTTI joined the build fingerprint |

**Not tested**

- The instrumented stage WITH the counter flag (added after that stage had run),
  and the install-time drop-and-redo loop (the detection is tested against the
  real log; the redo loop itself is untested).
- A Red Hat **toolset** gcc as stage0 beyond detection (`--gcc-toolchain` for
  later stages): written from documentation. Here the system clang already
  defaults to gcc-toolset-15, so it could not be isolated cheaply.
- Release tarballs (`--version`), `--ref`, `--install-deps` (sudo needs a
  password), `--lto full`, `--no-pgo`, `--link dylib`, `--check`, `--activate`.
- Any machine but this one: apt / zypper / pacman package names, aarch64,
  Python 3.6 at run time (syntax is checked with `ast`; the runtime is not).

## Not done

MLIR's Python bindings;
cross-compiled runtimes for other targets (needs sysroots); `--check` configures
the test suites but running them is left to you (`ninja -C <build>/stage3
check-all`) -- and on a finished build `--check` changes nothing by itself:
`--from stage3 --check` reconfigures with them; PGO for lld (the instrumented
lld exits through `_Exit()` and writes no profile; lld gets its layout from
BOLT instead).
