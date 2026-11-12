/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_layout.h - is the running macOS the one this kext was compiled for, as far as the Apple classes it subclasses go?
 *
 * The driver's own classes sit on top of Apple classes whose headers are public (IOFramebuffer) or were reconstructed
 * by hand (the accelerator family, AppleGraphicsDeviceControl). If Apple changes the size of one of those bases, the
 * driver's fields land on Apple's fields; if a virtual function is added or removed, every later vtable slot of the
 * driver's class is one off. Both used to show as a panic or a dead display some time after an update. This header
 * compares, when a kext starts, what the kext was compiled with against what the running kernel registered, and lets
 * the kext step aside with a line that names the class instead.
 *
 * What is compared, and what a difference means:
 *   class size          OSMetaClass of the name, getClassSize(), against sizeof() of the header   a difference REFUSES
 *   class not registered                                                                           counted, not fatal
 *   superclass name     getSuperClass()->getClassName() against the header's base                  counted, not fatal
 *   vtable length       the kernel's vtable against the slot count the compiler laid out           a difference REFUSES
 *   pure slots left     counted by the caller that can see its own vtable                          counted, not fatal
 * A vtable is compared only when the same comparison on IOService comes out equal on this kernel ("calibration"). It
 * relies on two facts: a vtable built with -fapple-kext ends in a null entry, and on x86_64 a pointer to a virtual
 * member function is 1 + 8 * slot. If either does not hold here, IOService shows it and no vtable is compared; sizes
 * still decide. The macOS build is written into the text and is never a reason to refuse.
 *
 * Verdict: any refusing difference -> refused, or overridden when the boot-arg nvlayout=0 is present; no class could
 * be compared -> unknown; otherwise ok. Only "refused" changes what a kext does.
 *
 * Two parts. The first has no include and no kernel call, so a host test compiles and runs it
 * (tools/test_layout_pure.cpp). The second, under KERNEL, asks the kernel and fills in the first.
 * NVRM, NVRMFB and NVRMAGDC each carry this file; the three copies are identical, and the test compares them. */
#ifndef NVRM_LAYOUT_H
#define NVRM_LAYOUT_H

/* ---- pure part ---- */

enum NvLayoutState { kNvLayoutNotRun = 0, kNvLayoutOk = 1, kNvLayoutRefused = 2, kNvLayoutOverridden = 3, kNvLayoutUnknown = 4 };
/* NvLayoutTally.firstKind: what the first refusing difference was */
enum { kNvLayoutDiffNone = 0, kNvLayoutDiffSize = 1, kNvLayoutDiffVtShort = 2, kNvLayoutDiffVtLong = 3 };
enum { kNvLayoutMaxSlots = 4096 };      /* no class has this many virtual functions: a larger count is a decoding mistake */

struct NvLayoutRow  { const char *name, *super; unsigned size; };            /* what this build was compiled for */
struct NvLayoutSeen { bool present; unsigned size; const char *super; };     /* what the running kernel registered */
struct NvLayoutVt   { const char *name; const void *const *slots; int built; };  /* slots: the kernel vtable after its 2 header words */
struct NvLayoutTally {
    unsigned classes, absent, sizeBad, superDiff;     /* rows compared / not registered / size differs / superclass name differs */
    unsigned vtables, vtBad, calibrated, pureLeft;    /* vtables compared / length differs / IOService came out equal / see nvLayoutPureLeft */
    const char *firstName; unsigned firstBuilt, firstSeen; int firstKind;    /* first refusing difference, for the text */
};

static inline bool nvLayoutStrEq(const char *a, const char *b)
{
    if (!a || !b) return a == b;
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}
static inline void nvLayoutTallyInit(NvLayoutTally *t)
{
    t->classes = t->absent = t->sizeBad = t->superDiff = 0;
    t->vtables = t->vtBad = t->calibrated = t->pureLeft = 0;
    t->firstName = nullptr; t->firstBuilt = t->firstSeen = 0; t->firstKind = kNvLayoutDiffNone;
}
static inline void nvLayoutFirst(NvLayoutTally *t, const char *name, unsigned built, unsigned seen, int kind)
{
    if (t->firstKind != kNvLayoutDiffNone) return;
    t->firstName = name; t->firstBuilt = built; t->firstSeen = seen; t->firstKind = kind;
}

