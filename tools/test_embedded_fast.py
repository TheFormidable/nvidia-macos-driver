#!/usr/bin/env python3
"""The per-draw lookups of a render pipeline's functions and of what they embed are made once.

nvmtlBindSamplerPipeline read the pipeline's vertex and fragment functions through two descriptor getters on every
draw, and nvmtl_bind_embedded then looked up two lists per function (its constexpr samplers and its embedded
arguments), each through an associated object. Both lists are computed once per function and kept, and the
descriptor is a copy nothing changes, so:
  the pipeline keeps the two functions in ivars, set where its descriptor is set (the only place);
  a function whose two lists are empty is marked so, and its later binds do what the full path does for two empty
  lists, which is only giving back parked sampler slots (nvmtl_vk_parked_samplers_keep with an empty mask).
This checks the sources for each step of that argument. No behaviour changes, so there is no switch.
Usage: test_embedded_fast.py [driver checkout]"""
import pathlib, re, sys

R = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
P = R / "plugin"
emb = (P / "NVMTLEmbedded.m").read_text()
objs = (P / "NVMTLObjects.m").read_text()
objh = (P / "NVMTLObjects.h").read_text()
dev = (P / "NVMTLDevice.m").read_text()
allm = "".join(f.read_text(errors="replace") for f in sorted(P.glob("*.m")))
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def block(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j, depth = src.index("{", i) + 1, 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[j], 0); j += 1
    return src[i:j]


be = block(emb, "void nvmtl_bind_embedded(NVMTLCommandBuffer *cb, uint32_t set, NVMTLFunction *fn, NVMTLBuffer *__strong const *bufAt, const NSUInteger *offAt)")
fast = "if (__atomic_load_n(&fn->_embState, __ATOMIC_RELAXED) == 1) { nvmtl_vk_parked_samplers_keep(&cb->_c, set, 0); return; }"
store = "__atomic_store_n(&fn->_embState, (uint8_t)(statics.count || embedded.count ? 2 : 1), __ATOMIC_RELAXED);"
arm("a function with nothing to bind gives back the parked sampler slots and returns, before any lookup",
    fast in be and be.index("if (!cb || !fn) return;") < be.index(fast) < be.index("nvmtl_static_samplers(fn)"))
arm("the mark is written once, right after both lists are known, from their counts",
    be.count("_embState") == 2 and be.index("nvmtl_embedded_plan(fn);") < be.index(store) < be.index("uint32_t keep = 0;"))
arm("nothing else writes the mark", len(re.findall(r"_embState", allm)) == 2 and "uint8_t _embState;" in objh)
# what the full path does with two empty lists: keep stays 0, the keep call, two loops over nothing, then the return
seg = be[be.index(store) + len(store):be.index("if (!embedded.count) return;")]
calls = re.findall(r"\b(nvmtl_\w+)\(", seg)
arm("with two empty lists the full path makes only the keep call with mask 0 before it returns",
    "uint32_t keep = 0;" in seg and "nvmtl_vk_parked_samplers_keep(&cb->_c, set, keep);" in seg
    and seg.count("for (NSArray *pair in statics)") == 2 and seg.count("for (NVMTLEmbeddedPlan *plan in embedded)") == 1
    and seg.index("nvmtl_vk_parked_samplers_keep(") < seg.rindex("for (NSArray *pair in statics)")
    and set(calls) <= {"nvmtl_vk_parked_samplers_keep", "nvmtl_retain_resource", "nvmtl_vk_bind_parked_sampler", "nvlog"}, calls)
arm("the static sampler setting is read once per process (so a function's answer cannot change with it)",
    "static int on = -1;" in block(emb, "static int nvmtl_static_samplers_on(void)"))
for key, fn in (("NVMTL_SSAMP_KEY", "static NSArray<NSArray *> *nvmtl_static_samplers(NVMTLFunction *fn)"),
                ("&nvmtl_embedded_plan_key", "static NSArray<NVMTLEmbeddedPlan *> *nvmtl_embedded_plan(NVMTLFunction *fn) {")):
    body = block(emb, fn)
    arm(f"the list under {key.lstrip('&')} is set once, where it is computed, and never replaced",
        allm.count(f"objc_setAssociatedObject(fn, {key},") == 1 and f"objc_setAssociatedObject(fn, {key}," in body
        and body.count(f"objc_getAssociatedObject(fn, {key});") == 2 and allm.count(f"objc_getAssociatedObject(fn, {key});") == 2)

arm("the pipeline's descriptor is set in one place, and its two functions are read right there",
    len(re.findall(r"->_descriptor\s*=", allm)) == 1
    and "ps->_vdesc = vdesc; ps->_descriptor = [d copy];\n    ps->_vfn = ps->_descriptor.vertexFunction; ps->_ffn = ps->_descriptor.fragmentFunction;" in dev)
arm("nothing changes the descriptor's functions afterwards",
    not re.search(r"_descriptor\.(vertex|fragment)Function\s*=|_descriptor set(Vertex|Fragment)Function", allm))
arm("the ivars are strong and written only there", "@public id<MTLFunction> _vfn, _ffn; }" in objh
    and len(re.findall(r"->_vfn\s*=", allm)) == 1 and len(re.findall(r"->_ffn\s*=", allm)) == 1)
bsp = block(objs, "- (BOOL)nvmtlBindSamplerPipeline {")
arm("a draw binds the embedded arguments of the functions the ivars hold, with no descriptor getter",
    "nvmtl_bind_embedded(_cb, NVMTL_SET_VERTEX,   (NVMTLFunction *)_ps->_vfn, _vbufAt, _vbufOff);" in bsp
    and "nvmtl_bind_embedded(_cb, NVMTL_SET_FRAGMENT, (NVMTLFunction *)_ps->_ffn, _fbufAt, _fbufOff);" in bsp
    and "_descriptor." not in bsp)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
