#!/usr/bin/env python3
"""The CI workflow runs what it says, from pinned inputs, with no more permission than reading the repository.

A workflow file is code nobody runs locally. This reads .github/workflows/ci.yml as text (no YAML library needed) and
holds the properties that matter: every action is pinned to a commit, the token is read-only and is not left in the
clone, the scripts and tests the jobs name exist, the jobs that gate do gate (the two that run tests get a full clone:
their tests read older revisions), the host tests and the Mac kext job run on Apple silicon and on Intel, the kext job
checks what an Intel Mac's compiler can slip into a kext, the host tests and the plugin get the Vulkan headers the
release uses (and a host test that needs them cannot pass by being skipped), and nothing uploads or publishes. Each
property has a control: the same check on a copy of the file with that one thing wrong has to fail.

.github/workflows/full-build.yml, the build from an empty folder, is held to the same rules (its actions pinned to the
commits ci.yml uses, its Xcode step equal to ci.yml's) and to what it is for: one job on macos-15-intel with a timeout,
the six build.sh commands in their order, the tools saved right after deps, its checks and its logs after a failure,
nothing uploaded. It must also parse as YAML where python's yaml is installed.

It also holds build/ci_plugin.sh to the compiler line of build/build_plugin_release.sh, which it copies: the release
script runs on the build Mac over ssh and cannot run on a CI runner, and a copy drifts. And it holds the Vulkan headers
of the automatic checks (build/ci_vulkan_headers.sh) to the Mesa commit the release takes its headers from, running the
script on copies of build/pins.sh (it stops before any download when the two pins disagree).
Usage: test_ci_workflow.py [driver checkout]"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
FAILS = []
GATES = ("kexts-linux", "host-tests", "plugin", "kexts-mac")


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def jobs(y):
    """-> {job name: its text}."""
    body = y.split("\njobs:\n", 1)[1] if "\njobs:\n" in y else ""
    return {m.group(1): m.group(2) for m in re.finditer(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", body, re.M | re.S)}


def problems(y, root):
    out = []
    code = "\n".join(l for l in y.splitlines() if not l.lstrip().startswith("#"))
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", code, re.M)
    if not uses:
        out.append("no action found (the check read nothing)")
    for u in uses:
        if not re.search(r"@[0-9a-f]{40}$", u):
            out.append(f"{u}: an action is not pinned to a commit")
    if len(re.findall(r"^permissions:\s*\n  contents: read\n(?=\S)", code, re.M)) != 1 or len(re.findall(r"\bpermissions:", code)) != 1 \
            or re.search(r":\s*write\b|write-all", code):
        out.append("the token is not read-only (one top-level `permissions:` with `contents: read` and nothing else)")
    if re.search(r"\bsecrets\b|GITHUB_TOKEN|github\.token", code) or "upload-artifact" in code or re.search(r"\bgh\s+(release|pr|api)\b", code):
        out.append("the workflow reads a secret or the token, or publishes something")
    if "pull_request_target" in code or "workflow_run" in code:
        out.append("pull_request_target / workflow_run runs with this repository's token on somebody else's change")
    for s in sorted(set(re.findall(r"\b(?:bash|python3)\s+((?:build|tools|kexts)/[\w./-]+\.(?:sh|py))", code))):
        if not (root / s).is_file():
            out.append(f"{s}: named by the workflow and not in the tree")
    for m in re.finditer(r"run_all_tests\.py((?: test_\w+)+)", code):
        for t in m.group(1).split():
            if not list((root / "tools").glob(t + ".*")):
                out.append(f"{t}: named by the workflow and not in tools/ (the runner would skip it without a word)")
        # the runner exits 0 when a named test was skipped or not run: the step has to hold its last line to the count
        n = len(m.group(1).split())
        if f"grep -qx '{n} passed, 0 failed, 0 skipped, 0 not run, of {n}'" not in code[m.end():m.end() + 400]:
            out.append(f"the step that names {n} tests does not hold the runner to '{n} passed, 0 failed, 0 skipped, 0 not run, of {n}' "
                       f"(a named test that was skipped or not run would pass unseen)")
    j = jobs(code)
    for g in GATES:
        if g not in j:
            out.append(f"job {g} is gone")
        elif "continue-on-error" in j[g]:
            out.append(f"job {g} no longer gates (continue-on-error)")
    for name, text in j.items():
        if text.count("actions/checkout@") != 1 or "persist-credentials: false" not in text:
            out.append(f"job {name}: one checkout, and it must not leave the token in the clone (persist-credentials: false)")
        if re.search(r"\bsudo\b", text):
            out.append(f"job {name} uses sudo")
    linux = j.get("kexts-linux", "")
    if "runs-on: ubuntu" not in linux or not re.search(r"run: bash build/kext_check\.sh\s*$", linux, re.M) \
            or not re.search(r"run_all_tests\.py.* test_switch_registry\b", linux):
        out.append("kexts-linux does not run the kext check and the switch registry test on Linux")
    if "fetch-depth: 0" not in linux:
        out.append("kexts-linux has no full clone: the registry test would skip its comparison with the previous commit and the 2.9 release")
    host = j.get("host-tests", "")
    if "fetch-depth: 0" not in host or not re.search(r"^\s*set -o pipefail\n\s*python3 tools/run_all_tests\.py -j \d+ --tail \d+ \| tee \"\$RUNNER_TEMP/tests\.txt\"\s*$", host, re.M):
        out.append("host-tests does not run every test from a full clone (with the runner's own exit code: set -o pipefail before the tee)")
    m = re.search(r"run_all_tests\.py -j \d+ --tail (\d+) ", host)
    if not m or int(m.group(1)) < 20000:
        out.append("host-tests does not print a failed test's output whole (--tail of at least 20000 characters)")
    m = re.search(r"runner: \[([^\]]*)\]", host)
    if not m or {r.strip() for r in m.group(1).split(",")} != {"macos-15", "macos-15-intel"} or "runs-on: ${{ matrix.runner }}" not in host \
            or "fail-fast: false" not in host:
        out.append("host-tests does not run on both an Apple-silicon and an Intel runner (macos-15, macos-15-intel), each to its end "
                   "(fail-fast: false)")
    # the runner counts a skip as no failure: a skip of a test that reads NM_VULKAN_INCLUDE must fail the job
    sk = re.search(r"if grep -E '\^skip \(([\w|]+)\)\\\.py:' \"\$RUNNER_TEMP/tests\.txt\"; then\n\s*echo \"::error::[^\"\n]*\"; exit 1\n\s*fi\n", host)
    if not sk or set(sk.group(1).split("|")) != vulkan_tests(root):
        out.append("host-tests does not fail when a test that needs the Vulkan headers is skipped (the list must be the tests that read "
                   f"NM_VULKAN_INCLUDE: {' '.join(sorted(vulkan_tests(root)))})")
    # a test must not make macOS start an install of the command line tools: the marker the runner lists fails the
    # job, in a step that also runs after a failed test, and its pattern finds the line the runner prints
    od = re.search(r"if: \$\{\{ !cancelled\(\) \}\}\n\s*run: \|\n(?:.*\n)*?\s*if grep -E '(\^left in [^'\n]*installondemand[^'\n]*)' "
                   r"\"\$RUNNER_TEMP/tests\.txt\"; then\n\s*echo \"::error::[^\"\n]*\"; exit 1\n\s*fi\n", host)
    line = re.search(r'print\(f"(left in \{root\} during the run [^"\n]*: \{n\})"\)', (root / "tools/run_all_tests.py").read_text())
    shown = line.group(1).replace("{root}", "/private/tmp").replace("{who}", "test_a.py, test_b.py") \
        .replace("{n}", ".com.apple.dt.CommandLineTools.installondemand.in-progress") if line else ""
    if not od or not shown or not re.search(od.group(1), shown) or re.search(od.group(1), shown.replace("installondemand", "other")):
        out.append("host-tests does not fail when a test left the marker of an on-demand install of the command line tools "
                   "(a step that also runs after a failed test, with a pattern that finds the runner's line for the marker and no other)")
    # the four tests that compile against vulkan/vulkan.h skip without headers: the job must hand them the release's
    hv = re.search(r"^\s*d=\$\(bash build/ci_vulkan_headers\.sh\)\n\s*echo \"NM_VULKAN_INCLUDE=\$d\" >> \"\$GITHUB_ENV\"", host, re.M)
    if not hv or hv.start() > host.find("tools/run_all_tests.py"):
        out.append("host-tests does not set NM_VULKAN_INCLUDE from build/ci_vulkan_headers.sh before the tests")
    for name in ("host-tests", "plugin"):
        if not re.search(r"path: build-out/ci-vulkan-headers\n\s*key: ci-vulkan-headers-\$\{\{ steps\.vkh\.outputs\.commit \}\}", j.get(name, "")) \
                or "NM_PIN_CI_VKHEADERS_COMMIT" not in j.get(name, ""):
            out.append(f"{name} does not keep the Vulkan headers under the key of their pin")
    mac = j.get("kexts-mac", "")
    # the release build takes Xcode NM_PIN_TOOL_XCODE_MIN or later; the image's default Xcode is older
    xc = mac.find('echo "DEVELOPER_DIR=$best/Contents/Developer" >> "$GITHUB_ENV"')
    if xc < 0 or "s/^NM_PIN_TOOL_XCODE_MIN=" not in mac or xc > mac.find("run: bash build/kext_check.sh"):
        out.append("kexts-mac does not select an Xcode the release accepts (NM_PIN_TOOL_XCODE_MIN) before it builds")
    if not re.search(r"name: NVIDIA RM core and NVKMS for macOS\n\s*id: rm\n", mac) \
            or not re.search(r"name: No option of the build computer.*?\n\s*if: \$\{\{ !cancelled\(\) && steps\.rm\.outcome == 'success' \}\}\n", mac, re.S):
        out.append("kexts-mac does not check its compile lines whenever the RM and NVKMS were compiled (also after a failed link)")
    m = re.search(r"runner: \[([^\]]*)\]", mac)
    if not m or {r.strip() for r in m.group(1).split(",")} != {"macos-15", "macos-15-intel"} or "runs-on: ${{ matrix.runner }}" not in mac:
        out.append("kexts-mac does not run on both an Apple-silicon and an Intel runner (macos-15, macos-15-intel)")
    if not all(s in mac for s in ("grep -cE -e '-mfunction-return|-mindirect-branch|-fcf-protection=(full|return)|",
                                  'f="$W/src/$part/_out/Darwin_x86_64/compile_cmds.sh"', "for part in nvidia nvidia-modeset; do",
                                  "grep -qE '(^|[[:space:]])___x86_' \"$O/nvrm/imports.txt\"", "shasum -a 256", "exit $bad")):
        out.append("kexts-mac does not check its compile lines and NVRM's imports for what an Intel Mac's compiler adds")
    # macOS's make leaves a space for every option the compiler refused: the two runners' lines differ in spacing
    if '"$f" | tr -s \' \' > "$cut"' not in mac or 'sum=$(shasum -a 256 < "$cut"' not in mac:
        out.append("kexts-mac does not take the checksum of its compile lines with runs of spaces squeezed (the two runners' would differ)")
    return out


def vulkan_tests(root):
    """-> the tests that compile against vulkan/vulkan.h: the ones that take the folder from NM_VULKAN_INCLUDE."""
    return {p.stem for p in (root / "tools").glob("test_*.py") if re.search(r"environ\.get\(['\"]NM_VULKAN_INCLUDE", p.read_text())}


def compile_line(text, end):
    """-> the words of the plugin's compiler line, without the include path and the output (they differ by machine)."""
    m = re.search(r"xcrun clang (-arch x86_64 .*?)" + end, text.replace("\\\n", " "), re.S)
    words, out, skip = (m.group(1).split() if m else []), [], False
    for w in words:
        if skip:
            skip = False; continue
        if w == "-o":
            skip = True; continue
        if w.startswith("-I"):
            continue
        out.append(w)
    return out