/* One class: the row this build was compiled with against what the kernel registered under that name. */
static inline void nvLayoutRow(const NvLayoutRow *row, const NvLayoutSeen *seen, NvLayoutTally *t)
{
    if (!seen->present) { t->absent++; return; }
    t->classes++;
    if (seen->size != row->size) { t->sizeBad++; nvLayoutFirst(t, row->name, row->size, seen->size, kNvLayoutDiffSize); }
    if (!nvLayoutStrEq(row->super, seen->super)) t->superDiff++;
}

/* The length of a kernel vtable against the number of slots this build laid out for the same class.
 * 0 same length, -1 the running vtable ends early, 1 it has more entries, 2 not comparable (no table, or a slot count
 * that cannot be right). Reads slots[0 .. built] only, and stops at the first null entry, so it never reads past the
 * end of the kernel's table. *endsAt: the number of entries found before the null, or built + 1 when the entry after
 * the last expected one is not null. */
static inline int nvLayoutVtCmp(const void *const *slots, int built, int *endsAt)
{
    if (endsAt) *endsAt = 0;
    if (!slots || built <= 0 || built > kNvLayoutMaxSlots) return 2;
    for (int i = 0; i < built; i++)
        if (!slots[i]) { if (endsAt) *endsAt = i; return -1; }
    if (slots[built]) { if (endsAt) *endsAt = built + 1; return 1; }
    if (endsAt) *endsAt = built;
    return 0;
}
/* One vtable into the tally. A table that cannot be compared is not counted and refuses nothing. */
static inline void nvLayoutVtRow(const NvLayoutVt *vt, NvLayoutTally *t)
{
    int endsAt = 0;
    const int r = nvLayoutVtCmp(vt->slots, vt->built, &endsAt);
    if (r == 2) return;
    t->vtables++;
    if (r != 0) { t->vtBad++; nvLayoutFirst(t, vt->name, (unsigned)vt->built, (unsigned)endsAt, r < 0 ? kNvLayoutDiffVtShort : kNvLayoutDiffVtLong); }
}
/* How many of the first n slots are the pure-virtual trap in BOTH tables: a function the base declares and the
 * driver's class does not implement. Counted only; expected 0. */
static inline unsigned nvLayoutPureLeft(const void *const *own, const void *const *base, int n, const void *pure)
{
    unsigned left = 0;
    if (!own || !base || !pure || n <= 0 || n > kNvLayoutMaxSlots) return 0;
    for (int i = 0; i < n; i++) if (own[i] == pure && base[i] == pure) left++;
    return left;
}
/* The vtable slot a pointer to a virtual member function names, from its two words. x86_64 (Itanium ABI): the first
 * word is 1 + 8 * slot for a virtual function and the second, the this-adjustment, is 0 here. -1: not such a pointer. */
static inline int nvLayoutPmfSlot(unsigned long long ptr, unsigned long long adj)
{
    if (adj != 0 || (ptr & 7) != 1) return -1;
    const unsigned long long slot = (ptr - 1) / 8;
    return slot <= (unsigned long long)kNvLayoutMaxSlots ? (int)slot : -1;
}
static inline NvLayoutState nvLayoutVerdict(const NvLayoutTally *t, bool optOut)
{
    if (t->sizeBad || t->vtBad) return optOut ? kNvLayoutOverridden : kNvLayoutRefused;
    if (!t->classes) return kNvLayoutUnknown;
    return kNvLayoutOk;
}

