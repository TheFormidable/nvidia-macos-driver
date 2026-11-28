/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * airrun_tests.h - the tests of tools/air-nir/metal: for each kernel its grid, its inputs and the reference its outputs
 * are checked against. Two harnesses include it: airrun.c, which runs each kernel through NVK's AIR-to-NIR frontend on
 * a build of NVK, and plugin_run.m, which runs it through the Metal plugin's own compute path. Before including it, a
 * harness defines struct buf (.map), struct timg (.stage.map, .w, .h, .d, .layers, .mips, .texel), struct run (.t,
 * .slot[], .tex[16], .threads, .why[256]) and the functions the setups call: slot (a buffer for [[buffer(i)]]),
 * tex_image, tex_buffer, tex_at, tex_level_offset, mip_size, samp_create and samp_create_pixel (a sampler with pixel
 * coordinates; airrun.c, which cannot hand its state to the frontend, runs no test that makes one), which take Vulkan's
 * format, view-type and sampler enums (vulkan_core.h) whatever the harness makes of them.
 */
/* struct test flags: how the push constants describe the grid */
#define GRID_INDIRECT (1u << 0)   /* threads_per_grid = 0x80000000 | threadgroup size, groups 0x80000000: an indirect
                                   * dispatch, the kernel reads the group count from the hardware */
#define GRID_BASE     (1u << 1)   /* the second half of a 64x64 grid: thread_base.y 32, threadgroup_base.y 4 */
#define MUST_REFUSE   (1u << 2)   /* passes only if the frontend refuses the kernel (nothing is dispatched) */
#define PLUGIN_ONLY   (1u << 3)   /* the dispatch binds what only the plugin hands the frontend (a pixel-coordinate
                                   * sampler's state): plugin_run.m runs it, airrun.c does not */
#define ROBUST_ONLY   (1u << 4)   /* reads past a buffer's end: run only with compute pipelines bounds-checked
                                   * (NVMTL_COMPUTE_ROBUST=1), skipped otherwise (the read would fault) */
#define MUST_ROAD     (1u << 5)   /* with the road on, its pipeline must be the road's (round 10: what the road newly
                                   * takes; the SPIR-V road's pipeline is then a failure) */

/* function-constant values a test's request carries (NVMTL_AIR_NIR_FC_VALUES) */
struct fcval {
   uint32_t index, size;
   uint8_t bytes[8];
};
struct fcset {
   unsigned n;
   struct fcval v[8];
};

struct test {
   const char *name;
   const char *air;       /* AIRDIR/<air>.air */
   const char *entry;     /* the kernel's name */
   unsigned tg[3];        /* threads per threadgroup */
   unsigned groups[3];    /* threadgroups in the grid */
   unsigned tg_len[2];    /* bytes of [[threadgroup(0)]] and [[threadgroup(1)]] */
   unsigned flags;
   const struct fcset *fc;   /* NULL: the request has no function-constant values */
   void (*setup)(struct run *);
   bool (*check)(struct run *);
   /* optional, before the dispatch: what the result must say (bindings, constexpr sampler slots) */
   bool (*expect)(struct run *, const struct nvmtl_air_nir_result *);
};

static uint32_t rng_state;

static uint32_t
rnd(void)
{
   uint32_t x = rng_state;
   x ^= x << 13;
   x ^= x >> 17;
   x ^= x << 5;
   return rng_state = x;
}

static float
rndf(float lo, float hi)
{
   return lo + (hi - lo) * (float)(rnd() >> 8) * (1.0f / 16777216.0f);
}


static void *
S(struct run *r, unsigned i)
{
   return r->slot[i].map;
}

#define FAILF(r, ...) (snprintf((r)->why, sizeof (r)->why, __VA_ARGS__), false)

static bool
check_u32(struct run *r, const char *what, const uint32_t *got, const uint32_t *want, unsigned n, unsigned stride)
{
   for (unsigned i = 0; i < n; i++) {
      if (got[i] != want[i]) {
         if (stride > 1)
            return FAILF(r, "%s[%u] (thread %u, value %u) = 0x%08x (%u), expected 0x%08x (%u)", what, i, i / stride,
                         i % stride, got[i], got[i], want[i], want[i]);
         return FAILF(r, "%s[%u] = 0x%08x (%u), expected 0x%08x (%u)", what, i, got[i], got[i], want[i], want[i]);
      }
   }
   return true;
}

/* |got - want| <= abs_tol + rel_tol * |want| (NaNs and infinities must match) */
static bool
check_f32(struct run *r, const char *what, const float *got, const float *want, unsigned n, unsigned stride,
          const float *abs_tol, const float *rel_tol)
{
   for (unsigned i = 0; i < n; i++) {
      float g = got[i], w = want[i];
      unsigned k = i % stride;
      bool ok;
      if (isnan(w) || isinf(w))
         ok = (isnan(w) && isnan(g)) || g == w;
      else
         ok = !isnan(g) && fabsf(g - w) <= abs_tol[k] + rel_tol[k] * fabsf(w);
      if (!ok)
         return FAILF(r, "%s[%u] (thread %u, value %u) = %.9g, expected %.9g", what, i, i / stride, k, g, w);
   }
   return true;
}

/* half <-> float, round to nearest even */
static uint16_t
f2h(float f)
{
   uint32_t x;
   memcpy(&x, &f, 4);
   uint32_t sign = (x >> 16) & 0x8000u, e = (x >> 23) & 0xff, m = x & 0x7fffffu;
   if (e == 0xff)
      return (uint16_t)(sign | 0x7c00u | (m ? 0x200u : 0));
   int ee = (int)e - 127 + 15;
   if (ee >= 31)
      return (uint16_t)(sign | 0x7c00u);
   if (ee <= 0) {
      if (ee < -10)
         return (uint16_t)sign;
      m |= 0x800000u;
      unsigned shift = (unsigned)(14 - ee);
      uint32_t h = m >> shift, rem = m & ((1u << shift) - 1), half = 1u << (shift - 1);
      if (rem > half || (rem == half && (h & 1)))
         h++;
      return (uint16_t)(sign | h);
   }
   uint32_t h = ((uint32_t)ee << 10) | (m >> 13), rem = m & 0x1fffu;
   if (rem > 0x1000u || (rem == 0x1000u && (h & 1)))
      h++;
   return (uint16_t)(sign | h);
}

static float
h2f(uint16_t h)
{
   uint32_t sign = (uint32_t)(h & 0x8000u) << 16, e = (h >> 10) & 0x1f, m = h & 0x3ffu, x;
   if (e == 0x1f)
      x = sign | 0x7f800000u | (m << 13);
   else if (e)
      x = sign | ((e + 112) << 23) | (m << 13);
   else if (!m)
      x = sign;
   else {
      e = 113;
      while (!(m & 0x400u)) {
         m <<= 1;
         e--;
      }
      x = sign | (e << 23) | ((m & 0x3ffu) << 13);
   }
   float f;
   memcpy(&f, &x, 4);
   return f;
}

static uint32_t
fbits(float f)
{
   uint32_t u;
   memcpy(&u, &f, 4);
   return u;
}

/* --- fmath, fmath_precise ------------------------------------------------------------------------------------- */

static void
fmath_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4), *c = slot(r, 2, n * 4);
   slot(r, 3, n * 12 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = rndf(-4, 4);
      b[i] = rndf(0.5f, 4) * ((rnd() & 1) ? -1.0f : 1.0f);
      c[i] = rndf(-4, 4);
   }
}

static bool
fmath_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1), *c = S(r, 2);
   float *want = calloc(n * 12, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = a[i], y = b[i], z = c[i], *o = want + i * 12;
      o[0] = x + y;
      o[1] = x * y;
      o[2] = fmaf(x, y, z);
      o[3] = sqrtf(fabsf(x));
      o[4] = expf(x);
      o[5] = sinf(x);
      o[6] = x / y;
      o[7] = 1.0f / sqrtf(fabsf(y) + 1.0f);
      o[8] = log2f(fabsf(z) + 1.0f);
      o[9] = floorf(x * 3.0f);
      o[10] = fmaxf(x, y) - fminf(y, z);
      o[11] = cosf(z) + tanhf(y * 0.25f);
   }
   /* the GPU's transcendental units are approximations: these bounds check the translation, not Metal's ulp
    * limits */
   static const float abs_tol[12] = { 0, 0, 0, 0, 0, 2e-5f, 0, 0, 2e-5f, 0, 0, 1e-4f };
   static const float rel_tol[12] = { 1e-6f, 1e-6f, 1e-6f, 2e-6f, 2e-5f, 0, 4e-6f, 4e-6f, 1e-5f, 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 12, 12, abs_tol, rel_tol);
   free(want);
   return ok;
}

/* --- imath ---------------------------------------------------------------------------------------------------- */

static void
imath_setup(struct run *r)
{
   unsigned n = r->threads;
   int32_t *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4);
   slot(r, 2, n * 16 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = (int32_t)rnd();
      int32_t y = (int32_t)rnd() >> (rnd() % 31);
      if (y == 0 || (y == -1 && a[i] == INT32_MIN))
         y = 7;
      b[i] = y;
   }
   a[0] = 0;
   b[0] = 1;
   a[1] = -7;
   b[1] = 2;
   a[2] = INT32_MIN;
   b[2] = 3;
}

static bool
imath_check(struct run *r)
{
   unsigned n = r->threads;
   const int32_t *a = S(r, 0), *b = S(r, 1);
   uint32_t *want = calloc(n * 16, 4);
   for (unsigned i = 0; i < n; i++) {
      int32_t x = a[i], y = b[i];
      uint32_t ux = (uint32_t)x, uy = (uint32_t)y, s = uy & 31u, *o = want + i * 16;
      o[0] = (uint32_t)(x / y);
      o[1] = (uint32_t)(x % y);
      o[2] = ux / uy;
      o[3] = ux % uy;
      o[4] = ux << s;
      o[5] = ux >> s;
      o[6] = (uint32_t)(x >> s);
      o[7] = (uint32_t)(x < y ? x : y);
      o[8] = (uint32_t)(x > y ? x : y);
      o[9] = ux < uy ? ux : uy;
      o[10] = ux > uy ? ux : uy;
      o[11] = (uint32_t)__builtin_popcount(ux);
      o[12] = ux ? (uint32_t)__builtin_clz(ux) : 32u;
      o[13] = uy ? (uint32_t)__builtin_ctz(uy) : 32u;
      o[14] = (uint32_t)(((uint64_t)ux * uy) >> 32);
      uint32_t rot = s ? (ux << s) | (ux >> (32 - s)) : ux;
      int32_t d = (int32_t)(ux - uy);
      uint32_t ad = d < 0 ? 0u - (uint32_t)d : (uint32_t)d;
      o[15] = rot ^ (ux * uy) ^ ad;
   }
   bool ok = check_u32(r, "out", S(r, 2), want, n * 16, 16);
   free(want);
   return ok;
}

/* --- conv ----------------------------------------------------------------------------------------------------- */

static void
conv_setup(struct run *r)
{
   unsigned n = r->threads;
   float *f = slot(r, 0, n * 4);
   int32_t *k = slot(r, 1, n * 4);
   uint16_t *h = slot(r, 2, n * 2);
   slot(r, 3, n * 10 * 4);
   slot(r, 4, n * 2 * 2);
   for (unsigned i = 0; i < n; i++) {
      f[i] = rndf(-1000, 1000);
      k[i] = (int32_t)rnd();
      h[i] = f2h(rndf(-100, 100));
   }
   f[0] = 0.5f;
   f[1] = -0.5f;
   f[2] = 2.5f;
   f[3] = 1.00048828125f;   /* a tie between two halves: rounds to even */
}

static bool
conv_check(struct run *r)
{
   unsigned n = r->threads;
   const float *f = S(r, 0);
   const int32_t *kk = S(r, 1);
   const uint16_t *hh = S(r, 2);
   uint32_t *want = calloc(n * 10, 4);
   uint16_t *hwant = calloc(n * 2, 2);
   for (unsigned i = 0; i < n; i++) {
      float x = f[i], y = h2f(hh[i]);
      int32_t k = kk[i];
      uint32_t *o = want + i * 10;
      o[0] = (uint32_t)(int32_t)x;
      o[1] = (uint32_t)fabsf(x);
      o[2] = fbits((float)k);
      o[3] = fbits((float)(uint32_t)k);
      o[4] = fbits(h2f(f2h(x)));
      o[5] = (uint32_t)(int32_t)y;
      o[6] = fbits(y * 2.0f);
      o[7] = (uint32_t)(int32_t)(int16_t)k;
      o[8] = (uint32_t)(uint8_t)k | ((uint32_t)(uint16_t)k << 8);
      o[9] = (uint32_t)(int32_t)(int8_t)(k >> 4);
      hwant[i * 2] = f2h(h2f(f2h(x)) + y);
      hwant[i * 2 + 1] = f2h(h2f(f2h((float)(k & 255))) * y);
   }
   bool ok = check_u32(r, "out", S(r, 3), want, n * 10, 10);
   if (ok) {
      const uint16_t *g = S(r, 4);
      for (unsigned i = 0; i < n * 2 && ok; i++) {
         if (g[i] != hwant[i])
            ok = FAILF(r, "hout[%u] (thread %u) = 0x%04x (%g), expected 0x%04x (%g)", i, i / 2, g[i], h2f(g[i]),
                       hwant[i], h2f(hwant[i]));
      }
   }
   free(want);
   free(hwant);
   return ok;
}

/* --- conv_edge ------------------------------------------------------------------------------------------------ */

static const float conv_edge_in[] = { NAN, INFINITY, -INFINITY, 3e9f, -3e9f, 2147483648.0f, -2147483904.0f, 5e9f,
                                      -1.5f, 1.5f, 65537.5f, -0.0f, 4294967040.0f, 1e38f, -1e38f, 7.75f };

static void
conv_edge_setup(struct run *r)
{
   unsigned n = r->threads;
   float *f = slot(r, 0, n * 4);
   slot(r, 1, n * 6 * 4);
   for (unsigned i = 0; i < n; i++)
      f[i] = i < sizeof conv_edge_in / sizeof *conv_edge_in ? conv_edge_in[i] : rndf(-70000, 70000);
}

/* Metal's conversions: NaN is 0, a value out of range is the type's limit, toward zero otherwise */
static int32_t conv_edge_i(float x) { return x != x ? 0 : x >= 2147483648.0f ? INT32_MAX : x <= -2147483648.0f ? INT32_MIN : (int32_t)x; }
static uint32_t conv_edge_u(float x) { return x != x ? 0 : x >= 4294967296.0f ? UINT32_MAX : x <= 0 ? 0 : (uint32_t)x; }

static bool
conv_edge_check(struct run *r)
{
   unsigned n = r->threads;
   const float *f = S(r, 0);
   uint32_t *want = calloc(n * 6, 4);
   for (unsigned i = 0; i < n; i++) {
      const float x = f[i];
      uint32_t *o = want + i * 6;
      o[0] = (uint32_t)conv_edge_i(x);
      o[1] = conv_edge_u(x);
      o[2] = (uint32_t)(int32_t)(int16_t)conv_edge_i(x);
      o[3] = (uint32_t)conv_edge_i(x * 2.0f);
      o[4] = isnan(x) ? 1u : 0u;
      o[5] = (isinf(x) ? 2u : 0u) | (isfinite(x) ? 4u : 0u);
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 6, 6);
   free(want);
   return ok;
}

/* --- metal_edge: minmax_nan, round_edge, pow_edge, narrow_exact -------------------------------------------------- */

static bool
edge_same(float got, float want, float rel)
{
   if (want != want) return got != got;
   if (want == 0) return got == 0 && signbit(got) == signbit(want);
   if (isinf(want)) return got == want;
   return fabsf(got - want) <= rel * fabsf(want);
}

static const float minmax_in[16] = { NAN, 3.0f, -2.0f, NAN, NAN, NAN, 0.5f, -0.5f, INFINITY, NAN, -INFINITY, 1.0f, 7.0f, -7.0f, NAN, 0.0f };

static void
minmax_nan_setup(struct run *r)
{
   float *f = slot(r, 0, r->threads * 4);
   slot(r, 1, r->threads * 8 * 4);
   for (unsigned i = 0; i < r->threads; i++)
      f[i] = i < 16 ? minmax_in[i] : rndf(-10, 10);
}

/* GLSL's NMin/NMax/NClamp, which the translator makes of precise::fmin, fmax, clamp, fmin3, fmax3 and fmedian3: a NaN
 * operand gives way to the other */
static float nmin(float a, float b) { return a != a ? b : b != b ? a : a < b ? a : b; }
static float nmax(float a, float b) { return a != a ? b : b != b ? a : a > b ? a : b; }

static bool
minmax_nan_check(struct run *r)
{
   const float *f = S(r, 0), *g = S(r, 1);
   for (unsigned i = 0; i < r->threads; i++) {
      const float x = f[i], y = f[i ^ 1];
      const float want[8] = { nmin(x, y), nmax(x, y), nmin(nmax(x, -1.0f), 1.0f), nmin(nmin(x, y), 0.25f), nmax(nmax(x, y), 0.25f),
                              nmax(nmin(x, y), nmin(nmax(x, y), 0.25f)), 0, nmin(x, INFINITY) };
      for (unsigned k = 0; k < 8; k++) {
         if (k == 6)
            continue;
         if (!edge_same(g[i * 8 + k], want[k], 0))
            return FAILF(r, "out[%u] (thread %u, x %g, y %g, output %u) = %g, expected %g", i * 8 + k, i, x, y, k, g[i * 8 + k], want[k]);
      }
   }
   return true;
}

static const float round_in[16] = { 0.49999997f, 2.5f, -2.5f, 8388609.0f, -0.5f, 0.5f, 1.5f, -1.49999988f,
                                    16777215.0f, -8388609.0f, 0.0f, -0.0f, 3.49999976f, 100.5f, -100.5f, 2.4999998f };

static void
round_edge_setup(struct run *r)
{
   float *f = slot(r, 0, r->threads * 4);
   slot(r, 1, r->threads * 4);
   slot(r, 2, r->threads * 2);
   for (unsigned i = 0; i < r->threads; i++)
      f[i] = i < 16 ? round_in[i] : rndf(-1000, 1000);
}

/* Metal's round: half away from zero (C's roundf); the half one on the half value */
static bool
round_edge_check(struct run *r)
{
   const float *f = S(r, 0), *g = S(r, 1);
   const uint16_t *h = S(r, 2);
   for (unsigned i = 0; i < r->threads; i++) {
      const float want = roundf(f[i]);
      if (!(g[i] == want && signbit(g[i]) == signbit(want)))
         return FAILF(r, "round(%.9g) (thread %u) = %.9g, expected %.9g", f[i], i, g[i], want);
      const float hx = h2f(f2h(f[i] * 0.25f)), hw = h2f(f2h(roundf(hx)));
      if (h2f(h[i]) != hw)
         return FAILF(r, "round(half %.9g) (thread %u) = %.9g, expected %.9g", hx, i, h2f(h[i]), hw);
   }
   return true;
}

static const float pow_in[32] = { -2, 3, -2, 2, -2, 0.5f, -0.0f, 3, 1, INFINITY, -1, INFINITY, 5, 0, NAN, 0,
                                   -8, 1.0f / 3.0f, -INFINITY, 3, -INFINITY, 2, 2, 10, 0.5f, -3, -3, -3, 9, 0.5f, -1, NAN };

static void
pow_edge_setup(struct run *r)
{
   float *f = slot(r, 0, r->threads * 2 * 4);
   slot(r, 1, r->threads * 4);
   for (unsigned i = 0; i < r->threads * 2; i++)
      f[i] = i < 32 ? pow_in[i] : (i & 1) ? rndf(-4, 4) : rndf(0.1f, 10);
}

/* Metal's pow is C's powf at its special values (a negative base with an integer exponent keeps the sign of an odd
 * one, 1 to any power and -1 to an infinite one is 1, x^0 is 1, a finite negative base to a non-integer power is NaN) */
static bool
pow_edge_check(struct run *r)
{
   const float *f = S(r, 0), *g = S(r, 1);
   for (unsigned i = 0; i < r->threads; i++) {
      const float x = f[i * 2], y = f[i * 2 + 1];
      const float want = powf(x, y);
      if (!edge_same(g[i], want, 2e-6f))
         return FAILF(r, "pow(%g, %g) (thread %u) = %.9g, expected %.9g", x, y, i, g[i], want);
   }
   return true;
}

static void
narrow_exact_setup(struct run *r)
{
   float *f = slot(r, 0, r->threads * 4);
   slot(r, 1, r->threads * 2 * 4);
   for (unsigned i = 0; i < r->threads; i++)
      f[i] = 1.0f + i * 0.0001234f;
}

static bool
narrow_exact_check(struct run *r)
{
   const float *f = S(r, 0), *g = S(r, 1);
   for (unsigned i = 0; i < r->threads; i++) {
      const float x = f[i], x3 = x * 3.0f;
      const float want0 = h2f(f2h(x3)) - x3, want1 = h2f(f2h(x)) * 2.0f;
      if (g[i * 2] != want0 || g[i * 2 + 1] != want1)
         return FAILF(r, "thread %u (x %.9g): %.9g and %.9g, expected %.9g and %.9g (the half rounding kept)", i, x, g[i * 2],
                      g[i * 2 + 1], want0, want1);
   }
   return true;
}