y = (ROOT / ".github/workflows/ci.yml").read_text()
p = problems(y, ROOT)
arm("the workflow is pinned, read-only, and names only what is in the tree", not p, "; ".join(p))
arm("it has the four jobs", sorted(jobs(y)) == ["host-tests", "kexts-linux", "kexts-mac", "plugin"], " ".join(sorted(jobs(y))))


def control(name, old, new, want, count=1):
    assert old in y, old
    got = problems(y.replace(old, new, count), ROOT)
    arm("control: " + name, any(want in g for g in got), "; ".join(got) or "nothing reported")


control("an action on a moving tag is caught", "@3d3c42e5aac5ba805825da76410c181273ba90b1", "@v7", "not pinned")
control("a write permission is caught", "  contents: read\n", "  contents: write\n", "not read-only")
control("a second permission is caught", "  contents: read\n", "  contents: read\n  packages: read\n", "not read-only")
control("a job with its own permissions is caught", "    timeout-minutes: 30\n", "    timeout-minutes: 30\n    permissions:\n      id-token: write\n", "not read-only")
control("a secret is caught", "run: bash build/ci_plugin.sh", "run: KEY=${{ secrets.SIGNING_KEY }} bash build/ci_plugin.sh", "secret")
control("an upload is caught", "      - name: Host tests\n", "      - uses: actions/upload-artifact@3d3c42e5aac5ba805825da76410c181273ba90b1\n      - name: Host tests\n", "publishes")
control("pull_request_target is caught", "  pull_request:\n", "  pull_request_target:\n", "pull_request_target")
control("a script that does not exist is caught", "bash build/kext_check.sh", "bash build/kext_chekc.sh", "not in the tree")
control("a test that does not exist is caught", " test_switch_registry ", " test_switch_registy ", "not in tools/")
control("a test added to the list without the count that holds the runner to it is caught", " test_switch_registry ", " test_switch_registry test_ci_workflow ", "does not hold the runner")
control("the count check dropped is caught", "          grep -qx '5 passed, 0 failed, 0 skipped, 0 not run, of 5' \"$RUNNER_TEMP/tests.txt\"\n", "", "does not hold the runner")
control("a shallow clone for the host tests is caught", "fetch-depth: 0          # negative controls", "fetch-depth: 1          # negative controls", "every test from a full clone")
control("a shallow clone for the registry test is caught", "fetch-depth: 0          # the registry test", "fetch-depth: 2          # the registry test", "kexts-linux has no full clone")
control("the Linux job made non-gating is caught", "    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    continue-on-error: true\n", "kexts-linux no longer gates")
control("the host tests made non-gating are caught", "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 80\n",
        "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 80\n    continue-on-error: true\n", "host-tests no longer gates")
