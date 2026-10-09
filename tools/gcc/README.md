# enterprise_gcc_tool.py: GNU binutils and GCC from source, as one pair

This tool is steps 3 and 4 of the satellite toolkit (PLAN.md section 1):

| step | command | done when |
|---|---|---|
| 3 `binutils` | `enterprise_gcc_tool.py build --component binutils` | `<opt>/binutils-current/bin/ld` runs |
| 4 `gcc` | `enterprise_gcc_tool.py build` (`--component gcc` is the default) | `<opt>/gcc-current/bin/gcc` runs |

It replaces `legacy/build_gcc_universal.sh` and the binutils line of
`legacy/binutils-gcc-notes-reboot.txt`. Every knob of the .sh is a flag here, and the
mapping table below covers all of them. The two components form ONE pair: by default, gcc
is configured with `--with-as=`/`--with-ld=` pointing at the realpath of the binutils that
`<opt>/binutils-current` points to.

The tool is one file and uses only the standard library. Its syntax is held to Python 3.6
(the tests check this with `ast.parse(..., feature_version=(3, 6))`) and it is pure ASCII.
It starts as `#!/usr/bin/python3 -I`, and when started by PyPy it re-execs itself under
`/usr/bin/python3`. It never runs a build as root: only `--install-deps` calls sudo.

## Quick start

    ./enterprise_gcc_tool.py doctor                     # what is missing, and the one command that installs it
    ./enterprise_gcc_tool.py build --component binutils # step 3: ~/opt/binutils-<ver>, binutils-current
    ./enterprise_gcc_tool.py build                      # step 4: the newest GCC release, paired with it
    ./enterprise_gcc_tool.py                            # on a terminal: the wizard

Run where? Every invocation first goes through `where_to_run()` (satellite_remote, embedded
between the `# >>> satellite_remote >>>` markers by `sync_remote.py`). `--local` runs the tool
here, and `--remote[=USER@HOST]` runs it on another machine in a session that survives a
dropped ssh connection. `--attach[=SESSION]` returns to such a session and `--sessions` lists
them. On a terminal with neither flag, the tool asks. The session short name is `gcc`
(`TOOL = "enterprise_gcc_tool"`).

## Commands

| command | what |
|---|---|
| `build` | Fetch, configure, make, install, verify and link `-current`. Resumable: a failure resumes at the stage that failed. |
| `plan` (= `build --dry-run`) | Resolves everything and prints the .sh's summary block, with the exact configure and make lines. Builds nothing. Exits 1 when the preflight finds a blocking problem. |
| `doctor [--json]` | Lists what this selection needs and lacks, with the one install command. `--install-deps` runs that command. |
| `status [--json]` | Shows what is installed where: versions, whether each one RUNS, which `-current` points where, whether this tool built it, the gcc/binutils pairing (and whether that binutils still exists), leftovers, and the builds with their status and stages. `--json` prints the toolkit's status contract (below). |
| `versions [--count N]` | Shows what upstream has: GCC releases, the newest snapshot of each series, trunk's BASE-VER, and binutils releases with or without a gold tarball. |
| `detect [--json]` | Shows this machine: OS, CPU and its x86-64 level, RAM, package manager, compilers, binutils sets, build tools and headers. |
| `toolchains [--no-check]` | Lists every gcc and clang found here (a clang is flagged as not recommended for GCC), test-compiles with each, lists every binutils set, and shows which would be used and paired. |
| `verify PREFIX` | Runs the post-install checks against a prefix, telling gcc from binutils by the files in it. |
| `profiles` | Lists the saved profiles. |
| `wizard` | Asks the questions (see below). |
| `clean [--build-id NAME] [--all] [--sources]` | Removes the build tree of one build directory. `--all` also removes its record and logs; `--sources` also removes the source tree and tarball it used. It only touches directories under `--workdir` whose record says they belong to this tool, and never touches a prefix or a `-current` link. |

Every subcommand takes `--opt-root DIR`, `--workdir DIR`, `--profile NAME` and `--yes`.
Every command that resolves options (`build`, `plan`, `doctor`, `toolchains`, `clean`,
`wizard`, `verify`) takes all of the build options. So, for example,
`doctor --json --languages c,d --multilib --check` reports what THAT build needs.

Exit codes: `0` done, `1` failed, `2` usage, `130` interrupted (^C, SIGTERM, SIGHUP).

With `--json` (`doctor`, `status`, `detect`), stdout is the JSON and nothing else. Every
message goes to stderr, an `ERROR:` included, so a failed `doctor --json` leaves stdout empty
and says why on stderr.

## Options

What to build:

