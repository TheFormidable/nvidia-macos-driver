/* test_dma_policy.c: the DMA layer's decisions (kexts/NVRM/nvrm_dma_policy.h), on the host, and the lifetime rule a
 * mapping relies on.
 *   cc -std=c11 -Wall -Wextra -Werror -I kexts/NVRM tools/test_dma_policy.c -o /tmp/test_dma_policy && /tmp/test_dma_policy
 *
 * Negative controls: wrong policies (an opt-out that gives raw addresses with a mapper, a page-off check that lets a
 * raw list through, a mapping that does not hold its own reference) are run through the same checks and must fail
 * them. */
#include <stdio.h>
#include <string.h>
#include "nvrm_dma_policy.h"

static int fails;
static void arm(const char *name, int ok) { printf("%s %s\n", ok ? "ok  " : "FAIL", name); fails += !ok; }

/* ---- the mode decision, as a property over every input ---- */
typedef enum nvrm_dma_mode (*decide_fn)(int, int, int, int);
/* The rules that make the decision safe: no mapper is 2.9 whatever the switch; a mapper the driver may use is used;
 * with a mapper and the switch the GPU is refused, never left on raw addresses; the untranslated mark is believed
 * unless IOKit gave the GPU a mapper of its own. Returns the number of input combinations that break a rule. */
static int decide_violations(decide_fn f)
{
    int bad = 0, o, dm, sm, by;
    for (o = 0; o < 2; o++) for (dm = 0; dm < 2; dm++) for (sm = 0; sm < 2; sm++) for (by = 0; by < 2; by++) {
        const enum nvrm_dma_mode m = f(o, dm, sm, by);
        const int translated = dm || (sm && !by);
        if (!translated && m != NVRM_DMA_NONE) bad++;
        if (translated && m == NVRM_DMA_NONE) bad++;               /* raw addresses on a translated bus */
        if (translated && o && m != NVRM_DMA_REFUSE) bad++;
        if (translated && !o && m != (dm ? NVRM_DMA_DEVICE : NVRM_DMA_SYSTEM)) bad++;
    }
    return bad;
}
/* what "nvrmdma=0 restores 2.9" would mean with a mapper: physical addresses through VT-d */
static enum nvrm_dma_mode decide_optout_is_raw(int o, int dm, int sm, int by)
{
    return o ? NVRM_DMA_NONE : nvrm_dma_decide(o, dm, sm, by);
}

/* ---- the page-off check ---- */
typedef int (*pageoff_fn)(enum nvrm_dma_mode, int);
static int pageoff_violations(pageoff_fn f)
{
    int bad = 0;
    if (!f(NVRM_DMA_NONE, 0) || !f(NVRM_DMA_NONE, 1)) bad++;      /* mode 0 is 2.9: always copy */
    if (f(NVRM_DMA_SYSTEM, 0) || f(NVRM_DMA_DEVICE, 0)) bad++;     /* a raw list with a mapper */
    if (!f(NVRM_DMA_SYSTEM, 1) || !f(NVRM_DMA_DEVICE, 1)) bad++;   /* a mapped staging buffer */
    if (f(NVRM_DMA_REFUSE, 0) || f(NVRM_DMA_REFUSE, 1)) bad++;     /* a refused GPU copies nothing */
    return bad;
}
static int pageoff_trusting(enum nvrm_dma_mode m, int inside) { (void)inside; return m != NVRM_DMA_REFUSE; }

/* ---- G9: the lifetime of the pages behind a mapping ----
 * A model of an IOKit memory descriptor: references, wire count (prepare/complete) and sub-descriptors, each of which
 * holds a reference on its parent. The pages are freed when the last reference goes. nvrm_dma.cpp's dmaMap takes its
 * own reference and wire count (md->retain(); md->prepare()) before it asks for the mapping and dmaRelease gives both
 * back after the mapping is gone (tools/test_dma_wiring_kernel.py holds that text). The invariant: a descriptor
 * returned for an allocation keeps its pages alive while any reference or sub-descriptor of it exists, so the pages
 * under a live mapping are never freed, in any order of: the RM unpinning a process's pages before it unmaps them,
 * the allocation being freed, a sub-descriptor (a kernel view of part of the allocation) outliving both. */