HOST_RUNNERS = "        # Apple silicon and Intel, as the kext job below: the tests must hold on both\n        runner: [macos-15, macos-15-intel]\n"
control("the host tests on Apple silicon only are caught", HOST_RUNNERS, HOST_RUNNERS.replace("[macos-15, macos-15-intel]", "[macos-15]"),
        "host-tests does not run on both")
control("the host tests on one runner by name are caught", "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 80\n", "    runs-on: macos-15\n    timeout-minutes: 80\n",
        "host-tests does not run on both")
control("host tests that are cancelled when the other runner's fail are caught", "    strategy:\n      fail-fast: false\n      matrix:\n" + HOST_RUNNERS,
        "    strategy:\n      matrix:\n" + HOST_RUNNERS, "host-tests does not run on both")
control("a checkout that keeps the token is caught", "          persist-credentials: false\n", "", "persist-credentials")
control("the kext check dropped from the Linux job is caught", "        run: bash build/kext_check.sh\n      - name: Switch registry", "        run: true\n      - name: Switch registry",
        "does not run the kext check")
control("sudo is caught", "run: bash build/ci_plugin.sh", "run: sudo bash build/ci_plugin.sh", "sudo")
control("the plugin job made non-gating is caught", "    runs-on: macos-26\n    timeout-minutes: 30\n", "    runs-on: macos-26\n    timeout-minutes: 30\n    continue-on-error: true\n",
        "plugin no longer gates")