/* The verdict as one line: the log line, and the registry property another kext reads. It starts with the state word. */
struct NvLayoutOut { char *p; unsigned cap, at; };
static inline void nvLayoutPutS(NvLayoutOut *o, const char *s)
{
    if (!s || !o->cap) return;
    while (*s && o->at + 1 < o->cap) o->p[o->at++] = *s++;
    o->p[o->at] = 0;
}
static inline void nvLayoutPutN(NvLayoutOut *o, unsigned v, unsigned base)
{
    char d[12]; int n = 0;
    do { d[n++] = "0123456789abcdef"[v % base]; v /= base; } while (v);
    if (base == 16) nvLayoutPutS(o, "0x");
    while (n) { const char c[2] = { d[--n], 0 }; nvLayoutPutS(o, c); }
}
static inline void nvLayoutPutCount(NvLayoutOut *o, unsigned v, const char *one, const char *many)
{
    nvLayoutPutN(o, v, 10); nvLayoutPutS(o, v == 1 ? one : many);
}
static inline void nvLayoutText(char *out, unsigned cap, NvLayoutState state, const NvLayoutTally *t, const char *osBuild, const char *osRelease)
{
    NvLayoutOut o = { out, cap, 0 };
    if (!out || !cap) return;
    out[0] = 0;
    if (state == kNvLayoutNotRun) { nvLayoutPutS(&o, "not run"); return; }
    if (state == kNvLayoutUnknown) nvLayoutPutS(&o, "unknown: no class could be compared");
    else if (state == kNvLayoutOk) {
        nvLayoutPutS(&o, "ok: "); nvLayoutPutCount(&o, t->classes, " class", " classes");
        if (t->calibrated) { nvLayoutPutS(&o, ", "); nvLayoutPutCount(&o, t->vtables, " vtable", " vtables"); }
        else nvLayoutPutS(&o, ", vtables not compared");
    } else {
        nvLayoutPutS(&o, state == kNvLayoutRefused ? "refused: " : "overridden (nvlayout=0): ");
        nvLayoutPutS(&o, t->firstName ? t->firstName : "?");
        if (t->firstKind == kNvLayoutDiffSize) {
            nvLayoutPutS(&o, " is "); nvLayoutPutN(&o, t->firstSeen, 16); nvLayoutPutS(&o, " bytes in this macOS, ");
            nvLayoutPutN(&o, t->firstBuilt, 16); nvLayoutPutS(&o, " in the driver");
        } else {
            nvLayoutPutS(&o, t->firstKind == kNvLayoutDiffVtLong ? " has more than " : " has ");
            nvLayoutPutN(&o, t->firstKind == kNvLayoutDiffVtLong ? t->firstBuilt : t->firstSeen, 10);
            nvLayoutPutS(&o, " virtual functions in this macOS, "); nvLayoutPutN(&o, t->firstBuilt, 10); nvLayoutPutS(&o, " in the driver");
        }
        nvLayoutPutS(&o, " ("); nvLayoutPutCount(&o, t->sizeBad, " size", " sizes"); nvLayoutPutS(&o, ", ");
        nvLayoutPutCount(&o, t->vtBad, " vtable", " vtables"); nvLayoutPutS(&o, " differ of ");
        nvLayoutPutN(&o, t->classes, 10); nvLayoutPutS(&o, ", "); nvLayoutPutN(&o, t->vtables, 10); nvLayoutPutS(&o, ")");
    }
    if (state != kNvLayoutUnknown) {
        if (t->absent) { nvLayoutPutS(&o, ", "); nvLayoutPutN(&o, t->absent, 10); nvLayoutPutS(&o, " not registered"); }
        if (t->superDiff) { nvLayoutPutS(&o, ", "); nvLayoutPutCount(&o, t->superDiff, " base name differs", " base names differ"); }
        if (t->pureLeft) { nvLayoutPutS(&o, ", "); nvLayoutPutCount(&o, t->pureLeft, " pure slot", " pure slots"); }
    }
    if ((osBuild && osBuild[0]) || (osRelease && osRelease[0])) {
        nvLayoutPutS(&o, "; macOS "); nvLayoutPutS(&o, osBuild && osBuild[0] ? osBuild : "?");
        if (osRelease && osRelease[0]) { nvLayoutPutS(&o, " (Darwin "); nvLayoutPutS(&o, osRelease); nvLayoutPutS(&o, ")"); }
    }
}
/* The state a text of nvLayoutText says: what a kext reads back from another kext's property. Anything that is not
 * one of the four texts, a missing property included, is kNvLayoutNotRun. */
