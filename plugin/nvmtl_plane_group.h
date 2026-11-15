/* nvmtl_plane_group.h - which publications of one command buffer copy the planes of one surface out together.
   A two-plane surface (NV12, the frames WindowServer draws for ScreenCaptureKit) written through private images was
   published plane by plane: each plane took the surface's read-write lock, a submission and a fence wait of its own. The
   second plane's lock moved the surface's one seed past the seed the first plane had just recorded, so the first plane
   looked stale and was uploaded again at its next bind, on the compositor's thread. Copied out under one lock, every plane
   follows the seed that lock left, and one submission with one wait covers them all.
   The planner takes the list of publications in their order and gives each one its part in it:
     NVMTL_PG_ALONE    published on its own, exactly as before;
     NVMTL_PG_LEAD     copies out every plane of its surface's group, at the place of the group's last publication;
     NVMTL_PG_COVERED  its plane is copied out by the group's lead.
   A group is every publication of one surface in the list. It is formed only when all of them are private copy-outs (no
   VRAM plane, no dirtied bookkeeping), each plane has one backing, and at least two planes are present (at most
   NVMTL_PG_MAX_PLANES); anything else leaves every publication of that surface alone, as before. The lead is the last
   one, so no plane is copied out earlier than its own place in the order.
   Pure: tools/test_plane_group.c proves the rules. */
#ifndef NVMTL_PLANE_GROUP_H
#define NVMTL_PLANE_GROUP_H
#include <stdint.h>
#define NVMTL_PG_MAX_PLANES 4
enum { NVMTL_PG_ALONE = -1, NVMTL_PG_LEAD = -2 };   /* any value >= 0 is NVMTL_PG_COVERED: the index of the lead */
typedef struct {
    uintptr_t surf;    /* the surface, 0 for a texture without one */
    uintptr_t root;    /* the private backing the plane is copied out of */
    uint32_t plane;    /* the plane of the surface */
    uint32_t planes;   /* the surface's plane count */
    int copy;          /* 1: a private copy-out; 0: VRAM, dirtied bookkeeping, or anything else */
} nvmtl_pg_item;

/* Fills role[0..n-1]. Returns the number of groups formed. */
static inline int nvmtl_pg_plan(const nvmtl_pg_item *it, int n, int *role)
{
    int groups = 0;
    for (int i = 0; i < n; i++) role[i] = NVMTL_PG_ALONE;
    for (int i = 0; i < n; i++) {
        if (!it[i].surf || role[i] != NVMTL_PG_ALONE) continue;
        int first = 1;   /* only the surface's first item in the list decides, so each surface is judged once */
        for (int j = 0; j < i; j++) if (it[j].surf == it[i].surf) { first = 0; break; }
        if (!first) continue;
        uintptr_t root[NVMTL_PG_MAX_PLANES] = { 0 };
        int ok = it[i].planes > 1, distinct = 0, last = i;
        for (int j = i; j < n && ok; j++) {
            if (it[j].surf != it[i].surf) continue;
            if (!it[j].copy || it[j].planes != it[i].planes || it[j].plane >= NVMTL_PG_MAX_PLANES || it[j].plane >= it[j].planes || !it[j].root) { ok = 0; break; }
            if (!root[it[j].plane]) { root[it[j].plane] = it[j].root; distinct++; }
            else if (root[it[j].plane] != it[j].root) { ok = 0; break; }   /* two backings of one plane: their order matters */
            last = j;
        }
        if (!ok || distinct < 2) continue;
        for (int j = i; j <= last; j++) if (it[j].surf == it[i].surf) role[j] = j == last ? NVMTL_PG_LEAD : last;
        groups++;
    }
    return groups;
}
#endif