control("host tests without the release's Vulkan headers are caught", '          echo "NM_VULKAN_INCLUDE=$d" >> "$GITHUB_ENV"\n', "", "does not set NM_VULKAN_INCLUDE")
control("the headers cached under a key that is not their pin are caught", "key: ci-vulkan-headers-${{ steps.vkh.outputs.commit }}",
        "key: ci-vulkan-headers-${{ hashFiles('build/ci_plugin.sh') }}", "under the key of their pin")
control("the Mac kext job made non-gating is caught", "    timeout-minutes: 90\n    steps:\n", "    timeout-minutes: 90\n    continue-on-error: true\n    steps:\n",
        "kexts-mac no longer gates")
control("a checksum of the compile lines as make spaced them is caught", """"$f" | tr -s ' ' > "$cut\"""", """"$f" > "$cut\"""",
        "runs of spaces squeezed")
KEXT_RUNNERS = "        # Apple silicon and Intel, both macOS 15 (labels from GitHub's list of standard hosted runners)\n        runner: [macos-15, macos-15-intel]\n"
control("the kext job on Apple silicon only is caught", KEXT_RUNNERS, KEXT_RUNNERS.replace("[macos-15, macos-15-intel]", "[macos-15]"),
        "kexts-mac does not run on both")
control("the kext job without its check of NVRM's imports is caught", "if grep -qE '(^|[[:space:]])___x86_'", "if false",
        "does not check its compile lines")
control("the kext job that checks only the RM's compile lines is caught", "for part in nvidia nvidia-modeset; do", "for part in nvidia; do",
        "does not check its compile lines")
control("a host test run whose failures the tee hides is caught", "          set -o pipefail\n          python3 tools/run_all_tests.py -j 4",
        "          python3 tools/run_all_tests.py -j 4", "every test from a full clone")
control("host tests that print only the end of a failed test's output are caught", "run_all_tests.py -j 4 --tail 20000 |", "run_all_tests.py -j 4 |",
        "output whole")
control("a --tail too short for a failed test's whole output is caught", "--tail 20000 |", "--tail 1200 |", "output whole")
control("host tests that pass with the Vulkan tests skipped are caught",
        "test_bc_small_mips|test_copy_null_image|test_graphics_bindings|test_opengl_perf", "test_bc_small_mips|test_copy_null_image|test_graphics_bindings",
        "is skipped")
control("a skip check that only prints is caught", 'NM_VULKAN_INCLUDE did not reach it"; exit 1\n', 'NM_VULKAN_INCLUDE did not reach it"\n', "is skipped")
ONDEMAND = "(the line above names the tests that were running)\"; exit 1\n"
control("a check for the on-demand install's marker that only prints is caught", ONDEMAND, ONDEMAND.replace("; exit 1", ""), "on-demand install")
control("a check for the marker that is not made after a failed test is caught",
        "        if: ${{ !cancelled() }}\n        run: |\n          [ -f \"$RUNNER_TEMP/tests.txt\" ]", "        run: |\n          [ -f \"$RUNNER_TEMP/tests.txt\" ]",
        "on-demand install")
control("a pattern that finds every entry the tests left is caught", "'^left in .*: \\.com\\.apple\\.dt\\.CommandLineTools\\.installondemand'",
        "'^left in .*installondemand|^left in '", "on-demand install")
control("a pattern for another line than the runner's is caught", "'^left in .*: \\.com\\.apple\\.dt\\.CommandLineTools\\.installondemand'",
        "'^left behind: \\.com\\.apple\\.dt\\.CommandLineTools\\.installondemand'", "on-demand install")
control("the kext job on the image's default Xcode is caught", '          echo "DEVELOPER_DIR=$best/Contents/Developer" >> "$GITHUB_ENV"\n', "", "does not select an Xcode")
control("the compile-line check that runs only after a good link is caught", "        if: ${{ !cancelled() && steps.rm.outcome == 'success' }}\n", "",
        "whenever the RM and NVKMS were compiled")
arm("the Vulkan tests are found by what they read (four of them)", vulkan_tests(ROOT) == {"test_bc_small_mips", "test_copy_null_image", "test_graphics_bindings", "test_opengl_perf"},
    " ".join(sorted(vulkan_tests(ROOT))))
arm("a name in a comment is not what the workflow runs", problems(y + "\n# bash build/no_such_script.sh ${{ secrets.X }}\n", ROOT) == [])