static inline bool nvLayoutTextStarts(const char *text, const char *word)
{
    if (!text) return false;
    while (*word) if (*text++ != *word++) return false;
    return *text == ':' || *text == ' ';
}
static inline NvLayoutState nvLayoutTextState(const char *text)
{
    if (nvLayoutTextStarts(text, "ok")) return kNvLayoutOk;
    if (nvLayoutTextStarts(text, "refused")) return kNvLayoutRefused;
    if (nvLayoutTextStarts(text, "overridden")) return kNvLayoutOverridden;
    if (nvLayoutTextStarts(text, "unknown")) return kNvLayoutUnknown;
    return kNvLayoutNotRun;
}
static inline bool nvLayoutTextRefused(const char *text) { return nvLayoutTextState(text) == kNvLayoutRefused; }

/* ---- kernel part ---- */
#ifdef KERNEL
#include <libkern/c++/OSMetaClass.h>
#include <libkern/c++/OSSymbol.h>
#include <libkern/OSAtomic.h>
#include <libkern/version.h>
#include <IOKit/IOLib.h>
#include <pexpert/pexpert.h>
extern "C" int sysctlbyname(const char *, void *, size_t *, void *, size_t);

/* A row of the class table: the class, and the base the header gives it. The names are string literals. */
#define NVL_ROW(Cls, Super)  { #Cls, #Super, (unsigned)sizeof(Cls) }
/* The number of vtable slots this build laid out for Cls, with no symbol reference and no object: a struct that adds
 * one virtual function to Cls is declared and never instantiated, and the slot of that function is the count.
 * NVL_TAIL(Cls) once at file scope, NVL_BUILT(Cls) where the count is needed. NVL_BUILT is not a constant expression:
 * build the tables as local arrays inside the function that evaluates, not as file-scope data. */
#define NVL_TAIL(Cls)        struct NvLayoutTail_##Cls : Cls { virtual void nvLayoutTailFn(); }
#define NVL_BUILT(Cls)       nvLayoutTailSlot(&NvLayoutTail_##Cls::nvLayoutTailFn)
/* The kernel's vtable of Cls. sym is the symbol written out, "__ZTV" + length + name ("__ZTV22IOGraphicsAccelerator2").
 * Name only vtables the kext imports already (the base of one of its own classes): a new name is a new import, and a
 * mistyped one stops the kext from loading. */
#define NVL_VTABLE(Cls, sym) extern const void *const nvLayoutVt_##Cls[] __asm(sym)
/* An entry of the vtable table, after NVL_TAIL(Cls) and NVL_VTABLE(Cls, ...) */
#define NVL_VT(Cls)          { #Cls, nvLayoutVt_##Cls + 2, NVL_BUILT(Cls) }

template <class T> static inline int nvLayoutTailSlot(void (T::*pmf)())
{
    union { void (T::*p)(); unsigned long long w[2]; } u;
    u.w[0] = u.w[1] = 0; u.p = pmf;
    return nvLayoutPmfSlot(u.w[0], u.w[1]);
}

struct NvLayoutRun { NvLayoutState state; NvLayoutTally tally; char text[200]; };