struct md_model { int refs, wires, pages_alive, freed_while_mapped, mapped; };
static void md_retain(struct md_model *m) { m->refs++; }
static void md_release(struct md_model *m)
{
    if (--m->refs == 0) { if (m->mapped) m->freed_while_mapped = 1; m->pages_alive = 0; }
}
static void md_prepare(struct md_model *m) { m->wires++; }
static void md_complete(struct md_model *m) { m->wires--; }
struct map_model { int holds_own_ref; };
static void map_make(struct md_model *md, const struct map_model *how)
{
    if (how->holds_own_ref) { md_retain(md); md_prepare(md); }
    md->mapped = 1;
}
static void map_drop(struct md_model *md, const struct map_model *how)
{
    md->mapped = 0;
    if (how->holds_own_ref) { md_complete(md); md_release(md); }
}
/* Runs every order of the four events and returns how many orders free the pages under a live mapping or leave them
 * alive when nothing refers to them any more. */
static int lifetime_violations(const struct map_model *how)
{
    static const int orders[24][4] = {
        {0,1,2,3},{0,1,3,2},{0,2,1,3},{0,2,3,1},{0,3,1,2},{0,3,2,1},{1,0,2,3},{1,0,3,2},{1,2,0,3},{1,2,3,0},{1,3,0,2},{1,3,2,0},
        {2,0,1,3},{2,0,3,1},{2,1,0,3},{2,1,3,0},{2,3,0,1},{2,3,1,0},{3,0,1,2},{3,0,2,1},{3,1,0,2},{3,1,2,0},{3,2,0,1},{3,2,1,0} };
    int bad = 0, i, k;
    for (i = 0; i < 24; i++) {
        /* owner: the allocation (or the process's pin) holds one reference and one wire; a sub-descriptor one ref */
        struct md_model md = { 1, 1, 1, 0, 0 };
        int sub = 0;
        md_retain(&md); sub = 1;                                       /* a sub-descriptor of the allocation exists */
        map_make(&md, how);                                            /* the RM maps it for the GPU */
        for (k = 0; k < 4; k++) {
            switch (orders[i][k]) {
            case 0: map_drop(&md, how); break;                         /* the RM's unmap (or the stop's) */
            case 1: md_complete(&md); md_release(&md); break;          /* owner gives up: unpin / free */
            case 2: if (sub) { md_release(&md); sub = 0; } break;      /* the sub-descriptor goes */
            case 3: if (md.mapped && !md.pages_alive) md.freed_while_mapped = 1; break;   /* the GPU reads */
            }
            if (md.mapped && !md.pages_alive) md.freed_while_mapped = 1;
        }
        if (md.freed_while_mapped || md.pages_alive || md.refs != 0 || md.wires != 0) bad++;
    }
    return bad;
}

