/* test_layout_pure.cpp - the decisions of kexts/NVRM/nvrm_layout.h (does the running macOS match the Apple classes a
   kext was compiled for?), on the host: class rows, vtable lengths, the member-pointer decode, the verdict, the text
   a kext publishes and another kext reads back, and that the three copies of the header are the same file.
   Build and run from the repository root:
     c++ -std=c++17 -Wall -Wextra -Werror -I kexts/NVRM tools/test_layout_pure.cpp -o /tmp/test_layout_pure && /tmp/test_layout_pure
   A wrong "refused" here means no accelerator, or no picture, on a machine that is fine; a wrong "ok" means the old
   panic. Every group below has a control that shows the check can come out the other way. */
#include <cstdio>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
#include "nvrm_layout.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

static NvLayoutTally tally() { NvLayoutTally t; nvLayoutTallyInit(&t); return t; }
static const char *text(NvLayoutState s, const NvLayoutTally &t, const char *build = "24G90", const char *rel = "24.6.0")
{
    static char buf[200];
    nvLayoutText(buf, sizeof buf, s, &t, build, rel);
    return buf;
}

/* real classes for the member-pointer decode: Base has three virtual functions, so a function added by a struct
   derived from it is slot 3 */
struct Base { virtual void a(); virtual void b() = 0; virtual void c(); int x; };
struct Tail : Base { virtual void tailFn(); };
struct Plain { void notVirtual(); };
void Plain::notVirtual() {}
template <class C> static int slotOf(void (C::*pmf)())
{
    union { void (C::*p)(); unsigned long long w[2]; } u;
    u.w[0] = u.w[1] = 0; u.p = pmf;
    return nvLayoutPmfSlot(u.w[0], u.w[1]);
}

static bool sameFile(const char *a, const char *b)
{
    static char x[1 << 16], y[1 << 16];
    FILE *fa = fopen(a, "rb"), *fb = fopen(b, "rb");
    const size_t na = fa ? fread(x, 1, sizeof x, fa) : 0, nb = fb ? fread(y, 1, sizeof y, fb) : 0;
    if (fa) fclose(fa);
    if (fb) fclose(fb);
    return fa && fb && na > 0 && na < sizeof x && na == nb && !memcmp(x, y, na);
}