def run_block(text, step):
    """-> the lines of the `run: |` block of the step with this name, without their indentation ("" when not found)."""
    m = re.search(r"^( *)- name: " + re.escape(step) + r"\n(.*?)(?=^\1- |\Z)", text, re.M | re.S)
    r = re.search(r"^( *)run: \|\n((?:\1  .*\n|[ ]*\n)+)", m.group(2), re.M) if m else None
    return "\n".join(l.strip() for l in r.group(2).splitlines()) if r else ""


def action_pins(text):
    """-> {owner/repo: {commits}} of the actions a workflow uses (actions/cache/save is actions/cache)."""
    out = {}
    for u in re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M):
        name, _, ref = u.partition("@")
        out.setdefault("/".join(name.split("/")[:2]), set()).add(ref)
    return out


XCODE_STEP = "Xcode 26 or later, as the release build needs"
FB_COMMANDS = (('deps', r'\./build\.sh --yes --workdir "\$NMCI_WORK" deps$'),
               ('doctor', r'\./build\.sh --workdir "\$NMCI_WORK" doctor\b'),
               ('fetch', r'\./build\.sh --workdir "\$NMCI_WORK" fetch$'),
               ('build', r'\./build\.sh --workdir "\$NMCI_WORK" --no-install build$'),
               ('package', r'\./build\.sh --workdir "\$NMCI_WORK" --base-package "\$BASEPKG" package$'),
               ('test', r'\./build\.sh --workdir "\$NMCI_WORK" test$'))


def full_problems(fb, ci, root):
    """the full build's workflow held to ci.yml's rules, and to what it is for."""
    # ci.yml's rules for every workflow; its rules for its own four jobs do not apply here
    ci_jobs = set(jobs(ci))
    out = [p for p in problems(fb, root) if not any(re.search(r"\b" + re.escape(n) + r"\b", p) for n in ci_jobs)]
    code = "\n".join(l for l in fb.splitlines() if not l.lstrip().startswith("#"))
    ci_code = "\n".join(l for l in ci.splitlines() if not l.lstrip().startswith("#"))
    have, want = action_pins(code), action_pins(ci_code)
    for name, refs in sorted(have.items()):
        if not refs <= want.get(name, set()):
            out.append(f"{name}@{' '.join(sorted(refs))}: not the commit ci.yml pins ({' '.join(sorted(want.get(name, set()))) or 'ci.yml does not use it'})")
    if re.search(r"upload|publish|deploy|\bgh\s|\bcurl\b.*-T\b|\bscp\b|\bssh\b", code, re.I):
        out.append("the full build uploads or sends something (the package never leaves the runner)")
    if not re.search(r"^on:\n  push:\n  workflow_dispatch:\n(?=\S)", code, re.M):
        out.append("the full build does not run on every push and by hand (on: push, workflow_dispatch, nothing else)")
    j = jobs(code)
    if len(j) != 1:
        out.append(f"the full build is not one job ({len(j)})")
    t = next(iter(j.values()), "")
    if not re.search(r"^    runs-on: macos-15-intel\n", t, re.M):
        out.append("the full build does not run on macos-15-intel")
    tm = re.search(r"^    timeout-minutes: (\d+)\n", t, re.M)
    if not tm or not 0 < int(tm.group(1)) < 360:
        out.append("the full build has no timeout below GitHub's 360 minutes")
    coe = bool(re.search(r"^    continue-on-error: true\n", t, re.M))
    said = "continue-on-error until it has been seen green once" in fb
    if coe != said:
        out.append("continue-on-error and the header's sentence about it disagree (it stays until the job has been seen green once, then both go)")
    if not re.search(r"^concurrency:\n  group: [^\n]+\n  cancel-in-progress: false\n", code, re.M):
        out.append("the full build's concurrency does not keep a running build (cancel-in-progress: false)")
    if not run_block(fb, XCODE_STEP) or run_block(fb, XCODE_STEP) != run_block(ci, XCODE_STEP):
        out.append(f"the step '{XCODE_STEP}' differs from ci.yml's (the two copies must stay equal)")
    at, last = {}, -1
    for word, rx in FB_COMMANDS:
        m = re.search(r"^\s*(?:run: )?(?:rc=0; )?" + rx, code, re.M)
        at[word] = m.start() if m else -1
        if not m or m.start() < last:
            out.append(f"the full build does not run ./build.sh {word} in its place (deps, doctor, fetch, --no-install build, package, test)")
        last = m.start() if m else last
    if "NM_BASE_PACKAGE_SHA256: ${{ steps.pins.outputs.basepkg_sha256 }}" not in code or "NM_PIN_CI_BASEPKG_SHA256=" not in code:
        out.append("the package step is not given the pinned SHA-256 of the 2.9 package (NM_BASE_PACKAGE_SHA256)")
    if re.search(r"uses: actions/cache@", code):
        out.append("a cache saved by the action's own post step (actions/cache): the tools must be saved by an explicit save step")
    saves = [m.start() for m in re.finditer(r"uses: actions/cache/save@\S+[^\n]*\n\s*with:\n\s*(?:#[^\n]*\n\s*)*path: \|\n[^\n]*\n[^\n]*nmw/tools/", code)]
    if len(saves) != 2 or not all(at["deps"] < s < at["doctor"] for s in saves):
        out.append("the tools are not saved by explicit save steps right after deps (before doctor)")
    if not re.search(r"if: \$\{\{ always\(\) && steps\.tcid\.outputs\.id != ''", code):
        out.append("the toolchain is not saved after a failed or cancelled deps")
    # the Python environment, Rust and the Translator belong to the toolchain they were made with: looked up under the
    # stamps of the restored toolchain, saved under the stamps deps left
    rk = re.search(r"- name: Restore the Python environment, Rust and the Translator\n(?:.*\n)*?\s+key: (.*)\n", code)
    sk = re.search(r"- name: Save the Python environment, Rust and the Translator\n(?:.*\n)*?\s+key: (.*)\n", code)
    if not (rk and rk.group(1).endswith("-${{ steps.tcid0.outputs.id }}") and sk and sk.group(1).endswith("-${{ steps.tcid.outputs.id }}")
            and rk.group(1)[:-len("${{ steps.tcid0.outputs.id }}")] == sk.group(1)[:-len("${{ steps.tcid.outputs.id }}")]
            and code.index("id: tcid0") < code.index("id: rest")):
        out.append("the Python environment, Rust and the Translator are not cached under the stamps of the toolchain they were made with")
    if not re.search(r"if: \$\{\{ failure\(\) \|\| cancelled\(\) \}\}\n\s*run: /bin/bash build/ci_full_build\.sh logs ", code):
        out.append("no last step that prints the logs when the build failed or ran out of time")
    if not re.search(r"if: \$\{\{ !cancelled\(\) \}\}\n\s*run: /bin/bash build/ci_full_build\.sh prove ", code):
        out.append("the checks of what the build did and did not do do not run (also after a failed step)")
    m = re.search(r"if: \$\{\{ !cancelled\(\) \}\}\n\s*run: /bin/bash build/ci_full_build\.sh reads \"\$NMCI_WORK\" \|\| echo ", code)
    if not m or code.find("ci_full_build.sh prove ") > m.start() or code.find("ci_full_build.sh prove ") < 0:
        out.append("the reads only a Mac can make are not printed after the checks, also after a failed step, without failing the job")
    if len(re.findall(r"\bdf -h\b", code)) != 3:
        out.append("the free disk is not printed at the start, after deps and at the end (df -h three times)")
    for s in sorted(set(re.findall(r"/bin/bash ((?:build|tools)/[\w./-]+\.sh)", code))):
        if not (root / s).is_file():
            out.append(f"{s}: named by the workflow and not in the tree")
    return out