/* --- loop_phi ------------------------------------------------------------------------------------------------- */

static void
loop_phi_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = 1 + rnd() % 100000;
   in[0] = 1;
   in[1] = 27;
   in[2] = 15;
}

static bool
loop_phi_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], steps = 0, peak = x;
      while (x != 1u) {
         x = (x & 1u) ? 3u * x + 1u : x >> 1;
         peak = peak > x ? peak : x;
         if (++steps >= 500u)
            break;
      }
      uint32_t a = 1, b = 0;
      for (uint32_t j = 0; j < (in[i] & 15u); j++) {
         uint32_t t = a + b;
         b = a;
         a = t;
         if (a > 1000u)
            break;
      }
      want[i * 3] = steps;
      want[i * 3 + 1] = peak;
      want[i * 3 + 2] = a * 65536u + b;
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 3, 3);
   free(want);
   return ok;
}

/* --- switch_case ---------------------------------------------------------------------------------------------- */

static void
switch_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 2 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = i < 64 ? i : rnd();
}

static bool
switch_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 2, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], v = 0, t;
      switch (x % 9u) {
      case 0: v = x * 3u; break;
      case 1: v = x + 7u; break;
      case 2: x ^= 0x55u; /* fall through */
      case 3: v = x << 2; break;
      case 5: v = x / 3u; break;
      case 7: v = ~x; break;
      default: v = x - 1u; break;
      }
      static const uint32_t table[8] = { 11, 23, 5, 99, 1000, 7, 64, 3 };
      t = table[x & 7u];
      want[i * 2] = v;
      want[i * 2 + 1] = t;
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 2, 2);
   free(want);
   return ok;
}

/* --- tg_reduce ------------------------------------------------------------------------------------------------ */

static void
tg_reduce_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, (n / 256) * 4);
   slot(r, 2, (n / 256) * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd() & 0xfffffu;
}

static bool
tg_reduce_check(struct run *r)
{
   unsigned n = r->threads, groups = n / 256;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(groups, 4);
   float *fwant = calloc(groups, 4);
   for (unsigned g = 0; g < groups; g++) {
      for (unsigned j = 0; j < 256; j++) {
         want[g] += in[g * 256 + j];
         fwant[g] += (float)(in[g * 256 + j] & 1023u) * 0.5f;
      }
   }
   static const float zero[1] = { 0 };
   bool ok = check_u32(r, "sums", S(r, 1), want, groups, 1) &&
             check_f32(r, "fsums", S(r, 2), fwant, groups, 1, zero, zero);
   free(want);
   free(fwant);
   return ok;
}

/* --- tg_arg --------------------------------------------------------------------------------------------------- */

static void
tg_arg_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
tg_arg_check(struct run *r)
{
   unsigned n = r->threads, tg = 128;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n, 4);
   for (unsigned i = 0; i < n; i++) {
      unsigned base = i / tg * tg, lid = i % tg;
      want[i] = in[base + tg - 1 - lid] + (uint32_t)(uint16_t)(((lid + 1) % tg) * 3);
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n, 1);
   free(want);
   return ok;
}

/* --- dev_atomics ---------------------------------------------------------------------------------------------- */

static void
dev_atomics_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4), *counters = slot(r, 1, 8 * 4);
   slot(r, 2, n * 4);
   slot(r, 3, 16 * 4);
   slot(r, 4, 2 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
   counters[2] = 0xffffffffu;
   counters[4] = 0xffffffffu;
   counters[5] = 1000000000u;
   memset(S(r, 2), 0xff, n * 4);
}

static bool
dev_atomics_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0), *counters = S(r, 1), *slots = S(r, 2), *hist = S(r, 3), *cas = S(r, 4);
   uint32_t want[8] = { n, 0, 0xffffffffu, 0, 0xffffffffu, 1000000000u, 0, 0 }, hwant[16] = { 0 }, cwant[2] = { 0, 7 };
   for (unsigned i = 0; i < n; i++) {
      uint32_t v = in[i];
      hwant[v & 15u]++;
      want[1] = want[1] > v ? want[1] : v;
      want[2] = want[2] < v ? want[2] : v;
      want[3] |= 1u << (v & 31u);
      want[4] &= ~(1u << ((v >> 5) & 31u));
      want[5] -= v & 7u;
      if (v & 1u)
         cwant[0] += v & 255u;
   }
   if (!check_u32(r, "counters", counters, want, 8, 1) || !check_u32(r, "hist", hist, hwant, 16, 1) ||
       !check_u32(r, "cas", cas, cwant, 2, 1))
      return false;
   /* every thread took one slot: the slots hold each thread id once */
   unsigned char *seen = calloc(n, 1);
   bool ok = true;
   for (unsigned i = 0; i < n && ok; i++) {
      if (slots[i] >= n || seen[slots[i]])
         ok = FAILF(r, "slots[%u] = %u: not a thread id, or one seen before", i, slots[i]);
      else
         seen[slots[i]] = 1;
   }
   free(seen);
   return ok;
}

/* --- tg_atomics ----------------------------------------------------------------------------------------------- */

static void
tg_atomics_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, (n / 256) * 18 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
tg_atomics_check(struct run *r)
{
   unsigned n = r->threads, groups = n / 256;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(groups * 18, 4);
   for (unsigned g = 0; g < groups; g++) {
      uint32_t *o = want + g * 18;
      for (unsigned j = 0; j < 256; j++) {
         uint32_t v = in[g * 256 + j];
         o[v & 15u]++;
         o[16] = o[16] > v ? o[16] : v;
         o[17] ^= v;
      }
   }
   bool ok = check_u32(r, "out", S(r, 1), want, groups * 18, 18);
   free(want);
   return ok;
}

/* --- const_table ---------------------------------------------------------------------------------------------- */

static void
const_table_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
const_table_check(struct run *r)
{
   static const float kF[16] = { 0.5f, 1.25f, -3.0f, 7.75f, 100.0f, -0.125f, 2.0f, 9.5f,
                                 -11.0f, 13.25f, 0.0f, 1e-3f, 42.0f, -42.0f, 3.5f, 6.0f };
   static const uint32_t kU[13] = { 2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41 };
   static const struct { uint32_t key; float scale; int16_t bias; } kE[5] = {
      { 1, 2.0f, -3 }, { 10, 0.5f, 4 }, { 100, -1.0f, 5 }, { 1000, 4.0f, -6 }, { 7, 8.0f, 7 } };
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   float *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i];
      want[i * 3] = kF[x & 15u] * 2.0f;
      want[i * 3 + 1] = (float)kU[(x >> 4) % 13u];
      want[i * 3 + 2] = (float)kE[x % 5u].key * kE[x % 5u].scale + (float)kE[x % 5u].bias;
   }
   static const float zero[3] = { 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n * 3, 3, zero, zero);
   free(want);
   return ok;
}

/* --- struct_buf ----------------------------------------------------------------------------------------------- */

struct particle {         /* Metal's layout of struct Particle in struct_buf.metal */
   float pos[4];          /* float3: 16 bytes */
   float vel[3];          /* packed_float3 */
   float mass;
   uint32_t id;
   uint32_t pad[3];
};
_Static_assert(sizeof(struct particle) == 48, "Particle is 48 bytes in Metal");

static void
struct_buf_setup(struct run *r)
{
   unsigned n = r->threads;
   struct particle *in = slot(r, 0, n * sizeof *in);
   slot(r, 1, n * sizeof *in);
   float *dt = slot(r, 2, 16);
   *dt = 0.125f;
   for (unsigned i = 0; i < n; i++) {
      for (unsigned k = 0; k < 3; k++) {
         in[i].pos[k] = rndf(-10, 10);
         in[i].vel[k] = rndf(-2, 2);
      }
      in[i].pos[3] = 0;
      in[i].mass = rndf(0, 5);
      in[i].id = rnd() >> 2;
   }
}

static bool
struct_buf_check(struct run *r)
{
   unsigned n = r->threads;
   const struct particle *in = S(r, 0), *got = S(r, 1);
   const float dt = 0.125f;
   for (unsigned i = 0; i < n; i++) {
      float pos[3], vel[3];
      for (unsigned k = 0; k < 3; k++)
         pos[k] = in[i].pos[k] + in[i].vel[k] * dt;
      for (unsigned k = 0; k < 3; k++)
         vel[k] = in[i].vel[k] * 0.5f + pos[(k + 1) % 3];
      float mass = sqrtf(pos[0] * pos[0] + pos[1] * pos[1] + pos[2] * pos[2]) + in[i].mass;
      uint32_t id = in[i].id * 2u + 1u;
      for (unsigned k = 0; k < 3; k++) {
         if (fabsf(got[i].pos[k] - pos[k]) > 1e-5f + 1e-6f * fabsf(pos[k]))
            return FAILF(r, "out[%u].pos[%u] = %.9g, expected %.9g", i, k, got[i].pos[k], pos[k]);
         if (fabsf(got[i].vel[k] - vel[k]) > 1e-5f + 1e-6f * fabsf(vel[k]))
            return FAILF(r, "out[%u].vel[%u] = %.9g, expected %.9g", i, k, got[i].vel[k], vel[k]);
      }
      if (fabsf(got[i].mass - mass) > 1e-5f + 4e-6f * fabsf(mass))
         return FAILF(r, "out[%u].mass = %.9g, expected %.9g", i, got[i].mass, mass);
      if (got[i].id != id)
         return FAILF(r, "out[%u].id = %u, expected %u", i, got[i].id, id);
   }
   return true;
}

/* --- grid2d --------------------------------------------------------------------------------------------------- */

static void
grid2d_setup(struct run *r)
{
   slot(r, 0, 64 * 64 * 4 * 4);
}

static bool
grid2d_check(struct run *r)
{
   /* a 64x32 grid of 16x8 threadgroups; with GRID_BASE, rows 32-63 of a 64x64 one (rows 0-31 stay 0) */
   const bool based = r->t->flags & GRID_BASE;
   const unsigned W = 64, H = based ? 64 : 32, Y0 = based ? 32 : 0, TX = 16, TY = 8;
   uint32_t *want = calloc(64 * 64 * 4, 4);
   for (unsigned y = Y0; y < H; y++) {
      for (unsigned x = 0; x < W; x++) {
         uint32_t *o = want + (y * W + x) * 4;
         o[0] = x | (y << 16);
         o[1] = (x % TX) | ((y % TY) << 8) | ((x / TX) << 16) | ((y / TY) << 24);
         o[2] = W | (H << 16);
         o[3] = (W / TX) | ((H / TY) << 8) | (TX << 16) | (TY << 24);
      }
   }
   bool ok = check_u32(r, "out", S(r, 0), want, 64 * 64 * 4, 4);
   free(want);
   return ok;
}

/* --- simd_builtins -------------------------------------------------------------------------------------------- */

static void
simd_setup(struct run *r)
{
   slot(r, 0, r->threads * 4 * 4);
}

static bool
simd_check(struct run *r)
{
   unsigned n = r->threads, tg = 128;
   uint32_t *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      unsigned lid = i % tg;
      want[i * 4] = lid % 32;
      want[i * 4 + 1] = lid / 32;
      want[i * 4 + 2] = 32 | ((tg / 32) << 16);
      want[i * 4 + 3] = lid;
   }
   bool ok = check_u32(r, "out", S(r, 0), want, n * 4, 4);
   free(want);
   return ok;
}

/* --- narrow --------------------------------------------------------------------------------------------------- */

static void
narrow_setup(struct run *r)
{
   unsigned n = r->threads;
   int8_t *a = slot(r, 0, n);
   int16_t *b = slot(r, 1, n * 2);
   uint8_t *c = slot(r, 2, n);
   uint16_t *d = slot(r, 3, n * 2);
   slot(r, 4, n * 2);
   slot(r, 5, n);
   slot(r, 6, n * 3 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = (int8_t)rnd();
      b[i] = (int16_t)rnd();
      c[i] = (uint8_t)rnd();
      d[i] = (uint16_t)rnd();
   }
}

static bool
narrow_check(struct run *r)
{
   unsigned n = r->threads;
   const int8_t *a = S(r, 0), *oc = S(r, 5);
   const int16_t *b = S(r, 1), *os = S(r, 4);
   const uint8_t *c = S(r, 2);
   const uint16_t *d = S(r, 3);
   const int32_t *oi = S(r, 6);
   for (unsigned i = 0; i < n; i++) {
      int16_t ws = (int16_t)(a[i] * b[i]);
      int8_t wc = (int8_t)(a[i] + (int8_t)c[i]);
      int32_t w0 = a[i] * 1000 + b[i], w1 = (int32_t)c[i] - (int32_t)d[i], w2 = (int16_t)d[i] + (uint8_t)a[i];
      if (os[i] != ws)
         return FAILF(r, "os[%u] = %d, expected %d", i, os[i], ws);
      if (oc[i] != wc)
         return FAILF(r, "oc[%u] = %d, expected %d", i, oc[i], wc);
      if (oi[i * 3] != w0 || oi[i * 3 + 1] != w1 || oi[i * 3 + 2] != w2)
         return FAILF(r, "oi[%u..] = %d %d %d, expected %d %d %d", i * 3, oi[i * 3], oi[i * 3 + 1], oi[i * 3 + 2], w0,
                      w1, w2);
   }
   return true;
}

/* --- vec_ops -------------------------------------------------------------------------------------------------- */

static void
vec_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 16), *b = slot(r, 1, n * 16);
   uint32_t *u = slot(r, 2, n * 4);
   slot(r, 3, n * 4 * 16);
   slot(r, 4, n * 16);
   for (unsigned i = 0; i < n * 4; i++) {
      a[i] = rndf(-4, 4);
      b[i] = rndf(-4, 4);
   }
   for (unsigned i = 0; i < n; i++)
      u[i] = rnd();
}

static bool
vec_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1);
   const uint32_t *u = S(r, 2);
   float *want = calloc(n * 16, 4);
   uint32_t *iwant = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      const float *x = a + i * 4, *y = b + i * 4;
      float *o = want + i * 16;
      bool gt[4], any = false, all = true;
      for (unsigned k = 0; k < 4; k++) {
         o[k] = x[k] * y[k] + x[3 - k];
         gt[k] = x[k] > y[k];
         any |= gt[k];
         all &= gt[k];
      }
      o[4] = x[1] * y[2] - x[2] * y[1];
      o[5] = x[2] * y[0] - x[0] * y[2];
      o[6] = x[0] * y[1] - x[1] * y[0];
      o[7] = x[0] * y[0] + x[1] * y[1] + x[2] * y[2] + x[3] * y[3];
      float v[3] = { y[0] + 0.5f, y[1] + 0.5f, y[2] + 0.5f };
      float len = sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
      for (unsigned k = 0; k < 3; k++)
         o[8 + k] = v[k] / len;
      o[11] = sqrtf(x[0] * x[0] + x[1] * x[1] + x[2] * x[2] + x[3] * x[3]);
      float extra[4] = { any ? 1.0f : 0.0f, all ? 1.0f : 0.0f, x[1], x[1] };
      for (unsigned k = 0; k < 4; k++)
         o[12 + k] = (gt[k] ? y[k] : x[k]) + extra[k];
      uint32_t w = u[i], bytes[4];
      for (unsigned k = 0; k < 4; k++)
         bytes[k] = (w >> (8 * k)) & 255u;
      static const int32_t mul[4] = { 3, -1, 7, 2 };
      uint32_t hw = w & 0x3bff3bffu;
      uint32_t add[4] = { (uint32_t)(h2f((uint16_t)hw) * 1024.0f), (uint32_t)(h2f((uint16_t)(hw >> 16)) * 1024.0f),
                          w >> 24, w & 0xffu };
      for (unsigned k = 0; k < 4; k++) {
         int32_t iv = (int32_t)bytes[k] - 128 + (int32_t)x[k];
         iwant[i * 4 + k] = (uint32_t)(iv * mul[k]) ^ (bytes[3 - k] + add[k]);
      }
   }
   static const float abs_tol[16] = { 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-5f, 1e-5f, 1e-5f, 1e-5f,
                                      1e-6f, 1e-6f, 1e-6f, 1e-6f, 0, 0, 0, 0 };
   static const float rel_tol[16] = { 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f,
                                      1e-5f, 1e-5f, 1e-5f, 4e-6f, 0, 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 16, 16, abs_tol, rel_tol) &&
             check_u32(r, "iout", S(r, 4), iwant, n * 4, 4);
   free(want);
   free(iwant);
   return ok;
}

/* --- tg_transpose --------------------------------------------------------------------------------------------- */

static void
tg_transpose_setup(struct run *r)
{
   float *in = slot(r, 0, 64 * 64 * 4);
   slot(r, 1, 64 * 64 * 4);
   slot(r, 2, 16 * 64 * 16);
   uint32_t *size = slot(r, 3, 16);
   size[0] = 64;
   size[1] = 64;
   for (unsigned i = 0; i < 64 * 64; i++)
      in[i] = (float)i + 0.5f;
}

static bool
tg_transpose_check(struct run *r)
{
   const float *in = S(r, 0);
   float *want = calloc(64 * 64, 4), *vwant = calloc(16 * 64 * 4, 4);
   for (unsigned y = 0; y < 64; y++) {
      for (unsigned x = 0; x < 64; x++)
         want[y * 64 + x] = in[x * 64 + y];
   }
   for (unsigned ty = 0; ty < 4; ty++) {
      for (unsigned tx = 0; tx < 4; tx++) {
         for (unsigned l = 0; l < 64; l++) {
            float *o = vwant + ((ty * 4 + tx) * 64 + l) * 4;
            o[0] = (float)((l * 7u) & 63u) * 2.0f;
            o[1] = (float)tx * 2.0f;
            o[2] = (float)ty * 2.0f;
            o[3] = 2.0f;
         }
      }
   }
   static const float zero[4] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, 64 * 64, 1, zero, zero) &&
             check_f32(r, "vout", S(r, 2), vwant, 16 * 64 * 4, 4, zero, zero);
   free(want);
   free(vwant);
   return ok;
}

/* --- thread_array --------------------------------------------------------------------------------------------- */

static void
thread_array_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 12 * 4);
   slot(r, 1, n * 20 * 4);
   for (unsigned i = 0; i < n * 12; i++)
      in[i] = rnd() % 1000;
}

static bool
thread_array_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 20, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t v[12], *o = want + i * 20;
      memcpy(v, in + i * 12, sizeof v);
      for (unsigned k = 0; k < 12; k++)
         o[12 + (v[k] & 7u)]++;
      for (unsigned k = 1; k < 12; k++) {
         uint32_t x = v[k];
         int j = (int)k - 1;
         while (j >= 0 && v[j] > x) {
            v[j + 1] = v[j];
            j--;
         }
         v[j + 1] = x;
      }
      memcpy(o, v, sizeof v);
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 20, 20);
   free(want);
   return ok;
}

/* --- ptr_ops -------------------------------------------------------------------------------------------------- */

struct rec {   /* struct Rec in ptr_ops.metal */
   uint32_t a;
   float b;
   uint32_t c[4];
};

static void
ptr_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *x = slot(r, 0, n * 16), *y = slot(r, 1, n * 16);
   struct rec *recs = slot(r, 2, 7 * sizeof(struct rec));
   slot(r, 3, n * 16);
   for (unsigned i = 0; i < n * 4; i++) {
      x[i] = rnd();
      y[i] = rnd();
   }
   for (unsigned i = 0; i < 7; i++) {
      recs[i].a = rnd();
      recs[i].b = rndf(0, 1000);
      for (unsigned k = 0; k < 4; k++)
         recs[i].c[k] = rnd();
   }
}

static bool
ptr_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *x = S(r, 0), *y = S(r, 1);
   const struct rec *recs = S(r, 2);
   uint32_t *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      const uint32_t *p = (i & 1u) ? x : y;
      uint32_t s = 0;
      for (unsigned k = 0; k < 4; k++)
         s += p[i * 4 + k];
      const struct rec *rr = recs + i % 7u;
      uint64_t big = (uint64_t)x[i * 4] * 0x100000001ull + i;
      want[i * 4] = s;
      want[i * 4 + 1] = rr->c[i & 3u] + rr->a;
      want[i * 4 + 2] = (uint32_t)(big >> 13) ^ (uint32_t)(big % 1000003ull);
      want[i * 4 + 3] = (uint32_t)(rr->b * 4.0f);
   }
   bool ok = check_u32(r, "out", S(r, 3), want, n * 4, 4);
   free(want);
   return ok;
}

/* --- simd_funcs ----------------------------------------------------------------------------------------------- */

static void
simd_funcs_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   float *fin = slot(r, 1, n * 4);
   slot(r, 2, n * 8 * 4);
   slot(r, 3, n * 2 * 4);
   for (unsigned i = 0; i < n; i++) {
      in[i] = (i / 32) % 2 ? rnd() % 8 : 1 + rnd() % 999;   /* every other SIMD-group has no zero */
      fin[i] = (float)((int)(rnd() % 128) - 64) * 0.25f;
   }
}

