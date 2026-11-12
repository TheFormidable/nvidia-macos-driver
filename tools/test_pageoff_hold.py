#!/usr/bin/env python3
"""The unlocked page-off wait lets go of the accelerator lock while it waits for the GPU. While the lock is free
another thread can drop the last reference to the resource or to the accelerator, and the function then read both again:
freed memory. Both are now retained before the lock is released and let go only when the function returns. The control
is the source before that change (commit 406d9824 upstream, the parent of b1603eb here), which unlocks with no reference
held.

The wait is the default path. Before it could be, four things were fixed, and each is asserted here against the source
of NVResource::pageoff and its helpers, with v2.9 (7412cdd) as the control that each arm tells apart:
- the result of taking the lock back is tested, and nothing below runs without it;
- after the lock is back, a resource whose system memory became current while the lock was free is not copied over;
- a hold that is the last reference when the function returns is kept, and the unlocked wait stands down for the boot;
  when it already is the last one as the lock comes back, nothing is read through the resource any more;
- a lock the driver took itself (nvFamLock) is pinned, so a page-off reached from under it never lets go of it.
The switch is on by default, the sysctl only reports, and boot-arg nvaccel_pageoff_locked is read once before the
family starts."""
import pathlib, re, subprocess, sys

R = pathlib.Path(__file__).resolve().parents[1]
SRC = "kexts/NVRM/accel/nvrm-accel.cpp"
HDR = "kexts/NVRM/accel/nvaccel_lock_owner.h"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-200:]})" if detail and not ok else ""))
    fails += not ok


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def func(src, head):
    """The text of a function from its head up to the first line that is a lone closing brace."""
    if head not in src:
        return ""
    a = src.index(head)
    return src[a:src.index("\n}\n", a)]


def held(src):
    """True when pageoff retains the resource and the accelerator before the unlock and releases them at scope exit."""
    body = func(src, "void NVResource::pageoff(")
    if "nvFamUnlock(a);" not in body:
        return None
    u = body.index("nvFamUnlock(a);")
    dtor = re.search(r"~NVHold\(\) \{(.*?)\} \} holdRes, holdAccel;", body[:u])
    return ("retain(); holdRes.o = this;" in body[:u] and "a->retain(); holdAccel.o = a;" in body[:u]
            and dtor is not None and "o->release();" in dtor.group(1))


def unlocked_block(src):
    """From the unlock in pageoff to the end of the unlocked branch (the `} else {` of the locked wait)."""
    body = func(src, "void NVResource::pageoff(")
    if "nvFamUnlock(a);" not in body:
        return ""
    u = body.index("nvFamUnlock(a);")
    e = body.find("} else {", u)
    return body[u:e if e > 0 else len(body)]


def relock_tested(src):
    b = unlocked_block(src)
    m = re.search(r"if \(!nvFamLock\(a, false\)\) \{(.*?)\n            \}", b, re.S)
    return bool(m) and "return;" in m.group(1) and "nvPoStandDown(" in m.group(1) and "nvFamLock(a, false);" not in b


def revalidated(src):
    """valid[0] bit 0 is recorded before the unlock, read again after the relock, and a resource that became current
    returns before the copy (and before the eligibility re-check that would call the family's page-off)."""
    body = func(src, "void NVResource::pageoff(")
    if "nvFamUnlock(a);" not in body:
        return False
    u = body.index("nvFamUnlock(a);")
    pre, b = body[:u], unlocked_block(src)
    if not re.search(r"const bool wasCurrent = v0 && \(v0\[0\] & 1u\);", pre):
        return False
    m = re.search(r"if \(!wasCurrent && v1 && \(v1\[0\] & 1u\)\) \{[^}]*if \(out\) \*out = true; return; \}", b)
    if not m or "nvFamLock(a, false)" not in b[:m.start()]:
        return False
    after = b[m.end():]
    fam = b.find("IOAccelResource2::pageoff(", m.end())
    return "*(unsigned short *volatile *)(r + 0x30)" in b[:m.start()] and fam > 0 and "md->prepare" not in b[:m.start()] \
        and after.strip() != ""


def last_ref_kept(src):
    body = func(src, "void NVResource::pageoff(")
    m = re.search(r"~NVHold\(\) \{(.*?)\} \} holdRes, holdAccel;", body)
    if not m:
        return False
    d = m.group(1)
    k = d.find("getRetainCount() <= 1")
    s = d.find("nvPoStandDown(", k)
    r = d.find("return;", s)
    return k >= 0 and s > k and r > s and d.rfind("o->release();") > r