fb_path = ROOT / ".github/workflows/full-build.yml"
fb = fb_path.read_text()
fp = full_problems(fb, y, ROOT)
arm("full-build.yml is held to ci.yml's rules and builds what it says", not fp, "; ".join(fp))
try:
    import yaml
    for f in (".github/workflows/ci.yml", ".github/workflows/full-build.yml"):
        d = yaml.safe_load((ROOT / f).read_text())
        arm(f"{f} parses as YAML, with its jobs", isinstance(d, dict) and bool(d.get("jobs")))
except ImportError:
    print("note: no python yaml here; the YAML parse was not checked")


def fb_control(name, old, new, want, count=1, ci_text=None):
    assert old in (fb if ci_text is None else ci_text), old
    got = full_problems(fb.replace(old, new, count), y, ROOT) if ci_text is None else full_problems(fb, ci_text.replace(old, new, count), ROOT)
    arm("control (full build): " + name, any(want in g for g in got), "; ".join(got) or "nothing reported")


fb_control("an action on a moving tag is caught", "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", "actions/cache/restore@v6", "not pinned")
fb_control("an action pinned to another commit than ci.yml's is caught", "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
           "actions/cache/restore@" + "1" * 40, "not the commit ci.yml pins")
fb_control("an action ci.yml does not use is caught", "      - name: The logs of what failed\n",
           "      - uses: someone/setup-thing@" + "2" * 40 + "\n      - name: The logs of what failed\n", "ci.yml does not use it")
fb_control("a write permission is caught", "  contents: read\n", "  contents: write\n", "not read-only")
fb_control("a secret is caught", "run: ./build.sh --workdir \"$NMCI_WORK\" test", "run: K=${{ secrets.K }} ./build.sh --workdir \"$NMCI_WORK\" test", "secret")
fb_control("an upload of the package is caught", "      - name: The logs of what failed\n",
           "      - uses: actions/upload-artifact@3d3c42e5aac5ba805825da76410c181273ba90b1\n      - name: The logs of what failed\n", "upload")
fb_control("a copy sent with scp is caught", "run: ./build.sh --workdir \"$NMCI_WORK\" test", "run: ./build.sh --workdir \"$NMCI_WORK\" test && scp x y:", "uploads or sends")
fb_control("pull_request_target is caught", "  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n", "pull_request_target")
fb_control("a pull_request trigger is caught", "  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n", "every push and by hand")
fb_control("a script that does not exist is caught", "/bin/bash build/ci_full_build.sh logs", "/bin/bash build/ci_full_buld.sh logs", "not in the tree")
fb_control("another runner is caught", "    runs-on: macos-15-intel\n", "    runs-on: macos-15\n", "macos-15-intel")
fb_control("a second job is caught", "\n    steps:\n", "\n    steps:\n      - run: true\n  second:\n    runs-on: ubuntu-24.04\n    steps:\n", "not one job")
fb_control("no timeout is caught", "    timeout-minutes: 340\n", "", "no timeout below")
fb_control("GitHub's own limit as the timeout is caught", "    timeout-minutes: 340\n", "    timeout-minutes: 360\n", "no timeout below")
fb_control("continue-on-error without the header's sentence is caught", "continue-on-error until it has been seen green once", "continue-on-error for now",
           "disagree")