static bool
simd_funcs_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   const float *fin = S(r, 1);
   uint32_t *want = calloc(n * 8, 4);
   float *fwant = calloc(n * 2, 4);
   for (unsigned g = 0; g < n / 32; g++) {
      const uint32_t *v = in + g * 32;
      const float *f = fin + g * 32;
      uint32_t sum = 0, mx = 0, mn = ~0u, ballot = 0;
      bool all = true, any = false;
      float fsum = 0;
      for (unsigned l = 0; l < 32; l++) {
         sum += v[l];
         mx = v[l] > mx ? v[l] : mx;
         mn = v[l] < mn ? v[l] : mn;
         ballot |= ((v[l] & 3u) == 0u) << l;
         all &= v[l] != 0u;
         any |= v[l] == 5u;
         fsum += f[l];
      }
      uint32_t pre = 0;
      float fpre = 0;
      for (unsigned l = 0; l < 32; l++) {
         uint32_t *o = want + (g * 32 + l) * 8;
         o[0] = sum;
         o[1] = mx;
         o[2] = pre;
         pre += v[l];
         o[3] = v[(l * 5u + 3u) & 31u];
         o[4] = v[7];
         o[5] = l + 3 < 32 ? v[l + 3] : v[l];
         o[6] = ballot;
         o[7] = (uint32_t)all | ((uint32_t)any << 1) | (mn & ~3u);
         fpre += f[l];
         fwant[(g * 32 + l) * 2] = fsum;
         fwant[(g * 32 + l) * 2 + 1] = fpre;
      }
   }
   static const float zero[2] = { 0, 0 };
   bool ok = check_u32(r, "out", S(r, 2), want, n * 8, 8) && check_f32(r, "fout", S(r, 3), fwant, n * 2, 2, zero, zero);
   free(want);
   free(fwant);
   return ok;
}

/* --- math_lib ------------------------------------------------------------------------------------------------- */

static void
math_lib_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4);
   int32_t *k = slot(r, 2, n * 4);
   slot(r, 3, n * 16 * 4);
   slot(r, 4, n * 8 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = rndf(-6, 6);
      b[i] = rndf(0.5f, 3) * ((rnd() & 1) ? -1.0f : 1.0f);
      k[i] = (int32_t)rnd();
   }
   a[0] = 2.5f;
   a[1] = -2.5f;
   a[2] = 0.0f;
   k[0] = INT32_MAX;
   k[1] = 5;
   k[2] = INT32_MIN;
   k[3] = 7;
}

static float
smoothstep_ref(float e0, float e1, float x)
{
   float t = (x - e0) / (e1 - e0);
   t = t < 0 ? 0 : t > 1 ? 1 : t;
   return t * t * (3.0f - 2.0f * t);
}

static bool
math_lib_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1);
   const int32_t *kk = S(r, 2);
   float *want = calloc(n * 16, 4);
   uint32_t *iwant = calloc(n * 8, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = a[i], y = b[i], *o = want + i * 16;
      o[0] = roundf(x);
      o[1] = truncf(x);
      o[2] = ceilf(x);
      o[3] = x - floorf(x);
      o[4] = fmodf(x, y);
      o[5] = copysignf(y, x);
      o[6] = x < -1.0f ? -1.0f : x > 2.0f ? 2.0f : x;
      o[7] = x + (y - x) * 0.25f;
      o[8] = (x < y ? 0.0f : 1.0f) + smoothstep_ref(-1.0f, 1.0f, x);
      o[9] = (x < 0 ? 0 : x > 1 ? 1 : x) + (y > 0 ? 1.0f : y < 0 ? -1.0f : 0.0f);
      o[10] = powf(fabsf(x) + 0.5f, 1.5f);
      o[11] = atan2f(x, y);
      float cx = x * 0.1f, cy = y * 0.1f;
      o[12] = asinf(cx < -1 ? -1 : cx > 1 ? 1 : cx) + acosf(cy < -1 ? -1 : cy > 1 ? 1 : cy);
      o[13] = sinhf(x * 0.5f);
      o[14] = h2f(f2h(h2f(f2h(x)) * 0.5f + h2f(f2h(y))));
      o[15] = exp2f(x) - logf(fabsf(y) + 1.0f);
      int32_t k = kk[i], m = kk[i ^ 1u];
      uint32_t uk = (uint32_t)k, um = (uint32_t)m, *u = iwant + i * 8;
      int64_t sum = (int64_t)k + m, dif = (int64_t)k - m;
      u[0] = (uint32_t)(int32_t)(sum > INT32_MAX ? INT32_MAX : sum < INT32_MIN ? INT32_MIN : sum);
      u[1] = (uint32_t)(int32_t)(dif > INT32_MAX ? INT32_MAX : dif < INT32_MIN ? INT32_MIN : dif);
      u[2] = (uint32_t)(dif < 0 ? -dif : dif);
      u[3] = (uk >> 3) & 0x1ffu;
      u[4] = (uint32_t)((int32_t)(uk << (32 - 12)) >> (32 - 7));
      uint32_t rev = 0;
      for (unsigned bit = 0; bit < 32; bit++)
         rev |= ((uk >> bit) & 1u) << (31 - bit);
      u[5] = rev;
      uint32_t uh = (uint32_t)(((uint64_t)uk + um) >> 1);
      int32_t ir = (int32_t)((sum + 1) >> 1);
      u[6] = uh ^ (uint32_t)ir;
      u[7] = ((uk & ~(0xfffu << 4)) | ((um & 0xfffu) << 4)) + ((uk & 0xffffu) * (um & 0xffffu) + 5u);
   }
   static const float abs_tol[16] = { 0, 0, 0, 0, 1e-4f, 0, 0, 1e-6f, 1e-6f, 0, 0, 2e-5f, 1e-4f, 2e-6f, 4e-3f, 1e-4f };
   static const float rel_tol[16] = { 0, 0, 0, 1e-6f, 0, 0, 0, 1e-6f, 1e-6f, 0, 1e-5f, 1e-6f, 0, 1e-4f, 0, 1e-5f };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 16, 16, abs_tol, rel_tol) &&
             check_u32(r, "iout", S(r, 4), iwant, n * 8, 8);
   free(want);
   free(iwant);
   return ok;
}

/* --- mat_const ------------------------------------------------------------------------------------------------ */

struct params {   /* struct Params in mat_const.metal */
   float m[4][4];  /* column-major: m[column][row] */
   float scale[2];
   uint32_t count;
   int32_t offset;
};
_Static_assert(sizeof(struct params) == 80, "Params is 80 bytes in Metal");

static void
mat_const_setup(struct run *r)
{
   unsigned n = r->threads;
   struct params *p = slot(r, 0, sizeof *p);
   float *in = slot(r, 1, n * 16);
   slot(r, 2, n * 16);
   for (unsigned c = 0; c < 4; c++) {
      for (unsigned k = 0; k < 4; k++)
         p->m[c][k] = rndf(-2, 2);
   }
   p->scale[0] = 1.5f;
   p->scale[1] = -0.75f;
   p->count = 40;
   p->offset = -13;
   for (unsigned i = 0; i < n * 4; i++)
      in[i] = rndf(-10, 10);
}

static bool
mat_const_check(struct run *r)
{
   unsigned n = r->threads;
   const struct params *p = S(r, 0);
   const float *in = S(r, 1);
   float *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      float v[4], rr[4];
      memcpy(v, in + i * 4, sizeof v);
      for (unsigned row = 0; row < 4; row++) {
         rr[row] = 0;
         for (unsigned c = 0; c < 4; c++)
            rr[row] += p->m[c][row] * v[c];
      }
      rr[0] *= p->scale[0];
      rr[1] *= p->scale[1];
      rr[3] += (float)((int32_t)p->count + p->offset);
      for (unsigned row = 0; row < 4; row++) {
         float s = 0;
         for (unsigned c = 0; c < 4; c++)
            s += p->m[row][c] * rr[c];
         want[i * 4 + row] = s * 0.0625f;
      }
   }
   static const float abs_tol[4] = { 1e-4f, 1e-4f, 1e-4f, 1e-4f }, rel_tol[4] = { 1e-5f, 1e-5f, 1e-5f, 1e-5f };
   bool ok = check_f32(r, "out", S(r, 2), want, n * 4, 4, abs_tol, rel_tol);
   free(want);
   return ok;
}

/* --- ctrl_flow ------------------------------------------------------------------------------------------------ */

static void
ctrl_flow_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
   in[0] = 0;
   in[1] = 0xffffffffu;
}

static bool
ctrl_flow_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], acc = 0;
      for (uint32_t a = 0; a < 8u; a++) {
         if (((x >> a) & 1u) == 0u)
            continue;
         for (uint32_t b = 0; b < 8u; b++) {
            if (b > a)
               break;
            acc += a * 8u + b;
            if (acc > 300u)
               break;
         }
      }
      uint32_t y = x, steps = 0;
      do {
         y = y * 1103515245u + 12345u;
         steps++;
      } while ((y >> 28) != 0u && steps < 40u);
      want[i * 3] = acc;
      want[i * 3 + 1] = steps;
      want[i * 3 + 2] = 99u;
      for (uint32_t k = 0; k < 32u; k++) {
         if ((((x * 2654435761u) >> k) & 1u) && k > (x & 15u)) {
            want[i * 3 + 2] = k;
            break;
         }
      }
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 3, 3);
   free(want);
   return ok;
}

/* --- atomic_float --------------------------------------------------------------------------------------------- */

static void
atomic_float_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, 16);
   slot(r, 2, 16);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
atomic_float_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   float want[4] = { 0 };
   uint32_t cwant[4] = { 0 };
   for (unsigned i = 0; i < n; i++) {
      want[in[i] & 3u] += (float)(in[i] & 255u) * 0.25f;
      cwant[in[i] & 3u] += in[i] >> 20;
   }
   static const float zero[1] = { 0 };
   return check_f32(r, "sums", S(r, 1), want, 4, 1, zero, zero) && check_u32(r, "counts", S(r, 2), cwant, 4, 1);
}

/* --- grid3d --------------------------------------------------------------------------------------------------- */

static void
grid3d_setup(struct run *r)
{
   slot(r, 0, r->threads * 2 * 4);
}

static bool
grid3d_check(struct run *r)
{
   const unsigned W = 16, H = 8, D = 8;
   uint32_t *want = calloc(W * H * D * 2, 4);
   for (unsigned z = 0; z < D; z++) {
      for (unsigned y = 0; y < H; y++) {
         for (unsigned x = 0; x < W; x++) {
            uint32_t *o = want + ((z * H + y) * W + x) * 2;
            unsigned lx = x % 4, ly = y % 4, lz = z % 4;
            o[0] = x | (y << 8) | (z << 16) | ((lx + ly * 4 + lz * 16) << 24);
            o[1] = lx | (ly << 4) | (lz << 8) | ((x / 4) << 12) | ((y / 4) << 16) | ((z / 4) << 20);
         }
      }
   }
   bool ok = check_u32(r, "out", S(r, 0), want, W * H * D * 2, 2);
   free(want);
   return ok;
}

/* --- agg_ops -------------------------------------------------------------------------------------------------- */

static void
agg_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   float *in = slot(r, 0, n * 16);
   slot(r, 1, n * 16);
   slot(r, 2, n * 8);
   for (unsigned i = 0; i < n * 4; i++)
      in[i] = rndf(-100, 100);
}

struct pair {
   float v[4];
   uint32_t tag;
   uint16_t small;
};

static struct pair
make_pair_ref(const float *in, uint32_t i, uint32_t mode)
{
   struct pair p;
   for (unsigned k = 0; k < 4; k++)
      p.v[k] = (mode & 1u) ? in[i * 4 + k] * 2.0f : in[i * 4 + 3 - k] + 1.0f;
   p.tag = (mode & 1u) ? i : ~i;
   p.small = (uint16_t)((mode & 1u) ? i * 3u : i + 7u);
   return p;
}

static bool
agg_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const float *in = S(r, 0);
   float *want = calloc(n * 4, 4);
   uint32_t *twant = calloc(n * 2, 4);
   for (uint32_t i = 0; i < n; i++) {
      struct pair p = make_pair_ref(in, i, i), q = make_pair_ref(in, i ^ 1u, i >> 1);
      for (unsigned k = 0; k < 4; k++)
         want[i * 4 + k] = p.v[k] - q.v[(k + 1) & 3];
      twant[i * 2] = p.tag ^ q.small;
      twant[i * 2 + 1] = q.tag + p.small;
   }
   static const float zero[4] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n * 4, 4, zero, zero) && check_u32(r, "tags", S(r, 2), twant, n * 2, 2);
   free(want);
   free(twant);
   return ok;
}

/* --- fc_refuse (round 12: no constant given; use_scale reads 0, as the translator reads it: a[i] + 1) ------- */

static void
fc_refuse_setup(struct run *r)
{
   float *a = slot(r, 0, r->threads * 4), *keep = slot(r, 30, r->threads * 4);
   for (unsigned i = 0; i < r->threads; i++)
      keep[i] = a[i] = rndf(-8, 8);
}

static bool
fc_refuse_check(struct run *r)
{
   unsigned n = r->threads;
   const float *before = S(r, 30);
   float *want = calloc(n, 4);
   for (unsigned i = 0; i < n; i++)
      want[i] = before[i] + 1.0f;
   static const float zero[1] = { 0 };
   bool ok = check_f32(r, "a", S(r, 0), want, n, 1, zero, zero);
   free(want);
   return ok;
}

/* --- fc_kernel ------------------------------------------------------------------------------------------------ */

#define FC_F32(f) { (uint8_t)(f), (uint8_t)((f) >> 8), (uint8_t)((f) >> 16), (uint8_t)((f) >> 24) }
/* set A: every constant given; set B leaves out scale (read only under use_scale, which is false) and bias (it has a
 * default); set C leaves out scale while use_scale is true: scale reads 0, as the translator reads a constant left out
 * (round 12; it was refused before) */
static const struct fcset fc_a = { 5, { { 0, 1, { 1 } }, { 1, 4, FC_F32(0x40200000u) /* 2.5 */ }, { 2, 4, { 1 } },
                                        { 3, 4, { 7 } }, { 4, 1, { 1 } } } };
static const struct fcset fc_b = { 3, { { 0, 1, { 0 } }, { 2, 4, { 2 } }, { 4, 1, { 0 } } } };
static const struct fcset fc_c = { 3, { { 0, 1, { 1 } }, { 2, 4, { 0 } }, { 4, 1, { 0 } } } };

static const uint8_t *
fc_get(const struct fcset *fc, uint32_t index)
{
   for (unsigned i = 0; fc && i < fc->n; i++) {
      if (fc->v[i].index == index)
         return fc->v[i].bytes;
   }
   return NULL;
}

static void
fc_kernel_setup(struct run *r)
{
   unsigned n = r->threads;
   float *in = slot(r, 0, n * 4);
   slot(r, 1, n * 4);
   uint32_t *extra = slot(r, 2, n * 4);
   slot(r, 3, n * 4);
   for (unsigned i = 0; i < n; i++) {
      in[i] = rndf(-8, 8);
      extra[i] = rnd() & 0xffffu;
   }
}

static bool
fc_kernel_check(struct run *r)
{
   const struct fcset *fc = r->t->fc;
   unsigned n = r->threads;
   const float *in = S(r, 0);
   const uint32_t *extra = S(r, 2);
   const uint8_t *p;
   bool use_scale = (p = fc_get(fc, 0)) && p[0], has_extra = (p = fc_get(fc, 4)) && p[0];
   float scale = 0;
   int32_t mode = 0;
   uint32_t bias = 1000;
   if ((p = fc_get(fc, 1)))
      memcpy(&scale, p, 4);
   if ((p = fc_get(fc, 2)))
      memcpy(&mode, p, 4);
   if ((p = fc_get(fc, 3)))
      memcpy(&bias, p, 4);
   float *want = calloc(n, 4);
   uint32_t *iwant = calloc(n, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = in[i];
      if (use_scale)
         x *= scale;
      x = mode == 0 ? x + 1.0f : mode == 1 ? -x : x * x;
      want[i] = x;
      iwant[i] = i + bias + (has_extra ? extra[i] : 0);
   }
   static const float zero[1] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n, 1, zero, zero) && check_u32(r, "iout", S(r, 3), iwant, n, 1);
   free(want);
   free(iwant);
   return ok;
}

/* --- textures ------------------------------------------------------------------------------------------------- */

#define U8(v) ((float)(v) / 255.0f)

static bool
check_f4(struct run *r, const char *what, const float *got, const float *want, unsigned n, float tol)
{
   for (unsigned i = 0; i < n; i++) {
      if (!(fabsf(got[i] - want[i]) <= tol))
         return FAILF(r, "%s[%u] (item %u, lane %u) = %.7g, expected %.7g (tolerance %g)", what, i, i / 4, i % 4, got[i],
                      want[i], tol);
   }
   return true;
}

static void
fill_bytes(struct run *r, unsigned i)
{
   struct timg *x = &r->tex[i];
   uint8_t *p = x->stage.map;
   for (size_t k = 0; k < x->stage.size; k++)
      p[k] = (uint8_t)rnd();
}

/* tex_read: rgba8 (2 levels), r8, rgba16f, r32f, r32ui read with texel fetches */
static void
tex_read_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 16, 16, 1, 1, 2, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8_UNORM, 1, 16, 16, 1, 1, 1, false);
   tex_image(r, 2, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 16, 16, 1, 1, 1, false);
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 16, 16, 1, 1, 1, false);
   tex_image(r, 4, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_UINT, 4, 16, 16, 1, 1, 1, false);
   fill_bytes(r, 0);
   fill_bytes(r, 1);
   fill_bytes(r, 4);
   uint16_t *h = r->tex[2].stage.map;
   for (unsigned k = 0; k < 16 * 16 * 4; k++)
      h[k] = f2h(rndf(-50, 50));
   float *f = r->tex[3].stage.map;
   for (unsigned k = 0; k < 16 * 16; k++)
      f[k] = rndf(-1000, 1000);
   slot(r, 0, 256 * 5 * 16);
   slot(r, 1, 256 * 16);
}

static bool
tex_read_check(struct run *r)
{
   float *want = calloc(256 * 5 * 4, 4);
   uint32_t *uwant = calloc(256 * 4, 4);
   for (uint32_t y = 0; y < 16; y++) {
      for (uint32_t x = 0; x < 16; x++) {
         unsigned i = y * 16 + x;
         float *o = want + i * 20;
         const uint8_t *a = tex_at(r, 0, 0, x, y, 0), *a1 = tex_at(r, 0, 1, x / 2, y / 2, 0);
         const uint8_t *b = tex_at(r, 1, 0, x, y, 0);
         const uint16_t *h = tex_at(r, 2, 0, x, y, 0);
         const float *f = tex_at(r, 3, 0, x, y, 0);
         const uint32_t *u = tex_at(r, 4, 0, x, y, 0);
         for (unsigned c = 0; c < 4; c++) {
            o[c] = U8(a[c]);
            o[4 + c] = U8(a1[c]);
            o[12 + c] = h2f(h[c]);
         }
         o[8] = U8(b[0]), o[9] = 0, o[10] = 0, o[11] = 1;
         o[16] = f[0], o[17] = 0, o[18] = 0, o[19] = 1;
         uwant[i * 4] = u[0], uwant[i * 4 + 1] = 0, uwant[i * 4 + 2] = 0, uwant[i * 4 + 3] = 1;
      }
   }
   bool ok = check_f4(r, "out", S(r, 0), want, 256 * 20, 1e-6f) && check_u32(r, "uout", S(r, 1), uwant, 256 * 4, 4);
   free(want);
   free(uwant);
   return ok;
}

/* tex_arrays: array<texture2d<float>, 4> at [[texture(1)]] (rgba8 at slots 1 to 4) by known and run-time indices, and
 * array<texture2d<float, access::write>, 2> at [[texture(8)]] (rgba16f at slots 8 and 9) written */
static const uint32_t tex_arrays_k[2] = { 1, 7 };   /* 7: past the end, the last element (the translator's clamp) */

