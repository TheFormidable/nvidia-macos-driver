// test_switch_report.c: the kernel's report of the boot arguments a machine carries (kexts/NVRM/nvrm_switch_report.h)
// and the generated table it walks (kexts/NVRM/nvrm_switch_table.h).
//
//   cc -std=c11 -Wall -Wextra -I../kexts/NVRM -o /tmp/test_switch_report test_switch_report.c && /tmp/test_switch_report
//
// What a wrong report would do: debug.nvrm_switches_nondefault is the one number that says "this machine runs the
// default paths". A report that counted the install line would never read 0; one that missed an escape or a test hook
// would read 0 on a machine that has a default path turned off. The controls at the end show the table checks fire:
// an unsorted table, a name listed twice, a tier outside the enum and a name too long for the kernel's buffer are each
// reported, so a generator that went wrong cannot pass by the checks having gone blind.

#include "nvrm_switch_report.h"
#include <stdio.h>
#include <string.h>

static int g_fail;
#define CHECK(cond, msg) do { const int ok_ = !!(cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", msg); if (!ok_) g_fail++; } while (0)

static const char *g_args;
/* the question PE_parse_boot_argn answers: is there a token whose name (up to '=') is exactly this */
static int present(const char *name)
{
    const size_t n = strlen(name);
    const char *p = g_args;
    while (*p) {
        while (*p == ' ') p++;
        if (!strncmp(p, name, n) && (p[n] == '=' || p[n] == ' ' || p[n] == 0)) return 1;
        while (*p && *p != ' ') p++;
    }
    return 0;
}
static int all(const char *name) { (void)name; return 1; }

/* what the kext relies on when it walks the generated table at start; returns how many entries break it */
static int table_problems(const struct nvrm_switch_name *t, unsigned n)
{
    unsigned i; int bad = 0;
    for (i = 0; i < n; i++) {
        if (!t[i].name || !t[i].name[0]) { bad++; continue; }
        if (t[i].tier >= NVRM_SW_TIER_COUNT) bad++;                         /* a tier the enum has */
        if (strlen(t[i].name) >= 48) bad++;                                 /* a name PE_parse_boot_argn can match */
        if (strchr(t[i].name, ' ') || strchr(t[i].name, '=') || strchr(t[i].name, '!')) bad++;
        if (i && t[i - 1].name && strcmp(t[i - 1].name, t[i].name) >= 0) bad++;   /* sorted, no name twice */
    }
    return bad;
}