int main(int argc, char **argv)
{
    /* class rows */
    const NvLayoutRow row = { "IOGraphicsAccelerator2", "IOAccelerator", 0xdd8 };
    NvLayoutTally t = tally();
    NvLayoutSeen same = { true, 0xdd8, "IOAccelerator" };
    nvLayoutRow(&row, &same, &t);
    T("a class of the size the driver was built for is compared and refuses nothing", t.classes == 1 && !t.sizeBad && !t.superDiff && nvLayoutVerdict(&t, false) == kNvLayoutOk);
    T("its text", !strcmp(text(kNvLayoutOk, t), "ok: 1 class, vtables not compared; macOS 24G90 (Darwin 24.6.0)"));
    t = tally();
    NvLayoutSeen grew = { true, 0xde8, "IOAccelerator" };
    nvLayoutRow(&row, &grew, &t);
    T("a class that grew refuses", t.sizeBad == 1 && nvLayoutVerdict(&t, false) == kNvLayoutRefused && t.firstKind == kNvLayoutDiffSize);
    T("and the text names the class and both sizes",
      !strcmp(text(kNvLayoutRefused, t, "25B78", "25.1.0"),
              "refused: IOGraphicsAccelerator2 is 0xde8 bytes in this macOS, 0xdd8 in the driver (1 size, 0 vtables differ of 1, 0); macOS 25B78 (Darwin 25.1.0)"));
    t = tally();
    NvLayoutSeen shrank = { true, 0xdd0, "IOAccelerator" };
    nvLayoutRow(&row, &shrank, &t);
    T("a class that shrank refuses", t.sizeBad == 1 && nvLayoutVerdict(&t, false) == kNvLayoutRefused && t.firstSeen == 0xdd0 && t.firstBuilt == 0xdd8);
    t = tally();
    NvLayoutSeen gone = { false, 0, nullptr };
    nvLayoutRow(&row, &gone, &t);
    T("a class the kernel has not registered is counted and refuses nothing", t.absent == 1 && t.classes == 0 && !t.sizeBad);
    T("with nothing compared the verdict is unknown, not ok", nvLayoutVerdict(&t, false) == kNvLayoutUnknown && nvLayoutVerdict(&t, true) == kNvLayoutUnknown);
    t = tally();
    NvLayoutSeen otherBase = { true, 0xdd8, "IOService" };
    nvLayoutRow(&row, &otherBase, &t);
    NvLayoutSeen noBase = { true, 0xdd8, nullptr };
    nvLayoutRow(&row, &noBase, &t);
    T("a different base name is counted and refuses nothing", t.superDiff == 2 && t.classes == 2 && nvLayoutVerdict(&t, false) == kNvLayoutOk);
    T("and the text says so", strstr(text(kNvLayoutOk, t), ", 2 base names differ; macOS") != nullptr);
    t = tally();
    nvLayoutRow(&row, &same, &t); nvLayoutRow(&row, &grew, &t);
    const NvLayoutRow row2 = { "IOAccelResource2", "IOAccelMemory", 0x100 };
    NvLayoutSeen grew2 = { true, 0x108, "IOAccelMemory" };
    nvLayoutRow(&row2, &grew2, &t);
    T("the first difference is the one named; all are counted", t.sizeBad == 2 && t.classes == 3 && !strcmp(t.firstName, "IOGraphicsAccelerator2"));

    /* vtable lengths. A kernel vtable is its slots and then a null entry. */
    static const int fn = 0;
    const void *const f = &fn;
    const void *const vt5[] = { f, f, f, f, f, nullptr, f, f };
    int ends = -7;
    T("a vtable with the slots the driver was built for", nvLayoutVtCmp(vt5, 5, &ends) == 0 && ends == 5);
    T("a running vtable that ends early", nvLayoutVtCmp(vt5, 7, &ends) == -1 && ends == 5);
    T("a running vtable with more entries", nvLayoutVtCmp(vt5, 4, &ends) == 1 && ends == 5);
    T("no table, or a slot count that cannot be right, is not comparable",
      nvLayoutVtCmp(nullptr, 5, &ends) == 2 && nvLayoutVtCmp(vt5, 0, &ends) == 2 && nvLayoutVtCmp(vt5, -1, &ends) == 2 && nvLayoutVtCmp(vt5, kNvLayoutMaxSlots + 1, &ends) == 2);
    T("the result does not need the out parameter", nvLayoutVtCmp(vt5, 5, nullptr) == 0 && nvLayoutVtCmp(vt5, 7, nullptr) == -1);
    /* it must not read past slots[built]: the table sits at the very end of a mapping and the next page is not readable */
    const long pg = sysconf(_SC_PAGESIZE);
    char *map = (char *)mmap(nullptr, (size_t)pg * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    bool guarded = map != MAP_FAILED && mprotect(map + pg, (size_t)pg, PROT_NONE) == 0;
    if (guarded) {
        const void **end = (const void **)(map + pg);
        for (int i = 1; i <= 6; i++) end[-i] = f;
        end[-1] = nullptr;                                   /* five slots and the terminator, flush against the guard page */
        T("reads slots[0 .. built] and nothing after it (the next page is unreadable)", nvLayoutVtCmp(end - 6, 5, &ends) == 0 && ends == 5);
        end[-1] = f;
        T("the same at the page end when the running table is longer", nvLayoutVtCmp(end - 6, 5, &ends) == 1);
        end[-4] = nullptr;
        T("a table that ends early is not read past its end", nvLayoutVtCmp(end - 6, 5, &ends) == -1 && ends == 2);
    } else printf("skip the guard-page arms: no mapping\n");
    t = tally();
    const NvLayoutVt okVt = { "IOFramebuffer", vt5, 5 }, shortVt = { "IOAccelResource2", vt5, 7 }, longVt = { "IOAccelVidMemory", vt5, 4 }, noVt = { "IOService", nullptr, 5 };
    nvLayoutVtRow(&okVt, &t); nvLayoutVtRow(&noVt, &t);
    T("an equal vtable is counted; one that cannot be compared is not", t.vtables == 1 && !t.vtBad);
    nvLayoutVtRow(&shortVt, &t); nvLayoutVtRow(&longVt, &t);
    T("a shorter and a longer vtable both refuse", t.vtables == 3 && t.vtBad == 2 && t.firstKind == kNvLayoutDiffVtShort && nvLayoutVerdict(&t, false) == kNvLayoutRefused);
    T("the text for a vtable that ends early",
      !strcmp(text(kNvLayoutRefused, t), "refused: IOAccelResource2 has 5 virtual functions in this macOS, 7 in the driver (0 sizes, 2 vtables differ of 0, 3); macOS 24G90 (Darwin 24.6.0)"));
    t = tally();
    nvLayoutVtRow(&longVt, &t);
    T("the text for a vtable with more entries", strstr(text(kNvLayoutRefused, t), "refused: IOAccelVidMemory has more than 4 virtual functions in this macOS, 4 in the driver (0 sizes, 1 vtable differ") != nullptr);

    /* pure slots */
    static const int trap = 0;
    const void *const p = &trap;
    const void *const own[] = { f, p, f, p, f };
    const void *const base[] = { f, p, p, f, f };
    T("pure slots left: only where both tables hold the trap", nvLayoutPureLeft(own, base, 5, p) == 1 && nvLayoutPureLeft(own, own, 5, p) == 2);
    T("pure slots left: nothing to compare is zero", nvLayoutPureLeft(nullptr, base, 5, p) == 0 && nvLayoutPureLeft(own, base, 0, p) == 0 && nvLayoutPureLeft(own, base, 5, nullptr) == 0);

    /* the member-pointer decode */
    T("the decode of 1 + 8 * slot", nvLayoutPmfSlot(1, 0) == 0 && nvLayoutPmfSlot(1 + 8 * 350, 0) == 350 && nvLayoutPmfSlot(1 + 8ull * kNvLayoutMaxSlots, 0) == kNvLayoutMaxSlots);
    T("what is not a virtual member pointer decodes to -1",
      nvLayoutPmfSlot(0, 0) == -1 && nvLayoutPmfSlot(8, 0) == -1 && nvLayoutPmfSlot(9, 16) == -1 && nvLayoutPmfSlot(0xffffff8000a1b2c1ull, 0) == -1
      && nvLayoutPmfSlot(1 + 8ull * (kNvLayoutMaxSlots + 1), 0) == -1);
#if defined(__x86_64__) && (defined(__GNUC__) || defined(__clang__))
    T("on this x86_64 compiler a function added by a derived struct decodes to the number of slots of its base", slotOf(&Tail::tailFn) == 3);
    T("and the slots of the base decode in order", slotOf(&Base::a) == 0 && slotOf(&Base::b) == 1 && slotOf(&Base::c) == 2);
    T("control: a member function that is not virtual does not decode to a slot", slotOf(&Plain::notVirtual) == -1);
#else
    printf("skip the decode against real classes: not an x86_64 build\n");
    (void)&slotOf<Base>;
#endif

    /* verdict */
    t = tally(); t.classes = 24; t.vtables = 5; t.calibrated = 1;
    T("nothing differs: ok, with or without the opt-out", nvLayoutVerdict(&t, false) == kNvLayoutOk && nvLayoutVerdict(&t, true) == kNvLayoutOk);
    T("the ok text", !strcmp(text(kNvLayoutOk, t), "ok: 24 classes, 5 vtables; macOS 24G90 (Darwin 24.6.0)"));
    t.sizeBad = 1;
    T("a size differs: refused, or overridden with nvlayout=0", nvLayoutVerdict(&t, false) == kNvLayoutRefused && nvLayoutVerdict(&t, true) == kNvLayoutOverridden);
    t.sizeBad = 0; t.vtBad = 1;
    T("a vtable differs: refused, or overridden with nvlayout=0", nvLayoutVerdict(&t, false) == kNvLayoutRefused && nvLayoutVerdict(&t, true) == kNvLayoutOverridden);
    t.vtBad = 0; t.absent = 3; t.superDiff = 9; t.pureLeft = 2;
    T("control: what is only counted never refuses", nvLayoutVerdict(&t, false) == kNvLayoutOk);

    /* the text another kext reads back */
    t = tally(); t.classes = 3; t.sizeBad = 1; nvLayoutFirst(&t, "IOFramebuffer", 0x1000, 0x1010, kNvLayoutDiffSize);
    static const NvLayoutState states[] = { kNvLayoutOk, kNvLayoutRefused, kNvLayoutOverridden, kNvLayoutUnknown };
    bool round = true;
    for (NvLayoutState s : states) if (nvLayoutTextState(text(s, t)) != s || nvLayoutTextRefused(text(s, t)) != (s == kNvLayoutRefused)) round = false;
    T("every verdict's text reads back as that verdict, and only a refusal reads as refused", round);
    T("an overridden text keeps the detail", strstr(text(kNvLayoutOverridden, t), "overridden (nvlayout=0): IOFramebuffer is 0x1010 bytes in this macOS, 0x1000 in the driver") != nullptr);
    T("the unknown text", !strcmp(text(kNvLayoutUnknown, t, "", ""), "unknown: no class could be compared"));
    T("control: a missing property, an empty one and other words are not a verdict",
      nvLayoutTextState(nullptr) == kNvLayoutNotRun && nvLayoutTextState("") == kNvLayoutNotRun && nvLayoutTextState("okay") == kNvLayoutNotRun
      && nvLayoutTextState("refusedx: y") == kNvLayoutNotRun && !nvLayoutTextRefused("not refused: z") && nvLayoutTextState(text(kNvLayoutNotRun, t)) == kNvLayoutNotRun);
    char small[24];
    memset(small, 'Z', sizeof small);
    nvLayoutText(small, 16, kNvLayoutRefused, &t, "24G90", "24.6.0");
    T("a short buffer is filled, terminated and not overrun", strlen(small) == 15 && small[16] == 'Z' && nvLayoutTextRefused(small));
    nvLayoutText(small, 0, kNvLayoutRefused, &t, "24G90", "24.6.0");
    T("a buffer of no size is left alone", small[0] == 'r');

    /* the three copies */
    const char *root = argc > 1 ? argv[1] : ".";
    char a[512], b[512], c[512];
    snprintf(a, sizeof a, "%s/kexts/NVRM/nvrm_layout.h", root);
    snprintf(b, sizeof b, "%s/kexts/NVRMFB/nvrm_layout.h", root);
    snprintf(c, sizeof c, "%s/kexts/NVRMAGDC/nvrm_layout.h", root);
    T("the copies in NVRM, NVRMFB and NVRMAGDC are the same file (run from the repository root, or pass it)", sameFile(a, b) && sameFile(a, c));
    T("control: the comparison tells two different files apart", !sameFile(a, __FILE__) && !sameFile(a, "kexts/NVRM/no-such-file.h"));

    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails != 0;
}