static void
tex_arrays_setup(struct run *r)
{
   for (unsigned i = 1; i <= 4; i++) {
      tex_image(r, i, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
      fill_bytes(r, i);
   }
   for (unsigned i = 8; i <= 9; i++)
      tex_image(r, i, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 1, 1, true);
   slot(r, 0, 64 * 4 * 16);
   memcpy(slot(r, 1, sizeof tex_arrays_k), tex_arrays_k, sizeof tex_arrays_k);
}

static bool
tex_arrays_check(struct run *r)
{
   float *want = calloc(64 * 16, 4), *w0 = calloc(64 * 4, 4), *w1 = calloc(64 * 4, 4);
   for (uint32_t y = 0; y < 8; y++) {
      for (uint32_t x = 0; x < 8; x++) {
         const unsigned i = y * 8 + x;
         float *o = want + i * 16;
         const unsigned dyn = 1 + (i & 3), kk = tex_arrays_k[i & 1] > 3 ? 3 : tex_arrays_k[i & 1];
         for (unsigned c = 0; c < 4; c++) {
            o[c] = U8(((const uint8_t *)tex_at(r, 3, 0, x, y, 0))[c]);
            o[4 + c] = U8(((const uint8_t *)tex_at(r, dyn, 0, x, y, 0))[c]);
            o[8 + c] = U8(((const uint8_t *)tex_at(r, 1 + kk, 0, y, x, 0))[c]);
            o[12 + c] = U8(((const uint8_t *)tex_at(r, 1, 0, x, y, 0))[c]) + U8(((const uint8_t *)tex_at(r, 4, 0, x, y, 0))[c]);
            w0[i * 4 + c] = o[12 + c];
            w1[i * 4 + c] = U8(((const uint8_t *)tex_at(r, 2, 0, x, y, 0))[c]) * 2.0f;
         }
      }
   }
   float *g0 = calloc(64 * 4, 4), *g1 = calloc(64 * 4, 4);
   for (unsigned k = 0; k < 64 * 4; k++) {
      g0[k] = h2f(((const uint16_t *)tex_at(r, 8, 0, 0, 0, 0))[k]);
      g1[k] = h2f(((const uint16_t *)tex_at(r, 9, 0, 0, 0, 0))[k]);
   }
   bool ok = check_f4(r, "out", S(r, 0), want, 64 * 16, 1e-6f) && check_f4(r, "w[0]", g0, w0, 64 * 4, 2e-3f) &&
             check_f4(r, "w[1]", g1, w1, 64 * 4, 2e-3f);
   free(g0);
   free(g1);
   free(want);
   free(w0);
   free(w1);
   return ok;
}

/* tex_write: storage rgba8, r32f, r32ui (read_write), rgba16f */
static void
tex_write_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 16, 16, 1, 1, 1, true);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 16, 16, 1, 1, 1, true);
   tex_image(r, 2, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_UINT, 4, 16, 16, 1, 1, 1, true);
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 16, 16, 1, 1, 1, true);
   uint32_t *u = r->tex[2].stage.map;
   for (unsigned k = 0; k < 256; k++)
      u[k] = rnd() & 0xffffffu;
   /* a copy of what the GPU reads, for the check (stage is overwritten by the readback) */
   uint32_t *keep = slot(r, 30, 256 * 4);
   memcpy(keep, u, 256 * 4);
}

static bool
tex_write_check(struct run *r)
{
   const uint32_t *before = S(r, 30);
   for (uint32_t y = 0; y < 16; y++) {
      for (uint32_t x = 0; x < 16; x++) {
         float px = (float)x / 15.0f, py = (float)y / 15.0f;
         float v8[4] = { px, py, 1.0f - px, 0.5f };
         const uint8_t *g8 = tex_at(r, 0, 0, x, y, 0);
         for (unsigned c = 0; c < 4; c++) {
            int want = (int)floorf(v8[c] * 255.0f + 0.5f);
            if (abs((int)g8[c] - want) > 1)   /* a tie (0.5 * 255) may round either way */
               return FAILF(r, "rgba8 (%u,%u)[%u] = %u, expected %d", x, y, c, g8[c], want);
         }
         float gf = *(const float *)tex_at(r, 1, 0, x, y, 0), wf = px * 100.0f + py;
         if (fabsf(gf - wf) > 1e-4f)
            return FAILF(r, "r32f (%u,%u) = %.7g, expected %.7g", x, y, gf, wf);
         uint32_t gu = *(const uint32_t *)tex_at(r, 2, 0, x, y, 0), wu = before[y * 16 + x] * 3u + 1u;
         if (gu != wu)
            return FAILF(r, "r32ui (%u,%u) = %u, expected %u", x, y, gu, wu);
         const uint16_t *gh = tex_at(r, 3, 0, x, y, 0);
         float vh[4] = { px, py, -1.5f, 2.0f };
         for (unsigned c = 0; c < 4; c++) {
            float want = h2f(f2h(vh[c]));
            if (fabsf(h2f(gh[c]) - want) > fabsf(want) * 1e-3f)
               return FAILF(r, "rgba16f (%u,%u)[%u] = %g, expected %g", x, y, c, h2f(gh[c]), want);
         }
      }
   }
   return true;
}

/* tex_array3d: 2D-array and 3D reads, 3D and 2D-array writes */
static void
tex_array3d_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 3, 1, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_3D, VK_FORMAT_R32_SFLOAT, 4, 8, 8, 4, 1, 1, false);
   tex_image(r, 2, VK_IMAGE_VIEW_TYPE_3D, VK_FORMAT_R32_SFLOAT, 4, 8, 8, 4, 1, 1, true);
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R32_UINT, 4, 8, 8, 1, 3, 1, true);
   fill_bytes(r, 0);
   float *f = r->tex[1].stage.map;
   for (unsigned k = 0; k < 8 * 8 * 4; k++)
      f[k] = rndf(-10, 10);
   slot(r, 0, 256 * 2 * 16);
}

static bool
tex_array3d_check(struct run *r)
{
   float *want = calloc(256 * 8, 4);
   for (uint32_t z = 0; z < 4; z++) {
      for (uint32_t y = 0; y < 8; y++) {
         for (uint32_t x = 0; x < 8; x++) {
            unsigned i = (z * 8 + y) * 8 + x;
            const uint8_t *a = tex_at(r, 0, 0, x, y, z % 3);
            for (unsigned c = 0; c < 4; c++)
               want[i * 8 + c] = U8(a[c]);
            want[i * 8 + 4] = *(const float *)tex_at(r, 1, 0, x, y, z);
            want[i * 8 + 5] = 0, want[i * 8 + 6] = 0, want[i * 8 + 7] = 1;
            float gv = *(const float *)tex_at(r, 2, 0, x, y, z);
            if (gv != (float)i * 0.5f)
               return FAILF(r, "3D write (%u,%u,%u) = %g, expected %g", x, y, z, gv, (float)i * 0.5f);
            if (z < 3 && *(const uint32_t *)tex_at(r, 3, 0, x, y, z) != i * 7u)
               return FAILF(r, "2D-array write (%u,%u,%u) = %u, expected %u", x, y, z, *(const uint32_t *)tex_at(r, 3, 0, x, y, z), i * 7u);
         }
      }
   }
   bool ok = check_f4(r, "out", S(r, 0), want, 256 * 8, 1e-6f);
   free(want);
   return ok;
}

/* sampling references: Vulkan's nearest and linear filtering of a normalized coordinate */
static int
wrap(int i, int n, VkSamplerAddressMode m)
{
   if (m == VK_SAMPLER_ADDRESS_MODE_REPEAT)
      return ((i % n) + n) % n;
   return i < 0 ? 0 : i >= n ? n - 1 : i;
}

static float
rgba8_at(struct run *r, unsigned t, uint32_t m, int x, int y, uint32_t layer, unsigned c)
{
   return U8(((const uint8_t *)tex_at(r, t, m, (uint32_t)x, (uint32_t)y, layer))[c]);
}

static void
sample_nearest(struct run *r, unsigned t, uint32_t m, float u, float v, uint32_t layer, VkSamplerAddressMode mode, float *out)
{
   int w = (int)mip_size(r->tex[t].w, m), h = (int)mip_size(r->tex[t].h, m);
   int x = wrap((int)floorf(u * w), w, mode), y = wrap((int)floorf(v * h), h, mode);
   for (unsigned c = 0; c < 4; c++)
      out[c] = rgba8_at(r, t, m, x, y, layer, c);
}

static void
sample_linear(struct run *r, unsigned t, uint32_t m, float u, float v, VkSamplerAddressMode mode, float *out)
{
   int w = (int)mip_size(r->tex[t].w, m), h = (int)mip_size(r->tex[t].h, m);
   float fu = u * w - 0.5f, fv = v * h - 0.5f;
   int i0 = (int)floorf(fu), j0 = (int)floorf(fv);
   float a = fu - floorf(fu), b = fv - floorf(fv);
   int i1 = wrap(i0 + 1, w, mode), j1 = wrap(j0 + 1, h, mode);
   i0 = wrap(i0, w, mode), j0 = wrap(j0, h, mode);
   for (unsigned c = 0; c < 4; c++)
      out[c] = (1 - a) * (1 - b) * rgba8_at(r, t, m, i0, j0, 0, c) + a * (1 - b) * rgba8_at(r, t, m, i1, j0, 0, c) +
               (1 - a) * b * rgba8_at(r, t, m, i0, j1, 0, c) + a * b * rgba8_at(r, t, m, i1, j1, 0, c);
}

/* a coordinate a jitter of 0.1 to 0.4 texel off a texel centre, for n texels; k texels below 0 to above 1 */
static float
centre_coord(int n, int lo, int hi)
{
   int k = lo + (int)(rnd() % (uint32_t)(hi - lo));
   float j = rndf(0.1f, 0.4f) * ((rnd() & 1) ? 1.0f : -1.0f);
   return ((float)k + 0.5f + j) / (float)n;
}

static void
tex_sample_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 2, false);
   fill_bytes(r, 0);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 16);
   samp_create(r, 1, VK_FILTER_LINEAR, VK_SAMPLER_ADDRESS_MODE_REPEAT, VK_SAMPLER_MIPMAP_MODE_NEAREST, 16);
   slot(r, 0, 256 * 5 * 16);
   float *uv = slot(r, 1, 256 * 8);
   for (unsigned i = 0; i < 256; i++) {
      uv[i * 2] = centre_coord(8, -8, 16);
      uv[i * 2 + 1] = centre_coord(8, -8, 16);
   }
}

static bool
tex_sample_expect(struct run *r, const struct nvmtl_air_nir_result *res)
{
   /* the constexpr sampler takes slot 2, the first no [[sampler(n)]] argument takes, as the translator's reflection says */
   if (res->static_sampler_count != 1 || res->static_samplers[0].slot != 2 || res->sampler_mask != 7)
      return FAILF(r, "constexpr samplers %u, the first at slot %u, sampler mask 0x%x: expected one at slot 2, mask 0x7",
                   res->static_sampler_count, res->static_samplers[0].slot, res->sampler_mask);
   return true;
}

static bool
tex_sample_check(struct run *r)
{
   const float *uv = S(r, 1), *got = S(r, 0);
   for (unsigned i = 0; i < 256; i++) {
      float u = uv[i * 2], v = uv[i * 2 + 1], w[5][4];
      sample_nearest(r, 0, 0, u, v, 0, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, w[0]);
      sample_linear(r, 0, 0, u, v, VK_SAMPLER_ADDRESS_MODE_REPEAT, w[1]);
      sample_linear(r, 0, 1, u, v, VK_SAMPLER_ADDRESS_MODE_REPEAT, w[2]);
      sample_linear(r, 0, 0, u, v, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, w[3]);
      /* gather of red: (i0,j1), (i1,j1), (i1,j0), (i0,j0) of the bilinear footprint, clamped */
      int i0 = (int)floorf(u * 8 - 0.5f), j0 = (int)floorf(v * 8 - 0.5f);
      VkSamplerAddressMode cl = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
      int x0 = wrap(i0, 8, cl), x1 = wrap(i0 + 1, 8, cl), y0 = wrap(j0, 8, cl), y1 = wrap(j0 + 1, 8, cl);
      w[4][0] = rgba8_at(r, 0, 0, x0, y1, 0, 0), w[4][1] = rgba8_at(r, 0, 0, x1, y1, 0, 0);
      w[4][2] = rgba8_at(r, 0, 0, x1, y0, 0, 0), w[4][3] = rgba8_at(r, 0, 0, x0, y0, 0, 0);
      static const char *what[5] = { "nearest clamp", "linear repeat", "linear repeat level 1", "constexpr linear clamp",
                                     "gather" };
      /* linear filtering: NVIDIA's 8-bit filter weights give up to 2/256 of the texel difference */
      static const float tol[5] = { 1e-6f, 1.0f / 128, 1.0f / 128, 1.0f / 128, 1e-6f };
      for (unsigned k = 0; k < 5; k++) {
         for (unsigned c = 0; c < 4; c++) {
            float g = got[(i * 5 + k) * 4 + c];
            if (!(fabsf(g - w[k][c]) <= tol[k]))
               return FAILF(r, "%s, thread %u (uv %.5f %.5f) lane %u = %.6f, expected %.6f (tolerance %g)", what[k], i, u, v,
                            c, g, w[k][c], tol[k]);
         }
      }
   }
   return true;
}

/* oob_kernel (metal/oob_kernel.metal, ROBUST_ONLY): a constant palette of 16 read at indices the data gives, some past
 * its end, some with byte offsets past 4 GiB (32-bit, they wrap), and a buffer of 5 ushorts read at indices up to 255
 * (the 4-byte rounding of its 10 bytes not compared) */
#define OOBK_PAL 16u
#define OOBK_H 5u
static void
oob_kernel_setup(struct run *r)
{
   float *pal = slot(r, 0, OOBK_PAL * 16);
   for (unsigned i = 0; i < OOBK_PAL * 4; i++)
      pal[i] = (float)(i + 1) * 0.25f;
   uint32_t *idx = slot(r, 1, 256 * 4);
   for (unsigned i = 0; i < 256; i++)
      idx[i] = (i % 5) == 1 ? OOBK_PAL + i * 3 : (i % 7) == 2 ? 0x10000000u * (1 + i % 15) + i % (OOBK_PAL + 3) : i % OOBK_PAL;
   slot(r, 2, 256 * 16);
   uint16_t *h = slot(r, 3, OOBK_H * 2);
   for (unsigned i = 0; i < OOBK_H; i++)
      h[i] = (uint16_t)(100 + 7 * i);
}

static bool
oob_kernel_check(struct run *r)
{
   const float *pal = S(r, 0), *out = S(r, 2);
   const uint32_t *idx = S(r, 1);
   const uint16_t *h = S(r, 3);
   for (unsigned i = 0; i < 256; i++) {
      const uint32_t at = idx[i] * 16u, e = at / 16u, k = idx[i] & 0xff;
      float want[4];
      for (unsigned c = 0; c < 4; c++)
         want[c] = e < OOBK_PAL ? pal[e * 4 + c] : 0.0f;
      if (k < OOBK_H)
         want[0] += (float)h[k];
      else if (k * 2 < ((OOBK_H * 2 + 3) & ~3u))
         want[0] = out[i * 4];   /* within the rounding: read as the memory there is */
      if (!check_f4(r, "out", out + i * 4, want, 4, 0))
         return false;
   }
   return true;
}

/* ab_kernel: argument buffers the CPU writes as Metal lays them out (metal/ab_kernel.metal): a texture field holds the
 * texture's gpuResourceID, a sampler field the sampler's, a pointer field the buffer's GPU address. Textures 0 to 4 and
 * 6 to 9 rgba8, 5 rgba16f (written); sampler 0 linear clamp, 1 nearest repeat, 2 nearest clamp; slot 0 the argument
 * buffer, 1 the output, 2 sel, 3 the array of four Items; 4 (io), 5 (Inner), 6 (Inner's data) and 7 (the Items'
 * vectors) are reached only through addresses */
struct ab_args { uint64_t tex, smp, io; float scale; uint32_t pad; uint64_t texs[3], smps[2], inner, wtex; };
struct ab_inner { uint64_t data, t; uint32_t bias, pad; };
struct ab_item { uint64_t t, v; };
static const uint32_t ab_sel[3] = { 2, 0, 3 };