int main(void)
{
    static const struct nvrm_switch_name tab[] = {
        { "-nvkmsnosmooth", NVRM_SW_INSTALL }, { "-nvoff", NVRM_SW_RECOVERY }, { "nvaccel", NVRM_SW_INSTALL }, { "nvfb", NVRM_SW_INSTALL },
        { "nvfbhdr", NVRM_SW_SETTING }, { "nvfbheads", NVRM_SW_INSTALL }, { "nvold", NVRM_SW_RETIRED }, { "nvrmdma", NVRM_SW_ESCAPE },
        { "nvrmsettle", NVRM_SW_DEV }, { "gfxrst", NVRM_SW_INTERNAL }, { "nvfault", NVRM_SW_TEST }, { "nvrtd3", NVRM_SW_HARDWARE },
        { "nvfblog", NVRM_SW_DIAG },
    };
    const unsigned n = sizeof tab / sizeof tab[0];
    struct { struct nvrm_switch_report r; unsigned char guard[8]; } box;
    struct nvrm_switch_report *r = &box.r;
    unsigned i;

    memset(box.guard, 0xA5, sizeof box.guard);
    g_args = "nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi=0x80 keepsyms=1";
    nvrm_switch_report_build(r, tab, n, present);
    CHECK(r->set == 0 && r->nondefault == 0 && r->names[0] == 0, "the install line alone reports nothing set");
    CHECK(r->by_tier[NVRM_SW_INSTALL] == 4, "the four install arguments are seen");

    g_args = "nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth nvfbhdr=1 nvrmdma=0 gfxrst=4";
    nvrm_switch_report_build(r, tab, n, present);
    CHECK(r->set == 2 && r->nondefault == 1, "a setting and an escape: two set, one of them not needed by an ordinary install");
    CHECK(!strcmp(r->names, "nvfbhdr nvrmdma!"), "the names are listed in table order and the escape is marked");
    CHECK(r->by_tier[NVRM_SW_INTERNAL] == 1, "a name that is not this driver's is seen and not counted");

    g_args = "nvold=1 nvrmsettle=250 -nvoff nvfbhd=1 nvfbheadsx=2";
    nvrm_switch_report_build(r, tab, n, present);
    CHECK(r->set == 3 && r->nondefault == 2, "a retired name and a development switch count as nondefault, a recovery switch does not");
    CHECK(r->by_tier[NVRM_SW_INSTALL] == 0 && r->by_tier[NVRM_SW_SETTING] == 0, "a name that only starts like a registered one is not that switch");
    CHECK(!strcmp(r->names, "-nvoff nvold! nvrmsettle!"), "flags with a dash are listed like the others");

    g_args = "nvfault=xid:13 nvrtd3=1 nvfblog=2";
    nvrm_switch_report_build(r, tab, n, present);
    CHECK(r->set == 3 && r->nondefault == 1, "a test hook counts as nondefault; a hardware switch and a diagnostic do not");
    CHECK(!strcmp(r->names, "nvfault! nvrtd3 nvfblog"), "the test hook is marked");
    for (i = 0; i < NVRM_SW_TIER_COUNT; i++)
        CHECK(nvrm_switch_tier_nondefault(i) == (i == NVRM_SW_ESCAPE || i == NVRM_SW_TEST || i == NVRM_SW_DEV || i == NVRM_SW_RETIRED),
              "exactly escape, test, dev and retired are nondefault");
    CHECK(!nvrm_switch_tier_counts(NVRM_SW_INSTALL) && !nvrm_switch_tier_counts(NVRM_SW_INTERNAL) && !nvrm_switch_tier_counts(NVRM_SW_TIER_COUNT),
          "the install line, internal names and a tier outside the enum are not counted");

    {   /* more names than fit: the counts stay exact, the text is cut at a name boundary and says so */
        static struct nvrm_switch_name big[200];
        static char store[200][24];
        for (i = 0; i < 200; i++) { snprintf(store[i], sizeof store[i], "nvlongswitchname%03u", i); big[i].name = store[i]; big[i].tier = NVRM_SW_DEV; }
        nvrm_switch_report_build(r, big, 200, all);
        CHECK(r->set == 200 && r->nondefault == 200, "the counts do not depend on the text fitting");
        CHECK(r->truncated == 1 && strlen(r->names) < NVRM_SWITCH_NAMES_MAX, "the text is cut and the cut is reported");
        CHECK(r->names[strlen(r->names) - 1] == '!', "the cut falls at the end of a name");
    }
    {   /* one name longer than the whole buffer is left out, and the buffer stays a string */
        static char huge[NVRM_SWITCH_NAMES_MAX + 40];
        struct nvrm_switch_name one[2];
        memset(huge, 'n', sizeof huge - 1);
        one[0].name = huge; one[0].tier = NVRM_SW_ESCAPE; one[1].name = "nvshort"; one[1].tier = NVRM_SW_SETTING;
        nvrm_switch_report_build(r, one, 2, all);
        CHECK(r->set == 2 && r->truncated == 1 && !strcmp(r->names, "nvshort"), "a name that cannot fit is counted, left out of the text, and a later one still listed");
    }
    for (i = 0; i < sizeof box.guard; i++) if (box.guard[i] != 0xA5) break;
    CHECK(i == sizeof box.guard, "nothing is written past the report");

    nvrm_switch_report_build(r, 0, 5, present);
    CHECK(r->set == 0 && r->names[0] == 0, "no table: an empty report");
    nvrm_switch_report_build(r, tab, n, 0);
    CHECK(r->set == 0, "no way to ask: an empty report");
    nvrm_switch_report_build(0, tab, n, present);
    CHECK(1, "no report to fill: nothing is written");
    {
        static const struct nvrm_switch_name bad[] = { { "nvx", 99 }, { 0, NVRM_SW_DEV }, { "", NVRM_SW_DEV }, { "nvy", NVRM_SW_TIER_COUNT } };
        nvrm_switch_report_build(r, bad, 4, all);
        CHECK(r->set == 0, "a tier outside the enum, a null name and an empty name are skipped");
    }

    /* the generated table itself */
    CHECK(NVRM_SWITCH_COUNT == sizeof nvrm_switch_names / sizeof nvrm_switch_names[0], "the count matches the generated table");
    CHECK(NVRM_SWITCH_COUNT >= 86, "the table holds at least the boot arguments 2.9 read");
    CHECK(table_problems(nvrm_switch_names, NVRM_SWITCH_COUNT) == 0, "the generated table is sorted, has no name twice, and every tier and name is usable");
    g_args = "nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth";
    nvrm_switch_report_build(r, nvrm_switch_names, NVRM_SWITCH_COUNT, present);
    CHECK(r->set == 0 && r->nondefault == 0 && r->by_tier[NVRM_SW_INSTALL] == 4, "the README install line is all tier install in the real table: an ordinary install reads 0");
    g_args = "nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth nvrmbhthread=0 nvrmrctimer=0 nvfbhdr=1";
    nvrm_switch_report_build(r, nvrm_switch_names, NVRM_SWITCH_COUNT, present);
    CHECK(r->set == 3 && r->nondefault == 2 && !strcmp(r->names, "nvfbhdr nvrmbhthread! nvrmrctimer!"),
          "in the real table the two opt-outs of the interrupt path are escapes and a display setting is not");
    nvrm_switch_report_build(r, nvrm_switch_names, NVRM_SWITCH_COUNT, all);
    CHECK(r->set > 0 && r->set == (int)NVRM_SWITCH_COUNT - r->by_tier[NVRM_SW_INSTALL] - r->by_tier[NVRM_SW_INTERNAL],
          "every registered name is counted once, the install line and internal names apart");

    /* controls: the table checks above fire on a table that is wrong */
    {
        static const struct nvrm_switch_name unsorted[] = { { "nvb", NVRM_SW_DEV }, { "nva", NVRM_SW_DEV } };
        static const struct nvrm_switch_name twice[] = { { "nva", NVRM_SW_DEV }, { "nva", NVRM_SW_ESCAPE } };
        static const struct nvrm_switch_name tier[] = { { "nva", NVRM_SW_TIER_COUNT } };
        static const struct nvrm_switch_name longname[] = { { "nvaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", NVRM_SW_DEV } };
        static const struct nvrm_switch_name spelled[] = { { "nva=1", NVRM_SW_DEV } };
        static const struct nvrm_switch_name good[] = { { "-nva", NVRM_SW_DEV }, { "nva", NVRM_SW_ESCAPE }, { "nvb", NVRM_SW_TEST } };
        CHECK(table_problems(unsorted, 2) == 1, "control: an unsorted table is reported");
        CHECK(table_problems(twice, 2) == 1, "control: a name listed twice is reported");
        CHECK(table_problems(tier, 1) == 1, "control: a tier outside the enum is reported");
        CHECK(table_problems(longname, 1) == 1, "control: a name too long for the kernel's buffer is reported");
        CHECK(table_problems(spelled, 1) == 1, "control: a name that carries its value is reported");
        CHECK(table_problems(good, 3) == 0, "control: a correct table is not reported");
    }

    printf("%s: %d failed\n", g_fail ? "FAIL" : "PASS", g_fail);
    return g_fail ? 1 : 0;
}