| option | default | meaning |
|---|---|---|
| `--component gcc\|binutils` | gcc | which of the two |
| `--track release\|snapshot\|git` | release | WHICH GCC: a release, a weekly snapshot, or git as it stands now |
| `--series N\|auto` | auto | the snapshot series; auto = the highest series listed (trunk) |
| `--gcc-version auto\|X.Y.Z\|N-YYYYMMDD` (alias `--version`) | auto | X.Y.Z for release, N-YYYYMMDD for snapshot |
| `--git-ref REF` | master | a branch, tag or commit SHA for `--track git` |
| `--gcc-git URL` (alias `--git-url`) | https://gcc.gnu.org/git/gcc.git | the repository |
| `--binutils-version auto\|X.Y[.Z]` | auto | auto = the newest release that has gold, when gold is wanted |
| `--source DIR` | | a tree you already have, configured out of tree (not modified, except by download_prerequisites when it lacks the in-tree libraries) |
| `--prefix DIR` | `<opt-root>/<component>-<version>` | must not exist |

How (gcc):

| option | default | meaning |
|---|---|---|
| `--mode profiled-lto\|lto\|plain\|quick` | profiled-lto | see the mode table |
| `--languages LIST` | c,c++,lto | `--enable-languages`, checked against the tree's `gcc/*/config-lang.in`; never exported to make (trap 6); `jit` adds `--enable-host-shared` |
| `--multilib` / `--no-multilib` | no | adds 32-bit libraries, which need the 32-bit libc headers (doctor names the packages) |
| `--boot-arch FLAGS\|none` | `-march=native -mtune=native` | flags the compiler ITSELF is built with (BOOT_CFLAGS/BOOT_CXXFLAGS = `-O2 -g FLAGS`) |
| `--with-arch LEVEL\|CPU` | unset (portable) | the DEFAULT -march of the installed gcc; prefer an ISA level (x86-64-v3) to a CPU name |
| `--with-tune CPU\|auto\|none` | auto | auto = native on x86 and aarch64, none elsewhere |
| `--binutils auto\|system\|DIR` | auto | auto = the realpath of `<opt>/binutils-current` when its as and ld run, else the system's; adds `--with-as=DIR/bin/as --with-ld=DIR/bin/ld` and puts DIR/bin first on PATH for the build |
| `--prereqs in-tree\|system` | in-tree | in-tree = contrib/download_prerequisites; system = the distribution's gmp/mpfr/mpc |

How (binutils):

| option | default | meaning |
|---|---|---|
| `--gold` / `--no-gold` | gold | `--enable-gold`. From 2.44 on, gold ships only in `binutils-with-gold-<ver>`, and that tarball is the one used. Never `--enable-gold=default`, which would make gold the prefix's `ld`. |
| `--binutils-tune native\|portable\|LEVEL\|'-flags'` (alias `--tune`) | native | CFLAGS/CXXFLAGS = `-g -O3 -march=native` (reboot.txt:5-7); portable = `-g -O3` |

How (both):