static void
ab_kernel_setup(struct run *r)
{
   for (unsigned i = 0; i <= 9; i++) {
      if (i == 5)
         continue;
      tex_image(r, i, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
      fill_bytes(r, i);
   }
   tex_image(r, 5, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 1, 1, true);
   samp_create(r, 0, VK_FILTER_LINEAR, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   samp_create(r, 1, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_REPEAT, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   samp_create(r, 2, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *io = slot(r, 4, 128 * 4);
   for (unsigned k = 0; k < 64; k++)
      io[k] = rndf(-8, 8);
   float *d = slot(r, 6, 64 * 4);
   for (unsigned k = 0; k < 64; k++)
      d[k] = rndf(-8, 8);
   struct ab_inner *in = slot(r, 5, sizeof *in);
   in->data = ab_addr(r, 6, 0);
   in->t = ab_tex(r, 4);
   in->bias = 17;
   struct ab_args *a = slot(r, 0, sizeof *a);
   a->tex = ab_tex(r, 0);
   a->smp = ab_samp(r, 0);
   a->io = ab_addr(r, 4, 0);
   a->scale = 2.5f;
   for (unsigned k = 0; k < 3; k++)
      a->texs[k] = ab_tex(r, 1 + k);
   a->smps[0] = ab_samp(r, 1);
   a->smps[1] = ab_samp(r, 2);
   a->inner = ab_addr(r, 5, 0);
   a->wtex = ab_tex(r, 5);
   slot(r, 1, 64 * 8 * 16);
   memcpy(slot(r, 2, sizeof ab_sel), ab_sel, sizeof ab_sel);
   float *v = slot(r, 7, 4 * 8 * 16);
   for (unsigned k = 0; k < 4 * 8 * 4; k++)
      v[k] = rndf(-4, 4);
   struct ab_item *it = slot(r, 3, 4 * sizeof *it);
   for (unsigned k = 0; k < 4; k++) {
      it[k].t = ab_tex(r, 6 + k);
      it[k].v = ab_addr(r, 7, k * 128u);
   }
}

static bool
ab_kernel_check(struct run *r)
{
   float *want = calloc(64 * 32, 4), *w5 = calloc(64 * 4, 4), *io2 = calloc(64, 4);
   const float *io = S(r, 4), *d = S(r, 6), *v = S(r, 7);
   for (uint32_t y = 0; y < 8; y++) {
      for (uint32_t x = 0; x < 8; x++) {
         const unsigned i = y * 8 + x;
         float *o = want + i * 32, s0[4], s1[4], s2[4];
         const float u = (x + 0.5f) / 8, vv = (y + 0.5f) / 8;
         sample_linear(r, 0, 0, (x + 0.75f) / 8, (y + 0.75f) / 8, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, s0);
         sample_nearest(r, 1, 0, u * 1.75f, vv * 1.75f, 0,
                        (i & 1) ? VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE : VK_SAMPLER_ADDRESS_MODE_REPEAT, s1);
         sample_nearest(r, 3, 0, u * 1.25f, vv * 1.25f, 0, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, s2);
         const unsigned item = (i + ab_sel[2]) & 3u;
         for (unsigned c = 0; c < 4; c++) {
            o[c] = s0[c];
            o[8 + c] = rgba8_at(r, 2, 0, x, y, 0, c);
            o[12 + c] = rgba8_at(r, 1 + ab_sel[i & 1], 0, x, y, 0, c);
            o[16 + c] = s1[c] + s2[c];
            o[20 + c] = rgba8_at(r, 4, 0, y, x, 0, c);
            o[24 + c] = rgba8_at(r, 6 + item, 0, x, y, 0, c) + v[item * 32 + (i & 7u) * 4 + c];
            w5[i * 4 + c] = rgba8_at(r, 3, 0, x, y, 0, c) * 2.0f;
         }
         o[4] = io[i] * 2.5f, o[5] = 2.5f, o[6] = 0, o[7] = 1;
         o[20] += d[i], o[21] += 17.0f;
         o[28] = (float)i, o[29] = o[30] = o[31] = 0;
         io2[i] = io[i] + 1.0f;
      }
   }
   float *g5 = calloc(64 * 4, 4);
   for (unsigned k = 0; k < 64 * 4; k++)
      g5[k] = h2f(((const uint16_t *)tex_at(r, 5, 0, 0, 0, 0))[k]);
   /* the linear sample: NVIDIA's 8-bit filter weights (tex_sample) */
   bool ok = true;
   for (unsigned i = 0; ok && i < 64; i++)
      ok = check_f4(r, "out (sample)", (const float *)S(r, 1) + i * 32, want + i * 32, 4, 1.0f / 128) &&
           check_f4(r, "out", (const float *)S(r, 1) + i * 32 + 4, want + i * 32 + 4, 28, 1e-6f);
   ok = ok && check_f4(r, "io[64..]", io + 64, io2, 64, 0) && check_f4(r, "wtex", g5, w5, 64 * 4, 2e-3f);
   free(want);
   free(w5);
   free(g5);
   free(io2);
   return ok;
}

/* ab_encoder: argument buffers filled through MTLArgumentEncoder (metal/ab_encoder.metal). Textures: 0 rgba8 (sampled,
 * sampler 0 nearest clamp), 1 and 2 rgba16f (the written array, element sel[i & 1]), 3 rgba16f read_write, 4 rgba16f
 * (the nested buffer's written texture), 5 an r32f texture_buffer read, 6 one written, 7 and 8 rgba16f (the two Items'
 * written textures, item (i + sel[2]) & 1). Slots: 0 the argument buffer, 1 the output, 2 sel, 3 the Items, 4 src,
 * 5 the nested argument buffer, 6 its output buffer. */
static const uint32_t abe_sel[3] = { 1, 0, 1 };
static const uint32_t abe_consts[4] = { 3, 5, 7, 11 };
static unsigned abe_len[3];   /* encodedLength of the encoders: EArgs, Inner2 (nested), WItem */

static void
ab_encoder_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 0);
   for (unsigned k = 1; k <= 4; k++)
      tex_image(r, k, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 1, 1, true);
   uint16_t *h = r->tex[3].stage.map;
   for (unsigned k = 0; k < 64 * 4; k++)
      h[k] = f2h((float)(rnd() % 64) * 0.125f);
   memcpy(slot(r, 30, 64 * 4 * 2), h, 64 * 4 * 2);   /* rw's texels as uploaded (the readback overwrites them) */
   float *tb = tex_buffer(r, 5, VK_FORMAT_R32_SFLOAT, 4, 64, false);
   for (unsigned k = 0; k < 64; k++)
      tb[k] = rndf(-100, 100);
   tex_buffer(r, 6, VK_FORMAT_R32_SFLOAT, 4, 64, true);
   for (unsigned k = 7; k <= 8; k++)
      tex_image(r, k, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 1, 1, true);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *src = slot(r, 4, 64 * 16);
   for (unsigned k = 0; k < 64 * 4; k++)
      src[k] = rndf(-8, 8);
   slot(r, 0, 256);
   slot(r, 1, 64 * 3 * 16);
   memcpy(slot(r, 2, sizeof abe_sel), abe_sel, sizeof abe_sel);
   slot(r, 3, 64);
   slot(r, 5, 64);
   slot(r, 6, 64 * 4);
   /* the argument buffer at [[buffer(0)]] */
   abe_len[0] = ab_enc_begin(r, 0, -1, 0, 0);
   ab_enc_tex(r, 0, 0);
   ab_enc_samp(r, 1, 0);
   ab_enc_buf(r, 2, 4, 0);
   uint32_t *c = ab_enc_const(r, 3);
   if (c)
      memcpy(c, abe_consts, sizeof abe_consts);
   ab_enc_tex(r, 4, 1);
   ab_enc_tex(r, 5, 2);
   ab_enc_tex(r, 6, 3);
   ab_enc_buf(r, 7, 5, 0);
   ab_enc_tex(r, 8, 5);
   ab_enc_tex(r, 9, 6);
   /* the one its [[id(7)]] points at, through the encoder that encoder gives for it */
   abe_len[1] = ab_enc_begin(r, 0, 7, 5, 0);
   ab_enc_buf(r, 0, 6, 0);
   ab_enc_tex(r, 1, 4);
   /* the two Items at [[buffer(3)]], one encoder's length apart */
   abe_len[2] = ab_enc_begin(r, 3, -1, 3, 0);
   ab_enc_tex(r, 0, 7);
   ab_enc_begin(r, 3, -1, 3, abe_len[2]);
   ab_enc_tex(r, 0, 8);
}

static bool
ab_encoder_check(struct run *r)
{
   if (abe_len[0] != 96 || abe_len[1] != 16 || abe_len[2] != 8)
      return FAILF(r, "encodedLength %u, nested %u, item %u: expected 96, 16, 8", abe_len[0], abe_len[1], abe_len[2]);
   float *want = calloc(64 * 12, 4), *w[6];
   for (unsigned k = 0; k < 6; k++)
      w[k] = calloc(64 * 4, 4);   /* wts[0], wts[1], rw, inner wt, item 0, item 1 */
   const float *src = S(r, 4), *tb = r->tex[5].stage.map;
   const uint16_t *rw0 = S(r, 30);
   for (unsigned q = 0; q < 64 * 4; q++)
      w[2][q] = h2f(rw0[q]) * 2.0f + 0.25f;
   for (uint32_t y = 0; y < 8; y++) {
      for (uint32_t x = 0; x < 8; x++) {
         const unsigned i = y * 8 + x;
         float *o = want + i * 12;
         for (unsigned c = 0; c < 4; c++)
            o[c] = rgba8_at(r, 0, 0, x, y, 0, c) + src[i * 4 + c] + (float)abe_consts[c];
         o[4] = tb[i], o[5] = 0, o[6] = 0, o[7] = 1;
         o[8] = (float)abe_sel[i & 1], o[9] = o[10] = o[11] = 0;
         const float wv[4] = { (float)i, 1, 2, 3 };
         memcpy(w[abe_sel[i & 1]] + i * 4, wv, sizeof wv);
         const float iv[4] = { 0.5f, (float)i * 0.25f, 0, 1 };
         memcpy(w[3] + i * 4, iv, sizeof iv);
         const float tv[4] = { (float)i * 0.5f, 0, 0, 1 };
         memcpy(w[4 + ((i + abe_sel[2]) & 1)] + i * 4, tv, sizeof tv);
      }
   }
   bool ok = check_f4(r, "out", S(r, 1), want, 64 * 12, 1e-6f);
   static const unsigned texi[6] = { 1, 2, 3, 4, 7, 8 }, wi[6] = { 0, 1, 2, 3, 4, 5 };
   static const char *const what[6] = { "wts[0]", "wts[1]", "rw", "inner wt", "item 0", "item 1" };
   for (unsigned k = 0; ok && k < 6; k++) {
      float *g = calloc(64 * 4, 4);
      for (unsigned q = 0; q < 64 * 4; q++)
         g[q] = h2f(((const uint16_t *)tex_at(r, texi[k], 0, 0, 0, 0))[q]);
      ok = check_f4(r, what[k], g, w[wi[k]], 64 * 4, 0);
      free(g);
   }
   const float *io = S(r, 6), *wtb = r->tex[6].stage.map;
   for (unsigned i = 0; ok && i < 64; i++) {
      if (io[i] != (float)i + 0.5f)
         ok = FAILF(r, "inner out[%u] = %g, expected %g", i, io[i], (float)i + 0.5f);
      else if (wtb[i] != (float)i * 2.0f)
         ok = FAILF(r, "wtb[%u] = %g, expected %g", i, wtb[i], (float)i * 2.0f);
   }
   for (unsigned k = 0; k < 6; k++)
      free(w[k]);
   free(want);
   return ok;
}

/* tex_cube: a cube sampled by direction (nearest), a 2D array by layer */
static void
tex_cube_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_CUBE, VK_FORMAT_R8G8B8A8_UNORM, 4, 4, 4, 1, 6, 1, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R8G8B8A8_UNORM, 4, 4, 4, 1, 2, 1, false);
   fill_bytes(r, 0);
   fill_bytes(r, 1);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *dir = slot(r, 0, 256 * 16);
   slot(r, 1, 256 * 2 * 16);
   for (unsigned i = 0; i < 256; i++) {
      float *d = dir + i * 4;
      for (;;) {
         unsigned face = rnd() % 6;
         float sc = centre_coord(4, 0, 4) * 2 - 1, tc = centre_coord(4, 0, 4) * 2 - 1, m = rndf(0.5f, 3.0f);
         float x = 0, y = 0, z = 0;
         switch (face) {
         case 0: x = m, z = -sc * m, y = -tc * m; break;
         case 1: x = -m, z = sc * m, y = -tc * m; break;
         case 2: y = m, x = sc * m, z = tc * m; break;
         case 3: y = -m, x = sc * m, z = -tc * m; break;
         case 4: z = m, x = sc * m, y = -tc * m; break;
         default: z = -m, x = -sc * m, y = -tc * m; break;
         }
         /* the 2D-array coordinate d.xy * 0.5 + 0.5 must not sit on a texel edge either */
         float au = (x * 0.5f + 0.5f) * 4, av = (y * 0.5f + 0.5f) * 4;
         if (fabsf(au - roundf(au)) < 0.05f || fabsf(av - roundf(av)) < 0.05f)
            continue;
         d[0] = x, d[1] = y, d[2] = z, d[3] = (float)(rnd() % 4);
         break;
      }
   }
}

static bool
tex_cube_check(struct run *r)
{
   const float *dir = S(r, 0), *got = S(r, 1);
   for (unsigned i = 0; i < 256; i++) {
      const float *d = dir + i * 4;
      float ax = fabsf(d[0]), ay = fabsf(d[1]), az = fabsf(d[2]), sc, tc, ma;
      unsigned face;
      if (ax >= ay && ax >= az) {
         face = d[0] >= 0 ? 0 : 1, sc = d[0] >= 0 ? -d[2] : d[2], tc = -d[1], ma = ax;
      } else if (ay >= az) {
         face = d[1] >= 0 ? 2 : 3, sc = d[0], tc = d[1] >= 0 ? d[2] : -d[2], ma = ay;
      } else {
         face = d[2] >= 0 ? 4 : 5, sc = d[2] >= 0 ? d[0] : -d[0], tc = -d[1], ma = az;
      }
      float want[2][4];
      sample_nearest(r, 0, 0, (sc / ma + 1) * 0.5f, (tc / ma + 1) * 0.5f, face, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, want[0]);
      sample_nearest(r, 1, 0, d[0] * 0.5f + 0.5f, d[1] * 0.5f + 0.5f, (uint32_t)d[3] % 2, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                     want[1]);
      for (unsigned k = 0; k < 2; k++) {
         for (unsigned c = 0; c < 4; c++) {
            float g = got[(i * 2 + k) * 4 + c];
            if (fabsf(g - want[k][c]) > 1e-6f)
               return FAILF(r, "%s, thread %u (%.4f %.4f %.4f, face %u) lane %u = %.6f, expected %.6f",
                            k ? "2D array" : "cube", i, d[0], d[1], d[2], face, c, g, want[k][c]);
         }
      }
   }
   return true;
}

/* tex_query */
static void
tex_query_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 3, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R8G8B8A8_UNORM, 4, 4, 6, 1, 5, 1, false);
   tex_image(r, 2, VK_IMAGE_VIEW_TYPE_3D, VK_FORMAT_R32_SFLOAT, 4, 4, 4, 7, 1, 1, false);
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 5, 3, 1, 1, 1, true);
   tex_buffer(r, 4, VK_FORMAT_R32_SFLOAT, 4, 100, false);
   slot(r, 0, 12 * 4);
}

static bool
tex_query_check(struct run *r)
{
   static const uint32_t want[12] = { 8, 4, 3, 4, 6, 5, 4, 7, 5, 3, 100, 2 };
   return check_u32(r, "out", S(r, 0), want, 12, 1);
}

/* tex_buffer */
static void
tex_buffer_setup(struct run *r)
{
   float *in = tex_buffer(r, 0, VK_FORMAT_R32_SFLOAT, 4, 256, false);
   tex_buffer(r, 1, VK_FORMAT_R32_UINT, 4, 256, true);
   for (unsigned i = 0; i < 256; i++)
      in[i] = rndf(0, 1000);
   slot(r, 0, 256 * 16);
}

static bool
tex_buffer_check(struct run *r)
{
   const float *in = r->tex[0].stage.map;
   const uint32_t *out = r->tex[1].stage.map;
   float *want = calloc(256 * 4, 4);
   uint32_t *uwant = calloc(256, 4);
   for (unsigned i = 0; i < 256; i++) {
      want[i * 4] = in[i], want[i * 4 + 3] = 1;
      uwant[i] = (uint32_t)(in[i] * 4.0f) + i;
   }
   bool ok = check_f4(r, "o", S(r, 0), want, 256 * 4, 0) && check_u32(r, "storage texel buffer", out, uwant, 256, 1);
   free(want);
   free(uwant);
   return ok;
}

/* tex_depth */
static void
tex_depth_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_D32_SFLOAT, 4, 8, 8, 1, 1, 1, false);
   float *d = r->tex[0].stage.map;
   for (unsigned k = 0; k < 64; k++) {
      d[k] = rndf(0, 1);
      if (fabsf(d[k] - 0.5f) < 1e-3f)
         d[k] = 0.25f;
   }
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *uv = slot(r, 0, 256 * 8);
   slot(r, 1, 256 * 3 * 4);
   for (unsigned i = 0; i < 256; i++) {
      uv[i * 2] = centre_coord(8, 0, 8);
      uv[i * 2 + 1] = centre_coord(8, 0, 8);
   }
}

static bool
tex_depth_expect(struct run *r, const struct nvmtl_air_nir_result *res)
{
   if (res->static_sampler_count != 1 || res->static_samplers[0].slot != 1)
      return FAILF(r, "constexpr samplers %u, the first at slot %u: expected one at slot 1", res->static_sampler_count,
                   res->static_samplers[0].slot);
   return true;
}

static bool
tex_depth_check(struct run *r)
{
   const float *uv = S(r, 0), *got = S(r, 1);
   for (unsigned i = 0; i < 256; i++) {
      float fx = floorf(uv[i * 2] * 8), fy = floorf(uv[i * 2 + 1] * 8);
      if (!(fx >= 0 && fx < 8 && fy >= 0 && fy < 8))
         return FAILF(r, "thread %u has the coordinate %g %g, outside the texture", i, uv[i * 2], uv[i * 2 + 1]);
      int x = (int)fx, y = (int)fy;
      float d = *(const float *)tex_at(r, 0, 0, (uint32_t)x, (uint32_t)y, 0);
      float want[3] = { d, 0.5f < d ? 1.0f : 0.0f, d };
      for (unsigned k = 0; k < 3; k++) {
         if (got[i * 3 + k] != want[k])
            return FAILF(r, "%s, thread %u = %g, expected %g", k == 0 ? "sample" : k == 1 ? "sample_compare less 0.5" : "read",
                         i, got[i * 3 + k], want[k]);
      }
   }
   return true;
}

/* tex_gather_depth (round 11): air.gather_depth_2d and _2d_array, component 0 of the bilinear footprint's four texels
 * (i0,j1), (i1,j1), (i1,j0), (i0,j0) from floor(coord * 8 - 0.5) + offset, clamped to the edge */
static void
tex_gdepth_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_D32_SFLOAT, 4, 8, 8, 1, 1, 1, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_D32_SFLOAT, 4, 8, 8, 1, 3, 1, false);
   float *d0 = r->tex[0].stage.map, *d1 = r->tex[1].stage.map;
   for (unsigned k = 0; k < 64; k++)
      d0[k] = rndf(0, 1);
   for (unsigned k = 0; k < 64 * 3; k++)
      d1[k] = rndf(0, 1);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *uv = slot(r, 0, 256 * 8);
   slot(r, 1, 256 * 4 * 16);
   for (unsigned i = 0; i < 256; i++) {
      uv[i * 2] = centre_coord(8, -2, 10);
      uv[i * 2 + 1] = centre_coord(8, -2, 10);
   }
}

static void
gdepth_want(struct run *r, unsigned tex, unsigned layer, float u, float v, int ox, int oy, float w[4])
{
   const int i0 = (int)floorf(u * 8 - 0.5f) + ox, j0 = (int)floorf(v * 8 - 0.5f) + oy;
   const VkSamplerAddressMode cl = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
   const int x0 = wrap(i0, 8, cl), x1 = wrap(i0 + 1, 8, cl), y0 = wrap(j0, 8, cl), y1 = wrap(j0 + 1, 8, cl);
   const int xs[4] = { x0, x1, x1, x0 }, ys[4] = { y1, y1, y0, y0 };
   for (unsigned c = 0; c < 4; c++)
      w[c] = *(const float *)tex_at(r, tex, 0, (uint32_t)xs[c], (uint32_t)ys[c], layer);
}

static bool
tex_gdepth_check(struct run *r)
{
   const float *uv = S(r, 0), *got = S(r, 1);
   static const char *what[4] = { "depth2d gather", "depth2d gather offset (1,-1)", "depth2d_array gather layer i % 3",
                                  "depth2d_array gather layer 2 offset (-2,1)" };
   for (unsigned i = 0; i < 256; i++) {
      const float u = uv[i * 2], v = uv[i * 2 + 1];
      float w[4][4];
      gdepth_want(r, 0, 0, u, v, 0, 0, w[0]);
      gdepth_want(r, 0, 0, u, v, 1, -1, w[1]);
      gdepth_want(r, 1, i % 3, u, v, 0, 0, w[2]);
      gdepth_want(r, 1, 2, u, v, -2, 1, w[3]);
      for (unsigned k = 0; k < 4; k++)
         for (unsigned c = 0; c < 4; c++)
            if (got[(i * 4 + k) * 4 + c] != w[k][c])
               return FAILF(r, "%s, thread %u (uv %.5f %.5f) lane %u = %g, expected %g", what[k], i, u, v, c,
                            got[(i * 4 + k) * 4 + c], w[k][c]);
   }
   return true;
}

/* tex_cube_write (round 11): every texel of six 8x8 faces written, half4 into RGBA16Float and uint4 into R32Uint, and the
 * written cubes' sizes */
static void
tex_cubew_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_CUBE, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 6, 1, true);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_CUBE, VK_FORMAT_R32_UINT, 4, 8, 8, 1, 6, 1, true);
   memset(r->tex[0].stage.map, 0xff, 8 * 8 * 6 * 8);
   memset(r->tex[1].stage.map, 0xff, 8 * 8 * 6 * 4);
   slot(r, 0, 16);
}

static bool
tex_cubew_check(struct run *r)
{
   for (uint32_t f = 0; f < 6; f++)
      for (uint32_t y = 0; y < 8; y++)
         for (uint32_t x = 0; x < 8; x++) {
            const uint16_t *h = tex_at(r, 0, 0, x, y, f);
            const float w[4] = { (float)x, (float)y, (float)f, 0.5f };
            for (unsigned c = 0; c < 4; c++)
               if (h2f(h[c]) != w[c])
                  return FAILF(r, "half cube face %u texel (%u,%u) lane %u = %g, expected %g", f, x, y, c, h2f(h[c]), w[c]);
            const uint32_t u = *(const uint32_t *)tex_at(r, 1, 0, x, y, f);
            if (u != x + 8 * y + 64 * f)
               return FAILF(r, "uint cube face %u texel (%u,%u) = %u, expected %u", f, x, y, u, x + 8 * y + 64 * f);
         }
   const uint32_t want[3] = { 8, 8, 8 };
   return check_u32(r, "sizes", S(r, 0), want, 3, 0);
}

/* pack565 (after 3.0.28): air.pack.unorm.rgb565 of float3 and of half3, as the translator packs it: NClamp(c, 0, 1)
 * (NaN to 0) times 31, 63, 31, rounded to even, at bits 0, 5, 11; the operands mix the edges (NaN, +-inf, -0.5, 0, 1,
 * 1.5) with values whose scaled form lies on or near a half */
static void
pack565_setup(struct run *r)
{
   float *in = slot(r, 0, 64 * 16);
   static const float edge[] = { NAN, INFINITY, -INFINITY, -0.5f, 0.0f, 1.0f, 1.5f, -0.0f };
   for (unsigned k = 0; k < 64 * 4; k++) {
      const uint32_t c = rnd() % 4;
      in[k] = c == 0 ? edge[rnd() % 8] : c == 1 ? ((float)(rnd() % 63) + 0.5f) / (k % 4 == 1 ? 63.0f : 31.0f) : rndf(-0.1f, 1.1f);
   }
   slot(r, 1, 64 * 4);
}
static uint32_t p565(const float *v)
{
   static const float max[3] = { 31.0f, 63.0f, 31.0f };
   static const unsigned shift[3] = { 0, 5, 11 };
   uint32_t acc = 0;
   for (unsigned k = 0; k < 3; k++) {
      const float c = fminf(fmaxf(v[k], 0.0f), 1.0f);   /* NClamp: fmaxf(NaN, 0) is 0 */
      acc |= (uint32_t)nearbyintf(c * max[k]) << shift[k];
   }
   return acc & 0xffffu;
}
static bool
pack565_check(struct run *r)
{
   const float *in = S(r, 0);
   const uint16_t *out = S(r, 1);
   for (unsigned i = 0; i < 64; i++) {
      float h[3];
      for (unsigned k = 0; k < 3; k++)
         h[k] = h2f(f2h(in[i * 4 + k]));
      const uint32_t want[2] = { p565(in + i * 4), p565(h) }, got[2] = { out[i * 2], out[i * 2 + 1] };
      if (!check_u32(r, "pack565 float3, half3", got, want, 2, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u: %g %g %g)", i, in[i * 4],
                         in[i * 4 + 1], in[i * 4 + 2]), false;
   }
   return true;
}

/* sample_bias_k (after 3.0.28): a kernel's sample with a bias, which the translator takes at level 0 (the bias
 * dropped), and with min_lod_clamp(1) too, the level then raised to 1 (FMax), on a 4x4 R32Float texture of three levels
 * whose texels all differ */
static void
sbias_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 4, 4, 1, 1, 3, false);
   float *t = r->tex[0].stage.map;
   for (unsigned k = 0; k < 16 + 4 + 1; k++)
      t[k] = (float)(k + 1) * 0.125f;
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 16.0f);
   float *b = slot(r, 0, 16 * 4);
   for (unsigned i = 0; i < 16; i++)
      b[i] = i % 4 == 0 ? 0.0f : rndf(0.5f, 3.0f);
   slot(r, 1, 16 * 2 * 16);
}
static bool
sbias_check(struct run *r)
{
   const float *got = S(r, 1);
   for (unsigned i = 0; i < 16; i++) {
      const unsigned x = i % 4, y = i / 4 % 4;
      /* the texels as the setup wrote them (not read back from the staging copy, which PLUGIN_RUN_CORRUPT also changes) */
      const float l0 = (float)(y * 4 + x + 1) * 0.125f, l1 = (float)(16 + (y / 2) * 2 + x / 2 + 1) * 0.125f;
      if (got[i * 8] != l0)
         return FAILF(r, "bias sample, thread %u = %g, expected level 0's %g", i, got[i * 8], l0);
      if (got[i * 8 + 4] != l1)
         return FAILF(r, "bias sample with min_lod_clamp(1), thread %u = %g, expected level 1's %g", i, got[i * 8 + 4], l1);
   }
   return true;
}

/* tex_fence */
static void
tex_fence_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 16, 16, 1, 1, 1, true);
   float *f = r->tex[0].stage.map, *keep = slot(r, 30, 256 * 4);
   for (unsigned k = 0; k < 256; k++)
      keep[k] = f[k] = (float)(int)(rnd() % 1000);
}