/* Compare and decide. Thread context (a probe or start routine, or a worker thread): it looks classes up by name,
 * which takes and drops the class registry's lock, reads a boot-arg and a sysctl, and logs one line with `kext` in
 * front; call it with no lock held. It allocates nothing that outlives the call and starts nothing.
 *   rows, nRows                   the class table (NVL_ROW)
 *   vts, nVts                     the vtable table (NVL_VT); may be empty
 *   ioServiceSlots, ioServiceBuilt  the calibration: nvLayoutVt_IOService + 2 and NVL_BUILT(IOService), or nullptr
 *                                 and 0 for a kext that compares no vtable
 * On return run->state is the verdict, run->tally the counts and run->text the line, which the caller publishes as a
 * registry property. tally.pureLeft is left 0: a caller that counts pure slots (nvLayoutPureLeft) keeps that number
 * itself. What the caller does with the verdict:
 *   kNvLayoutRefused      step aside the way the kext already does when it may not run; construct nothing more
 *   every other state     carry on exactly as before */
static inline void nvLayoutEvaluate(NvLayoutRun *run, const char *kext, const NvLayoutRow *rows, unsigned nRows,
                                    const NvLayoutVt *vts, unsigned nVts, const void *const *ioServiceSlots, int ioServiceBuilt)
{
    NvLayoutTally *t = &run->tally;
    nvLayoutTallyInit(t);
    for (unsigned i = 0; rows && i < nRows; i++) {
        NvLayoutSeen seen = { false, 0, nullptr };
        const OSSymbol *sym = OSSymbol::withCStringNoCopy(rows[i].name);
        const OSMetaClass *meta = sym ? OSMetaClass::getMetaClassWithName(sym) : nullptr;
        if (sym) sym->release();
        if (meta) {
            const OSMetaClass *base = meta->getSuperClass();
            seen.present = true; seen.size = meta->getClassSize(); seen.super = base ? base->getClassName() : nullptr;
        }
        nvLayoutRow(&rows[i], &seen, t);
    }
    /* the method is trusted on this kernel only if it gives the right answer for a class that cannot have changed
       under us: the kext was linked against this IOService */
    t->calibrated = nvLayoutVtCmp(ioServiceSlots, ioServiceBuilt, nullptr) == 0 ? 1 : 0;
    for (unsigned i = 0; t->calibrated && vts && i < nVts; i++) nvLayoutVtRow(&vts[i], t);

    uint32_t arg = 1;
    const bool optOut = PE_parse_boot_argn("nvlayout", &arg, sizeof arg) && arg == 0;
    run->state = nvLayoutVerdict(t, optOut);

    char build[32]; size_t len = sizeof build - 1;
    build[0] = 0;
    if (sysctlbyname("kern.osversion", build, &len, nullptr, 0) != 0) build[0] = 0;
    build[sizeof build - 1] = 0;
    nvLayoutText(run->text, sizeof run->text, run->state, t, build, osrelease);
    IOLog("%s: Apple class layout %s\n", kext ? kext : "NVRM", run->text);
}

/* The same, once per kext, for a kext that asks from more than one place or thread. `once` is static storage,
 * zero-initialised; the first caller evaluates and the others wait for it (1 ms steps, 5 s at most) and read its
 * result. After the first evaluation once->run never changes.
 * Returns the verdict. kNvLayoutNotRun means the first caller had not finished within the bound, which cannot happen
 * unless that thread is stuck; treat it like every state that is not kNvLayoutRefused and do not read once->run. */
struct NvLayoutOnce { volatile UInt32 word; NvLayoutRun run; };     /* word: 0 not run, 1 running, 2 done */
static inline NvLayoutState nvLayoutEvaluateOnce(NvLayoutOnce *once, const char *kext, const NvLayoutRow *rows, unsigned nRows,
                                                 const NvLayoutVt *vts, unsigned nVts, const void *const *ioServiceSlots, int ioServiceBuilt)
{
    if (OSCompareAndSwap(0, 1, &once->word)) {
        nvLayoutEvaluate(&once->run, kext, rows, nRows, vts, nVts, ioServiceSlots, ioServiceBuilt);
        OSCompareAndSwap(1, 2, &once->word);
        return once->run.state;
    }
    for (unsigned ms = 0; once->word != 2 && ms < 5000; ms++) IOSleep(1);
    return once->word == 2 ? once->run.state : kNvLayoutNotRun;
}
#endif /* KERNEL */

#endif
