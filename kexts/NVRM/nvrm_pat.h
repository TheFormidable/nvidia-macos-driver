/* nvrm_pat.h - whether write-combined mappings are really write-combined on this machine, from the CPU's page
 * attribute table (IA32_PAT, MSR 0x277). Pure logic: no kernel call, so tools/test_nvrm_pat.c runs it on the host.
 *
 * Why: every CPU write through BAR1 (NVK's mapped VRAM, the kernel's CPU fill of the framebuffer) is mapped with
 * kIOMapWriteCombineCache, and XNU builds such a mapping with the PAT and PCD bits of the page table entry set, which
 * selects entry 6 of the table (pmap_x86_common.c, pmap_enter_options: NOT_CACHEABLE without GUARDED). Stock XNU sets
 * entry 6 to write-combining on every CPU at start and at wake (mtrr.c, pat_init and mtrr_update_action). The fix-PAT
 * kernel patches of AMD_Vanilla replace that write with a fixed table: the default one (Algrey / Zormeister) writes
 * 0x0007010600070106, whose entry 6 is uncached-minus, so a write-combined mapping is uncached (unless the firmware's
 * memory type range registers mark the range write-combining: uncached-minus is the one type an MTRR can still turn
 * into WC) and every CPU store to BAR1 goes out on its own; the other one (Shaneee / Zormeister) writes 0x0606060606060606, all write-back, which leaves
 * the type of every mapping to the firmware's memory type range registers. Nothing in a log said which table a
 * machine runs, and a slow BAR1 looks the same as a slow link from user space.
 *
 * The kext reads the table once at start on the CPU that starts it and publishes the value and this verdict as
 * read-only sysctls (debug.nvrm_pat, debug.nvrm_pat_wc, debug.nvrm_pat_why) and one log line.
 *
 * Since 3.0.13 it also puts entry 6 back (nvrm_pat_with_wc6): when the table it finds has anything but WC there, it
 * makes on every CPU the one change stock XNU's pat_init makes (entry 6 to WC, every other entry left as it was), at
 * start and again after every wake, because the patched kernel writes its own table back on each CPU at wake. On the
 * default AMD_Vanilla table that gives 0x0001010600070106, the table a test boot from a USB stick ran with on a
 * Ryzen 5 5500 and an RTX 5050 under macOS 26.7.3 (2026-11-21, OpenCore's Replace changed by one byte). The boot
 * argument nvpatwc=0 leaves the table as the kernel wrote it, as in 3.0.12. */
#ifndef NVRM_PAT_H
#define NVRM_PAT_H

#include <stdint.h>

#define NVRM_PAT_MSR 0x277u

/* The memory types an entry can hold (Intel SDM vol. 3, table "Memory Types That Can Be Encoded With PAT"; the same on
 * AMD). 2 and 3 are reserved: writing them into the MSR faults, so a table that was read cannot hold them. */
enum { NVRM_PAT_UC = 0, NVRM_PAT_WC = 1, NVRM_PAT_WT = 4, NVRM_PAT_WP = 5, NVRM_PAT_WB = 6, NVRM_PAT_UCMINUS = 7 };

/* Which entry a kind of mapping selects on XNU: write-combined = PAT|PCD = 6; uncached (kIOMapInhibitCache) and the
 * default cache mode of device memory = PCD = 2. */
#define NVRM_PAT_ENTRY_WCOMB 6u
#define NVRM_PAT_ENTRY_IO    2u

/* The tables this file can name. */
#define NVRM_PAT_POWER_ON  0x0007040600070406ull   /* the architectural reset value: entry 6 is uncached-minus */
#define NVRM_PAT_MACOS     0x0001040600070406ull   /* the reset value after stock XNU's pat_init: entry 6 is WC */
#define NVRM_PAT_ALGREY    0x0007010600070106ull   /* AMD_Vanilla "Algrey / Zormeister | Fix PAT" (enabled by default) */
#define NVRM_PAT_SHANEEE   0x0606060606060606ull   /* AMD_Vanilla "Shaneee / Zormeister | Fix PAT" */
#define NVRM_PAT_ALGREY_WC6  0x0001010600070106ull /* the Algrey table with entry 6 put back to WC (nvrm_pat_with_wc6) */
#define NVRM_PAT_SHANEEE_WC6 0x0601060606060606ull /* the Shaneee table with entry 6 put back to WC */

/* The five high bits of every entry are reserved and must be zero. */
#define NVRM_PAT_RESERVED_MASK 0xF8F8F8F8F8F8F8F8ull

/* What debug.nvrm_pat_wc says. */
enum {
    NVRM_PAT_WC_UNKNOWN = -1,   /* the table could not be read, or it holds bits this file cannot decode */
    NVRM_PAT_WC_NO      = 0,    /* write-combined mappings are not write-combined: uncached, write-through or write-protect
                                   (for uncached-minus: unless an MTRR marks the range WC, which this file cannot see) */
    NVRM_PAT_WC_YES     = 1,    /* write-combined mappings are write-combined (a WC entry wins over any MTRR type) */
    NVRM_PAT_WC_MTRR    = 2,    /* the entry is write-back, so the firmware's MTRRs give the type of the range */
};