static bool
tex_fence_check(struct run *r)
{
   const float *before = S(r, 30), *after = r->tex[0].stage.map;
   for (unsigned k = 0; k < 256; k++) {
      if (after[k] != (before[k] + 1.0f) * 2.0f)
         return FAILF(r, "texel %u = %g, expected %g", k, after[k], (before[k] + 1.0f) * 2.0f);
   }
   return true;
}

/* tex_pixel: constexpr pixel-coordinate samplers */
static void
tex_pixel_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 0);
   float *xy = slot(r, 0, 256 * 8);
   slot(r, 1, 256 * 3 * 16);
   for (unsigned i = 0; i < 256; i++) {
      for (unsigned k = 0; k < 2; k++) {
         unsigned kind = rnd() % 3;
         float v = kind == 0 ? (float)(rnd() % 8) + 0.5f            /* a texel centre */
                 : kind == 1 ? (float)(rnd() % 9)                   /* an exact texel edge, 8 included */
                             : rndf(-3, 11);                        /* anywhere, outside too */
         xy[i * 2 + k] = v;
      }
   }
}

static bool
tex_pixel_expect(struct run *r, const struct nvmtl_air_nir_result *res)
{
   uint32_t slots = 0;
   for (unsigned k = 0; k < res->static_sampler_count && k < 32; k++)
      slots |= 1u << res->static_samplers[k].slot;
   if (res->static_sampler_count != 3 || slots != 7)
      return FAILF(r, "%u constexpr samplers at slots 0x%x: expected 3 at 0x7", res->static_sampler_count, slots);
   return true;
}

static bool
tex_pixel_check(struct run *r)
{
   const float *xy = S(r, 0), *got = S(r, 1);
   for (unsigned i = 0; i < 256; i++) {
      float x = xy[i * 2], y = xy[i * 2 + 1], w[3][4];
      int ix = (int)floorf(x), iy = (int)floorf(y);
      bool inside = ix >= 0 && ix < 8 && iy >= 0 && iy < 8;
      sample_nearest(r, 0, 0, x / 8, y / 8, 0, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, w[0]);
      for (unsigned c = 0; c < 4; c++)
         w[1][c] = inside ? w[0][c] : 0.0f;
      sample_linear(r, 0, 0, x / 8, y / 8, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, w[2]);
      static const char *what[3] = { "nearest clamp_to_edge", "nearest clamp_to_zero", "linear clamp_to_edge" };
      static const float tol[3] = { 1e-6f, 1e-6f, 1.0f / 128 };
      for (unsigned k = 0; k < 3; k++) {
         for (unsigned c = 0; c < 4; c++) {
            float g = got[(i * 3 + k) * 4 + c];
            if (!(fabsf(g - w[k][c]) <= tol[k]))
               return FAILF(r, "%s, thread %u (%.4f, %.4f) lane %u = %.6f, expected %.6f", what[k], i, x, y, c, g, w[k][c]);
         }
      }
   }
   return true;
}

/* tex_aref (metal/tex_aref.metal rewritten to an array_ref, PLUGIN_ONLY): textures 1 to 4 rgba8 8x8 at [[texture(1)]];
 * slot 1 the indices, 6 past the bound ones (texture 7: nothing bound, the dummy's zeros) */
static const uint32_t taref_k[8] = { 0, 3, 1, 6, 2, 0, 3, 1 };
static void
taref_setup(struct run *r)
{
   for (unsigned k = 1; k <= 4; k++) {
      tex_image(r, k, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
      fill_bytes(r, k);
   }
   slot(r, 0, 64 * 32);
   memcpy(slot(r, 1, sizeof taref_k), taref_k, sizeof taref_k);
}

static bool
taref_check(struct run *r)
{
   const float *out = S(r, 0);
   for (uint32_t i = 0; i < 64; i++) {
      const uint32_t x = i % 8, y = i / 8, e = taref_k[i & 7];
      float want[8];
      for (unsigned c = 0; c < 4; c++) {
         want[c] = rgba8_at(r, 3, 0, x, y, 0, c);
         want[4 + c] = e < 4 ? rgba8_at(r, 1 + e, 0, x, y, 0, c) : 0.0f;
      }
      if (!check_f4(r, "out", out + i * 8, want, 8, 1e-6f))
         return false;
   }
   return true;
}

/* tex_warrays (metal/tex_warrays.metal, PLUGIN_ONLY): textures 0 and 1 r32f texture buffers of 64 (read), 2 and 3 r32f
 * written texture buffers, 4 to 6 rgba16f 8x8 written (the array at [[texture(4)]]); slot 0 k (5: past the end), slot 1
 * the output */
static void
twa_setup(struct run *r)
{
   for (unsigned k = 0; k < 2; k++) {
      float *tb = tex_buffer(r, k, VK_FORMAT_R32_SFLOAT, 4, 64, false);
      for (unsigned q = 0; q < 64; q++)
         tb[q] = rndf(-50, 50);
   }
   for (unsigned k = 2; k < 4; k++)
      tex_buffer(r, k, VK_FORMAT_R32_SFLOAT, 4, 64, true);
   for (unsigned k = 4; k < 7; k++)
      tex_image(r, k, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R16G16B16A16_SFLOAT, 8, 8, 8, 1, 1, 1, true);
   *(uint32_t *)slot(r, 0, 16) = 5;
   slot(r, 1, 64 * 16);
}

static bool
twa_check(struct run *r)
{
   const float *tb0 = r->tex[0].stage.map, *tb1 = r->tex[1].stage.map, *wtb1 = r->tex[3].stage.map, *out = S(r, 1);
   for (unsigned i = 0; i < 64; i++) {
      const float want[4] = { tb1[i] + tb0[i] * 2.0f, 0, 0, 3 };
      if (!check_f4(r, "out", out + i * 4, want, 4, 1e-5f))
         return false;
      const float wb = (float)i + 0.25f;
      if (!check_f4(r, "wtb[1]", wtb1 + i, &wb, 1, 0))
         return false;
   }
   for (unsigned k = 4; k < 7; k++) {
      for (unsigned i = 0; i < 64; i++) {
         const unsigned e = (i & 3u) == 3u ? 5u : (i % 3u == 1u ? 0u : 2u), slot_ = e > 2 ? 2 : e;
         float want[4] = { 0, 0, 0, 0 }, got[4];
         if (k == 5) {
            want[0] = (float)i, want[1] = 1, want[2] = 2, want[3] = 3;
         } else if (k - 4 == slot_) {
            want[0] = (float)i * 0.5f, want[1] = (float)e, want[2] = 0, want[3] = 1;
         }
         for (unsigned c = 0; c < 4; c++)
            got[c] = h2f(((const uint16_t *)tex_at(r, k, 0, i % 8, i / 8, 0))[c]);
         if (!check_f4(r, k == 4 ? "w[0]" : k == 5 ? "w[1]" : "w[2]", got, want, 4, 0))
            return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (texel %u)", i), false;
      }
   }
   return true;
}

/* tex_pixmodes, tex_pixwrap (metal/tex_pixmodes.metal, metal/tex_pixwrap.metal, PLUGIN_ONLY): pixel-coordinate
 * samples as the translator lowers them, the references here step by step as its emulation computes (images/sample.rs
 * lower_pixel_linear_sample and clamp_pixel_coord_component_finite, fetch_coord.rs, gather.rs lower_pixel_gather_2d).
 * Textures r32f: 0 1D of 7, 1 2D 5x6 with 2 levels, 2 3D 3x4x2, 3 2D array 4x3 of 3 layers; slot 0 the 64 threads'
 * coordinates (x, y, z, layer), slot 1 16 samples per thread. Address modes: 0 clamp to zero, 1 clamp to edge, 2
 * repeat, 3 mirrored repeat. */
static const uint32_t pxm_size[4][3] = { { 7, 1, 1 }, { 5, 6, 1 }, { 3, 4, 2 }, { 4, 3, 1 } };

static void
pxm_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_1D, VK_FORMAT_R32_SFLOAT, 4, 7, 1, 1, 1, 1, false);
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 5, 6, 1, 1, 2, false);
   tex_image(r, 2, VK_IMAGE_VIEW_TYPE_3D, VK_FORMAT_R32_SFLOAT, 4, 3, 4, 2, 1, 1, false);
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R32_SFLOAT, 4, 4, 3, 1, 3, 1, false);
   for (unsigned k = 0; k < 4; k++) {
      float *f = r->tex[k].stage.map;
      for (size_t q = 0; q < r->tex[k].stage.size / 4; q++)
         f[q] = (float)((int)(rnd() % 801) - 400) * 0.01f;
   }
   float *c = slot(r, 0, 64 * 16);
   for (unsigned i = 0; i < 64; i++) {
      c[i * 4 + 0] = (i % 13) == 7 ? NAN : -3.0f + fmodf((float)i * 0.37f, 14.0f);
      c[i * 4 + 1] = (i % 4) == 1 ? (float)(i % 9) - 1.5f : -2.0f + fmodf((float)i * 0.53f, 10.0f);
      c[i * 4 + 2] = -1.0f + fmodf((float)i * 0.29f, 5.0f);
      c[i * 4 + 3] = (float)(i % 5);   /* layers 3 and 4 are past the last: the last */
   }
   slot(r, 1, 64 * 16 * 16);
}

/* the texel of texture k at level m, as a float4 an r32f fetch gives */
static void
pxm_texel(struct run *r, unsigned k, unsigned m, int x, int y, int zl, float o[4])
{
   o[0] = *(const float *)tex_at(r, k, m, (uint32_t)x, (uint32_t)y, (uint32_t)zl);
   o[1] = o[2] = 0;
   o[3] = 1;
}

static uint32_t pxm_dim(unsigned k, unsigned m, unsigned axis) { const uint32_t v = pxm_size[k][axis] >> m; return v ? v : 1; }

/* clamp_pixel_coord_component_finite */
static float
pxm_finite(float c, uint32_t size, unsigned mode)
{
   const float sf = (float)size;
   if (mode != 2 && mode != 3)
      return fminf(fmaxf(c, -9.0f), sf + 9.0f);   /* NaN to the low end, as NClamp */
   const float period = mode == 2 ? sf : sf * 2.0f;
   const float fin = fminf(fmaxf(c, -4194304.0f), 4194304.0f);
   return fin - period * floorf(fin / period);
}

/* build_pixel_fetch_coord_from_signed_components, one axis; *in cleared where a clamp-to-zero axis is outside */
static int
pxm_addr(int c, uint32_t size, unsigned mode, bool *in)
{
   const int sz = (int)size, below = c < 0, nonneg = below ? 0 : c, max = sz - 1, above = nonneg > max;
   if (mode == 0 && (below || above))
      *in = false;
   if (mode == 2)
      return ((c % sz) + sz) % sz;
   if (mode == 3) {
      const int period = 2 * sz, phase = ((c % period) + period) % period;
      return phase >= sz ? period - 1 - phase : phase;
   }
   return above ? max : nonneg;
}

static int pxm_layer(float w, uint32_t count) { const uint32_t l = (uint32_t)w; return (int)(l < count ? l : count - 1); }

static void
pxm_fetch(struct run *r, unsigned k, unsigned m, unsigned dims, const int *ci, const unsigned *mode, int layer, float o[4])
{
   bool in = true;
   int a[3] = { 0, 0, 0 };
   for (unsigned i = 0; i < dims; i++)
      a[i] = pxm_addr(ci[i], pxm_dim(k, m, i), mode[i], &in);
   pxm_texel(r, k, m, a[0], a[1], layer >= 0 ? layer : a[2], o);
   if (!in)
      o[0] = o[1] = o[2] = o[3] = 0;
}

/* lower_pixel_linear_sample */
static void
pxm_linear(struct run *r, unsigned k, unsigned m, unsigned dims, const float *c, const int *off, const unsigned *mode,
           int layer, float o[4])
{
   int base[3];
   float frac[3], w[3][2];
   for (unsigned i = 0; i < dims; i++) {
      const float f = pxm_finite(c[i], pxm_dim(k, m, i), mode[i]), biased = f - 0.5f, fl = floorf(biased);
      base[i] = (int)fl + (off ? off[i] : 0);
      frac[i] = biased - fl;
      w[i][0] = 1.0f - frac[i];
      w[i][1] = frac[i];
   }
   float acc[4] = { 0, 0, 0, 0 };
   for (unsigned tap = 0; tap < (1u << dims); tap++) {
      int ci[3];
      for (unsigned i = 0; i < dims; i++)
         ci[i] = base[i] + (int)((tap >> i) & 1);
      float col[4];
      pxm_fetch(r, k, m, dims, ci, mode, layer, col);
      float weight = w[0][tap & 1];
      for (unsigned i = 1; i < dims; i++)
         weight *= w[i][(tap >> i) & 1];
      for (unsigned q = 0; q < 4; q++)
         acc[q] += weight == 0.0f ? 0.0f : col[q] * weight;
   }
   memcpy(o, acc, sizeof acc);
}

/* build_pixel_fetch_coord: the texel at floor(coord) + offset */
static void
pxm_nearest(struct run *r, unsigned k, unsigned m, unsigned dims, const float *c, const int *off, const unsigned *mode,
            int layer, float o[4])
{
   int ci[3];
   for (unsigned i = 0; i < dims; i++)
      ci[i] = (int)floorf(pxm_finite(c[i], pxm_dim(k, m, i), mode[i])) + (off ? off[i] : 0);
   pxm_fetch(r, k, m, dims, ci, mode, layer, o);
}

/* lower_pixel_gather_2d: component comp of the texels (0,1), (1,1), (1,0), (0,0) from floor(coord - 0.5) + offset */
static void
pxm_gather(struct run *r, unsigned k, const float *c, const int *off, const unsigned *mode, unsigned comp, float o[4])
{
   static const int texel[4][2] = { { 0, 1 }, { 1, 1 }, { 1, 0 }, { 0, 0 } };
   const float c2[2] = { c[0] - 0.5f, c[1] - 0.5f };
   for (unsigned t = 0; t < 4; t++) {
      int ci[2];
      for (unsigned i = 0; i < 2; i++)
         ci[i] = (int)floorf(pxm_finite(c2[i], pxm_dim(k, 0, i), mode[i])) + (off ? off[i] : 0) + texel[t][i];
      float col[4];
      pxm_fetch(r, k, 0, 2, ci, mode, -1, col);
      o[t] = col[comp];
   }
}

struct pxm_case {
   char op;                 /* l linear, n nearest, g gather, h the hardware filter (linear) */
   unsigned tex, level, mode, comp;
   int off[3];
   bool has_off;
};

static bool
pxm_check_cases(struct run *r, const struct pxm_case *cases)
{
   const float *cs = S(r, 0), *got = S(r, 1);
   if (getenv("PXM_ALL")) {   /* every failing sample, for a diagnosis */
      for (unsigned j = 0; j < 16; j++) {
         unsigned bad = 0;
         for (unsigned i = 0; i < 64; i++) {
            const struct pxm_case *e = &cases[j];
            const unsigned dims = e->tex == 0 ? 1 : e->tex == 2 ? 3 : 2;
            const unsigned mode[3] = { e->mode, e->mode, e->mode };
            const float *c = cs + i * 4;
            const int layer = e->tex == 3 ? pxm_layer(c[3], 3) : -1;
            float want[4];
            if (e->op == 'g') pxm_gather(r, e->tex, c, e->has_off ? e->off : NULL, mode, e->comp, want);
            else if (e->op == 'n') pxm_nearest(r, e->tex, e->level, dims, c, e->has_off ? e->off : NULL, mode, layer, want);
            else pxm_linear(r, e->tex, e->level, dims, c, e->has_off ? e->off : NULL, mode, layer, want);
            for (unsigned q = 0; q < 4; q++)
               bad += !(fabsf(got[(i * 16 + j) * 4 + q] - want[q]) <= (e->op == 'h' ? 0.035f : 2e-6f)) && !(e->op == 'h' && isnan(c[0]));
         }
         fprintf(stderr, "pxm sample %u: %u lanes differ\n", j, bad);
      }
   }
   for (unsigned i = 0; i < 64; i++) {
      const float *c = cs + i * 4;
      for (unsigned j = 0; j < 16; j++) {
         const struct pxm_case *e = &cases[j];
         const unsigned dims = e->tex == 0 ? 1 : e->tex == 2 ? 3 : 2;
         const unsigned mode[3] = { e->mode, e->mode, e->mode };
         const int layer = e->tex == 3 ? pxm_layer(c[3], 3) : -1;
         float want[4];
         float tol = 2e-6f;
         if (e->op == 'g')
            pxm_gather(r, e->tex, c, e->has_off ? e->off : NULL, mode, e->comp, want);
         else if (e->op == 'n')
            pxm_nearest(r, e->tex, e->level, dims, c, e->has_off ? e->off : NULL, mode, layer, want);
         else
            pxm_linear(r, e->tex, e->level, dims, c, e->has_off ? e->off : NULL, mode, layer, want);
         if (e->op == 'h') {
            /* NVIDIA's filter weights have 8 bits: within 4/512 of each of the four texels' 4.0 at most */
            if (isnan(c[0]))
               continue;   /* the hardware's NaN coordinate is its own */
            tol = 0.035f;
         }
         if (!check_f4(r, "sample", got + (i * 16 + j) * 4, want, 4, tol)) {
            /* where the value read is in the texture, when it is one texel's */
            char where[64] = "";
            const struct timg *x = &r->tex[e->tex];
            for (unsigned m = 0; m < x->mips && !where[0]; m++)
               for (uint32_t zl = 0; zl < (x->d > 1 ? mip_size(x->d, m) : x->layers) && !where[0]; zl++)
                  for (uint32_t y = 0; y < mip_size(x->h, m) && !where[0]; y++)
                     for (uint32_t q = 0; q < mip_size(x->w, m) && !where[0]; q++)
                        if (*(const float *)tex_at(r, e->tex, m, q, y, zl) == got[(i * 16 + j) * 4])
                           snprintf(where, sizeof where, ", texel (%u,%u,%u) of level %u", q, y, zl, m);
            return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, sample %u, coord %g %g %g %g%s)",
                            i, j, c[0], c[1], c[2], c[3], where), false;
         }
      }
   }
   return true;
}

static bool
pxm_check(struct run *r)
{
   static const struct pxm_case cases[16] = {
      { 'l', 0, 0, 1 }, { 'l', 0, 0, 0 }, { 'n', 0, 0, 0 }, { 'h', 1, 0, 1 }, { 'l', 1, 0, 0 }, { 'l', 1, 1, 1 },
      { 'l', 1, 0, 1, 0, { 1, -1 }, true }, { 'n', 1, 0, 1, 0, { -2, 1 }, true }, { 'l', 2, 0, 0 }, { 'n', 2, 0, 1 },
      { 'l', 3, 0, 1 }, { 'n', 3, 0, 0 }, { 'g', 1, 0, 1, 0 }, { 'g', 1, 0, 0, 1, { 1, 0 }, true }, { 'n', 0, 0, 1 },
      { 'l', 2, 0, 1 },
   };
   return pxm_check_cases(r, cases);
}

static bool
pxw_check(struct run *r)
{
   static const struct pxm_case cases[16] = {
      { 'l', 0, 0, 2 }, { 'l', 0, 0, 3 }, { 'l', 1, 0, 2 }, { 'l', 1, 0, 3, 0, { 1, 1 }, true }, { 'l', 2, 0, 2 },
      { 'n', 2, 0, 3 }, { 'l', 3, 0, 3 }, { 'n', 1, 1, 2 }, { 'g', 1, 0, 2, 0 }, { 'g', 1, 0, 3, 0, { -1, 2 }, true },
      { 'n', 0, 0, 3 }, { 'n', 3, 0, 2 }, { 'l', 1, 1, 2 }, { 'l', 2, 0, 3 }, { 'n', 0, 0, 2 },
      { 'n', 1, 0, 3, 0, { 2, -1 }, true },
   };
   return pxm_check_cases(r, cases);
}

/* tex_tbarray (metal/tex_tbarray.metal, PLUGIN_ONLY, rounds 10 and 11): texture_buffer arrays indexed at run time;
 * textures 0-2 read (r32f, 64 texels), 4-5 written; slot 0 the index per thread. Round 10's translator kept each such
 * array one array binding, which the plugin's layout holds with one descriptor (zeros on the SPIR-V road, refused on the
 * road); since round 11 both read and write the element through the heap's texel buffers at the slot the per-draw table
 * holds for texture n + min(index, N - 1), and the road must take it (MUST_ROAD) */
static void
tba_setup(struct run *r)
{
   for (unsigned k = 0; k < 3; k++) {
      float *tb = tex_buffer(r, k, VK_FORMAT_R32_SFLOAT, 4, 64, false);
      for (unsigned q = 0; q < 64; q++)
         tb[q] = (float)(k * 100 + q);
   }
   for (unsigned k = 4; k < 6; k++)
      tex_buffer(r, k, VK_FORMAT_R32_SFLOAT, 4, 64, true);
   uint32_t *sel = slot(r, 0, 64 * 4);
   for (unsigned i = 0; i < 64; i++)
      sel[i] = (i * 7u + 3u) % 11u;
   slot(r, 1, 64 * 16);
}

