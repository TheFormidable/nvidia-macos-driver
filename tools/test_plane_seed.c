/* test_plane_seed.c - own-write tracking for the planes of one IOSurface, proven on the host without a GPU.
   Build and run anywhere:  cc -I plugin tools/test_plane_seed.c -o /tmp/test_plane_seed && /tmp/test_plane_seed */
#include "nvmtl_plane_seed.h"
#include <stdio.h>
#include <string.h>
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)
#define S 1000000000ull
int main(void)
{
    static nvmtl_ps p; memset(&p, 0, sizeof p);
    nvmtl_ps_record(&p, 7, 11, 0, 5, 5, 1 * S);   /* our surface-out of plane 0 moved the seed 4 -> 5 */
    ARM("plane 1's image stays current after our own write to plane 0", nvmtl_ps_covers(&p, 7, 11, 1, 4, 5, 2 * S, 2 * S));
    ARM("plane 0's image does not: those are new pixels for it", !nvmtl_ps_covers(&p, 7, 11, 0, 4, 5, 2 * S, 2 * S));
    ARM("a seed that moved further than our records is somebody else's write", !nvmtl_ps_covers(&p, 7, 11, 1, 4, 6, 2 * S, 2 * S));
    nvmtl_ps_record(&p, 7, 11, 1, 6, 6, 3 * S);
    ARM("a chain of our writes to other planes is covered", nvmtl_ps_covers(&p, 7, 11, 2, 4, 6, 4 * S, 5 * S));
    ARM("a chain through this plane's own write is not", !nvmtl_ps_covers(&p, 7, 11, 1, 4, 6, 4 * S, 5 * S));
    ARM("an old record is not trusted", !nvmtl_ps_covers(&p, 7, 11, 1, 4, 5, 10 * S, 2 * S));
    ARM("a reused surface id (another cookie) never matches", !nvmtl_ps_covers(&p, 7, 99, 1, 4, 5, 2 * S, 2 * S) && !nvmtl_ps_covers(&p, 8, 11, 1, 4, 5, 2 * S, 2 * S));
    ARM("no cookie, no cover", !nvmtl_ps_covers(&p, 7, 0, 1, 4, 5, 2 * S, 2 * S));
    ARM("a lock whose unlock reported another seed (a foreign write between) records nothing",
        nvmtl_ps_record(&p, 9, 11, 0, 5, 6, S) == 0 && !nvmtl_ps_covers(&p, 9, 11, 1, 4, 5, 2 * S, 2 * S));
    ARM("an unchanged seed needs no record", nvmtl_ps_covers(&p, 7, 11, 1, 5, 5, 2 * S, 2 * S));
    /* planes in their own VRAM: when the other planes' tags follow the seed move of our mark of one plane */
    ARM("a mark that took and moved the seed by one is ours: the other planes' tags follow", nvmtl_ps_mark_moved(0, 8, 9));
    ARM("the seed wrapping around is still a move by one", nvmtl_ps_mark_moved(0, 0xffffffffu, 0));
    ARM("a mark that left the seed where it was moves no tag", !nvmtl_ps_mark_moved(0, 8, 8));
    ARM("a move by two has someone else's write in it", !nvmtl_ps_mark_moved(0, 8, 10));
    ARM("a seed that went back is not ours", !nvmtl_ps_mark_moved(0, 9, 8));
    ARM("a lock that was taken marked nothing: a move then is someone else's", !nvmtl_ps_mark_moved(1, 8, 9));
    ARM("a refused mark marked nothing", !nvmtl_ps_mark_moved(-1, 8, 9));
    /* what the planar self-test measures before any tag may follow: every mark it made moved the seed by exactly one */
    static nvmtl_ps_premise q;
    ARM("no mark measured, nothing proven", !nvmtl_ps_premise_holds(&q));
    nvmtl_ps_premise_note(&q, 8, 9); nvmtl_ps_premise_note(&q, 0xffffffffu, 0);
    ARM("every mark moved the seed by one (also across the wrap): the premise holds", nvmtl_ps_premise_holds(&q) && q.marks == 2);
    nvmtl_ps_premise_note(&q, 9, 9);
    ARM("one mark that moved nothing ends it, and is kept for the log", !nvmtl_ps_premise_holds(&q) && q.odd_before == 9 && q.odd_after == 9);
    nvmtl_ps_premise_note(&q, 9, 11); nvmtl_ps_premise_note(&q, 11, 12);
    ARM("later marks do not bring it back, and the first odd one stays", !nvmtl_ps_premise_holds(&q) && q.marks == 5 && q.by_one == 3 && q.odd_after == 9);
    return fails ? 1 : 0;
}
