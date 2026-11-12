/* test_surfshare_status.c - no two NVRM_SS_* status values in kexts/NVRM/nvrm_surfshare_abi.h share a number.
   Build and run from the repository root:
     cc -std=c11 -Wall -Wextra -Werror -I kexts/NVRM tools/test_surfshare_status.c -o /tmp/test_surfshare_status && /tmp/test_surfshare_status
   (or give the header's path as the only argument).

   The accelerator's page-off reads the status word of a refused copy and acts on which refusal it was. Twice a new status
   was about to be given "the next free value" by two changes at once, which would have made a copy refused for DMA look
   like a bad plane range. The header is read as text, so a status added later is covered without touching this file. */
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

/* the numbers other code relies on: a change here is a change of the interface, not of the test */
_Static_assert(NVRM_SS_BUSY == 0x80000007u && NVRM_SS_NO_DMA == 0x80000008u && NVRM_SS_BAD_RANGE == 0x80000009u, "status values");
_Static_assert(NVRM_SS_PLANE_GPU_MARK == 0x80000000u && NVRM_SS_PLANE_TRYLOCK == 0x40000000u && NVRM_SS_PLANE_RANGED == 0x20000000u,
               "plane flags");
_Static_assert((NVRM_SS_PLANE_GPU_MARK & NVRM_SS_PLANE_TRYLOCK) == 0 && (NVRM_SS_PLANE_GPU_MARK & NVRM_SS_PLANE_RANGED) == 0
               && (NVRM_SS_PLANE_TRYLOCK & NVRM_SS_PLANE_RANGED) == 0, "each plane flag is its own bit");
_Static_assert(((NVRM_SS_PLANE_GPU_MARK | NVRM_SS_PLANE_TRYLOCK | NVRM_SS_PLANE_RANGED) & 0xFu) == 0, "the flags leave the plane index alone");
_Static_assert(sizeof(struct NVRMSurfShareEsc) == 40 && sizeof(struct NVRMSurfDma) == 48, "struct sizes");

/* a duplicate case label does not compile: the first line of defence, for the names known today */
static const char *known(unsigned v)
{
    switch (v) {
    case NVRM_SS_BAD_VERSION: return "NVRM_SS_BAD_VERSION";
    case NVRM_SS_OFF:         return "NVRM_SS_OFF";
    case NVRM_SS_NO_SURFACE:  return "NVRM_SS_NO_SURFACE";
    case NVRM_SS_NO_CACHE:    return "NVRM_SS_NO_CACHE";
    case NVRM_SS_NO_VRAM:     return "NVRM_SS_NO_VRAM";
    case NVRM_SS_TOO_SMALL:   return "NVRM_SS_TOO_SMALL";
    case NVRM_SS_BUSY:        return "NVRM_SS_BUSY";
    case NVRM_SS_NO_DMA:      return "NVRM_SS_NO_DMA";
    case NVRM_SS_BAD_RANGE:   return "NVRM_SS_BAD_RANGE";
    default:                  return NULL;
    }
}

struct def { char name[64]; unsigned long value; };

/* every "#define NVRM_SS_<NAME> <number>" of the text whose number is 0x800000NN: the values the kernel answers in a
   status word. Returns how many were found (at most max). */
static unsigned collect(const char *text, struct def *out, unsigned max)
{
    unsigned n = 0;
    for (const char *p = text; (p = strstr(p, "#define")) != NULL; ) {
        p += 7;
        while (*p == ' ' || *p == '\t') p++;
        if (strncmp(p, "NVRM_SS_", 8) != 0) continue;
        const char *e = p;
        while (isalnum((unsigned char)*e) || *e == '_') e++;
        const size_t len = (size_t)(e - p);
        const char *v = e;
        while (*v == ' ' || *v == '\t' || *v == '(') v++;
        char *end = NULL;
        const unsigned long val = strtoul(v, &end, 0);
        if (end == v || len >= sizeof out[0].name) continue;      /* not a number (a string, another macro) */
        if (val < 0x80000000ul || val > 0x800000FFul) continue;   /* a version, a flag below the top bit, an op */
        if (n < max) { memcpy(out[n].name, p, len); out[n].name[len] = 0; out[n].value = val; n++; }
    }
    return n;
}
/* the number of pairs that share a value; the first pair found is printed */
static unsigned duplicates(const struct def *d, unsigned n, int print)
{
    unsigned dup = 0;
    for (unsigned i = 0; i < n; i++)
        for (unsigned j = i + 1; j < n; j++)
            if (d[i].value == d[j].value) {
                if (print && !dup) printf("      %s and %s are both 0x%08lx\n", d[i].name, d[j].name, d[i].value);
                dup++;
            }
    return dup;
}
static const struct def *find(const struct def *d, unsigned n, const char *name)
{
    for (unsigned i = 0; i < n; i++) if (!strcmp(d[i].name, name)) return &d[i];
    return NULL;
}
static char *slurp(const char *path)
{
    FILE *f = fopen(path, "rb"); if (!f) return NULL;
    static char buf[1 << 16];
    const size_t got = fread(buf, 1, sizeof buf - 1, f);
    fclose(f); buf[got] = 0;
    return buf;
}

