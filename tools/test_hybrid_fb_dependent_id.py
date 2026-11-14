#!/usr/bin/env python3
"""Hybrid panel framebuffers (NMIntelFB, NMGopFB) set IOFBDependentID before IOFramebuffer::start.

IOFramebuffer::start attaches a framebuffer to its shared controller only when the dependent ID already exists. Set
afterwards, the controller stays unset until open(), and the power change that registerPowerDriver() queues at the end
of start() takes the controller's lock through a null pointer. NVRMFB already sets it first. NMIntelFB also maps its
surface before it publishes anything other threads reach, and a failed start undoes what it published through the same
routine stop() uses. Reads the kernel sources.

   python3 tools/test_hybrid_fb_dependent_id.py [tree-root]   (pass an older tree to see the arms fail)
The negative controls are made from the text read: the same checks run on a copy with a publisher moved ahead of the
map check, and on a copy with the two properties moved behind IOFramebuffer::start, and must fail."""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
FILES = {"NMIntelFB": "kexts/NMIntelFB/intelfb.cpp", "NMGopFB": "kexts/NMGopFB/gopfb.cpp"}
fails = []


def arm(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def body_of(src, head):
    """The text from `head` to the end of its function, or "" when the function is not there."""
    i = src.find(head)
    return src[i:src.find("\n}\n", i)] if i >= 0 else ""


def start_bodies(src, cls):
    """Each `bool <cls>::start(` body, split at #ifdef branches so both start paths are checked on their own."""
    body = body_of(src, f"bool {cls}::start(IOService *provider)")
    if "#ifdef" in body and "#endif" in body:
        # NMIntelFB: the virtual panel path returns inside #ifdef ... #endif; the real panel path follows it
        a = body.index("#ifdef")
        c = body.index("#endif", a)
        return [body[a:c], body[c:]]
    return [body]


def ordered(body):
    """True when every IOFramebuffer::start call comes after both dependent properties are set."""
    if "IOFramebuffer::start(provider)" not in body:
        return None
    s = body.index("IOFramebuffer::start(provider)")
    d = body.find('setProperty("IOFBDependentID"')
    x = body.find('setProperty("IOFBDependentIndex"')
    return 0 <= d < s and 0 <= x < s


def check(read):
    results = {}
    for cls, rel in FILES.items():
        for n, body in enumerate(start_bodies(read(rel), cls)):
            r = ordered(body)
            if r is not None:
                results[f"{cls} start path {n}"] = r
    return results


src = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in FILES.values()}
now = check(src.get)
arm("three start paths were found (NMIntelFB virtual, NMIntelFB panel, NMGopFB)", len(now) == 3)
for name, ok in now.items():
    arm(f"{name}: the dependent ID and index are set before IOFramebuffer::start", ok)

ifb = src[FILES["NMIntelFB"]]
panel = start_bodies(ifb, "NMIntelFB")[-1]
arm("NMIntelFB: a surface that will not map stops start before IOFramebuffer::start (no controller-less framebuffer)",
    0 <= panel.find("if (!fSurfMap) {") < panel.find("IOFramebuffer::start(provider)"))
PUBLISHERS = ("resumeCapture();", "tcStart();", "gDmcTarget = this;", "publishBacklight();")


def map_before_publish(body):
    """The surface map check returns before anything that publishes state other threads can reach."""
    m = body.find("if (!fSurfMap) {")
    return m >= 0 and all(body.find(x) > m for x in PUBLISHERS)


arm("NMIntelFB: the surface-map failure return comes before resumeCapture, tcStart, the DMC target and the backlight",
    map_before_publish(panel))
fail_ret = panel[panel.find("if (!IOFramebuffer::start(provider)) {"):]
fail_ret = fail_ret[:fail_ret.find("return false;")]
arm("NMIntelFB: a failed IOFramebuffer::start (after state is published) runs the shared unwind", "startUnwind();" in fail_ret)
unwind = body_of(ifb, "void NMIntelFB::startUnwind()")
arm("NMIntelFB: the unwind clears the DMC target under its lock, frees the blob, the Type-C timer, external heads, "
    "resume capture, the backlight and both mappings",
    all(x in unwind for x in ("IOLockLock(gDmcLock)", "if (gDmcTarget == this) gDmcTarget = nullptr;", "IOFree(fDmcBlob",
                              "thread_call_free(fTcTimer)", "nmhd_stop(&fHeads, &fHeadOps)", "fResumeOn = false;",
                              "thread_call_free(fResumeCall)", "fBacklight->detach(this)", "OSSafeReleaseNULL(fSurfMap)",
                              "OSSafeReleaseNULL(fMMIO)")))
stop = body_of(ifb, "void NMIntelFB::stop(IOService *provider)")
arm("NMIntelFB: stop() uses the same unwind, so the two cannot drift apart", "startUnwind();" in stop)
# Negative control, made from the text: resumeCapture moved in front of the map check must be caught.
early = panel.replace("    resumeCapture();\n", "", 1)
early = early.replace("    if (!fSurfMap) {", "    resumeCapture();\n    if (!fSurfMap) {", 1)
arm("negative control: a publisher ahead of the map check is caught", early != panel and not map_before_publish(early))
nvfb = (ROOT / "kexts/NVRMFB/fb/nvrm-fb.cpp").read_text(encoding="utf-8")
arm("NVRMFB still sets its dependent ID before super::start (the reference order)",
    0 <= nvfb.find('setProperty("IOFBDependentID"') < nvfb.find("if (!super::start(provider)) {"))


# Negative control, made from the text: the two properties moved behind IOFramebuffer::start in each start path (the
# order of release 2.9) must be caught by the same check.
def late_props(body):
    lines = [l for l in body.split("\n")
             if 'setProperty("IOFBDependentID"' in l or 'setProperty("IOFBDependentIndex"' in l]
    rest = "\n".join(l for l in body.split("\n") if l not in lines)
    i = rest.index("IOFramebuffer::start(provider)")
    j = rest.index("\n", i)
    return rest[:j] + "\n" + "\n".join(lines) + rest[j:]


old = {}
for cls, rel in FILES.items():
    for n, body in enumerate(start_bodies(src[rel], cls)):
        r = ordered(late_props(body)) if "IOFramebuffer::start(provider)" in body else None
        if r is not None:
            old[f"{cls} start path {n}"] = r
arm("negative control: the check fails on the old order (properties after start)",
    len(old) == 3 and not any(old.values()))

print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
