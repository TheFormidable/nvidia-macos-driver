/* test_cvar_seed.c - the size a compute kernel's creation-time pipeline is specialized to (plugin/nvmtl_cvar_seed.h),
   on hand-assembled SPIR-V modules shaped like the translator's (translator passes/mod.rs kernel_local_size_ids,
   wrapper/src/linearize.rs).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_cvar_seed.c -o /tmp/test_cvar_seed && /tmp/test_cvar_seed
   Each arm is a sentence about a module the rule must seed or must leave unspecialized. */
#include <stdio.h>
#include <string.h>
#include "nvmtl_cvar_seed.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

#define OP(wc, op) (((uint32_t)(wc) << 16) | (uint32_t)(op))
static uint32_t m[128]; static size_t mn;
static void begin(void) { mn = 0; m[mn++] = 0x07230203u; m[mn++] = 0x00010300u; m[mn++] = 0; m[mn++] = 100; m[mn++] = 0; }
static void decorate_specid(uint32_t target, uint32_t id) { m[mn++] = OP(4, 71); m[mn++] = target; m[mn++] = 1; m[mn++] = id; }
static void spec_u32(uint32_t ty, uint32_t res, uint32_t v) { m[mn++] = OP(4, 50); m[mn++] = ty; m[mn++] = res; m[mn++] = v; }
static void spec_bool(uint32_t ty, uint32_t res, int v) { m[mn++] = OP(3, v ? 48 : 49); m[mn++] = ty; m[mn++] = res; }
static void type_int(uint32_t res) { m[mn++] = OP(4, 21); m[mn++] = res; m[mn++] = 32; m[mn++] = 0; }
static void type_bool(uint32_t res) { m[mn++] = OP(2, 20); m[mn++] = res; }
static void function(void) { m[mn++] = OP(5, 54); m[mn++] = 1; m[mn++] = 99; m[mn++] = 0; m[mn++] = 2; }
static void constant(uint32_t ty, uint32_t res, uint32_t v) { m[mn++] = OP(4, 43); m[mn++] = ty; m[mn++] = res; m[mn++] = v; }

/* a translated kernel: SpecIds 0-2 the size (x, y, z), SpecId 3 the linearize flag when lin */
static void kernel(uint32_t x, uint32_t y, uint32_t z, int lin, int lin_default)
{
    begin();
    decorate_specid(10, 0); decorate_specid(11, 1); decorate_specid(12, 2);
    if (lin) decorate_specid(13, 3);
    type_int(5); type_bool(6);
    spec_u32(5, 10, x); spec_u32(5, 11, y); spec_u32(5, 12, z);
    if (lin) spec_bool(6, 13, lin_default);
    function();
}

