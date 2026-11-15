/* test_plane_group.c - which publications copy the planes of one surface out together, proven on the host without a GPU.
   Build and run anywhere:  cc -I plugin tools/test_plane_group.c -o /tmp/test_plane_group && /tmp/test_plane_group */
#include "nvmtl_plane_group.h"
#include <stdio.h>
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)
#define P(s, r, pl, n, c) ((nvmtl_pg_item){ (uintptr_t)(s), (uintptr_t)(r), (pl), (n), (c) })
int main(void)
{
    int role[8], g;
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 71, 1, 2, 1) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("both private planes of an NV12 surface form one group, led by the last", g == 1 && role[0] == 1 && role[1] == NVMTL_PG_LEAD); }
    { nvmtl_pg_item it[] = { P(7, 71, 1, 2, 1), P(9, 90, 0, 1, 1), P(7, 70, 0, 2, 1) };
      g = nvmtl_pg_plan(it, 3, role);
      ARM("the lead is the surface's last publication; another surface between stays alone", g == 1 && role[0] == 2 && role[1] == NVMTL_PG_ALONE && role[2] == NVMTL_PG_LEAD); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1) };
      g = nvmtl_pg_plan(it, 1, role);
      ARM("one plane alone is published as before", g == 0 && role[0] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 1, 1), P(7, 70, 0, 1, 1) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("a one-plane surface is never grouped", g == 0 && role[0] == NVMTL_PG_ALONE && role[1] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 71, 1, 2, 0) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("a VRAM or dirtied plane leaves the whole surface on the old path", g == 0 && role[0] == NVMTL_PG_ALONE && role[1] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 72, 0, 2, 1), P(7, 71, 1, 2, 1) };
      g = nvmtl_pg_plan(it, 3, role);
      ARM("two backings of one plane leave the whole surface on the old path (their order matters)", g == 0 && role[0] == NVMTL_PG_ALONE && role[1] == NVMTL_PG_ALONE && role[2] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 70, 0, 2, 1), P(7, 71, 1, 2, 1) };
      g = nvmtl_pg_plan(it, 3, role);
      ARM("the same backing named twice (a view and its parent) is covered once", g == 1 && role[0] == 2 && role[1] == 2 && role[2] == NVMTL_PG_LEAD); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 71, 1, 2, 1), P(8, 80, 0, 2, 1), P(8, 81, 1, 2, 1) };
      g = nvmtl_pg_plan(it, 4, role);
      ARM("two capture surfaces form two groups", g == 2 && role[0] == 1 && role[1] == NVMTL_PG_LEAD && role[2] == 3 && role[3] == NVMTL_PG_LEAD); }
    { nvmtl_pg_item it[] = { P(0, 0, 0, 0, 0), P(7, 70, 0, 2, 1), P(7, 71, 1, 2, 1) };
      g = nvmtl_pg_plan(it, 3, role);
      ARM("a texture without a surface stays alone", g == 1 && role[0] == NVMTL_PG_ALONE && role[1] == 2 && role[2] == NVMTL_PG_LEAD); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 71, 2, 2, 1) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("a plane index past the surface's plane count is never grouped", g == 0 && role[0] == NVMTL_PG_ALONE && role[1] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 6, 1), P(7, 71, 5, 6, 1) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("a plane past NVMTL_PG_MAX_PLANES is never grouped", g == 0 && role[0] == NVMTL_PG_ALONE && role[1] == NVMTL_PG_ALONE); }
    { nvmtl_pg_item it[] = { P(7, 70, 0, 2, 1), P(7, 71, 1, 3, 1) };
      g = nvmtl_pg_plan(it, 2, role);
      ARM("disagreeing plane counts are never grouped", g == 0); }
    if (fails) printf("FAIL: %d\n", fails); else puts("PASS: plane groups");
    return fails != 0;
}