fb_control("the sentence left after continue-on-error went is caught", "    continue-on-error: true\n", "", "disagree")
fb_control("a concurrency that cancels the running build is caught", "  cancel-in-progress: false\n", "  cancel-in-progress: true\n", "cancel-in-progress: false")
fb_control("a checkout that keeps the token is caught", "          persist-credentials: false\n", "", "persist-credentials")
fb_control("sudo is caught", "run: ./build.sh --workdir \"$NMCI_WORK\" test", "run: sudo ./build.sh --workdir \"$NMCI_WORK\" test", "sudo")
fb_control("an Xcode step changed here only is caught", "[ -n \"$best\" ] || { echo \"::error::no Xcode $min or later",
           "[ -n \"$best\" ] || best=/Applications/Xcode.app; [ -n \"$best\" ] || { echo \"::error::no Xcode $min or later", "differs from ci.yml's")
fb_control("an Xcode step changed in ci.yml only is caught", "[ -n \"$best\" ] || { echo \"::error::no Xcode $min or later",
           "[ -n \"$best\" ] || best=/Applications/Xcode.app; [ -n \"$best\" ] || { echo \"::error::no Xcode $min or later", "differs from ci.yml's", ci_text=y)
fb_control("the Xcode step dropped is caught", "      - name: " + XCODE_STEP + "\n", "      - name: Some Xcode\n", "differs from ci.yml's")
fb_control("doctor dropped is caught", "rc=0; ./build.sh --workdir \"$NMCI_WORK\" doctor", "rc=0; true", "./build.sh doctor in its place")
fb_control("fetch dropped is caught", "        run: ./build.sh --workdir \"$NMCI_WORK\" fetch\n", "        run: true\n", "./build.sh fetch in its place")
fb_control("deps without --yes is caught", "run: ./build.sh --yes --workdir \"$NMCI_WORK\" deps", "run: ./build.sh --workdir \"$NMCI_WORK\" deps", "./build.sh deps in its place")
fb_control("an installing build is caught", '"$NMCI_WORK" --no-install build', '"$NMCI_WORK" build', "./build.sh build in its place")
fb_control("the package without its base is caught", "--base-package \"$BASEPKG\" package", "package", "./build.sh package in its place")
fb_control("the tests dropped are caught", "run: ./build.sh --workdir \"$NMCI_WORK\" test\n", "run: true\n", "./build.sh test in its place")
fb_control("the package step without the pinned SHA-256 is caught", "          NM_BASE_PACKAGE_SHA256: ${{ steps.pins.outputs.basepkg_sha256 }}\n", "", "NM_BASE_PACKAGE_SHA256")
fb_control("a cache saved by the action's post step is caught", "uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9   # v6.1.0\n        with:\n          # build.sh's toolchain",
           "uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9   # v6.1.0\n        with:\n          # build.sh's toolchain", "explicit save step")
fb_control("the toolchain saved only at the end is caught", "      - name: Save the toolchain (also after a failed or cancelled deps)\n",
           "      - name: Later\n        run: ./build.sh --workdir \"$NMCI_WORK\" doctor\n      - name: Save the toolchain (also after a failed or cancelled deps)\n",
           "right after deps")
fb_control("the toolchain saved only after a good deps is caught", "if: ${{ always() && steps.tcid.outputs.id != ''", "if: ${{ success() && steps.tcid.outputs.id != ''",
           "failed or cancelled deps")
fb_control("a Python environment, Rust and Translator cached apart from their toolchain are caught", "build/toolchain.sh') }}-${{ steps.tcid0.outputs.id }}\n",
           "build/toolchain.sh') }}\n", "the toolchain they were made with")
fb_control("one saved under the restored toolchain's stamps is caught", "build/toolchain.sh') }}-${{ steps.tcid.outputs.id }}\n",
           "build/toolchain.sh') }}-${{ steps.tcid0.outputs.id }}\n", "the toolchain they were made with")
fb_control("the logs step dropped is caught", "        if: ${{ failure() || cancelled() }}\n", "", "prints the logs")
fb_control("logs that are not shown after the job's timeout are caught", "if: ${{ failure() || cancelled() }}", "if: ${{ failure() }}", "ran out of time")
fb_control("the checks run only after a good build is caught", "        if: ${{ !cancelled() }}\n        run: /bin/bash build/ci_full_build.sh prove",
           "        run: /bin/bash build/ci_full_build.sh prove", "also after a failed step")
fb_control("reads that can fail the job are caught", 'reads "$NMCI_WORK" || echo "the reads ended early (exit $?)"', 'reads "$NMCI_WORK"', "without failing the job")
fb_control("reads only after a good build are caught", "        if: ${{ !cancelled() }}\n        run: /bin/bash build/ci_full_build.sh reads",
           "        run: /bin/bash build/ci_full_build.sh reads", "also after a failed step")