int main(void)
{
    unsigned long long out[16], a[4], l[4];
    enum nvrm_dma_why why;

    /* mode: one answer per input, and the rules over all of them */
    arm("no mapper -> none", nvrm_dma_decide(0, 0, 0, 0) == NVRM_DMA_NONE);
    arm("no mapper, opted out -> none (the switch changes nothing without a mapper)", nvrm_dma_decide(1, 0, 0, 0) == NVRM_DMA_NONE);
    arm("system mapper -> system", nvrm_dma_decide(0, 0, 1, 0) == NVRM_DMA_SYSTEM);
    arm("device mapper wins over the system mapper", nvrm_dma_decide(0, 1, 1, 0) == NVRM_DMA_DEVICE);
    arm("device mapper alone -> device", nvrm_dma_decide(0, 1, 0, 0) == NVRM_DMA_DEVICE);
    arm("nub marked untranslated -> none even with a system mapper", nvrm_dma_decide(0, 0, 1, 1) == NVRM_DMA_NONE);
    arm("a device mapper is believed over the untranslated mark", nvrm_dma_decide(0, 1, 1, 1) == NVRM_DMA_DEVICE);
    arm("mapper present and opted out -> refuse, never raw addresses", nvrm_dma_decide(1, 0, 1, 0) == NVRM_DMA_REFUSE);
    arm("device mapper and opted out -> refuse", nvrm_dma_decide(1, 1, 0, 0) == NVRM_DMA_REFUSE);
    arm("all 16 inputs follow the rules", decide_violations(nvrm_dma_decide) == 0);
    arm("control: an opt-out that keeps raw addresses with a mapper is caught", decide_violations(decide_optout_is_raw) > 0);
    arm("only system and device modes remap", nvrm_dma_remaps(NVRM_DMA_SYSTEM) && nvrm_dma_remaps(NVRM_DMA_DEVICE) &&
        !nvrm_dma_remaps(NVRM_DMA_NONE) && !nvrm_dma_remaps(NVRM_DMA_REFUSE));
    arm("the modes are the numbers of the accelerator's interface", NVRM_DMA_NONE == 0 && NVRM_DMA_SYSTEM == 1 &&
        NVRM_DMA_DEVICE == 2 && NVRM_DMA_REFUSE == 3);

    /* settle: routing and self-test */
    arm("mode 0 stays mode 0 whatever routing and self-test say",
        nvrm_dma_settle(NVRM_DMA_NONE, 0, 0, &why) == NVRM_DMA_NONE && why == NVRM_DMA_WHY_MODE);
    arm("system mapper, routed, self-test ok -> system", nvrm_dma_settle(NVRM_DMA_SYSTEM, 1, 1, &why) == NVRM_DMA_SYSTEM && why == NVRM_DMA_WHY_MODE);
    arm("mapper but the RM's calls not routed here -> refuse", nvrm_dma_settle(NVRM_DMA_SYSTEM, 0, 1, &why) == NVRM_DMA_REFUSE && why == NVRM_DMA_WHY_NOT_ROUTED);
    arm("device mapper, not routed -> refuse", nvrm_dma_settle(NVRM_DMA_DEVICE, 0, 1, &why) == NVRM_DMA_REFUSE && why == NVRM_DMA_WHY_NOT_ROUTED);
    arm("self-test failed -> refuse", nvrm_dma_settle(NVRM_DMA_DEVICE, 1, 0, &why) == NVRM_DMA_REFUSE && why == NVRM_DMA_WHY_SELFTEST);
    arm("an opt-out refusal says so", nvrm_dma_settle(NVRM_DMA_REFUSE, 1, 1, &why) == NVRM_DMA_REFUSE && why == NVRM_DMA_WHY_OPTOUT);
    arm("settle never turns a refusal into a mode that maps", nvrm_dma_settle(NVRM_DMA_REFUSE, 1, 1, NULL) == NVRM_DMA_REFUSE);
    arm("each refusal has its own line, naming VT-d",
        strstr(nvrm_dma_why_text(NVRM_DMA_REFUSE, NVRM_DMA_WHY_OPTOUT), "nvrmdma=0") != NULL &&
        strstr(nvrm_dma_why_text(NVRM_DMA_REFUSE, NVRM_DMA_WHY_SELFTEST), "self-test") != NULL &&
        strstr(nvrm_dma_why_text(NVRM_DMA_REFUSE, NVRM_DMA_WHY_NOT_ROUTED), "VT-d") != NULL &&
        strcmp(nvrm_dma_why_text(NVRM_DMA_REFUSE, NVRM_DMA_WHY_SELFTEST), nvrm_dma_why_text(NVRM_DMA_REFUSE, NVRM_DMA_WHY_OPTOUT)) != 0);
    arm("mode 0's line says physical", strstr(nvrm_dma_why_text(NVRM_DMA_NONE, NVRM_DMA_WHY_MODE), "physical") != NULL);

    /* address width */
    arm("device width is used", nvrm_dma_bits(47, 0) == 47);
    arm("a 32-bit allocation asks for 32", nvrm_dma_bits(47, 1) == 32);
    arm("0 and >64 mean 64", nvrm_dma_bits(0, 0) == 64 && nvrm_dma_bits(99, 0) == 64);
    arm("never below 32", nvrm_dma_bits(8, 0) == 32);
    arm("the temporary 40-bit window is honoured", nvrm_dma_bits(40, 0) == 40);

    /* addressable */
    arm("range below the limit", nvrm_dma_addressable(0xFFFFF000ull, 0x1000, 32));
    arm("range crossing the limit", !nvrm_dma_addressable(0xFFFFF000ull, 0x2000, 32));
    arm("a last byte exactly at 2^bits is above the limit", !nvrm_dma_addressable(1ull << 32, 1, 32)
        && nvrm_dma_addressable((1ull << 32) - 1, 1, 32));
    arm("64 bits always fits unless it wraps", nvrm_dma_addressable(~0ull - 0xFFF, 0x1000, 64) && !nvrm_dma_addressable(~0ull - 0xFFF, 0x2000, 64));
    arm("empty range is not addressable", !nvrm_dma_addressable(0x1000, 0, 40));
    arm("37-bit limit (AppleVTD's 128 GB)", nvrm_dma_addressable((1ull << 37) - 0x1000, 0x1000, 37) && !nvrm_dma_addressable(1ull << 37, 0x1000, 37));

    /* segments -> va_array */
    a[0] = 0x80000000ull; l[0] = 0x4000;
    memset(out, 0, sizeof out);
    arm("one segment, per page", nvrm_dma_expand(a, l, 1, 4, 0, out) == NVRM_DMA_X_OK && out[0] == 0x80000000ull && out[3] == 0x80003000ull);
    memset(out, 0, sizeof out);
    arm("one segment, contiguous: only the base", nvrm_dma_expand(a, l, 1, 4, 1, out) == NVRM_DMA_X_OK && out[0] == 0x80000000ull && out[1] == 0);
    a[1] = 0x90000000ull; l[1] = 0x2000;
    arm("two separate runs, per page", nvrm_dma_expand(a, l, 2, 6, 0, out) == NVRM_DMA_X_OK && out[4] == 0x90000000ull && out[5] == 0x90001000ull);
    arm("two separate runs cannot be a contiguous allocation", nvrm_dma_expand(a, l, 2, 6, 1, out) == NVRM_DMA_X_NOT_CONTIG);
    arm("two separate runs are counted as two", nvrm_dma_runs(a, l, 2) == 2 && nvrm_dma_runs(a, l, 1) == 1 && nvrm_dma_runs(a, l, 0) == 0);
    a[1] = 0x80004000ull;
    arm("two segments that continue each other are one run", nvrm_dma_expand(a, l, 2, 6, 1, out) == NVRM_DMA_X_OK && out[0] == 0x80000000ull &&
        nvrm_dma_runs(a, l, 2) == 1);
    arm("too few pages mapped", nvrm_dma_expand(a, l, 2, 7, 0, out) == NVRM_DMA_X_SHORT && nvrm_dma_expand(a, l, 2, 7, 1, out) == NVRM_DMA_X_SHORT);
    a[0] = 0x80000800ull;
    arm("an unaligned bus address is refused", nvrm_dma_expand(a, l, 1, 4, 0, out) == NVRM_DMA_X_UNALIGNED);
    a[0] = 0x80000000ull; l[0] = 0x1800; l[1] = 0x1000; a[1] = 0x80002000ull;
    arm("a short middle segment is refused", nvrm_dma_expand(a, l, 2, 3, 0, out) == NVRM_DMA_X_UNALIGNED);
    l[0] = 0;
    arm("an empty segment is refused", nvrm_dma_expand(a, l, 1, 1, 0, out) == NVRM_DMA_X_BAD);
    arm("no segments, no pages, no arrays", nvrm_dma_expand(a, l, 0, 1, 0, out) == NVRM_DMA_X_BAD && nvrm_dma_expand(NULL, l, 1, 1, 0, out) == NVRM_DMA_X_BAD);
    a[0] = 0x80000000ull; l[0] = 0x8000;
    memset(out, 0, sizeof out);
    arm("more mapped than asked for: only the asked pages are written", nvrm_dma_expand(a, l, 1, 2, 0, out) == NVRM_DMA_X_OK && out[1] == 0x80001000ull && out[2] == 0);
    a[0] = ~0ull - 0xFFF; l[0] = 0x2000;
    arm("a segment that wraps is refused", nvrm_dma_expand(a, l, 1, 2, 0, out) == NVRM_DMA_X_BAD);

    /* budget */
    arm("system budget 64 GB", nvrm_dma_budget_ok(NVRM_DMA_SYSTEM, 63ull << 30, 1ull << 30) && !nvrm_dma_budget_ok(NVRM_DMA_SYSTEM, 63ull << 30, (1ull << 30) + 4096));
    arm("device budget 6 GB", nvrm_dma_budget_ok(NVRM_DMA_DEVICE, 5ull << 30, 1ull << 30) && !nvrm_dma_budget_ok(NVRM_DMA_DEVICE, 6ull << 30, 4096));
    arm("a request larger than the budget", !nvrm_dma_budget_ok(NVRM_DMA_DEVICE, 0, 7ull << 30));
    arm("no overflow", !nvrm_dma_budget_ok(NVRM_DMA_SYSTEM, ~0ull, 4096) && !nvrm_dma_budget_ok(NVRM_DMA_SYSTEM, 4096, ~0ull));

    /* read-back schedule */
    arm("the first 64 mappings are read back in full", nvrm_dma_verify_full(0) && nvrm_dma_verify_full(63));
    arm("then every 64th", !nvrm_dma_verify_full(65) && nvrm_dma_verify_full(128) && !nvrm_dma_verify_full(129));
    arm("first and last page always, the middle only in full",
        nvrm_dma_verify_page(0, 10, 0) && nvrm_dma_verify_page(9, 10, 0) && !nvrm_dma_verify_page(5, 10, 0) && nvrm_dma_verify_page(5, 10, 1));
    arm("a full read-back stops at 4096 pages but keeps the last", !nvrm_dma_verify_page(5000, 9000, 1) && nvrm_dma_verify_page(8999, 9000, 1));

    /* page lists for the copy engine */
    out[0] = 0x80000000ull; out[1] = 0x80001000ull; out[2] = 0x80003000ull;
    arm("pages inside the mapped range", nvrm_dma_pages_inside(out, 3, 0x80000000ull, 0x4000));
    arm("a page past the end", !nvrm_dma_pages_inside(out, 3, 0x80000000ull, 0x3000));
    arm("a page before the start", !nvrm_dma_pages_inside(out, 3, 0x80001000ull, 0x4000));
    out[1] = 0x80001800ull;
    arm("an unaligned page", !nvrm_dma_pages_inside(out, 3, 0x80000000ull, 0x4000));
    arm("a raw physical address is not inside", (out[1] = 0x123456000ull, !nvrm_dma_pages_inside(out, 3, 0x80000000ull, 0x4000)));
    arm("empty list or empty range", !nvrm_dma_pages_inside(out, 0, 0x80000000ull, 0x4000) && !nvrm_dma_pages_inside(out, 1, 0x80000000ull, 0));
    out[0] = 0x80003000ull;
    arm("the last page needs a whole page inside", nvrm_dma_pages_inside(out, 1, 0x80000000ull, 0x4000) && !nvrm_dma_pages_inside(out, 1, 0x80000000ull, 0x3800));
    arm("the page-off answer follows the rules", pageoff_violations(nvrm_dma_pageoff_allowed) == 0);
    arm("control: a check that trusts any list with a mapper is caught", pageoff_violations(pageoff_trusting) > 0);

    /* G9 */
    {
        const struct map_model own = { 1 }, borrowed = { 0 };
        arm("a mapping that holds its own reference and wire keeps the pages alive in every order of unmap, unpin, "
            "sub-descriptor release, and frees them after the last", lifetime_violations(&own) == 0);
        arm("control: a mapping that borrows the owner's reference sees its pages freed under it", lifetime_violations(&borrowed) > 0);
    }

    printf(fails ? "FAILED\n" : "PASS\n");
    return fails != 0;
}