def orphan_not_read(src):
    """With the lock back, a resource that only this function still holds is left alone: the check stands after the
    relock and before the first read of what the resource points at (its device cache, the surface, the valid bits)."""
    b = unlocked_block(src)
    rl = b.find("if (!nvFamLock(a, false))")
    k = b.find("if (getRetainCount() <= 1) { if (out) *out = false; return; }", rl)
    reads = [b.find(t, rl) for t in ("*(void *volatile *)(r + 0xe0)", "nvres_vidmem(this)", "*(unsigned short *volatile *)(r + 0x30)")]
    return 0 <= rl < k and all(x > k for x in reads)


def pinned(src, hdr):
    lock = func(src, "static bool nvFamLock(NVAccel *a, bool tryOnly)")
    unlock = func(src, "static void nvFamUnlock(NVAccel *a)")
    b = unlocked_block(src)
    ok_lock = "a->acceleratorDidLock(\"\", 0);" in lock and "a->pinLock();" in lock \
        and lock.index("a->pinLock();") > lock.index("a->acceleratorDidLock(\"\", 0);") \
        and lock.index("a->pinLock();") < lock.index("return true;")
    ok_unlock = "a->unpinLock();" in unlock and "a->acceleratorWillUnlock(\"\", 0);" in unlock \
        and unlock.index("a->unpinLock();") < unlock.index("a->acceleratorWillUnlock(\"\", 0);")
    rl = b.find("if (!nvFamLock(a, false))")
    ok_relock = rl >= 0 and b.find("a->unpinLock();", rl) > rl
    mr = func(hdr, "static inline int nvaccel_owner_may_release(")
    ok_rule = "pinned" in mr and "== 0" in mr
    return ok_lock and ok_unlock and ok_relock and ok_rule


def default_on(src):
    h = src.find("virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE {")
    start = src[h:] if h >= 0 else ""
    o = start.find("nvPoReadOptOut();")
    f = start.find("bool r = IOGraphicsAccelerator2::start(provider);")
    rd = re.search(r"SYSCTL_INT\(_debug, OID_AUTO, nvaccel_pageoff_unlocked_wait, (CTLFLAG_[A-Z]+)", src)
    opt = func(src, "static void nvPoReadOptOut(void)\n{")
    return ("static int gPoUnlocked = 1;" in src and "gPoUnlocked = 0;" not in src.replace(opt, "")
            and rd is not None and rd.group(1) == "CTLFLAG_RD"
            and 'PE_parse_boot_argn("nvaccel_pageoff_locked"' in opt and "OSCompareAndSwap(0, 1, &once)" in opt
            and "gPoUnlocked = 0;" in opt and 0 <= o < f)


src = (R / SRC).read_text()
hdr = (R / HDR).read_text()
new = held(src)
arm("the unlocked wait is still in pageoff (the check looked at the right function)", new is not None)
arm("the resource and the accelerator are held before the lock is released, until the function returns", new is True)
old = None
for rev in ("406d9824", "b1603eb^"):
    old = git_show(rev, SRC)
    if old is not None and "nvFamUnlock(a);" in func(old, "void NVResource::pageoff("):
        break
    old = None
if old is not None:
    arm("control: the earlier source released the lock with nothing held", held(old) is False)
else:
    arm("control: the earlier source is readable", False, "neither 406d9824 nor b1603eb^ has the unlocked wait")

arm("the result of taking the lock back is tested; a failure stands down and returns", relock_tested(src))
arm("after the relock, a resource whose system memory became current is reported done and not copied over", revalidated(src))
arm("a hold that is the last reference is kept and the unlocked wait stands down", last_ref_kept(src))
arm("with the lock back, a resource that only the page-off still holds is not read through", orphan_not_read(src))
arm("the driver's own locks are pinned (nvFamLock / nvFamUnlock), the relock unpins, a pinned owner may not let go",
    pinned(src, hdr))
arm("on by default; the sysctl is read-only; boot-arg nvaccel_pageoff_locked is read once before the family starts",
    default_on(src))

v29 = git_show("7412cdd", SRC)
v29h = git_show("7412cdd", HDR)
if v29 is not None and v29h is not None:
    arm("control: v2.9 ignores the relock result", not relock_tested(v29))
    arm("control: v2.9 copies after the relock without looking at valid[0]", not revalidated(v29))
    arm("control: v2.9 releases the last reference", not last_ref_kept(v29))
    arm("control: v2.9 reads through the resource whoever still holds it", not orphan_not_read(v29))
    arm("control: v2.9 lets go of a lock its own code took", not pinned(v29, v29h))
    arm("control: v2.9 is off by default with a writable switch", not default_on(v29))
else:
    arm("control: v2.9 (7412cdd) is readable", False)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
