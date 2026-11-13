#!/usr/bin/env python3
"""The CI workflow runs what it says, from pinned inputs, with no more permission than reading the repository.

A workflow file is code nobody runs locally. This reads .github/workflows/ci.yml as text (no YAML library needed) and
holds the properties that matter: every action is pinned to a commit, the token is read-only and is not left in the
clone, the scripts and tests the jobs name exist, the two jobs that gate do gate and get a full clone (their tests
read older revisions), the Mac kext job runs on Apple silicon and on Intel and checks what an Intel Mac's compiler can
slip into a kext, the host tests and the plugin get the Vulkan headers the release uses, and nothing uploads or
publishes. Each property has a control: the same check on a copy of the file with that one thing
wrong has to fail.

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
GATES = ("kexts-linux", "host-tests")


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
    if "fetch-depth: 0" not in host or not re.search(r"run: python3 tools/run_all_tests\.py -j \d+\s*$", host, re.M):
        out.append("host-tests does not run every test from a full clone")
    # the four tests that compile against vulkan/vulkan.h skip without headers: the job must hand them the release's
    hv = re.search(r"^\s*d=\$\(bash build/ci_vulkan_headers\.sh\)\n\s*echo \"NM_VULKAN_INCLUDE=\$d\" >> \"\$GITHUB_ENV\"", host, re.M)
    if not hv or hv.start() > host.find("tools/run_all_tests.py"):
        out.append("host-tests does not set NM_VULKAN_INCLUDE from build/ci_vulkan_headers.sh before the tests")
    for name in ("host-tests", "plugin"):
        if not re.search(r"path: build-out/ci-vulkan-headers\n\s*key: ci-vulkan-headers-\$\{\{ steps\.vkh\.outputs\.commit \}\}", j.get(name, "")) \
                or "NM_PIN_CI_VKHEADERS_COMMIT" not in j.get(name, ""):
            out.append(f"{name} does not keep the Vulkan headers under the key of their pin")
    mac = j.get("kexts-mac", "")
    m = re.search(r"runner: \[([^\]]*)\]", mac)
    if not m or {r.strip() for r in m.group(1).split(",")} != {"macos-15", "macos-15-intel"} or "runs-on: ${{ matrix.runner }}" not in mac:
        out.append("kexts-mac does not run on both an Apple-silicon and an Intel runner (macos-15, macos-15-intel)")
    if not all(s in mac for s in ("grep -cE -e '-mfunction-return|-mindirect-branch|-fcf-protection=(full|return)|",
                                  'f="$W/src/$part/_out/Darwin_x86_64/compile_cmds.sh"', "for part in nvidia nvidia-modeset; do",
                                  "grep -qE '(^|[[:space:]])___x86_' \"$O/nvrm/imports.txt\"", "shasum -a 256", "exit $bad")):
        out.append("kexts-mac does not check its compile lines and NVRM's imports for what an Intel Mac's compiler adds")
    return out


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
control("the host tests made non-gating are caught", "    runs-on: macos-15\n    timeout-minutes: 45\n", "    runs-on: macos-15\n    timeout-minutes: 45\n    continue-on-error: true\n",
        "host-tests no longer gates")
control("a checkout that keeps the token is caught", "          persist-credentials: false\n", "", "persist-credentials")
control("the kext check dropped from the Linux job is caught", "        run: bash build/kext_check.sh\n      - name: Switch registry", "        run: true\n      - name: Switch registry",
        "does not run the kext check")
control("sudo is caught", "run: bash build/ci_plugin.sh", "run: sudo bash build/ci_plugin.sh", "sudo")
control("host tests without the release's Vulkan headers are caught", '          echo "NM_VULKAN_INCLUDE=$d" >> "$GITHUB_ENV"\n', "", "does not set NM_VULKAN_INCLUDE")
control("the headers cached under a key that is not their pin are caught", "key: ci-vulkan-headers-${{ steps.vkh.outputs.commit }}",
        "key: ci-vulkan-headers-${{ hashFiles('build/ci_plugin.sh') }}", "under the key of their pin")
control("the kext job on Apple silicon only is caught", "runner: [macos-15, macos-15-intel]", "runner: [macos-15]", "both an Apple-silicon and an Intel runner")
control("the kext job without its check of NVRM's imports is caught", "if grep -qE '(^|[[:space:]])___x86_'", "if false",
        "does not check its compile lines")
control("the kext job that checks only the RM's compile lines is caught", "for part in nvidia nvidia-modeset; do", "for part in nvidia; do",
        "does not check its compile lines")
arm("a name in a comment is not what the workflow runs", problems(y + "\n# bash build/no_such_script.sh ${{ secrets.X }}\n", ROOT) == [])

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