int main(void)
{
    uint32_t d[3] = { 0, 0, 0 }, v = 0;
    const uint32_t w[3] = { 1024, 1024, 64 };

    kernel(64, 1, 1, 0, 0);
    arm("the translator's default 64x1x1 is read from SpecIds 0-2", nvmtl_cvar_seed_size(m, mn, d) && d[0] == 64 && d[1] == 1 && d[2] == 1);
    kernel(32, 1, 1, 1, 0);
    arm("a kernel's own ceiling (32x1x1) is read too, with the linearize flag defaulting to false", nvmtl_cvar_seed_size(m, mn, d) && d[0] == 32 && d[1] == 1 && d[2] == 1);
    kernel(64, 1, 1, 1, 1);
    arm("a linearize flag that defaults to true is not seeded (specializing it to 0 would change the kernel)", !nvmtl_cvar_seed_size(m, mn, d));

    begin(); type_int(5); constant(5, 10, 64); constant(5, 11, 1); constant(5, 12, 1); function();
    arm("a fixed-size module (Workgroups dispatch: plain constants, no SpecIds) stays unspecialized", !nvmtl_cvar_seed_size(m, mn, d));

    kernel(64, 1, 1, 0, 0); const size_t full = mn;
    arm("a module cut inside its constants is not read", !nvmtl_cvar_seed_size(m, full - 7, d));
    m[5] = OP(0, 71);
    arm("an instruction of zero words ends the walk", !nvmtl_cvar_seed_size(m, full, d));
    kernel(64, 1, 1, 0, 0); m[0] = 0x12345678u;
    arm("a module without the SPIR-V magic is not read", !nvmtl_cvar_seed_size(m, mn, d));
    arm("a module shorter than its header is not read", !nvmtl_spirv_specid_default(m, 4, 0, &v));

    begin(); decorate_specid(10, 0); decorate_specid(11, 1); decorate_specid(12, 2); type_int(5);
    spec_u32(5, 10, 64); spec_u32(5, 11, 1); function(); spec_u32(5, 12, 1);
    arm("a constant after the first function is not taken (decorations and constants come before it)", !nvmtl_cvar_seed_size(m, mn, d));

    begin(); decorate_specid(10, 0); decorate_specid(11, 1); decorate_specid(12, 2); type_int(5);
    m[mn++] = OP(5, 50); m[mn++] = 5; m[mn++] = 10; m[mn++] = 64; m[mn++] = 0;   /* a 64-bit literal */
    spec_u32(5, 11, 1); spec_u32(5, 12, 1); function();
    arm("a SpecId whose constant is wider than 32 bits is not taken (the map entry is 4 bytes)", !nvmtl_cvar_seed_size(m, mn, d));

    kernel(8, 8, 1, 0, 0);
    arm("each SpecId is matched to its own constant (8x8x1)", nvmtl_cvar_seed_size(m, mn, d) && d[0] == 8 && d[1] == 8 && d[2] == 1);

    begin(); m[mn++] = OP(4, 71); m[mn++] = 20; m[mn++] = 30; m[mn++] = 0;   /* Location 0 on another constant */
    decorate_specid(10, 0); decorate_specid(11, 1); decorate_specid(12, 2); type_int(5);
    spec_u32(5, 20, 999); spec_u32(5, 10, 64); spec_u32(5, 11, 1); spec_u32(5, 12, 1); function();
    arm("a decoration other than SpecId whose literal is the same number is not taken for it", nvmtl_cvar_seed_size(m, mn, d) && d[0] == 64);

    begin(); decorate_specid(10, 0); decorate_specid(11, 1); decorate_specid(12, 2); decorate_specid(20, 0); type_int(5);
    spec_u32(5, 10, 64); spec_u32(5, 11, 1); spec_u32(5, 12, 1); spec_u32(5, 20, 32); function();
    arm("a SpecId on two constants with different defaults is not seeded (specializing one default would change the other)", !nvmtl_cvar_seed_size(m, mn, d));

    const uint32_t a[3] = { 64, 1, 1 }, wide[3] = { 2048, 1, 1 }, deep[3] = { 1, 1, 128 }, zero[3] = { 0, 1, 1 };
    arm("64x1x1 fits a device of 1024x1024x64 and 1024 invocations", nvmtl_cvar_size_fits(a, w, 1024));
    const uint32_t edge[3] = { 1024, 1, 1 }, cube[3] = { 8, 8, 1 };
    arm("a size exactly at a per-dimension limit and at the invocation limit fits (a dispatch takes it as it is)",
        nvmtl_cvar_size_fits(edge, w, 1024) && nvmtl_cvar_size_fits(cube, w, 64));
    arm("a size over a per-dimension limit is not seeded (a dispatch would linearize or refuse it)", !nvmtl_cvar_size_fits(wide, w, 4096) && !nvmtl_cvar_size_fits(deep, w, 1024));
    arm("a size over the invocation limit is not seeded", !nvmtl_cvar_size_fits(a, w, 32));
    arm("a zero dimension is not seeded (a dispatch of it is a no-op)", !nvmtl_cvar_size_fits(zero, w, 1024));
    const uint32_t none[3] = { 0, 0, 0 };
    arm("limits not known yet (all zero) seed nothing", !nvmtl_cvar_size_fits(a, none, 0));

    uint32_t sd[4];
    nvmtl_cvar_spec_data(a, 1, w, sd);
    arm("a size that fits is specialized {x, y, z, 0} whether or not the kernel can linearize", sd[0] == 64 && sd[1] == 1 && sd[2] == 1 && sd[3] == 0);
    nvmtl_cvar_spec_data(wide, 1, w, sd);
    arm("the linearize flag is set only for a linearizable kernel over a per-dimension limit, as at dispatch", sd[3] == 1);
    nvmtl_cvar_spec_data(wide, 0, w, sd);
    arm("and never for a kernel that cannot linearize", sd[3] == 0);

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