static bool
tba_check(struct run *r)
{
   const uint32_t *sel = S(r, 0);
   const float *out = S(r, 1);
   for (unsigned i = 0; i < 64; i++) {
      const float want[4] = { (float)((sel[i] % 3u) * 100 + i), 0, 0, 1 };
      if (!check_f4(r, "tb[k]", out + i * 4, want, 4, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, k %u)", i, sel[i]), false;
      const float *w = r->tex[4 + (sel[i] & 1u)].stage.map, wb = (float)i + 0.5f;
      if (!check_f4(r, "wtb[k]", w + i, &wb, 1, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, k %u)", i, sel[i]), false;
   }
   return true;
}

/* tex_null, tex_null_tb (metal/tex_null.metal, PLUGIN_ONLY, round 11): null textures as the translator lowers them,
 * its placeholder image at the first texture binding its module leaves free. tex_null: texture 0 (rgba8 16x16, also
 * the placeholder's binding) and texture 1 (r32f 8x4, bound and not the kernel's); tex_null_tb: a texture_buffer at 0
 * (r32f, 256 texels), so the placeholder at texture 1 (rgba8 16x16 here), texture 2 (r32f 8x4) bound and not read: a
 * placeholder anywhere else reads another size or other texels */
static void
tnull_setup(struct run *r)
{
   const bool tb = !strcmp(r->t->name, "tex_null_tb");
   if (tb) {
      float *b = tex_buffer(r, 0, VK_FORMAT_R32_SFLOAT, 4, 256, false);
      for (unsigned q = 0; q < 256; q++)
         b[q] = (float)q * 0.25f + 3.0f;
      tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 16, 16, 1, 1, 1, false);
      fill_bytes(r, 1);
      tex_image(r, 2, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 8, 4, 1, 1, 1, false);
      fill_bytes(r, 2);
   } else {
      tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 16, 16, 1, 1, 1, false);
      fill_bytes(r, 0);
      tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_SFLOAT, 4, 8, 4, 1, 1, 1, false);
      fill_bytes(r, 1);
   }
   slot(r, 0, 256 * 3 * 16);
}

static bool
tnull_check(struct run *r)
{
   const bool tb = !strcmp(r->t->name, "tex_null_tb");
   const unsigned p = tb ? 1 : 0;   /* the placeholder's texture index */
   float *want = calloc(256 * 12, 4);
   for (unsigned i = 0; i < 256; i++) {
      const unsigned x = i % 16, y = i / 16;
      float *o = want + i * 12;
      o[0] = 1;
      o[1] = tb ? 1 : 0;   /* tex_null_tb: is_null_texture of the 3D null; tex_null: of the argument t */
      o[2] = 16;
      o[3] = 16;
      const uint8_t *a = tex_at(r, p, 0, x, y, 0);
      for (unsigned c = 0; c < 4; c++)
         o[4 + c] = U8(a[c]);
      if (tb) {
         o[8] = (float)i * 0.25f + 3.0f, o[9] = 0, o[10] = 0, o[11] = 1;
      } else {
         const uint8_t *a = tex_at(r, 0, 0, x, y, 0);
         for (unsigned c = 0; c < 4; c++)
            o[8 + c] = U8(a[c]);
      }
   }
   const bool ok = check_f4(r, "out", S(r, 0), want, 256 * 12, 1e-6f);
   free(want);
   return ok;
}

/* mad_sat (metal/mad_sat.metal, round 11): air.mad_sat.s/u.i32, a * b + c clamped to the type; the operands mix the
 * edges (INT_MIN, INT_MAX, 0, +-1, 46341, 65536) with random words. mad_sat_v (v4i32) the translator refuses: so must
 * the frontend */
static uint32_t
ms_word(void)
{
   static const uint32_t edge[] = { 0x80000000u, 0x7fffffffu, 0, 1, 0xffffffffu, 46341, 65536, 0xffff0000u };
   const uint32_t k = rnd();
   return k % 3 ? edge[rnd() % 8] : rnd();
}
static void
ms_setup(struct run *r)
{
   uint32_t *in = slot(r, 0, 64 * 3 * 16);
   for (unsigned k = 0; k < 64 * 3 * 4; k++)
      in[k] = ms_word();
   slot(r, 1, 64 * 16);
   slot(r, 2, 64 * 16);
}
static uint32_t ms_s(uint32_t a, uint32_t b, uint32_t c)
{
   const int64_t v = (int64_t)(int32_t)a * (int32_t)b + (int32_t)c;
   return (uint32_t)(int32_t)(v < INT32_MIN ? INT32_MIN : v > INT32_MAX ? INT32_MAX : v);
}
static uint32_t ms_u(uint32_t a, uint32_t b, uint32_t c)
{
   const uint64_t v = (uint64_t)a * b + c;
   return v > UINT32_MAX ? UINT32_MAX : (uint32_t)v;
}
static bool
ms_check(struct run *r)
{
   const uint32_t *in = S(r, 0), *so = S(r, 1), *uo = S(r, 2);
   for (unsigned i = 0; i < 64; i++) {
      const uint32_t *a = in + i * 12, *b = a + 4, *c = a + 8;
      uint32_t ws[4], wu[4];
      for (unsigned k = 0; k < 3; k++) {
         ws[k] = ms_s(a[k], b[k], c[k]);
         wu[k] = ms_u(a[k], b[k], c[k]);
      }
      ws[3] = ms_s(a[3], b[1], c[2]);
      wu[3] = ms_u(a[3], b[0], c[1]);
      if (!check_u32(r, "mad_sat.s.i32", so + i * 4, ws, 4, 0) || !check_u32(r, "mad_sat.u.i32", uo + i * 4, wu, 4, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u)", i), false;
   }
   return true;
}

/* simd_fill (metal/simd_fill.metal, PLUGIN_ONLY, round 11): simd_shuffle_and_fill_down/up with a cluster width, the
 * three-operand forms (the SIMD-group's width, air.get_simdgroup_size.i16), a half4 one, the quad-group builtins; the
 * references are the translator's lowering (air_calls/shuffle.rs) over each SIMD-group of 32 lanes */
static const uint16_t sf_dm[6][2] = { { 1, 4 }, { 3, 8 }, { 5, 0 }, { 2, 32 }, { 7, 16 }, { 9, 2 } };
static void
sf_setup(struct run *r)
{
   float *in = slot(r, 0, 64 * 16);
   for (unsigned k = 0; k < 64 * 4; k++)
      in[k] = rndf(-100, 100);
   uint16_t *dm = slot(r, 1, 6 * 4);
   memcpy(dm, sf_dm, sizeof sf_dm);
   slot(r, 2, (18 * 64 + 63 * 18 + 2) * 16);
   slot(r, 3, 64 * 16);
}

static unsigned sf_down(unsigned lane, unsigned delta, unsigned mod, bool *inb, unsigned *fl)
{
   const unsigned safe = mod ? mod : 1, local = lane % safe, base = lane - local, src = local + delta, wrapped = src % safe;
   *inb = src < safe;
   *fl = base + wrapped;
   return base + (*inb ? src : wrapped);
}
static unsigned sf_up(unsigned lane, unsigned delta, unsigned mod, bool *inb)
{
   const unsigned safe = mod ? mod : 1, local = lane % safe, base = lane - local;
   *inb = local >= delta;
   return base + (local + safe - delta % safe) % safe;
}

static bool
sf_check(struct run *r)
{
   const float *in = S(r, 0), *out = S(r, 2);
   const uint32_t *lanes = S(r, 3);
   float want[4], fill[4];
   for (unsigned i = 0; i < 64; i++) {
      const unsigned sg = i / 32 * 32, lane = i % 32;
      for (unsigned k = 0; k < 6; k++) {
         bool inb;
         unsigned fl, dl = sf_down(lane, sf_dm[k][0], sf_dm[k][1], &inb, &fl);
         for (unsigned c = 0; c < 4; c++) {
            want[c] = inb ? in[(sg + dl) * 4 + c] : -in[(sg + fl) * 4 + c] - 1000.0f;
            fill[c] = h2f(f2h(want[c]));   /* the half4 form: data and fill as halves */
            if (!inb)
               fill[c] = h2f(f2h(-in[(sg + fl) * 4 + c] - 1000.0f));
         }
         if (!check_f4(r, "fill_down", out + (i * 18 + k * 3) * 4, want, 4, 0) ||
             !check_f4(r, "fill_down half4", out + (i * 18 + k * 3 + 2) * 4, fill, 4, 0))
            return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, case %u)", i, k), false;
         const unsigned ul = sf_up(lane, sf_dm[k][0], sf_dm[k][1], &inb);
         for (unsigned c = 0; c < 4; c++)
            want[c] = inb ? in[(sg + ul) * 4 + c] : -in[(sg + ul) * 4 + c] - 1000.0f;
         if (!check_f4(r, "fill_up", out + (i * 18 + k * 3 + 1) * 4, want, 4, 0))
            return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, case %u)", i, k), false;
      }
      bool inb;
      unsigned fl, dl = sf_down(lane, i % 5, 32, &inb, &fl);
      for (unsigned c = 0; c < 4; c++)
         want[c] = inb ? in[(sg + dl) * 4 + c] : -in[(sg + fl) * 4 + c] - 1000.0f;
      if (!check_f4(r, "fill_down (width)", out + (i * 18 + 18 * 64) * 4, want, 4, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u)", i), false;
      const unsigned ul = sf_up(lane, i % 7, 32, &inb);
      for (unsigned c = 0; c < 4; c++)
         want[c] = inb ? in[(sg + ul) * 4 + c] : -in[(sg + ul) * 4 + c] - 1000.0f;
      if (!check_f4(r, "fill_up (width)", out + (i * 18 + 18 * 64 + 1) * 4, want, 4, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u)", i), false;
      const uint32_t lw[4] = { lane & 3u, i >> 2, 4, 16 };   /* 64 threads: 16 quad-groups */
      if (!check_u32(r, "quad-group builtins", lanes + i * 4, lw, 4, 0))
         return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u)", i), false;
   }
   return true;
}

/* tex_bicubic (metal/tex_bicubic.metal, PLUGIN_ONLY, round 10): bicubic samples as the translator emulates them
 * (images/sample.rs lower_pixel_bicubic_sample, catmull_rom_weights), the references step by step as it computes;
 * tex_pixmodes's textures and pixel coordinates, the normalized ones in slot 2 (each pixel one over its axis' size) */
static void
bic_setup(struct run *r)
{
   pxm_setup(r);
   const float *c = S(r, 0);
   float *n = slot(r, 2, 64 * 16);
   for (unsigned i = 0; i < 64; i++) {
      n[i * 4 + 0] = c[i * 4 + 0] / 5.0f;   /* texture 1's width (texture 0's 7 too: r[0] reads x / 5 * 7) */
      n[i * 4 + 1] = c[i * 4 + 1] / 6.0f;
      n[i * 4 + 2] = n[i * 4 + 3] = 0;
   }
}

static void
bic_weights(float u, float w[4])
{
   const float u2 = u * u, u3 = u2 * u;
   w[0] = (-0.5f * u + u2) + -0.5f * u3;
   w[1] = (1.0f - 2.5f * u2) + 1.5f * u3;
   w[2] = (0.5f * u + 2.0f * u2) - 1.5f * u3;
   w[3] = -0.5f * u2 + 0.5f * u3;
}

static void
bic_sample(struct run *r, unsigned k, unsigned m, unsigned dims, const float *c, bool normalized, const int *off,
           unsigned mode, float o[4])
{
   int base[2];
   float w[2][4];
   const unsigned modes[3] = { mode, mode, mode };
   for (unsigned i = 0; i < dims; i++) {
      const uint32_t sz = pxm_dim(k, m, i);
      const float ci = normalized ? c[i] * (float)sz : c[i];
      const float f = pxm_finite(ci, sz, mode), biased = f - 0.5f, fl = floorf(biased);
      base[i] = (int)fl + (off ? off[i] : 0);
      bic_weights(biased - fl, w[i]);
   }
   float acc[4] = { 0, 0, 0, 0 };
   for (unsigned tap = 0; tap < (dims == 1 ? 4u : 16u); tap++) {
      const unsigned idx[2] = { tap % 4, (tap / 4) % 4 };
      int ci[3] = { 0, 0, 0 };
      for (unsigned i = 0; i < dims; i++)
         ci[i] = base[i] + (int)idx[i] - 1;
      float col[4];
      pxm_fetch(r, k, m, dims, ci, modes, -1, col);
      const float weight = dims == 1 ? w[0][idx[0]] : w[0][idx[0]] * w[1][idx[1]];
      if (weight == 0.0f)
         continue;
      for (unsigned q = 0; q < 4; q++)
         acc[q] += col[q] * weight;
   }
   memcpy(o, acc, sizeof acc);
}

static bool
bic_check(struct run *r)
{
   static const struct { unsigned tex, level, mode; bool normalized; int off[2]; } cases[10] = {
      { 0, 0, 1, true }, { 1, 0, 1, true }, { 1, 0, 2, true }, { 1, 0, 0, true }, { 1, 0, 3, true }, { 1, 1, 1, true },
      { 1, 0, 1, true, { 1, -1 } }, { 1, 0, 1, false }, { 1, 0, 0, false, { -1, 1 } }, { 0, 0, 1, false },
   };
   const float *cs = S(r, 0), *ns = S(r, 2), *got = S(r, 1);
   for (unsigned i = 0; i < 64; i++)
      for (unsigned j = 0; j < 10; j++) {
         const unsigned dims = cases[j].tex == 0 ? 1 : 2;
         float c[2] = { cs[i * 4], cs[i * 4 + 1] };
         if (cases[j].normalized) {
            c[0] = ns[i * 4];
            c[1] = ns[i * 4 + 1];
         }
         float want[4];
         bic_sample(r, cases[j].tex, cases[j].level, dims, c, cases[j].normalized, cases[j].off[0] || cases[j].off[1] ?
                    cases[j].off : NULL, cases[j].mode, want);
         if (!check_f4(r, "bicubic sample", got + (i * 16 + j) * 4, want, 4, 5e-5f))
            return snprintf(r->why + strlen(r->why), sizeof r->why - strlen(r->why), " (thread %u, sample %u, coord %g %g)", i, j,
                            c[0], c[1]), false;
      }
   return true;
}

/* tex_argpixel: argument samplers bound with pixel coordinates (round 5: their state reaches the frontend) */
static void
tex_argpixel_setup(struct run *r)
{
   tex_pixel_setup(r);
   samp_create_pixel(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE);
   samp_create_pixel(r, 1, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER);   /* clamp_to_zero */
   samp_create_pixel(r, 2, VK_FILTER_LINEAR, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE);
}

/* tex_offset: run-time and constant offsets */
static void
tex_offset_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 0);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 0);
   float *uv = slot(r, 0, 256 * 8);
   int32_t *off = slot(r, 1, 256 * 8);
   slot(r, 2, 256 * 3 * 16);
   for (unsigned i = 0; i < 256; i++) {
      uv[i * 2] = centre_coord(8, 0, 8);
      uv[i * 2 + 1] = centre_coord(8, 0, 8);
      off[i * 2] = (int32_t)(rnd() % 8) - 4;
      off[i * 2 + 1] = (int32_t)(rnd() % 8) - 4;
   }
}

static bool
tex_offset_check(struct run *r)
{
   const float *uv = S(r, 0), *got = S(r, 2);
   const int32_t *off = S(r, 1);
   VkSamplerAddressMode cl = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
   for (unsigned i = 0; i < 256; i++) {
      float u = uv[i * 2], v = uv[i * 2 + 1], w[3][4];
      int x = (int)floorf(u * 8), y = (int)floorf(v * 8);
      for (unsigned c = 0; c < 4; c++) {
         w[0][c] = rgba8_at(r, 0, 0, wrap(x + off[i * 2], 8, cl), wrap(y + off[i * 2 + 1], 8, cl), 0, c);
         w[1][c] = rgba8_at(r, 0, 0, wrap(x + 2, 8, cl), wrap(y - 3, 8, cl), 0, c);
      }
      int i0 = (int)floorf(u * 8 - 0.5f) - 1, j0 = (int)floorf(v * 8 - 0.5f) + 2;
      int x0 = wrap(i0, 8, cl), x1 = wrap(i0 + 1, 8, cl), y0 = wrap(j0, 8, cl), y1 = wrap(j0 + 1, 8, cl);
      w[2][0] = rgba8_at(r, 0, 0, x0, y1, 0, 0), w[2][1] = rgba8_at(r, 0, 0, x1, y1, 0, 0);
      w[2][2] = rgba8_at(r, 0, 0, x1, y0, 0, 0), w[2][3] = rgba8_at(r, 0, 0, x0, y0, 0, 0);
      static const char *what[3] = { "run-time offset", "constant offset", "gather with an offset" };
      for (unsigned k = 0; k < 3; k++) {
         for (unsigned c = 0; c < 4; c++) {
            float g = got[(i * 3 + k) * 4 + c];
            if (fabsf(g - w[k][c]) > 1e-6f)
               return FAILF(r, "%s, thread %u (uv %.4f %.4f, offset %d %d) lane %u = %.6f, expected %.6f", what[k], i, u, v,
                            off[i * 2], off[i * 2 + 1], c, g, w[k][c]);
         }
      }
   }
   return true;
}

/* fc_argindex: a buffer index (bidx + 1 = 3) and a texture index (tidx * 2 = 4) from function constants */
static const struct fcset fc_argidx = { 2, { { 0, 4, { 2 } }, { 1, 4, { 2 } } } };

static void
fc_argindex_setup(struct run *r)
{
   tex_image(r, 4, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 4);
   slot(r, 0, 64 * 16);
   float *in = slot(r, 3, 64 * 4);
   for (unsigned i = 0; i < 64; i++)
      in[i] = rndf(-4, 4);
}

static bool
fc_argindex_check(struct run *r)
{
   const float *in = S(r, 3);
   float want[64 * 4];
   for (unsigned y = 0; y < 8; y++)
      for (unsigned x = 0; x < 8; x++) {
         const uint8_t *p = tex_at(r, 4, 0, x, y, 0);
         for (unsigned c = 0; c < 4; c++)
            want[(y * 8 + x) * 4 + c] = U8(p[c]) + in[y * 8 + x];
      }
   return check_f4(r, "out", S(r, 0), want, 64 * 4, 1e-6f);
}

/* fc_texture: a texture index from a function constant, and sincos */
static const struct fcset fc_tex = { 1, { { 0, 4, { 2 } } } };   /* tex_base = 2: texture(3) */

static void
fc_texture_setup(struct run *r)
{
   tex_image(r, 3, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 3);
   float *in = slot(r, 0, 64 * 4);
   slot(r, 1, 64 * 2 * 16);
   for (unsigned i = 0; i < 64; i++)
      in[i] = rndf(-4, 4);
}

static bool
fc_texture_expect(struct run *r, const struct nvmtl_air_nir_result *res)
{
   if (res->texture_mask[0] != 1u << 3)
      return FAILF(r, "texture mask 0x%x: expected texture 3 alone (0x8)", res->texture_mask[0]);
   return true;
}

/* fc_texture_none (round 12): tex_base not given reads 0, as the translator reads it: texture(1) */
static void
fc_texture_none_setup(struct run *r)
{
   tex_image(r, 1, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 1, 1, false);
   fill_bytes(r, 1);
   float *in = slot(r, 0, 64 * 4);
   slot(r, 1, 64 * 2 * 16);
   for (unsigned i = 0; i < 64; i++)
      in[i] = rndf(-4, 4);
}

static bool
fc_texture_none_expect(struct run *r, const struct nvmtl_air_nir_result *res)
{
   if (res->texture_mask[0] != 1u << 1)
      return FAILF(r, "texture mask 0x%x: expected texture 1 alone (0x2)", res->texture_mask[0]);
   return true;
}

static bool fc_texture_read_check(struct run *r, unsigned tex);
static bool fc_texture_check(struct run *r) { return fc_texture_read_check(r, 3); }
static bool fc_texture_none_check(struct run *r) { return fc_texture_read_check(r, 1); }

static bool
fc_texture_read_check(struct run *r, unsigned tex)
{
   const float *in = S(r, 0), *got = S(r, 1);
   for (unsigned y = 0; y < 8; y++) {
      for (unsigned x = 0; x < 8; x++) {
         unsigned i = y * 8 + x;
         for (unsigned c = 0; c < 4; c++) {
            if (fabsf(got[i * 8 + c] - rgba8_at(r, tex, 0, (int)x, (int)y, 0, c)) > 1e-6f)
               return FAILF(r, "read, thread %u lane %u = %g, expected %g", i, c, got[i * 8 + c], rgba8_at(r, tex, 0, (int)x, (int)y, 0, c));
         }
         float ws = sinf(in[i]), wc = cosf(in[i]);
         if (fabsf(got[i * 8 + 4] - ws) > 2e-5f || fabsf(got[i * 8 + 5] - wc) > 2e-5f)
            return FAILF(r, "sincos(%g), thread %u = %g %g, expected %g %g", in[i], i, got[i * 8 + 4], got[i * 8 + 5], ws, wc);
      }
   }
   return true;
}

/* tex_pixel_array: a nearest pixel-coordinate sampler on a 2D array, and uniform<T> */
static void
tex_pixel_array_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D_ARRAY, VK_FORMAT_R8G8B8A8_UNORM, 4, 8, 8, 1, 3, 1, false);
   fill_bytes(r, 0);
   float *xy = slot(r, 0, 256 * 8);
   uint32_t *layer = slot(r, 1, 256 * 4);
   slot(r, 2, 256 * 16);
   slot(r, 3, 256 * 4);
   for (unsigned i = 0; i < 256; i++) {
      for (unsigned k = 0; k < 2; k++) {
         unsigned kind = rnd() % 3;
         xy[i * 2 + k] = kind == 0 ? (float)(rnd() % 8) + 0.5f : kind == 1 ? (float)(rnd() % 9) : rndf(-3, 11);
      }
      layer[i] = rnd() % 3;
   }
}