int main(int argc, char **argv)
{
    static struct def d[64];
    const char *paths[] = { argc > 1 ? argv[1] : NULL, "kexts/NVRM/nvrm_surfshare_abi.h", "../kexts/NVRM/nvrm_surfshare_abi.h" };
    char *text = NULL;
    for (unsigned i = 0; i < 3 && !text; i++) if (paths[i]) text = slurp(paths[i]);
    ARM("the header is found (run from the repository root, or pass its path)", text != NULL);
    if (!text) return 1;

    const unsigned n = collect(text, d, 64);
    ARM("the text holds the nine statuses and the completion-mark flag, at least", n >= 10 && n < 64);
    ARM("no two NVRM_SS_* values of the form 0x800000NN share a number", duplicates(d, n, 1) == 0);

    /* the text and the compiled header agree: the reader above sees what the compiler sees */
    unsigned agree = 0, statuses = 0;
    for (unsigned i = 0; i < n; i++) {
        const char *k = known((unsigned)d[i].value);
        if (k && !strcmp(k, d[i].name)) agree++;
        if (strncmp(d[i].name, "NVRM_SS_PLANE_", 14) != 0) statuses++;
    }
    ARM("every status the compiler knows is found in the text with the same value", agree == 9);
    ARM("the flag at 0x80000000 is the only value in that range that is not a status", statuses == n - 1 && find(d, n, "NVRM_SS_PLANE_GPU_MARK"));
    ARM("the DMA refusal and the bad plane range are two different answers",
        find(d, n, "NVRM_SS_NO_DMA") && find(d, n, "NVRM_SS_BAD_RANGE")
        && find(d, n, "NVRM_SS_NO_DMA")->value == 0x80000008ul && find(d, n, "NVRM_SS_BAD_RANGE")->value == 0x80000009ul);

    /* negative controls: the check has to be able to fail */
    static struct def c[8];
    const char *clash = "#define NVRM_SS_BUSY          0x80000007u\n"
                        "#define NVRM_SS_NO_DMA        0x80000008u   /* one change's next free value */\n"
                        "#define NVRM_SS_BAD_RANGE     0x80000008u   /* and the other's */\n";
    unsigned cn = collect(clash, c, 8);
    ARM("control: two statuses given the same number are reported", cn == 3 && duplicates(c, cn, 0) == 1);
    const char *spelled = "#define NVRM_SS_A 0x8000000Au\n#define\tNVRM_SS_B\t0X8000000a\n#define NVRM_SS_C (0x8000000Au)\n";
    cn = collect(spelled, c, 8);
    ARM("control: the same number written another way is still the same number", cn == 3 && duplicates(c, cn, 0) == 3);
    const char *others = "#define NVRM_SS_RM_VERSION 2u\n#define NVRM_SS_PLANE_TRYLOCK 0x40000000u\n#define NVRM_SS_FN_RM \"nvSurfShareRm\"\n"
                         "#define NVRM_FENCE_ST_OFF 0x80000002u\n#define NVRM_SS_OFF 0x80000002u\n";
    cn = collect(others, c, 8);
    ARM("control: versions, lower flags, names and other families are not statuses", cn == 1 && !strcmp(c[0].name, "NVRM_SS_OFF"));

    printf("%s\n", fails ? "FAILED" : "all passed");
    return fails ? 1 : 0;
}
