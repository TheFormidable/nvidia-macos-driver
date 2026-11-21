// test_nvrm_pat.c: the kernel's verdict on the CPU's page attribute table (kexts/NVRM/nvrm_pat.h), which says whether
// write-combined mappings (every CPU write through BAR1) are really write-combined on this machine.
//
//   cc -std=c11 -Wall -Wextra -I../kexts/NVRM -o /tmp/test_nvrm_pat test_nvrm_pat.c && /tmp/test_nvrm_pat
//
// What a wrong verdict would do: debug.nvrm_pat_wc is the one number a user reads to learn whether a slow BAR1 comes
// from the fix-PAT kernel patch. Calling the default AMD_Vanilla table write-combined would send him looking at the
// link; calling stock macOS uncached would send him to his bootloader for nothing. The tables are the ones the
// patches write (their Replace bytes: mov eax / mov edx / wrmsr) and the one stock XNU's pat_init leaves.

#include "nvrm_pat.h"
#include <stdio.h>
#include <string.h>

static int g_fail;
#define CHECK(cond, msg) do { const int ok_ = !!(cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", msg); if (!ok_) g_fail++; } while (0)

static struct nvrm_pat_verdict dec(uint64_t pat, int ok)
{
    struct nvrm_pat_verdict v;
    memset(&v, 0x5a, sizeof v);   /* every field must be written, also on the early returns */
    nvrm_pat_decode(pat, ok, &v);
    return v;
}

int main(void)
{
    struct nvrm_pat_verdict v;

    /* The header's named constants are compared with the literal tables, so a typo in one of them fails here. */
    CHECK(NVRM_PAT_MACOS == 0x0001040600070406ull && NVRM_PAT_ALGREY == 0x0007010600070106ull
          && NVRM_PAT_SHANEEE == 0x0606060606060606ull && NVRM_PAT_POWER_ON == 0x0007040600070406ull,
          "the named tables are the literal ones");

    v = dec(0x0001040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_YES && v.pa6 == NVRM_PAT_WC && v.pa2 == NVRM_PAT_UCMINUS,
          "stock macOS (0x0001040600070406): entry 6 is WC, entry 2 UC-, write-combined mappings are write-combined");
    CHECK(!strcmp(v.table, "macOS's own"), "stock macOS is named as macOS's own table");

    v = dec(0x0007010600070106ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_NO && v.pa6 == NVRM_PAT_UCMINUS && v.pa2 == NVRM_PAT_UCMINUS,
          "AMD_Vanilla Algrey (0x0007010600070106): entries 6 and 2 are UC-, write-combined mappings are not write-combined");
    CHECK(strstr(v.table, "Algrey") != NULL, "the default AMD_Vanilla table is named");
    CHECK(strstr(v.words, "uncached-minus") != NULL && strstr(v.words, "MTRR") != NULL,
          "the Algrey verdict says uncached-minus, and that an MTRR could still make it WC");

    v = dec(0x0606060606060606ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_MTRR && v.pa6 == NVRM_PAT_WB,
          "AMD_Vanilla Shaneee (0x0606060606060606): entry 6 is WB, so the MTRRs decide (neither yes nor no)");
    CHECK(strstr(v.table, "Shaneee") != NULL && strstr(v.words, "MTRR") != NULL, "the Shaneee table is named and the MTRRs mentioned");

    v = dec(0x0007040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_NO && v.pa6 == NVRM_PAT_UCMINUS && strstr(v.table, "power-on") != NULL,
          "the power-on table (0x0007040600070406, pat_init's write replaced by nothing): not write-combined, named");

    v = dec(0x0001010600070106ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_YES && !strcmp(v.table, "unrecognised"),
          "a hand-fixed Algrey table with entry 6 set to WC: write-combined, and not passed off as a known table");

    v = dec(0x0004040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_NO && v.pa6 == NVRM_PAT_WT && strstr(v.words, "not combined") != NULL,
          "entry 6 write-through: not write-combined");
    v = dec(0x0005040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_NO && v.pa6 == NVRM_PAT_WP, "entry 6 write-protect: not write-combined");
    v = dec(0x0000040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_NO && v.pa6 == NVRM_PAT_UC && strstr(v.words, "uncached:") != NULL
          && strstr(v.words, "MTRR") == NULL, "entry 6 strong uncached: not write-combined, whatever the MTRRs say");

    v = dec(NVRM_PAT_MACOS | (1ull << 51), 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN && strstr(v.words, "reserved") != NULL,
          "a reserved high bit in entry 6: not decoded, reported as not known");
    v = dec(NVRM_PAT_MACOS | (0x80ull << 0), 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "a reserved high bit in an entry no mapping of interest uses: still not decoded");
    v = dec(0x0002040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "a reserved type (2) in entry 6: not decoded");
    v = dec(0x0001040600070403ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "a reserved type (3) in another entry: not decoded");
    v = dec(0x0201040600070406ull, 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "a reserved type (2) in entry 7, the last one: not decoded");
    v = dec(0x0001040600070406ull | (1ull << 59), 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "a reserved high bit in entry 7: not decoded");
    v = dec(0x0001040600070406ull | (1ull << 3), 1);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN, "the lowest reserved bit of entry 0: not decoded");

    v = dec(NVRM_PAT_MACOS, 0);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN && v.pa6 == 0 && v.pa2 == 0 && !strcmp(v.table, "not read"),
          "a failed read: not known, whatever value the caller passed");
    v = dec(0, 0);
    CHECK(v.wc == NVRM_PAT_WC_UNKNOWN && v.words && strstr(v.words, "could not be read") != NULL, "a failed read says so in words");

    CHECK(!strcmp(nvrm_pat_type_name(1), "WC") && !strcmp(nvrm_pat_type_name(7), "UC-") && !strcmp(nvrm_pat_type_name(6), "WB")
          && !strcmp(nvrm_pat_type_name(0), "UC") && !strcmp(nvrm_pat_type_name(4), "WT") && !strcmp(nvrm_pat_type_name(5), "WP")
          && !strcmp(nvrm_pat_type_name(2), "reserved"), "type names");
    CHECK(nvrm_pat_entry(NVRM_PAT_ALGREY, 1) == NVRM_PAT_WC && nvrm_pat_entry(NVRM_PAT_ALGREY, 0) == NVRM_PAT_WB,
          "Algrey's entry 1 is WC (the one no XNU mapping selects) and entry 0 WB");

    /* every verdict's sentence fits the kernel's buffer with the table in hex and its name in front of it
     * ("0x%016llx, %s: %s", 20 + name + 2 + words, 192 bytes) */
    {
        static const uint64_t all[] = { NVRM_PAT_MACOS, NVRM_PAT_ALGREY, NVRM_PAT_SHANEEE, NVRM_PAT_POWER_ON,
                                        0x0004040600070406ull, 0x0000040600070406ull, NVRM_PAT_MACOS | (1ull << 51),
                                        0x0001010600070106ull };
        size_t worst = 0, i;
        for (i = 0; i < sizeof all / sizeof all[0]; i++) {
            v = dec(all[i], 1);
            if (20 + strlen(v.table) + 2 + strlen(v.words) > worst) worst = 20 + strlen(v.table) + 2 + strlen(v.words);
        }
        v = dec(0, 0);
        if (20 + strlen(v.table) + 2 + strlen(v.words) > worst) worst = 20 + strlen(v.table) + 2 + strlen(v.words);
        CHECK(worst < 192, "the longest verdict fits debug.nvrm_pat_why's 192 bytes");
    }

    printf(g_fail ? "FAILED %d\n" : "ok\n", g_fail);
    return g_fail != 0;
}