struct nvrm_pat_verdict {
    int wc;              /* one of NVRM_PAT_WC_* */
    unsigned pa6, pa2;   /* the types of the entries write-combined and uncached mappings use, 0..7 (0 when not read) */
    const char *table;   /* which known table this is, or "unrecognised" */
    const char *words;   /* what that means for a write-combined mapping, in a sentence a user can read */
};

static inline unsigned nvrm_pat_entry(uint64_t pat, unsigned i) { return (unsigned)(pat >> (8u * i)) & 7u; }

static inline const char *nvrm_pat_type_name(unsigned t)
{
    switch (t) {
    case NVRM_PAT_UC: return "UC";
    case NVRM_PAT_WC: return "WC";
    case NVRM_PAT_WT: return "WT";
    case NVRM_PAT_WP: return "WP";
    case NVRM_PAT_WB: return "WB";
    case NVRM_PAT_UCMINUS: return "UC-";
    default: return "reserved";
    }
}

/* A field holding a reserved encoding (2 or 3) cannot have been written to the MSR either. */
static inline int nvrm_pat_has_reserved(uint64_t pat)
{
    unsigned i;
    if (pat & NVRM_PAT_RESERVED_MASK) return 1;
    for (i = 0; i < 8; i++) {
        const unsigned t = nvrm_pat_entry(pat, i);
        if (t == 2u || t == 3u) return 1;
    }
    return 0;
}

/* read_ok: the MSR read succeeded (rdmsr_carefully returned 0). pat is ignored when it did not. */
static inline void nvrm_pat_decode(uint64_t pat, int read_ok, struct nvrm_pat_verdict *v)
{
    v->pa6 = 0; v->pa2 = 0;
    if (!read_ok) {
        v->wc = NVRM_PAT_WC_UNKNOWN; v->table = "not read";
        v->words = "the page attribute table could not be read on this CPU";
        return;
    }
    if (nvrm_pat_has_reserved(pat)) {
        v->wc = NVRM_PAT_WC_UNKNOWN; v->table = "unrecognised";
        v->words = "reserved bits are set: not decoded";
        return;
    }
    v->pa6 = nvrm_pat_entry(pat, NVRM_PAT_ENTRY_WCOMB);
    v->pa2 = nvrm_pat_entry(pat, NVRM_PAT_ENTRY_IO);
    v->table = pat == NVRM_PAT_MACOS    ? "macOS's own"
             : pat == NVRM_PAT_ALGREY   ? "AMD_Vanilla Algrey fix-PAT"
             : pat == NVRM_PAT_SHANEEE  ? "AMD_Vanilla Shaneee fix-PAT"
             : pat == NVRM_PAT_POWER_ON ? "the power-on table, entry 6 never set"
             : pat == NVRM_PAT_ALGREY_WC6  ? "AMD_Vanilla Algrey fix-PAT with entry 6 put back to WC"
             : pat == NVRM_PAT_SHANEEE_WC6 ? "AMD_Vanilla Shaneee fix-PAT with entry 6 put back to WC"
             : "unrecognised";
    switch (v->pa6) {
    case NVRM_PAT_WC:
        v->wc = NVRM_PAT_WC_YES;
        v->words = "write-combined mappings are write-combined";
        break;
    case NVRM_PAT_UC:
        v->wc = NVRM_PAT_WC_NO;
        v->words = "write-combined mappings are uncached: every CPU write through BAR1 goes out one store at a time";
        break;
    case NVRM_PAT_UCMINUS:
        /* UC- with an MTRR type of WC gives WC (SDM table "Effective Page-Level Memory Types"); every other MTRR type
         * gives UC. The MTRRs are not read here, so the words say what the verdict assumes. */
        v->wc = NVRM_PAT_WC_NO;
        v->words = "write-combined mappings are uncached-minus: one store at a time through BAR1, unless an MTRR marks it WC";
        break;
    case NVRM_PAT_WB:
        v->wc = NVRM_PAT_WC_MTRR;
        v->words = "write-combined mappings are write-back in the table, so the firmware's MTRRs decide their type";
        break;
    default:   /* WT, WP */
        v->wc = NVRM_PAT_WC_NO;
        v->words = "write-combined mappings are write-through or write-protect: CPU writes through BAR1 are not combined";
        break;
    }
}

/* The table with entry 6 set to write-combining and the other seven as they were: what stock XNU's pat_init writes
 * (mtrr.c, mtrr_update_action: pat &= ~(0xFF << 48); pat |= 1 << 48), so the five reserved bits of the entry are
 * cleared too. */
static inline uint64_t nvrm_pat_with_wc6(uint64_t pat) { return (pat & ~(0xFFull << 48)) | (0x01ull << 48); }

/* Whether the driver changes a table it read: it was read, it holds no reserved encoding (such a table was not
 * written by any code this file knows, so it is left alone), and entry 6 is not WC already (pat_init's own test). */
static inline int nvrm_pat_fix_wanted(uint64_t pat, int read_ok)
{
    return read_ok && !nvrm_pat_has_reserved(pat) && nvrm_pat_entry(pat, NVRM_PAT_ENTRY_WCOMB) != NVRM_PAT_WC;
}

#endif