fb_control("a df dropped is caught", "        run: df -h \"$RUNNER_TEMP\" /\n", "        run: true\n", "df -h three times")
arm("control (full build): a name in a comment is not what it runs", full_problems(fb + "\n# /bin/bash build/no_such.sh ${{ secrets.X }} upload\n", y, ROOT) == [])

rel = compile_line((ROOT / "build/build_plugin_release.sh").read_text(), r"2>\.\./out/build\.err")
ci_text = (ROOT / "build/ci_plugin.sh").read_text()
ci = compile_line(ci_text, r'2>"\$OUT/build\.err"')
arm("build/ci_plugin.sh compiles the plugin with the release script's compiler line", len(rel) > 15 and rel == ci,
    " ".join(sorted(set(rel) ^ set(ci))) or "the order differs")
arm("control: a flag dropped from the copy is caught", compile_line(ci_text.replace(" -DNVMTL_RELEASE=1", ""), r'2>"\$OUT/build\.err"') != rel)
arm("control: a source file added to the release line is caught",
    compile_line((ROOT / "build/build_plugin_release.sh").read_text().replace("NVMTLDevice.m ", "NVMTLDevice.m nvmtl_new.c "), r"2>\.\./out/build\.err") != ci)
arm("it holds the plugin to its warning ceiling and to macOS 15, as the release script does",
    'warn_ceiling.sh" plugin' in ci_text and '[ "$minos" = 15.0 ]' in ci_text)
arm("its Vulkan headers come from build/ci_vulkan_headers.sh, and from nothing else",
    'INC=$(bash "$HERE/build/ci_vulkan_headers.sh")' in ci_text and "git " not in ci_text and "HEADERS_SHA" not in ci_text)


def pins(text):
    return {m.group(1): m.group(2) for m in re.finditer(r"^(NM_PIN_[A-Z0-9_]+)=(\S*)$", text, re.M)}


pins_text = (ROOT / "build/pins.sh").read_text()
P = pins(pins_text)
arm("build/pins.sh pins the checks' Vulkan-Headers beside the Mesa commit they were compared with, and that is the release's Mesa",
    bool(re.fullmatch(r"[0-9a-f]{40}", P.get("NM_PIN_CI_VKHEADERS_COMMIT", ""))) and P.get("NM_PIN_CI_VKHEADERS_MESA") == P.get("NM_PIN_MESA_COMMIT")
    and len(P.get("NM_PIN_MESA_COMMIT", "")) == 40, f"{P.get('NM_PIN_CI_VKHEADERS_MESA')} vs {P.get('NM_PIN_MESA_COMMIT')}")


def headers_script(pins_text, env_include=None):
    """runs build/ci_vulkan_headers.sh in a copy of the tree with this pins.sh; -> (exit code, stdout, stderr). It must
    never get as far as a download here: every case below stops before one or is answered by NM_VULKAN_INCLUDE."""
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "build").mkdir()
        shutil.copy(ROOT / "build/ci_vulkan_headers.sh", pathlib.Path(d) / "build")
        (pathlib.Path(d) / "build/pins.sh").write_text(pins_text)
        env = {k: v for k, v in os.environ.items() if k != "NM_VULKAN_INCLUDE"}
        if env_include is not None:
            env["NM_VULKAN_INCLUDE"] = env_include
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        r = subprocess.run(["bash", str(pathlib.Path(d) / "build/ci_vulkan_headers.sh"), str(pathlib.Path(d) / "work")],
                           capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr


moved = pins_text.replace("NM_PIN_MESA_COMMIT=" + P.get("NM_PIN_MESA_COMMIT", "x"), "NM_PIN_MESA_COMMIT=" + "1" * 40)
rc, out, err = headers_script(moved)
arm("control: a Mesa pin moved without comparing the headers again stops the script, before any download",
    rc == 2 and out == "" and "compared with Mesa" in err and not pins(moved).get("NM_PIN_CI_VKHEADERS_MESA") == pins(moved).get("NM_PIN_MESA_COMMIT"),
    (rc, err[-200:]))
rc, out, err = headers_script(re.sub(r"^NM_PIN_CI_VKHEADERS_COMMIT=.*\n", "", pins_text, flags=re.M))
arm("control: a pins.sh without the checks' Vulkan-Headers pin stops it", rc == 2 and out == "" and "does not name" in err, (rc, err[-200:]))
with tempfile.TemporaryDirectory() as inc:
    (pathlib.Path(inc) / "vulkan").mkdir()
    rc_empty, out_empty, _ = headers_script(pins_text, inc)
    (pathlib.Path(inc) / "vulkan/vulkan.h").write_text("/* stand-in */\n")
    rc, out, err = headers_script(pins_text, inc)
arm("NM_VULKAN_INCLUDE is taken as it is, and the folder is all the script prints", rc == 0 and out == inc + "\n", (rc, out, err[-200:]))
arm("control: NM_VULKAN_INCLUDE without vulkan/vulkan.h stops it", rc_empty == 2 and out_empty == "")
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