| option | default | meaning |
|---|---|---|
| `--host-cc PATH` / `--host-cxx PATH` | /usr/bin/gcc, else gcc on the system PATH | the compiler that bootstraps; CC from the environment is ignored, with a warning |
| `--configure-extra 'ARGS'` | | more configure arguments (shell-split) |
| `--jobs N` / `-j N` | CPUs | make -j |
| `--no-strip` | | gcc: `make install` instead of `install-strip` |
| `--check` | | `make -k check` after the build: totals shown, never fatal (needs dejagnu and expect) |
| `--keep-build` | | keeps the build tree after a verified install |
| `--link-current` / `--no-link-current` | link (asked on a terminal) | `<opt>/<component>-current` -> the prefix, once verified |
| `--opt-root DIR` | ~/opt | where prefixes and the `-current` links live |
| `--workdir DIR` | ~/src/enterprise_gcc_tool | sources, build trees, records and logs |
| `--resume` / `--restart` | | answers the "earlier run" question without asking |
| `--install-deps` | | `sudo <pm> install` what doctor lists (apt-get update first; CRB / PowerTools / RHEL's CodeReady Builder enabled on EL when needed): the required base packages in one command, then each option's packages (multilib, gdc, gnat, dejagnu/expect, git, the system GMP/MPFR/MPC) and the wanted ones each in a command of its own, best effort, so one name the repositories lack cannot cost flex and texinfo |
| `--ignore-disk` | | builds even where less disk is free than the mode wants |
| `--dry-run` | | the plan |
| `--profile NAME` / `--save-profile NAME` | | loads / saves the choices |
| `--yes` / `-y` | | asks nothing: Proceed is yes, an earlier compatible run is resumed (never deleted) |
| `--verbose` / `-v` | | shows the whole build output on the console |

An option value that starts with `-` needs `=`, for example
`--boot-arch='-march=x86-64-v3'`, `--binutils-tune='-mcpu=neoverse-v2'` or
`--configure-extra='--with-gmp=/x'`.

### Modes (build_gcc_universal.sh:119-132, 442-448, 337-338)

| `--mode` | make target | BUILD_CONFIG | configure | disk wanted | time (the .sh's estimate) |
|---|---|---|---|---|---|
| profiled-lto | profiledbootstrap | bootstrap-lto | | 30 GB | 3-5 h |
| lto | bootstrap | bootstrap-lto | | 30 GB | 2-4 h |
| plain | bootstrap | | | 15 GB | 1.5-3 h |
| quick | (all) | | `--disable-bootstrap` | 10 GB | 30-60 min |

`--track git` adds 5 GB. Binutils wants about 3 GB; that figure is an estimate, not a
measurement. The disk check uses the nearest existing parent of the build directory and
refuses to start unless `--ignore-disk` is given. RAM: under 4 GB the .sh's warning is printed
("an LTO bootstrap links large objects and may OOM"). In the LTO modes, under 1.5 GB available
per job, the warning suggests a lower `--jobs`.

## What a build does

```
gcc:      source -> prereqs -> configure -> build -> [check] -> install -> verify -> activate
binutils: source ->            configure -> build -> [check] -> install -> verify -> activate
```

- **source**: The release and snapshot listings are fetched with `Accept-Encoding: gzip` and
  decoded when the header says gzip or the body starts with the gzip magic (the .sh's
  `--compressed` lesson). A mirror that does not answer, or answers with nothing that parses,
  is skipped. The newest release is confirmed with a HEAD of its tarball. The download goes to
  `.part` and is then renamed, with each mirror tried in turn ("that mirror failed, trying the
  next one"). A GCC tarball is checked against the mirror's `sha512.sum` when there is one,
  then against xz (the `lzma` module; `xz -t` where Python lacks the module). A corrupt
  tarball, new or cached, is deleted and fetched again once. It is extracted into a temporary
  directory and renamed. For `--track git` the tool runs `git clone --depth 1 --branch REF`
  into `<src>.cloning` and renames it; a SHA ref goes through `git init` + `fetch` +
  `checkout FETCH_HEAD`. An existing clone is fetched and checked out, or the tool warns and
  builds what is there. The id becomes `<BASE-VER>-git-<sha>` and names the prefix, and the
  prefix-collision check runs only then. A resumed git build is NOT re-fetched; `--restart`
  fetches the newest commit.
- **prereqs**: The tool reads gmp, mpfr, mpc, isl and gettext from THIS tree's
  `contrib/download_prerequisites`. It fetches each missing archive from gcc.gnu.org,
  sourceware.org and mirrors.kernel.org in turn, and checks it against
  `contrib/prerequisites.sha512`. A mismatch, such as an HTML error page saved under the
  archive's name, is deleted. Then the script itself runs and finds the archives present. The
  stage is done when every name the script lists is a link to a directory. On failure the
  message names the real alternatives: `--prereqs system` with the packages, or
  `--configure-extra '--with-gmp=DIR ...'`.
  The script unpacks an archive only when its directory is not there yet, so an unpack cut
  short (^C, SIGTERM, a crash) would be taken as whole by the next run. While the script runs,
  the tree holds a mark, `.enterprise_gcc_tool-prereqs-unpacking`; a run that finds it removes
  the unpacked directories and their links first (the checked archives stay) and unpacks
  again. A tree without the mark whose links all resolve (the .sh's, say) is used as it is.
- **configure**: Runs out of tree in `<workdir>/build/<component>-<id>/obj`, which must not be
  inside the source tree. It starts clean, as the .sh did. The line is the .sh's line
  (gcc) or reboot.txt's line (binutils); `plan` prints it. `CC=`/`CXX=` are configure
  arguments.
- **build**: `make -jN` (plus BUILD_CONFIG, BOOT_CFLAGS/BOOT_CXXFLAGS and the mode's target)
  runs in its own process session. Its output goes to `logs/build.log`. The console gets a
  status line (with the current stage) or, without a terminal, a heartbeat every five
  minutes, plus the first error lines. On failure the tool shows:
  - the FIRST real errors;
  - the sub-configure that failed (the newest `config.log` ending in `configure: exit 1`) and
    its error lines;
  - out of memory: re-run with fewer `--jobs`, and the build resumes;
  - disk full;
  - the stage, read from `stage_current` / `stage_last`, else "stage1" in the last 300 lines.
    A stage-1 failure says "changing --mode will NOT help" and names the host compiler and the
    binutils on PATH. A later failure says `--mode plain --restart`.

  "Exited 0 but `gcc/xgcc` (`ld/ld-new`) is not there" is a failure.
- **install**: Runs `make install-strip`, else `make install`, serially, with `DESTDIR=` a
  staging directory beside the prefix. The tool writes `BUILD-INFO.txt` and
  `.enterprise_gcc_tool.json` (component, id, track, mode, languages, flags, host, the paired
  binutils, the configure line, `verify`), then does one rename. If the staging directory is
  on another filesystem, it copies and then renames. An existing prefix is refused before
  anything is built. Binutils uses plain `make install` so that it keeps the `-g` its CFLAGS
  ask for.
- **verify**: gcc checks:
  - the .sh's C++20 program (sum=55), built with the rpath and run with LD_LIBRARY_PATH;
  - the same binary with the rpath alone, and its libstdc++ resolving inside the prefix;
  - C hello;
  - `-O2 -flto`;
  - the `.base64` data probe in a clean environment;
  - `-print-prog-name=as/ld` equal to the paired binutils;
  - `-fuse-ld=gold`;
  - the baked-in default `-march` (`gcc -Q --help=target`) when `--with-arch` is set;
  - `cc1plus`'s `.comment` naming its own version (bootstrap modes only);
  - a hello for each extra language (fortran, d, go, objc).

  binutils checks:
  - as/ld/ld.bfd/ar/nm/objdump/readelf/strip say GNU;
  - ld.gold says GNU gold;
  - `ld --help` lists `-plugin`;
  - the system's gcc with `-B <prefix>/bin/` finds this as and ld, links C, `-fuse-ld=gold`
    and `-flto`, and the programs run.

  The checks run in a scratch directory beside the build, not in /tmp, which may be noexec.
  If any check fails, the prefix stays installed but is NOT linked as `-current`, and the
  marker records `verify: failed`.
- **activate**: Writes a symlink `<opt>/<component>-current.new` and renames it over the old
  one (`os.replace`). If `-current` is a real directory, it is left alone and the tool says
  so. Then the tool prints the PATH / LD_LIBRARY_PATH / `-Wl,-rpath` advice (the .sh's, with
  the GLIBCXX explanation) through the `-current` path. After a verified install the build
  tree is removed (`--keep-build` keeps it); the record and the logs stay.

### Resume, lock, record

Each build directory `<workdir>/build/<component>-<id>/` holds `state.json`, which records
the stages, times, the first error line, the fingerprint and the configure line. It also
holds `logs/<step>.log` (the previous run's copy is kept as `.prev`), `obj/`, and `.lock`
(flock, with the holder's pid). Only one instance can work in a build directory at a time.
The source download takes a lock on `<workdir>/src`, which is waited for.

The fingerprint holds everything that reaches configure or make: component, id, mode,
languages, multilib, boot flags, with-arch, with-tune, the host compiler's kind and version,
the paired binutils realpath, configure-extra, prereqs and the prefix. A run with the same
fingerprint resumes: the configure is kept and make is run again (GCC's bootstrap continues
where it stopped). A run with a different fingerprint asks whether to delete or quit; Enter
deletes. With nobody to ask (no terminal, or `--yes`), the tool resumes a compatible run and
quits on an incompatible one; it never deletes on a guess. `--restart` wipes the build
directory: everything except the lock is removed, and symlinks are removed without being
followed. `status` shows a record that says "running" while nobody holds the lock as
"killed".

Done is asked of the machine, not only of the record. When the record says the install is
done but `<prefix>/bin/gcc` (`bin/ld`) no longer runs, because the prefix was removed or broken
since, install, verify and activate are not done. The build tree that was removed after the
verified install brings back build and configure with them, so `build` builds it again rather
than say "nothing to do" beside a dangling `-current`. A prefix that is there but does not run
is refused, as any existing prefix is: remove it, or `--prefix NEW`.

### Environment

Every child gets an allow-list:
- HOME, USER, LOGNAME, TERM, TMPDIR, LANG;
- the proxy variables;
- SSL_CERT_FILE/DIR, GIT_SSL_CAINFO, CURL_CA_BUNDLE, WGETRC;
- `PATH=[paired binutils/bin] + /usr/bin:/usr/sbin:/bin:/sbin` (no /usr/local/bin, HANDOFF
  trap 5);
- `LC_ALL=C`.

So LANGUAGES, CC, CXX, every *FLAGS, AS/LD/AR/NM/RANLIB, *_FOR_TARGET, LD_LIBRARY_PATH,
LD_RUN_PATH, LIBRARY_PATH, CPATH, MAKEFLAGS/MFLAGS, CONFIG_SITE, DESTDIR and
GCC_EXEC_PREFIX never reach a build. The preflight names the ones that are set in your
shell.

### Doctor contract

`doctor --json` prints exactly:

    {"tool": "enterprise_gcc_tool", "ok": bool, "package_manager": "dnf|apt-get|zypper|pacman"|null,
     "missing": [{"what": ..., "package": ..., "level": "required|wanted|optional"}],
     "enable_repos": ["crb" | "powertools" | "codeready-builder-for-rhel-N-ARCH-rpms"] | [],
     "install_command": "sudo dnf install -y ..."}

It exits 0 when ok and 1 otherwise. Presence is probed: the tool runs, the header exists, the
host g++ compiles, links and runs the probe program in the build's environment. Nothing is
asked of rpm or dpkg. `ok` is false when anything required is missing, or when the tool runs
as root (an entry with an empty package says so). `install_command` covers the required and
wanted packages. `enable_repos` names the repository that holds texinfo, dejagnu,
libmpc-devel and isl-devel on Enterprise Linux (not Fedora), when one of them is wanted:
`["crb"]` on AlmaLinux, Rocky and CentOS 9/10, `["powertools"]` on EL 8, and
`["codeready-builder-for-rhel-<major>-<arch>-rpms"]` on RHEL itself. `--install-deps` enables
it with `dnf config-manager --set-enabled`, on RHEL with `subscription-manager repos
--enable`.

| need | when | dnf | apt-get | zypper | pacman |
|---|---|---|---|---|---|
| gcc, g++ | always | gcc gcc-c++ | gcc g++ | gcc gcc-c++ | gcc |
| make, binutils, bison, flex, m4, perl, tar | always | same names | same | same | same |
| makeinfo | always | texinfo | texinfo | texinfo | texinfo |
| xz | always | xz | xz-utils | xz | xz |
| zlib.h | always | zlib-devel | zlib1g-dev | zlib-devel | zlib |
| stdio.h | always | glibc-devel | libc6-dev | glibc-devel | glibc |
| zstd.h (wanted) | always | libzstd-devel | libzstd-dev | libzstd-devel | zstd |
| gawk (required only with no awk), bzip2, gzip, cmp/diff, find, file; wget\|curl, patch, which (wanted) | gcc | gawk bzip2 gzip diffutils findutils file wget patch which | ... `which` = debianutils | same | same |
| git | gcc --track git | git | git | git | git |
| gnu/stubs-32.h | --multilib | glibc-devel.i686 libstdc++-devel.i686 | gcc-multilib g++-multilib | glibc-devel-32bit gcc-c++-32bit | lib32-glibc lib32-gcc-libs |
| gdc / gnatmake | --languages d / ada | gcc-gdc / gcc-gnat | gdc / gnat | gcc-d / gcc-ada | gcc-d / gcc-ada |
| runtest, expect | --check | dejagnu expect | same | same | same |
| gmp.h mpfr.h mpc.h (+ isl optional) | --prereqs system | gmp-devel mpfr-devel libmpc-devel (isl-devel) | libgmp-dev libmpfr-dev libmpc-dev (libisl-dev) | gmp-devel mpfr-devel mpc-devel (isl-devel) | gmp mpfr libmpc (libisl) |

GMP/MPFR/MPC/ISL are NOT required by default, as in the .sh (175-178): contrib/download_prerequisites
fetches the versions the release was tested against.

### Status contract

`status --json` prints the toolkit's status shape (PLAN.md section 3) at the top level:

    {"tool": "enterprise_gcc_tool", "opt_root": DIR,
     "current": {"gcc-current": TARGET|null, "binutils-current": TARGET|null},
     "installed": [{"name": "gcc"|"binutils", "version", "prefix", "runs": bool,
                    "built_by_tool": bool, "compiler": STR|null, "for": null, ...}],
     "builds": [{"id", "status", "stage", "error", ...}],
     "workdir": DIR, "components": {...}}

- `current`: the realpath of each `-current` link (a dangling link still names its target),
  `null` when there is none.
- `installed`: every `<opt>/gcc-*` and `<opt>/binutils-*` with a `bin/gcc` (`bin/ld`).
  `runs` is whether that binary runs. `built_by_tool` is whether the prefix's
  `.enterprise_gcc_tool.json` says this tool installed it; the main file's third state,
  "present but not built by the toolkit", is `runs` and not `built_by_tool`. `compiler` is
  the host compiler the marker records (its banner), `for` is always null. Extra keys:
  `verify` (ok / failed / unverified), and for gcc `mode`, `languages`, `paired_binutils`,
  `paired_binutils_present`; for binutils `gold`.
- `builds`: one per `<workdir>/build/<component>-<id>/`. `id` is that directory's name, which
  `clean --build-id` takes. `status` is `done`, `running`, `killed` (recorded running while
  nobody holds the lock), `failed`, `interrupted`, `incomplete` or `new`. `stage` is the one
  running, failed or interrupted, else the first one not done (the last one when done), and
  `error` the first line recorded for it. Extra keys: `dir`, `component`, `version`,
  `stages`, `prefix`, `running`, `by_this_tool`.
- `workdir` and `components` (each `-current` link in detail, and the leftovers: directories
  without a main binary, and staging directories) are extra keys the tool's own text output
  reads.

## Profiles and the wizard

Profiles live in `~/.config/enterprise_gcc_tool/profiles/<name>.ini` (`$XDG_CONFIG_HOME`
respected), in a `[build]` section whose first line is `# enterprise_gcc_tool profile`. They
store:
- the choices: component, track, series, git_ref, git_url, gcc_version, binutils_version,
  source, prefix, workdir, opt_root, mode, languages, boot_arch, with_arch, with_tune,
  host_cc, host_cxx, binutils, binutils_tune, configure_extra, prereqs, jobs;
- the switches: multilib, gold, link_current, strip, check, keep_build.

They never store resume, restart, yes or install-deps. Precedence is defaults, then the
profile, then the command line.

The wizard runs when there is no command on a terminal. It first offers the saved profiles
(use one as it is, or edit it). Then it asks:
- the component;
- which GCC (latest release / a version / snapshot + series / git ref), or the binutils
  version, gold and tuning;
- the mode, with time and disk for each;
- the languages and multilib;
- the boot flags;
- the default -march (portable / x86-64-v2/v3/v4 / native, with the SIGILL warning);
- -mtune;
- the binutils pairing;
- the host compiler (numbered, auto, or a path);
- the prefix, link-current, and whether to save the answers as a profile.

Every question is also a flag. Without a terminal, the wizard exits 2.

## Every knob of build_gcc_universal.sh, and where it went

| .sh (line) | here | notes |
|---|---|---|
| `TRACK` (90) | `--track release\|snapshot\|git` | same values, same default |
| `SERIES=17` (91) | `--series N\|auto` | default `auto` = the highest series listed (trunk); a hard-coded 17 goes stale when 17 ships |
| `GIT_REF=master` (92) | `--git-ref` | same default; also accepts a commit SHA (the .sh's `clone --branch` could not) |
| `GCC_GIT` (93) | `--gcc-git` / `--git-url` | same default |
| `GCC_VERSION=auto` (97) | `--gcc-version` / `--version` | same values; validated per track (X.Y.Z / N-YYYYMMDD) |
| `SRC_ROOT=~/src/gcc-build` (98) | `--workdir` (sources in `<workdir>/src`) | default `~/src/enterprise_gcc_tool`; `--source DIR` reuses an unpacked tree such as `~/src/gcc-build/gcc-16.2.0` |
| `PREFIX` (99, 378) | `--prefix` | default `<opt-root>/gcc-<id>` (the .sh hard-coded `$HOME/opt`) |
| `JOBS=$(nproc)` (100) | `--jobs` / `-j` | default: the CPUs this process may use |
| `INSTALL_DEPS=1` (101) | `--install-deps` | opt-in (the toolkit's rule: the main file installs everything once) |
| `LINK_CURRENT=1` (102) | `--link-current` / `--no-link-current` | on by default; asked once on a terminal; only after verify passes |
| `LANGUAGES=c,c++,lto` (103) + `export -n` (116) | `--languages` | same default; never in any child's environment (allow-list); checked against the tree |
| `MULTILIB=0` (117) + its packages (192-197) | `--multilib` / `--no-multilib` | doctor names glibc-devel.i686 libstdc++-devel.i686 / gcc-multilib g++-multilib; `--install-deps` installs them after the base packages, in a command of their own that may fail with the .sh's warning ("32-bit headers unavailable; multilib may fail") |
| `MODE=profiled-lto` (132) | `--mode` | the same four modes, targets, BUILD_CONFIG and disk figures |
| `BOOT_ARCH` (141) | `--boot-arch` | same default; `none` = empty |
| `WITH_ARCH` (157) | `--with-arch` | same default (unset); the wizard repeats the SIGILL / ISA-level advice |
| `WITH_TUNE=native` (158) | `--with-tune` | `auto` = native on x86/aarch64, nothing elsewhere; `none` leaves it out |
| `HOST_CC` / `HOST_CXX` (87-88, 262-274) | `--host-cc` / `--host-cxx` | /usr/bin/gcc first; the C++ partner from the same directory (gcc-14 -> g++-14) |
| env `CC` ignored (276-281) | the same warning | and every other trap variable is named |
| `CONFIGURE_EXTRA` (435) | `--configure-extra` | shell-split, appended last |
| ASCII self-check (43-50) | the test `test_pure_ascii` | Python refuses a stray U+00A0 itself; the package's SHA256SUMS covers the rest |
| refuses root (164) | `build`, `wizard` and `clean` refuse; `doctor --json` reports `ok: false` | |
| DNF_PKGS / APT_PKGS (169-174) | `doctor` tables (+ m4, + zypper, pacman) | |
| GNU_MIRRORS / SNAP_MIRRORS (202-207) | `GCC_MIRRORS`, `SNAP_MIRRORS` | same order |
| `curl --compressed` (209-211) | gzip asked for and undone | also when only the magic says so |
| `latest_gcc_version` / `latest_snapshot` (212-233) | `latest_gcc_release` / `latest_snapshot` | + a HEAD to confirm the tarball |
| `fetch_from` (235-247) | `download_from` | `.part`, then rename; fails on HTTP errors. curl's `--retry 3 --retry-delay 5` kept: each mirror is tried again up to 3 times, 5 s apart, after a transient failure (a timeout, a reset or dropped connection, a short body, HTTP 408/429/500/502/503/504); a 404 or a 403, a name that does not resolve and a refused connection go to the next mirror at once |
| preflight tools (254-257) | `doctor` | |
| development-compiler banner (327-333) | the same banner | says "has not shipped" only when that series has no release |
| disk need (336-340) | the disk check | refuses (the .sh only warned); `--ignore-disk` |
| RAM warning (342-344) | the same warning | + the per-job note in the LTO modes |
| git clone / fetch / id (348-363) | the source stage | `<BASE-VER>-git-<sha>` |
| `xz -t` (368) | `xz_ok` | + the mirror's sha512.sum |
| prefix collision (378-382) | the same refusal | before anything is built; after the clone for git |
| summary block (384-396) | `plan` and the plan of `build` | + the configure and make lines |
| `download_prerequisites` (398-407) | the prereqs stage | + mirror fallback, sha512, gettext; correct advice |
| out-of-tree build (379, 412) | `<workdir>/build/<comp>-<id>/obj` | refused inside the source tree |
| CONFIGURE_ARGS (414-435) | `gcc_configure_args` | identical, in order; + `--with-as/--with-ld`, + `--enable-host-shared` for jit |
| MODE table, BOOT_CFLAGS (442-457) | `gcc_make_args` | identical |
| error grep + stage-1 classification (459-475) | `failure_report` | the FIRST errors, the failed config.log, OOM, disk full, stage_current |
| `install-strip \|\| install` (480) | `do_install` | into DESTDIR staging, one rename, serial |
| C++20 check with rpath (485-513) | `verify_gcc` | identical program, flags and LD_LIBRARY_PATH; + 9 more checks |
| default -march check (515-519) | `verify_gcc` | |
| gcc-current via `ln -sfn` (523-527) | `link_current` | atomic, refuses a real directory |
| PATH / LD_LIBRARY_PATH / rpath advice (529-547) | `advice` | through the `-current` link |

From `binutils-gcc-notes-reboot.txt`:

| line | here |
|---|---|
| `../binutils-with-gold-2.46/configure` | the with-gold tarball for >= 2.44 (the plain one before 2.44 still has gold); `--binutils-version` |
| `--prefix="$HOME/opt/binutils-246"` | `<opt-root>/binutils-<ver>` (dotted, as PLAN.md writes it) |
| `--enable-plugins --enable-lto --enable-gold` | exactly that (`--no-gold` drops the last) |
| `CFLAGS/CXXFLAGS="-g -O3 -march=native"` | `--binutils-tune native` (the default), as configure arguments |
| the `dnf install ...` line (epel-release, dejagnu, gcc-gfortran, gcc-go, gcc-gdc, mingw64-binutils, binutils-devel ...) | doctor asks only for what the selected build needs: dejagnu/expect with `--check`, gdc with `--languages d`. The rest (mingw, fortran/go hosts) is not needed to build binutils or GCC. |

Extra configure flags such as `--enable-ld=default` or `--enable-64-bit-bfd` were NOT added.
With `--enable-gold`, ld.bfd is already the default `ld`, and BFD64 is automatic on 64-bit
hosts. On x86_64 they would change nothing, and on a 32-bit host the second would change what
is built. Pass them with `--configure-extra` if wanted.

## Deviations from the .sh, and why

- Dependencies are installed only with `--install-deps` (the .sh's default was on). This is
  the toolkit contract: the main file installs everything once, up front, from `doctor --json`.
- The disk check refuses instead of warning. It is a contract of the toolkit, and
  `--ignore-disk` goes ahead anyway.
- The default work directory is `~/src/enterprise_gcc_tool`, not `~/src/gcc-build`, so that
  the .sh's trees are never touched. Only `--source` reuses one, for example
  `--source ~/src/gcc-build/gcc-16.2.0` (with its in-tree GMP/MPFR/MPC/ISL). `--workdir
  ~/src/gcc-build` does NOT: the tool keeps its sources in `<workdir>/src/gcc-<id>`, the .sh
  kept them in `$SRC_ROOT/gcc-<id>`, so it would download a fresh copy into
  `~/src/gcc-build/src/gcc-16.2.0`.
- `SERIES` defaults to auto (trunk), not a hard-coded 17.
- `--with-tune` is omitted on architectures where `native` is not accepted, instead of
  passing it everywhere.
- The configure no longer runs on every invocation. A resumed build keeps its configure and
  re-runs make. The .sh always ran `rm -rf $BUILD`; `--restart` does that now.
- The .sh's error advice `CONFIGURE_EXTRA='--with-system-gmp'` named a configure option that
  does not exist. The advice is now `--prereqs system` with the real packages, or
  `--with-gmp=DIR`.
- The gcc is paired with the built binutils (`--with-as/--with-ld`), per PLAN.md.
  `--binutils system` builds exactly what the .sh built.
- `-current` is linked only after verification passes. The .sh linked it after its two
  checks.
- Binutils is installed with `make install`, not `install-strip`, to keep the `-g` in the
  CFLAGS of reboot.txt.
- `--version` is kept as an alias of `--gcc-version`. However, satellite_remote's
  `_sr_wants_question` skips the "run where?" question whenever `--version` appears among the
  arguments, so with that spelling the tool runs locally without asking. Use `--gcc-version`,
  or give `--local` / `--remote`.

## Layout

```
<workdir>/src/gcc-16.2.0.tar.xz, gcc-16.2.0/        release (snapshot: gcc-17-20260823...)
<workdir>/src/gcc-git-<ref>/                        --track git
<workdir>/src/binutils-with-gold-2.46{.tar.xz,/}
<workdir>/build/gcc-16.2.0/{state.json,.lock,logs/,obj/}
<opt-root>/gcc-16.2.0/{bin,lib64,...,BUILD-INFO.txt,.enterprise_gcc_tool.json}
<opt-root>/gcc-current -> gcc-16.2.0
<opt-root>/binutils-2.46/, binutils-current -> binutils-2.46
```

## Tests

    /usr/bin/python3 -I tools/gcc/tests/test_tool.py        # 112 tests, ~6 s

These are stdlib unittest tests with fakes. They use no network (fake mirrors, `file://`
URLs, one HTTP server on 127.0.0.1) and compile no GCC: fake source trees whose Makefiles the real `make` runs. They pass as
root (this container) and as an unprivileged user; the tests that drive `build` set the uid
to 1000.

What is covered:
- the 3.6 syntax and ASCII;
- the satellite_remote block equals `sync_remote.block()`, and `where_to_run` is the first
  statement of `main()`;
- the listings (release, snapshot, binutils with and without gold; gzip with and without the
  header), mirror fallback and `.part`, sha512 and xz checks, one refetch of a corrupt
  tarball;
- the retry: a reset retried on the same mirror, a 404 not; what counts as transient; the
  real `download()` against a 127.0.0.1 server answering 503, a short body and 404;
- track/version validation, git ids, local tree ids;
- the mode table, the exact configure and make lines (`--with-as/--with-ld` realpaths,
  `CC=`/`CXX=` as arguments, `--disable-multilib`, `--disable-bootstrap`, `--with-arch` only
  when set, jit);
- LANGUAGES, CC and MAKEFLAGS absent from a real `make`'s environment;
- host picking (gcc-14 -> g++-14) and the CC warning;
- stage-1 versus later classification, the first errors, and the failed config.log;
- prerequisites (five archives, a bad checksum deleted, the next mirror tried; an unpack cut
  short is unpacked again, a failed one leaves the mark, an unmarked whole tree is used as it
  is, no link is followed);
- the staging install (install-strip failing, then install), refusal of an existing prefix;
- the atomic `-current` link and the real-directory refusal;
- doctor's JSON shape, the packages per manager, the repository id per Enterprise Linux
  (crb, powertools, RHEL's codeready-builder through subscription-manager), the options that
  add packages, root; `--install-deps`' commands in order (base, then each option apart, then
  the wanted), with the multilib warning;
- with `--json`, an ERROR on stderr and nothing on stdout (`--host-cc` that is no compiler, an
  unknown profile, a bad `--mode`);
- `status --json`: the contract's exact keys against a fake opt root (ours, foreign, one that
  does not run, leftovers, the pairing, a dangling `-current`), and each build's status,
  stage and error;
- state, lock, the resume decision, `stages_to_run`, the fingerprint, wipe;
- profiles, the wizard (scripted), `--dry-run`, exit codes;
- clean only of its own;
- end to end:
  - a whole binutils build from a fake tree through `main()`, with the real verification
    through wrappers of the system's binutils;
  - a gcc build that fails in stage 2 and resumes without reconfiguring, then fails
    verification and is not linked;
  - a changed mode refused without `--restart`;
  - a binutils prefix removed after a finished build: built again from configure and linked,
    not "nothing to do"; one that is there but does not run: refused.

One REAL binutils build was run here: the GNU binutils-2.42 release tarball (Ubuntu's
`binutils_2.42.orig.tar.xz` from archive.ubuntu.com, the only reachable source of a GNU
tarball) was served from a local mirror on 127.0.0.1. It went through the real listing, the
download through `.part`, the xz check and the extract. Then it went through the real
configure (reboot.txt's line, `-g -O3 -march=native`), `make -j4` and the DESTDIR install
with one rename (638 MB). All 7 verify checks passed (GNU tools, ld.gold, -plugin, `gcc -B`
links C, gold and -flto, and the programs run), and binutils-current was linked. `plan` then
paired gcc with it (`--with-as/--with-ld` = its realpath).

Two concessions in that run's driver:
- The tool's root refusal was bypassed, because this container only has root.
- The doctor's `flex` row was waived (the release tarball ships the generated lexers).

What that run taught: makeinfo IS required, even for a release tarball. An out-of-tree build
remakes `bfd.info`, and the first attempt failed exactly there. The first-errors summary now
catches `makeinfo: not found` as the first error. Texinfo is not installed here, so the rerun
used a no-op makeinfo (the equivalent of `make MAKEINFO=true`: the info manuals were not
rebuilt). That rerun also exercised a real resume: the configure was kept and make
continued.

What was NOT tested (this container cannot reach gnu.org, gcc.gnu.org, kernel.org or
sourceware; it runs Ubuntu 24.04 on x86_64 with 4 CPUs):
- a real GCC build in any mode;
- the real GNU, snapshot and infrastructure mirrors, their listings, the `sha512.sum` files,
  the binutils-with-gold tarball names, and `contrib/download_prerequisites` over the
  network;
- `git clone` of gcc.git;
- the multilib packages, CRB/EPEL locations and dnf on AlmaLinux 10, zypper, pacman;
  `subscription-manager` on RHEL and PowerTools on EL 8 (the commands are tested, not run);
- `--install-deps` with sudo;
- aarch64;
- the remote path (satellite_remote has its own tests).
