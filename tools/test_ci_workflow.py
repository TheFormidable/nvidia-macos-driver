#!/usr/bin/env python3
"""The CI workflow runs what it says, from pinned inputs, with no more permission than reading the repository.

A workflow file is code nobody runs locally. This reads .github/workflows/ci.yml as text (no YAML library needed) and
holds the properties that matter: every action is pinned to a commit, the token is read-only and is not left in the
clone, the scripts and tests the jobs name exist, the two jobs that gate do gate and get a full clone (their tests
read older revisions), and nothing uploads or publishes. Each property has a control: the same check on a copy of the file with that one thing
wrong has to fail.

It also holds build/ci_plugin.sh to the compiler line of build/build_plugin_release.sh, which it copies: the release
script runs on the build Mac over ssh and cannot run on a CI runner, and a copy drifts.
Usage: test_ci_workflow.py [driver checkout]"""
import pathlib
import re
import sys

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
control("a shallow clone for the host tests is caught", "fetch-depth: 0          # negative controls", "fetch-depth: 1          # negative controls", "every test from a full clone")
control("a shallow clone for the registry test is caught", "fetch-depth: 0          # the registry test", "fetch-depth: 2          # the registry test", "kexts-linux has no full clone")
control("the Linux job made non-gating is caught", "    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    continue-on-error: true\n", "kexts-linux no longer gates")
control("the host tests made non-gating are caught", "    runs-on: macos-15\n    timeout-minutes: 45\n", "    runs-on: macos-15\n    timeout-minutes: 45\n    continue-on-error: true\n",
        "host-tests no longer gates")
control("a checkout that keeps the token is caught", "          persist-credentials: false\n", "", "persist-credentials")
control("the kext check dropped from the Linux job is caught", "        run: bash build/kext_check.sh\n      - name: Switch registry", "        run: true\n      - name: Switch registry",
        "does not run the kext check")
control("sudo is caught", "run: bash build/ci_plugin.sh", "run: sudo bash build/ci_plugin.sh", "sudo")
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
arm("its Vulkan headers are the commit build_vulkan_loader.sh pins",
    "HEADERS_SHA" in ci_text and bool(re.search(r"^HEADERS_SHA=[0-9a-f]{40}$", (ROOT / "build/build_vulkan_loader.sh").read_text(), re.M)))
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