static bool
tex_pixel_array_check(struct run *r)
{
   const float *xy = S(r, 0), *got = S(r, 2);
   const uint32_t *layer = S(r, 1), *ugot = S(r, 3);
   for (unsigned i = 0; i < 256; i++) {
      float w[4];
      if (layer[i] >= 3)
         return FAILF(r, "thread %u has layer %u, outside the texture", i, layer[i]);
      sample_nearest(r, 0, 0, xy[i * 2] / 8, xy[i * 2 + 1] / 8, layer[i], VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, w);
      for (unsigned c = 0; c < 4; c++) {
         if (fabsf(got[i * 4 + c] - w[c]) > 1e-6f)
            return FAILF(r, "thread %u (%.4f, %.4f, layer %u) lane %u = %.6f, expected %.6f", i, xy[i * 2], xy[i * 2 + 1],
                         layer[i], c, got[i * 4 + c], w[c]);
      }
      if (ugot[i] != (layer[0] + 5u) * 3u + i)
         return FAILF(r, "uniform, thread %u = %u, expected %u", i, ugot[i], (layer[0] + 5u) * 3u + i);
   }
   return true;
}

/* ---- round 6: integer-texture samples, pack and unpack, multisampled reads ------------------------------------- */

static uint32_t
r32_at(struct run *r, unsigned t, uint32_t m, int x, int y)
{
   uint32_t v;
   memcpy(&v, tex_at(r, t, m, (uint32_t)x, (uint32_t)y, 0), 4);
   return v;
}

static void
tex_intsample_setup(struct run *r)
{
   tex_image(r, 0, VK_IMAGE_VIEW_TYPE_2D, VK_FORMAT_R32_UINT, 4, 8, 8, 1, 1, 2, false);
   fill_bytes(r, 0);
   samp_create(r, 0, VK_FILTER_NEAREST, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, VK_SAMPLER_MIPMAP_MODE_NEAREST, 16);
   slot(r, 0, 256 * 5 * 16);
   float *uv = slot(r, 1, 256 * 8);
   for (unsigned i = 0; i < 256; i++) {
      uv[i * 2] = centre_coord(8, -12, 20);
      uv[i * 2 + 1] = centre_coord(8, -12, 20);
   }
}

/* the translator's nearest fetch of an integer texture (fetch_coord.rs): floor(c * n), then the address mode */
static int
int_addr(int i, int n, int mode, bool *outside)
{
   if (mode == 0) {   /* clamp_to_zero */
      *outside |= i < 0 || i >= n;
      return i < 0 ? 0 : i >= n ? n - 1 : i;
   }
   if (mode == 2)
      return ((i % n) + n) % n;
   if (mode == 3) {
      int p = ((i % (2 * n)) + 2 * n) % (2 * n);
      return p >= n ? 2 * n - 1 - p : p;
   }
   return i < 0 ? 0 : i >= n ? n - 1 : i;
}

static bool
tex_intsample_check(struct run *r)
{
   const float *uv = S(r, 1);
   const uint32_t *got = S(r, 0);
   static const int mode[5] = { 2, 3, 0, 1, 1 };
   static const unsigned level[5] = { 0, 0, 0, 1, 0 };
   static const char *what[5] = { "repeat", "mirrored repeat", "clamp_to_zero", "clamp_to_edge level 1",
                                  "argument sampler (the hardware's nearest)" };
   for (unsigned i = 0; i < 256; i++) {
      for (unsigned k = 0; k < 5; k++) {
         float u = uv[i * 2], v = uv[i * 2 + 1];
         if (k == 4) {
            u = fminf(fmaxf(u, 0), 1);
            v = fminf(fmaxf(v, 0), 1);
         }
         const int n = 8 >> level[k];
         bool outside = false;
         const int x = int_addr((int)floorf(u * n), n, mode[k], &outside), y = int_addr((int)floorf(v * n), n, mode[k], &outside);
         const uint32_t want[4] = { outside ? 0 : r32_at(r, 0, level[k], x, y), 0, 0, outside ? 0 : 1 };
         for (unsigned c = 0; c < 4; c++) {
            if (got[(i * 5 + k) * 4 + c] != want[c])
               return FAILF(r, "%s, thread %u (uv %.5f %.5f) lane %u = %u, expected %u", what[k], i, u, v, c,
                            got[(i * 5 + k) * 4 + c], want[c]);
         }
      }
   }
   return true;
}

static void
pack_setup(struct run *r)
{
   uint32_t *w = slot(r, 0, 256 * 4);
   float *f = slot(r, 1, 256 * 16);
   int32_t *e = slot(r, 2, 256 * 16);
   slot(r, 3, 256 * 6 * 16);
   slot(r, 4, 256 * 16);
   for (unsigned i = 0; i < 256; i++) {
      w[i] = rnd() & ~0x40004000u;   /* neither half a NaN or an infinity */
      for (unsigned c = 0; c < 4; c++) {
         f[i * 4 + c] = rndf(-2.0f, 2.0f);
         e[i * 4 + c] = (int32_t)(rnd() % 9) - 4;
      }
   }
}

static float
half_to_f(uint16_t h)
{
   _Float16 x;
   memcpy(&x, &h, 2);
   return (float)x;
}

static bool
pack_check(struct run *r)
{
   const uint32_t *w = S(r, 0), *packed = S(r, 4);
   const float *f = S(r, 1), *got = S(r, 3);
   const int32_t *e = S(r, 2);
   for (unsigned i = 0; i < 256; i++) {
      const uint32_t x = w[i];
      const float *v = f + i * 4;
      float want[6][4];
      for (unsigned c = 0; c < 4; c++) {
         want[0][c] = (float)((x >> (8 * c)) & 0xff) / 255.0f;
         want[1][c] = fmaxf((float)(int8_t)(x >> (8 * c)) / 127.0f, -1.0f);
         want[4][c] = ldexpf(v[c], e[i * 4 + c]);
      }
      for (unsigned c = 0; c < 2; c++) {
         want[2][c] = (float)((x >> (16 * c)) & 0xffff) / 65535.0f;
         want[2][2 + c] = fmaxf((float)(int16_t)(x >> (16 * c)) / 32767.0f, -1.0f);
         want[3][c] = want[2][c];
         want[3][2 + c] = half_to_f((uint16_t)(x >> (16 * c)));
      }
      want[5][0] = ldexpf(v[0], e[i * 4 + 1]);
      for (unsigned c = 0; c < 3; c++)
         want[5][1 + c] = want[0][c];
      /* a value unpacked to half carries half's precision */
      static const float tol[6][4] = { { 1e-6f, 1e-6f, 1e-6f, 1e-6f }, { 1e-6f, 1e-6f, 1e-6f, 1e-6f },
                                       { 1e-6f, 1e-6f, 1e-6f, 1e-6f }, { 1e-3f, 1e-3f, 0, 0 },
                                       { 0, 0, 0, 0 }, { 0, 1e-3f, 1e-3f, 1e-3f } };
      for (unsigned k = 0; k < 6; k++)
         for (unsigned c = 0; c < 4; c++) {
            const float g = got[(i * 6 + k) * 4 + c];
            if (!(fabsf(g - want[k][c]) <= tol[k][c]))
               return FAILF(r, "thread %u result %u lane %u = %.7g, expected %.7g (word 0x%08x)", i, k, c, g, want[k][c], x);
         }
      /* the packs: round to nearest of the clamped value times the range, each field within 1 of it */
      for (unsigned k = 0; k < 4; k++) {
         const unsigned fields = k < 2 ? 4 : 2, bits = 32 / fields;
         for (unsigned c = 0; c < fields; c++) {
            const float src = k == 0 ? v[c] * 0.25f + 0.5f : k == 1 ? v[c] * 0.25f : k == 2 ? v[c] * 0.25f + 0.5f : v[2 + c] * 0.25f;
            const bool snorm = k == 1 || k == 3;
            const float range = bits == 8 ? (snorm ? 127.0f : 255.0f) : (snorm ? 32767.0f : 65535.0f);
            const long want_v = lrintf(fminf(fmaxf(src, snorm ? -1.0f : 0.0f), 1.0f) * range);
            const uint32_t raw = (packed[i * 4 + k] >> (bits * c)) & ((1u << bits) - 1);
            const long got_v = snorm ? (bits == 8 ? (long)(int8_t)raw : (long)(int16_t)raw) : (long)raw;
            if (labs(got_v - want_v) > 1)
               return FAILF(r, "thread %u pack %u field %u = %ld, expected %ld", i, k, c, got_v, want_v);
         }
      }
   }
   return true;
}

static void
tex_ms_setup(struct run *r)
{
   static const float clear[4] = { 0.25f, 0.5f, 0.75f, 1.0f };
   tex_ms_clear(r, 0, 8, 8, 4, clear);
   slot(r, 0, 64 * 2 * 16);
}

static bool
tex_ms_check(struct run *r)
{
   const float *got = S(r, 0);
   const float want[2][4] = { { 64 / 255.0f, 128 / 255.0f, 191 / 255.0f, 1 }, { 8, 8, 4, 128 / 255.0f } };
   for (unsigned i = 0; i < 64; i++)
      for (unsigned k = 0; k < 2; k++)
         for (unsigned c = 0; c < 4; c++) {
            const float g = got[(i * 2 + k) * 4 + c];
            /* the clear colour's conversion to 8 bits rounds either way at 0.5 */
            const bool size = k == 1 && c < 3;
            if (!(fabsf(g - want[k][c]) <= (size ? 0.0f : 1.0f / 255 + 1e-6f)))
               return FAILF(r, "texel %u result %u lane %u = %.6f, expected %.6f", i, k, c, g, want[k][c]);
         }
   return true;
}

/* tex_depth_ms (round 11, PLUGIN_ONLY): depth2d_ms (base 0) and a depth2d_ms_array of two layers (bases 0.25, 0.5),
 * each sample's depth base + (sample + 1) / 16: read by sample, by layer, and the queries */
static const float tdm_base[3] = { 0.0f, 0.25f, 0.5f };
static void
tex_dms_setup(struct run *r)
{
   tex_ms_depth(r, 0, "tex_depth_ms", 8, 8, 4, 1, tdm_base);
   tex_ms_depth(r, 1, "tex_depth_ms", 8, 8, 4, 2, tdm_base + 1);
   slot(r, 0, 64 * 3 * 16);
}

static bool
tex_dms_check(struct run *r)
{
   const float *got = S(r, 0);
   for (unsigned i = 0; i < 64; i++) {
      const float w[3][4] = {
         { 0.0625f, 0.125f, 0.1875f, 0.25f },
         { 0.25f + (float)(i % 4 + 1) * 0.0625f, 0.5f + (float)(3 - i % 4 + 1) * 0.0625f, 4, 4 },
         { 8, 8, 8, 8 },
      };
      static const char *what[3] = { "depth2d_ms samples 0-3", "depth2d_ms_array layers 0, 1 and the sample counts",
                                     "the widths and heights" };
      for (unsigned k = 0; k < 3; k++)
         for (unsigned c = 0; c < 4; c++)
            if (got[(i * 3 + k) * 4 + c] != w[k][c])
               return FAILF(r, "%s, texel %u lane %u = %g, expected %g", what[k], i, c, got[(i * 3 + k) * 4 + c], w[k][c]);
   }
   return true;
}

static const struct test tests[] = {
   { "fmath", NULL, "fmath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, fmath_setup, fmath_check },
   { "fmath_precise", NULL, "fmath_precise", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, fmath_setup, fmath_check },
   { "imath", NULL, "imath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, imath_setup, imath_check },
   { "conv", NULL, "conv", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, conv_setup, conv_check },
   { "conv_edge", NULL, "conv_edge", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, conv_edge_setup, conv_edge_check },
   { "minmax_nan", "metal_edge", "minmax_nan", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, minmax_nan_setup, minmax_nan_check },
   { "round_edge", "metal_edge", "round_edge", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, round_edge_setup, round_edge_check },
   { "pow_edge", "metal_edge", "pow_edge", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, pow_edge_setup, pow_edge_check },
   { "narrow_exact", "metal_edge", "narrow_exact", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, narrow_exact_setup, narrow_exact_check },
   { "loop_phi", NULL, "loop_phi", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, loop_phi_setup, loop_phi_check },
   { "switch", NULL, "switch_case", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, switch_setup, switch_check },
   { "tg_reduce", NULL, "tg_reduce", { 256, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, tg_reduce_setup, tg_reduce_check },
   { "tg_arg", NULL, "tg_arg", { 128, 1, 1 }, { 32, 1, 1 }, { 128 * 4, 128 * 2 }, 0, NULL, tg_arg_setup, tg_arg_check },
   { "dev_atomics", NULL, "dev_atomics", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, dev_atomics_setup, dev_atomics_check },
   { "tg_atomics", NULL, "tg_atomics", { 256, 1, 1 }, { 32, 1, 1 }, { 0 }, 0, NULL, tg_atomics_setup, tg_atomics_check },
   { "const_table", NULL, "const_table", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, const_table_setup, const_table_check },
   { "struct_buf", NULL, "struct_buf", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, NULL, struct_buf_setup, struct_buf_check },
   { "grid2d", NULL, "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, 0, NULL, grid2d_setup, grid2d_check },
   { "grid2d_indirect", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_INDIRECT, NULL, grid2d_setup, grid2d_check },
   { "grid2d_base", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_BASE, NULL, grid2d_setup, grid2d_check },
   { "simd_builtins", NULL, "simd_builtins", { 128, 1, 1 }, { 8, 1, 1 }, { 0 }, 0, NULL, simd_setup, simd_check },
   { "narrow", NULL, "narrow", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, narrow_setup, narrow_check },
   { "vec_ops", NULL, "vec_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, vec_ops_setup, vec_ops_check },
   { "tg_transpose", NULL, "tg_transpose", { 16, 16, 1 }, { 4, 4, 1 }, { 0 }, 0, NULL, tg_transpose_setup, tg_transpose_check },
   { "thread_array", NULL, "thread_array", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, thread_array_setup, thread_array_check },
   { "ptr_ops", NULL, "ptr_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, ptr_ops_setup, ptr_ops_check },
   { "simd_funcs", NULL, "simd_funcs", { 128, 1, 1 }, { 32, 1, 1 }, { 0 }, 0, NULL, simd_funcs_setup, simd_funcs_check },
   { "math_lib", NULL, "math_lib", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, math_lib_setup, math_lib_check },
   { "mat_const", NULL, "mat_const", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, NULL, mat_const_setup, mat_const_check },
   { "ctrl_flow", NULL, "ctrl_flow", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, ctrl_flow_setup, ctrl_flow_check },
   { "atomic_float", NULL, "atomic_float", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, atomic_float_setup, atomic_float_check },
   { "grid3d", NULL, "grid3d", { 4, 4, 4 }, { 4, 2, 2 }, { 0 }, 0, NULL, grid3d_setup, grid3d_check },
   { "agg_ops", NULL, "agg_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, agg_ops_setup, agg_ops_check },
   { "fc_refuse", NULL, "fc_refuse", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_ROAD, NULL, fc_refuse_setup, fc_refuse_check },
   { "ib_refuse", NULL, "ib_refuse", { 16, 16, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, NULL, NULL, NULL },
   { "fc_kernel_a", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, &fc_a, fc_kernel_setup, fc_kernel_check },
   { "fc_argindex", NULL, "fc_argindex", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, 0, &fc_argidx, fc_argindex_setup, fc_argindex_check },
   { "fc_kernel_b", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, &fc_b, fc_kernel_setup, fc_kernel_check },
   { "fc_kernel_c", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, MUST_ROAD, &fc_c, fc_kernel_setup, fc_kernel_check },
   { "fc_kernel_none", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, MUST_ROAD, NULL, fc_kernel_setup, fc_kernel_check },
   { "tex_read", NULL, "tex_read", { 8, 8, 1 }, { 2, 2, 1 }, { 0 }, 0, NULL, tex_read_setup, tex_read_check },
   { "tex_write", NULL, "tex_write", { 8, 8, 1 }, { 2, 2, 1 }, { 0 }, 0, NULL, tex_write_setup, tex_write_check },
   { "tex_array3d", NULL, "tex_array3d", { 8, 8, 1 }, { 1, 1, 4 }, { 0 }, 0, NULL, tex_array3d_setup, tex_array3d_check },
   { "tex_sample", NULL, "tex_sample", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_sample_setup, tex_sample_check,
     tex_sample_expect },
   { "tex_cube", NULL, "tex_cube", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_cube_setup, tex_cube_check },
   { "tex_query", NULL, "tex_query", { 1, 1, 1 }, { 1, 1, 1 }, { 0 }, 0, NULL, tex_query_setup, tex_query_check },
   { "tex_buffer", NULL, "tex_buffer", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_buffer_setup, tex_buffer_check },
   { "tex_depth", NULL, "tex_depth", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_depth_setup, tex_depth_check,
     tex_depth_expect },
   { "tex_gather_depth", NULL, "tex_gather_depth", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, MUST_ROAD, NULL, tex_gdepth_setup,
     tex_gdepth_check },
   { "tex_cube_write", NULL, "tex_cube_write", { 8, 8, 1 }, { 1, 1, 6 }, { 0 }, MUST_ROAD, NULL, tex_cubew_setup,
     tex_cubew_check },
   { "pack565", "pack565_bias", "pack565", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_ROAD, NULL, pack565_setup, pack565_check },
   { "sample_bias_k", "pack565_bias", "sample_bias_k", { 16, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_ROAD, NULL, sbias_setup,
     sbias_check },
   { "tex_fence", NULL, "tex_fence", { 8, 8, 1 }, { 2, 2, 1 }, { 0 }, 0, NULL, tex_fence_setup, tex_fence_check },
   { "tex_pixel", NULL, "tex_pixel", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_pixel_setup, tex_pixel_check, tex_pixel_expect },
   { "tex_argpixel", NULL, "tex_argpixel", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, tex_argpixel_setup, tex_pixel_check },
   { "tex_aref", NULL, "tex_aref", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, taref_setup, taref_check },
   { "tex_warrays", NULL, "tex_warrays", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, twa_setup, twa_check },
   { "tex_pixmodes", NULL, "tex_pixmodes", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, pxm_setup, pxm_check },
   { "tex_pixwrap", NULL, "tex_pixwrap", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, pxm_setup, pxw_check },
   { "tex_tbarray", NULL, "tex_tbarray", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, tba_setup, tba_check },
   { "tex_null", NULL, "tex_null", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, tnull_setup, tnull_check },
   { "simd_fill", NULL, "simd_fill", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, sf_setup, sf_check },
   { "mad_sat", NULL, "mad_sat_k", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_ROAD, NULL, ms_setup, ms_check },
   { "mad_sat_v", "mad_sat", "mad_sat_v", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, NULL, ms_setup, NULL },
   { "tex_null_tb", "tex_null", "tex_null_tb", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, tnull_setup, tnull_check },
   { "tex_bicubic", NULL, "tex_bicubic", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, bic_setup, bic_check },
   { "tex_pixborder", NULL, "tex_pixborder", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_REFUSE, NULL, pxm_setup, pxm_check },
   { "tex_offset", NULL, "tex_offset", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_offset_setup, tex_offset_check },
   { "fc_texture", NULL, "fc_texture", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, 0, &fc_tex, fc_texture_setup, fc_texture_check,
     fc_texture_expect },
   { "fc_texture_none", "fc_texture", "fc_texture", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, MUST_ROAD, NULL, fc_texture_none_setup,
     fc_texture_none_check, fc_texture_none_expect },
   { "tex_pixel_array", NULL, "tex_pixel_array", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_pixel_array_setup,
     tex_pixel_array_check },
   { "tex_intsample", NULL, "tex_intsample", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, tex_intsample_setup,
     tex_intsample_check },
   { "pack", NULL, "pack", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, 0, NULL, pack_setup, pack_check },
   { "tex_ms", NULL, "tex_ms", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, tex_ms_setup, tex_ms_check },
   { "tex_depth_ms", NULL, "tex_depth_ms", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY | MUST_ROAD, NULL, tex_dms_setup,
     tex_dms_check },
   { "tex_depth_asize", "tex_depth_ms", "tex_depth_asize", { 1, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, NULL, NULL, NULL },
   { "tex_arrays", NULL, "tex_arrays", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, tex_arrays_setup, tex_arrays_check },
   { "ab_kernel", NULL, "ab_kernel", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, ab_kernel_setup, ab_kernel_check },
   { "ab_encoder", NULL, "ab_encoder", { 8, 8, 1 }, { 1, 1, 1 }, { 0 }, PLUGIN_ONLY, NULL, ab_encoder_setup, ab_encoder_check },
   { "oob_kernel", NULL, "oob_kernel", { 64, 1, 1 }, { 4, 1, 1 }, { 0 }, PLUGIN_ONLY | ROBUST_ONLY, NULL, oob_kernel_setup,
     oob_kernel_check },
};
