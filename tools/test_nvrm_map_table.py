"""NVRM's index of kernel mappings has no capacity, never hands out a mapping it does not track, and answers its lookups
from the interval trees under a leaf lock.

Field logs from an RTX 3050 and an RTX 5070 showed 'high water 1024/1024' after about 20 minutes: every later surface had
no physical address and was refused. The fixed table then grew to 8192 entries, but a mapping that still did not fit
was returned untracked (never released), and every lookup was a scan. Now each mapping is an entry of its own in two
interval trees (kexts/NVRM/nvrm_itree.h, compared operation by operation with the old table in tools/test_nvrm_itree.c).
This file checks the wiring in os-xnu.cpp: the entry is allocated before anything is mapped, each function does its
work with the tree under the simple lock, and nothing that can block or log runs while that lock is held."""
import os, re, subprocess, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = "kexts/NVRM/os-xnu.cpp"
LOCK = "IOSimpleLockLockDisableInterrupt(nv_xnu_maps_slock)"
UNLOCK = "IOSimpleLockUnlockEnableInterrupt(nv_xnu_maps_slock, s)"
# what each function must do with the trees, inside the lock
TREE_CALLS = {
    "nv_xnu_map_insert": ["nvit_insert(&nv_xnu_map_by_va", "nvit_insert(&nv_xnu_map_by_pa"],
    "nv_xnu_phys_for_kernel_va": ["nvit_stab(&nv_xnu_map_by_va"],
    "os_unmap_kernel_space": ["nvit_first_lo(&nv_xnu_map_by_va", "nvit_next(", "nvit_remove(&nv_xnu_map_by_va",
                              "nvit_remove(&nv_xnu_map_by_pa"],
    "nv_xnu_kmap_overlap": ["nvit_overlap(&nv_xnu_map_by_pa"],
}
NOT_UNDER_LOCK = ("IOLog", "kprintf", "kern_os_malloc", "kern_os_free", "IOLock", "->release()", "::with", "->map(",
                  "nv_pm_alias", "IOSleep")


def body(text, fn):
    # Anchor on the DEFINITION, which starts a line with its return type: a comment that names the function must not
    # be read as its body.
    d = re.search(r"^[A-Za-z_][^\n/;]*\b%s\s*\([^;]*?\)\s*\n?\{" % re.escape(fn), text, re.M)
    if not d:
        return None
    b = text[d.start():]
    return b[:b.find("\n}\n")]


def problems(text):
    out = []
    if re.search(r"#define NV_XNU_MAX_MAPS\b", text) or re.search(r"\bnv_xnu_maps\s*\[", text):
        out.append("a fixed-size mapping table is still there")
    if "FULL" in text and "nv_xnu_maps" in text and re.search(r"nv_xnu_maps FULL", text):
        out.append("a mapping can still be handed out untracked (the FULL branch)")
    if "static IOSimpleLock *nv_xnu_maps_slock" not in text or "nv_xnu_maps_slock = IOSimpleLockAlloc()" not in text:
        out.append("the index has no simple lock allocated by nv_xnu_maps_init")
    for fn, calls in TREE_CALLS.items():
        b = body(text, fn)
        if b is None:
            out.append(fn + " has no definition in this file")
            continue
        if LOCK not in b or UNLOCK not in b:
            out.append(fn + " does not take and drop the index lock")
            continue
        locked = b[b.find(LOCK):b.rfind(UNLOCK)]
        for c in calls:
            if c not in locked:
                out.append("%s does not call %s under the lock" % (fn, c.split("(")[0]))
        for bad in NOT_UNDER_LOCK:
            if bad in locked:
                out.append("%s calls %s with the index lock held" % (fn, bad))
    m = body(text, "os_map_kernel_space")
    if m is None:
        out.append("os_map_kernel_space has no definition in this file")
    else:
        a = m.find("kern_os_malloc(sizeof *m)")
        firsts = [i for i in (m.find("nv_pm_alias("), m.find("withPhysicalAddress(")) if i >= 0]
        if a < 0 or not firsts or a > min(firsts):
            out.append("os_map_kernel_space maps before it has the entry that tracks the mapping")
        if m.count("nv_xnu_map_insert(") != 2:
            out.append("os_map_kernel_space does not track both the alias and the real mapping")
        if "return va;" in m and "nv_xnu_map_insert(m, md, map, va" not in m:
            out.append("os_map_kernel_space returns a mapping it did not put in the index")
    u = body(text, "os_unmap_kernel_space")
    if u is not None and u.find("kern_os_free(m)") < u.rfind(UNLOCK):
        out.append("os_unmap_kernel_space frees the entry before it has dropped the lock")
    return out


class MapTable(unittest.TestCase):
    def test_index_is_unbounded_and_locked(self):
        with open(os.path.join(ROOT, SRC)) as f:
            self.assertEqual(problems(f.read()), [])

    def test_negative_control_the_fixed_table_fails(self):
        # the last revision with the fixed table of 8192 entries
        old = subprocess.run(["git", "-C", ROOT, "show", "4aea73c:" + SRC], capture_output=True, text=True, check=True).stdout
        found = problems(old)
        self.assertIn("a fixed-size mapping table is still there", found)
        self.assertIn("a mapping can still be handed out untracked (the FULL branch)", found)
        self.assertTrue(any("does not take and drop the index lock" in p for p in found), found)

    def test_negative_control_each_defect_is_seen(self):
        with open(os.path.join(ROOT, SRC)) as f:
            cur = f.read()
        cases = [
            # a log line moved inside the locked section of a lookup
            (("    struct nvit_node *n = nvit_stab(", "    kprintf(\"x\");\n    struct nvit_node *n = nvit_stab("),
             "nv_xnu_phys_for_kernel_va calls kprintf with the index lock held"),
            # the overlap check asked of the VA tree
            (("nvit_overlap(&nv_xnu_map_by_pa", "nvit_overlap(&nv_xnu_map_by_va"),
             "nv_xnu_kmap_overlap does not call nvit_overlap under the lock"),
            # an unmap that forgets the physical tree
            (("        if (!m->alias) nvit_remove(&nv_xnu_map_by_pa, &m->pa_node);\n", ""),
             "os_unmap_kernel_space does not call nvit_remove under the lock"),
            # the entry allocated after the mapping is made
            (("    nv_xnu_map *m = (nv_xnu_map *)kern_os_malloc(sizeof *m);\n", "    nv_xnu_map *m = NULL;\n"),
             "os_map_kernel_space maps before it has the entry that tracks the mapping"),
        ]
        for (old, new), want in cases:
            self.assertEqual(cur.count(old), 1, old)
            self.assertIn(want, problems(cur.replace(old, new)), want)


if __name__ == "__main__":
    unittest.main()
