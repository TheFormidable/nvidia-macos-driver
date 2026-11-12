/* nvrm_switch_report.h - which registered boot arguments this boot carries, and how many of them an ordinary install
 * does not need. Pure logic: no kernel call, so tools/test_switch_report.c runs it on the host.
 *
 * The table (nvrm_switch_table.h) is generated from tools/switches.tsv, the registry of every switch. The caller hands
 * in a function that says whether one boot argument is present (PE_parse_boot_argn in the kext, a string search in the
 * test). The report is built once at start and never changes: boot arguments do not change while the system runs.
 *
 * What it is for: the driver's default paths need no switch. A machine that still carries one (an opt-out somebody set
 * while testing, a test hook, a name a later version dropped) behaves differently from every other machine, and nothing
 * in a report said so. `sysctl debug.nvrm_switches` now does, and debug.nvrm_switches_nondefault is 0 on an ordinary
 * install.
 *
 * It decides nothing: no code path reads the report, so a wrong or empty one leaves the driver exactly as it was. */
#ifndef NVRM_SWITCH_REPORT_H
#define NVRM_SWITCH_REPORT_H

#include "nvrm_switch_table.h"

#define NVRM_SWITCH_NAMES_MAX 384

struct nvrm_switch_report {
    int set;                 /* registered boot arguments present, the install line and foreign names not counted */
    int nondefault;          /* of those: escape, test, dev or retired - not needed by an ordinary install */
    int by_tier[NVRM_SW_TIER_COUNT];
    int truncated;           /* the names did not all fit; the counts are exact all the same */
    char names[NVRM_SWITCH_NAMES_MAX];   /* "nvfbhdr nvrmdma! ...": '!' marks a nondefault one */
};

typedef int (*nvrm_switch_present_fn)(const char *name);

/* The install line is what every machine carries, and an internal name is not this driver's switch: neither is news. */
static inline int nvrm_switch_tier_counts(unsigned tier)
{
    return tier < NVRM_SW_TIER_COUNT && tier != NVRM_SW_INSTALL && tier != NVRM_SW_INTERNAL;
}

/* A setting, a hardware or recovery switch and a diagnostic are a user's own choice for the machine. These four are
 * not: an escape turns a default path off, a test hook injects a fault, a development switch is a leftover, and a
 * retired name does nothing any more. */
static inline int nvrm_switch_tier_nondefault(unsigned tier)
{
    return tier == NVRM_SW_ESCAPE || tier == NVRM_SW_TEST || tier == NVRM_SW_DEV || tier == NVRM_SW_RETIRED;
}

static inline void nvrm_switch_report_build(struct nvrm_switch_report *r, const struct nvrm_switch_name *tab, unsigned count,
                                            nvrm_switch_present_fn present)
{
    unsigned i, used = 0;
    if (!r) return;
    for (i = 0; i < sizeof *r; i++) ((char *)r)[i] = 0;
    if (!tab || !present) return;
    for (i = 0; i < count; i++) {
        const char *n = tab[i].name;
        const unsigned tier = tab[i].tier;
        unsigned len = 0, need;
        int marked;
        if (tier >= NVRM_SW_TIER_COUNT || !n || !n[0] || !present(n)) continue;
        r->by_tier[tier]++;
        if (!nvrm_switch_tier_counts(tier)) continue;
        marked = nvrm_switch_tier_nondefault(tier);
        r->set++;
        if (marked) r->nondefault++;
        while (n[len]) len++;
        need = len + (marked ? 1u : 0u) + (used ? 1u : 0u);
        /* +1 for the terminator. A name that does not fit is left out whole; a shorter one after it may still fit. */
        if (need >= NVRM_SWITCH_NAMES_MAX || used + need + 1 > NVRM_SWITCH_NAMES_MAX) { r->truncated = 1; continue; }
        if (used) r->names[used++] = ' ';
        for (len = 0; n[len]; len++) r->names[used++] = n[len];
        if (marked) r->names[used++] = '!';
        r->names[used] = 0;
    }
}

#endif
