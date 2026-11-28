// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
//
// render_tests.h: the render pipelines of tools/air-nir/metal-render through the plugin's own render path (plugin_run.m),
// on either road. Each test is a vertex and a fragment function of one .metal file, drawn into offscreen targets of
// 64x64 (at most four colour attachments and a depth attachment), read back and compared per pixel with a reference the
// CPU computes, within the tolerance the test states. Pixels within 1.5 pixels of a primitive's edge are not compared
// where the primitive does not cover the whole target (which pixels an edge covers is the rasterizer's rule, not the
// shader's).
//
// Included by plugin_run.m after airrun_tests.h (rnd, rndf, struct fcset).

#define RW 64u
#define RH 64u
#define RMAXCOL 4u
#define RMAXBUF 16u
#define RMAXTEX 16u
#define RMAXSAMP 8u
static const float clear_col[4] = { 0.1f, 0.2f, 0.3f, 0.4f };

struct rrun {
   const struct rtest *t;
   id<MTLBuffer> vbuf[RMAXBUF], fbuf[RMAXBUF];   /* vertex and fragment stage buffers, by index */
   id<MTLTexture> ftex[RMAXTEX];
   id<MTLBuffer> tstage[RMAXTEX];                 /* each fragment texture's texels, uploaded before the draw */
   unsigned tw[RMAXTEX], th[RMAXTEX], tbpp[RMAXTEX];
   id<MTLSamplerState> fsamp[RMAXSAMP];
   /* argument buffers: the buffers, textures and samplers a test reaches only through one are made resident
    * (useResource:usage:stages:) and not bound */
   uint32_t vbuf_ab, fbuf_ab, ftex_ab, fsamp_ab;
   id<MTLBuffer> back[RMAXCOL], dback;            /* the attachments, read back */
   uint8_t tex[16 * 16 * 4];                      /* r_texture's texels */
   uint8_t texn[3][16 * 16 * 4];                  /* r_handlechoice's three textures */
   float pal[16][4];                              /* r_buffers' palette */
   uint32_t idx[1000];
   uint8_t abt[12][16 * 16 * 4];                  /* r_argbuf's textures */
   char why[256];
};

struct rtest {
   const char *name, *air, *vfn, *ffn;
   MTLPixelFormat col[RMAXCOL];      /* MTLPixelFormatInvalid: none */
   MTLPixelFormat depth;
   MTLPrimitiveType prim;
   unsigned vstart, vcount, instances, base_instance;
   const struct fcset *fc;           /* the fragment function's constants */
   void (*vdesc)(MTLVertexDescriptor *);
   void (*setup)(struct rrun *);
   bool (*check)(struct rrun *);
   const char *tolerance;
   bool spirv_road;                  /* the frontend must refuse it: it passes only on the SPIR-V road */
   bool fc_both;                     /* the vertex function is made with the constants too */
   const char *linked_air, *linked_fn; /* a visible function of another library, linked into the fragment stage */
   const char *road_only;            /* why the SPIR-V road makes no pipeline of it: skipped there */
};

static id<MTLBuffer>
rbuf(const void *data, size_t n)
{
   id<MTLBuffer> b = [dev newBufferWithLength:n options:MTLResourceStorageModeShared];
   if (!b) {
      fprintf(stderr, "plugin_run: newBufferWithLength:%zu failed\n", n);
      exit(2);
   }
   if (data)
      memcpy(b.contents, data, n);
   return b;
}

static const float quad[12] = { -1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1 };   /* the whole target, two triangles */

static void
setup_quad(struct rrun *r)
{
   r->vbuf[0] = rbuf(quad, sizeof quad);
}

/* the centre of pixel (px, py): u from the left edge, v from the bottom (Metal's NDC y is up), w from the top */
static float px_u(unsigned px) { return (px + 0.5f) / RW; }
static float px_v(unsigned py) { return 1.0f - (py + 0.5f) / RH; }
static float px_w(unsigned py) { return (py + 0.5f) / RH; }

static const float *
rcol(struct rrun *r, unsigned att, unsigned px, unsigned py)
{
   return (const float *)r->back[att].contents + ((size_t)py * RW + px) * 4;
}

static bool
rwant(struct rrun *r, unsigned att, unsigned px, unsigned py, const float *want, unsigned n, float tol)
{
   const float *got = rcol(r, att, px, py);
   for (unsigned c = 0; c < n; c++) {
      if (!(fabsf(got[c] - want[c]) <= tol)) {
         snprintf(r->why, sizeof r->why, "attachment %u pixel (%u,%u) component %u: %.7g, want %.7g (tolerance %g)", att, px, py,
                  c, got[c], want[c], tol);
         return false;
      }
   }
   return true;
}

/* signed distance in pixels from the pixel centre to the edge a->b of a triangle (positive inside a counter-clockwise
 * triangle in pixel coordinates, y down), and the screen-space barycentrics */
static float
edge_dist(const float *a, const float *b, float x, float y)
{
   const float ex = b[0] - a[0], ey = b[1] - a[1];
   return ((x - a[0]) * ey - (y - a[1]) * ex) / sqrtf(ex * ex + ey * ey);
}

/* 1 inside by more than 1.5 pixels, -1 outside by more than 1.5, 0 near an edge; bary when inside */
static int
tri_where(const float s[3][2], unsigned px, unsigned py, float bary[3])
{
   const float x = px + 0.5f, y = py + 0.5f;
   float d[3];
   const float area = (s[1][0] - s[0][0]) * (s[2][1] - s[0][1]) - (s[2][0] - s[0][0]) * (s[1][1] - s[0][1]);
   const float sign = area > 0 ? -1.0f : 1.0f;
   d[0] = sign * edge_dist(s[1], s[2], x, y);
   d[1] = sign * edge_dist(s[2], s[0], x, y);
   d[2] = sign * edge_dist(s[0], s[1], x, y);
   const float m = fminf(d[0], fminf(d[1], d[2]));
   if (m > 1.5f) {
      const float a = fabsf(area);
      bary[0] = fabsf((s[1][0] - x) * (s[2][1] - y) - (s[2][0] - x) * (s[1][1] - y)) / a;
      bary[1] = fabsf((s[2][0] - x) * (s[0][1] - y) - (s[0][0] - x) * (s[2][1] - y)) / a;
      bary[2] = 1.0f - bary[0] - bary[1];
      return 1;
   }
   return m < -1.5f ? -1 : 0;
}

static void
ndc_to_px(float x, float y, float out[2])
{
   out[0] = (x * 0.5f + 0.5f) * RW;
   out[1] = (0.5f - y * 0.5f) * RH;
}

/* ---- r_stage_in --------------------------------------------------------------------------------------------------- */

struct rs_vertex {
   float pos[2], a3[3], a4[4];
   uint8_t un[4];
   uint16_t h2[2];
   uint8_t ub[4];
};
_Static_assert(sizeof(struct rs_vertex) == 48, "the vertex descriptor's layout");

static void
rs_vdesc(MTLVertexDescriptor *vd)
{
   static const struct { MTLVertexFormat f; unsigned off; } a[] = {
      { MTLVertexFormatFloat2, 0 }, { MTLVertexFormatFloat3, 8 }, { MTLVertexFormatFloat4, 20 },
      { MTLVertexFormatUChar4Normalized, 36 }, { MTLVertexFormatHalf2, 40 }, { MTLVertexFormatUChar4, 44 },
   };
   for (unsigned i = 0; i < 6; i++) {
      vd.attributes[i].format = a[i].f;
      vd.attributes[i].offset = a[i].off;
      vd.attributes[i].bufferIndex = 2;
   }
   vd.layouts[2].stride = sizeof(struct rs_vertex);
   vd.layouts[2].stepFunction = MTLVertexStepFunctionPerVertex;
}

static uint16_t
half_bits(float f)
{
   _Float16 h = (_Float16)f;
   uint16_t b;
   memcpy(&b, &h, 2);
   return b;
}

static void
rs_setup(struct rrun *r)
{
   struct rs_vertex v[6];
   for (unsigned i = 0; i < 6; i++) {
      const float u = quad[2 * i] * 0.5f + 0.5f, w = quad[2 * i + 1] * 0.5f + 0.5f;
      v[i] = (struct rs_vertex) { { quad[2 * i], quad[2 * i + 1] }, { 0.25f + 0.5f * u, 0.75f - 0.5f * w, 0.5f * u + 0.25f * w },
                                  { u, w, 1 - u, 0.5f }, { (uint8_t)(255 * u), (uint8_t)(255 * w), 128, 64 },
                                  { half_bits(0.25f + 0.5f * u), half_bits(0.5f * w) }, { (uint8_t)(200 * u), (uint8_t)(100 * w), 7, 9 } };
   }
   r->vbuf[2] = rbuf(v, sizeof v);
}

static bool
rs_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_v(py);
         const float c0[4] = { 0.25f + 0.5f * u, 0.75f - 0.5f * w, 0.5f * u + 0.25f * w, 0.25f + 0.5f * u };
         const float c1[4] = { u, w, 1 - u, 0.5f };
         const float c2[4] = { u, w, 128 / 255.0f, 64 / 255.0f };
         const float c3[4] = { 200 * u / 256, 100 * w / 256, 0.5f * w, 7 / 16.0f + 9 };
         if (!rwant(r, 0, px, py, c0, 4, 1e-4f) || !rwant(r, 1, px, py, c1, 4, 1e-4f) || !rwant(r, 2, px, py, c2, 4, 1e-4f) ||
             !rwant(r, 3, px, py, c3, 4, 1e-4f))
            return false;
      }
   return true;
}

/* ---- r_instanced -------------------------------------------------------------------------------------------------- */

static void
ri_setup(struct rrun *r)
{
   static const float corner[12] = { 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1 };
   r->vbuf[0] = rbuf(corner, sizeof corner);
}

static bool
ri_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const unsigned cx = px / 16, cy = py / 16, iid = cy * 4 + cx;
         if (px % 16 == 0 || px % 16 == 15 || py % 16 == 0 || py % 16 == 15)
            continue;
         const bool drawn = iid >= 2 && iid < 10;
         const float want[4] = { (float)iid, 3, 2, 1 };
         if (!rwant(r, 0, px, py, drawn ? want : clear_col, 4, 0))
            return false;
      }
   return true;
}

/* ---- r_link ------------------------------------------------------------------------------------------------------- */

static bool
rl_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_v(py);
         const float c0[4] = { u, w, 1 - u, 0.5f }, c1[4] = { 2 * w, 0.25f, u + w, 3 - u };
         if (!rwant(r, 0, px, py, c0, 4, 1e-4f) || !rwant(r, 1, px, py, c1, 4, 1e-4f))
            return false;
      }
   return true;
}

/* ---- r_interp ----------------------------------------------------------------------------------------------------- */

static const float rp_pos[12] = { -0.8f, -0.8f, 0, 1, 0.8f * 2, -0.6f * 2, 0, 2, 0, 0.8f * 4, 0, 4 };
static const float rp_val[3] = { 0.5f, 1.0f, 2.0f };

static void
rp_setup(struct rrun *r)
{
   r->vbuf[0] = rbuf(rp_pos, sizeof rp_pos);
   r->vbuf[1] = rbuf(rp_val, sizeof rp_val);
}

static bool
rp_check(struct rrun *r)
{
   float s[3][2];
   for (unsigned i = 0; i < 3; i++)
      ndc_to_px(rp_pos[4 * i] / rp_pos[4 * i + 3], rp_pos[4 * i + 1] / rp_pos[4 * i + 3], s[i]);
   unsigned inside = 0;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         float b[3];
         const int where = tri_where(s, px, py, b);
         if (where == 0)
            continue;
         if (where < 0) {
            if (!rwant(r, 0, px, py, clear_col, 4, 0))
               return false;
            continue;
         }
         inside++;
         float num = 0, den = 0, lin = 0;
         for (unsigned i = 0; i < 3; i++) {
            num += b[i] * rp_val[i] / rp_pos[4 * i + 3];
            den += b[i] / rp_pos[4 * i + 3];
            lin += b[i] * rp_val[i];
         }
         const float want[4] = { num / den, lin, rp_val[0], 10 };
         if (!rwant(r, 0, px, py, want, 2, 2e-3f) || !rwant(r, 0, px, py, want, 4, 2e-3f))
            return false;
         if (rcol(r, 0, px, py)[2] != rp_val[0] || rcol(r, 0, px, py)[3] != 10) {
            snprintf(r->why, sizeof r->why, "pixel (%u,%u): flat %.7g and %.7g, want the first vertex's %.7g and 10", px, py,
                     rcol(r, 0, px, py)[2], rcol(r, 0, px, py)[3], rp_val[0]);
            return false;
         }
      }
   if (inside < 200) {
      snprintf(r->why, sizeof r->why, "only %u pixels inside the triangle", inside);
      return false;
   }
   return true;
}

/* ---- r_texture ---------------------------------------------------------------------------------------------------- */

static void
rt_setup(struct rrun *r)
{
   setup_quad(r);
   for (unsigned i = 0; i < sizeof r->tex; i++)
      r->tex[i] = (uint8_t)(rnd() >> 24);
   MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:16 mipmapped:NO];
   td.usage = MTLTextureUsageShaderRead;
   td.storageMode = MTLStorageModePrivate;
   r->ftex[0] = [dev newTextureWithDescriptor:td];
   r->tstage[0] = rbuf(r->tex, sizeof r->tex);
   r->tw[0] = r->th[0] = 16;
   r->tbpp[0] = 4;
   MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
   sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterNearest;
   sd.sAddressMode = sd.tAddressMode = MTLSamplerAddressModeClampToEdge;
   sd.normalizedCoordinates = YES;
   r->fsamp[0] = [dev newSamplerStateWithDescriptor:sd];
   [sd release];
}

static float
rt_texel(const struct rrun *r, int x, int y, unsigned c)
{
   x = x < 0 ? 0 : x > 15 ? 15 : x;
   y = y < 0 ? 0 : y > 15 ? 15 : y;
   return r->tex[(y * 16 + x) * 4 + c] / 255.0f;
}

static bool
rt_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_w(py);
         float n[4], l[4];
         const float tx = u * 16 - 0.5f, ty = w * 16 - 0.5f;
         const int x0 = (int)floorf(tx), y0 = (int)floorf(ty);
         const float fx = tx - x0, fy = ty - y0;
         for (unsigned c = 0; c < 4; c++) {
            n[c] = rt_texel(r, (int)(u * 16), (int)(w * 16), c);
            l[c] = (1 - fy) * ((1 - fx) * rt_texel(r, x0, y0, c) + fx * rt_texel(r, x0 + 1, y0, c)) +
                   fy * ((1 - fx) * rt_texel(r, x0, y0 + 1, c) + fx * rt_texel(r, x0 + 1, y0 + 1, c));
         }
         if (!rwant(r, 0, px, py, n, 4, 1e-6f) || !rwant(r, 1, px, py, l, 4, 1.0f / 128))
            return false;
      }
   return true;
}

/* ---- r_argpixel (round 5) ---------------------------------------------------------------------------------------- */

static void
rap_setup(struct rrun *r)
{
   rt_setup(r);
   [r->fsamp[0] release];
   for (unsigned i = 0; i < 2; i++) {
      MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
      sd.minFilter = sd.magFilter = i ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
      sd.mipFilter = MTLSamplerMipFilterNotMipmapped;
      sd.sAddressMode = sd.tAddressMode = MTLSamplerAddressModeClampToEdge;
      sd.normalizedCoordinates = NO;
      r->fsamp[i] = [dev newSamplerStateWithDescriptor:sd];
      [sd release];
   }
}

/* ---- r_fcout (round 5) -------------------------------------------------------------------------------------------- */

/* fco_tint true (its varying exists), fco_extra false (its does not), fco_target 0: the output is [[color(1)]] */
static const struct fcset fc_fcout = { 3, { { 0, 1, { 1 } }, { 1, 1, { 0 } }, { 2, 4, { 0 } } } };

static bool
rfo_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float want[4] = { px_u(px), px_w(py), 0.5f, 1 };
         if (!rwant(r, 0, px, py, clear_col, 4, 1e-6f) || !rwant(r, 1, px, py, want, 4, 1e-4f))
            return false;
      }
   return true;
}

/* ---- r_discard ---------------------------------------------------------------------------------------------------- */

static bool
rd_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const bool gone = ((px >> 2) ^ (py >> 2)) & 1;
         const float want[4] = { (px + 0.5f) / 64, (py + 0.5f) / 64, 0.5f, 1 };
         if (!rwant(r, 0, px, py, gone ? clear_col : want, 4, 1e-6f))
            return false;
      }
   return true;
}

/* ---- r_depth ------------------------------------------------------------------------------------------------------ */

static bool
rz_check(struct rrun *r)
{
   const float *d = r->dback.contents;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float want[4] = { 0.5f, 1, 0, 1 };
         if (!rwant(r, 0, px, py, want, 4, 1e-6f))
            return false;
         const float dz = (px + 0.5f + py + 0.5f) / 256.0f, got = d[py * RW + px];
         if (!(fabsf(got - dz) <= 1e-6f)) {
            snprintf(r->why, sizeof r->why, "depth at (%u,%u): %.7g, want %.7g", px, py, got, dz);
            return false;
         }
      }
   return true;
}

/* ---- r_mrt -------------------------------------------------------------------------------------------------------- */

static bool
rm_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_w(py);
         const uint8_t *g = (const uint8_t *)r->back[0].contents + ((size_t)py * RW + px) * 4;
         const float want[4] = { u, w, 0.25f, 1 };
         for (unsigned c = 0; c < 4; c++) {
            const int q = (int)lrintf(want[c] * 255);
            if (abs((int)g[c] - q) > 1) {
               snprintf(r->why, sizeof r->why, "rgba8 pixel (%u,%u) component %u: %u, want %d (1 LSB)", px, py, c, g[c], q);
               return false;
            }
         }
         const float f = ((const float *)r->back[1].contents)[py * RW + px], fw = u * 100 + w;
         if (!(fabsf(f - fw) <= 1e-4f * fw)) {
            snprintf(r->why, sizeof r->why, "r32float pixel (%u,%u): %.7g, want %.7g (1e-4 relative)", px, py, f, fw);
            return false;
         }
      }
   return true;
}

/* ---- r_front ------------------------------------------------------------------------------------------------------ */

static const float rf_tri[12] = { -0.9f, -0.9f, -0.5f, 0.9f, -0.1f, -0.9f,    /* clockwise on screen: front */
                                  0.1f, -0.9f, 0.9f, -0.9f, 0.5f, 0.9f };     /* counter-clockwise: back */

static void
rf_setup(struct rrun *r)
{
   r->vbuf[0] = rbuf(rf_tri, sizeof rf_tri);
}

static bool
rf_check(struct rrun *r)
{
   float s[2][3][2];
   for (unsigned t = 0; t < 2; t++)
      for (unsigned i = 0; i < 3; i++)
         ndc_to_px(rf_tri[6 * t + 2 * i], rf_tri[6 * t + 2 * i + 1], s[t][i]);
   static const float red[4] = { 1, 0, 0, 1 }, green[4] = { 0, 1, 0, 1 };
   unsigned seen[2] = { 0, 0 };
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         float b[3];
         const int w0 = tri_where(s[0], px, py, b), w1 = tri_where(s[1], px, py, b);
         const float *want = w0 > 0 ? red : w1 > 0 ? green : (w0 < 0 && w1 < 0) ? clear_col : NULL;
         if (!want)
            continue;
         if (w0 > 0 || w1 > 0)
            seen[w0 > 0 ? 0 : 1]++;
         if (!rwant(r, 0, px, py, want, 4, 0))
            return false;
      }
   if (seen[0] < 100 || seen[1] < 100) {
      snprintf(r->why, sizeof r->why, "too few pixels inside the triangles (%u, %u)", seen[0], seen[1]);
      return false;
   }
   return true;
}

/* ---- r_points ----------------------------------------------------------------------------------------------------- */

static const float rpt_px[8] = { 16, 16, 48, 16, 16, 48, 40, 44 };

static void
rpt_setup(struct rrun *r)
{
   float p[8];
   for (unsigned i = 0; i < 4; i++) {
      p[2 * i] = rpt_px[2 * i] / 32 - 1;
      p[2 * i + 1] = 1 - rpt_px[2 * i + 1] / 32;
   }
   r->vbuf[0] = rbuf(p, sizeof p);
}

static bool
rpt_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float *want = clear_col;
         float pc[4] = { 0, 0, 0, 1 };
         for (unsigned i = 0; i < 4; i++) {
            const float x0 = rpt_px[2 * i] - 4, y0 = rpt_px[2 * i + 1] - 4;
            if (px >= x0 && px < x0 + 8 && py >= y0 && py < y0 + 8) {
               pc[0] = (px + 0.5f - x0) / 8;
               pc[1] = (py + 0.5f - y0) / 8;
               want = pc;
            }
         }
         if (!rwant(r, 0, px, py, want, 4, 1e-5f))
            return false;
      }
   return true;
}

/* ---- r_fc --------------------------------------------------------------------------------------------------------- */

static const struct fcset fc_render = { 3, { { 0, 1, { 1 } }, { 1, 4, { 0x00, 0x00, 0x00, 0x3f } /* 0.5 */ }, { 2, 4, { 2 } } } };

static bool
rfc_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float want[4] = { 1, px_u(px) * 0.5f, 0, 1 };
         if (!rwant(r, 0, px, py, want, 4, 1e-6f))
            return false;
      }
   return true;
}

/* ---- r_buffers ---------------------------------------------------------------------------------------------------- */

static void
rb_setup(struct rrun *r)
{
   setup_quad(r);
   for (unsigned i = 0; i < 16; i++)
      for (unsigned c = 0; c < 4; c++)
         r->pal[i][c] = rndf(0, 1);
   for (unsigned i = 0; i < 1000; i++)
      r->idx[i] = rnd();
   const uint32_t n = 1000;
   r->fbuf[0] = rbuf(r->pal, sizeof r->pal);
   r->fbuf[3] = rbuf(r->idx, sizeof r->idx);
   r->fbuf[5] = rbuf(&n, sizeof n);
}

static bool
rb_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++)
         if (!rwant(r, 0, px, py, r->pal[r->idx[(py * 64 + px) % 1000] & 15], 4, 0))
            return false;
   return true;
}

/* ---- r_clip ------------------------------------------------------------------------------------------------------- */

static bool
rc_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float x = (px + 0.5f) / 32 - 1;   /* NDC; clipped where x < 0.25 */
         if (fabsf(x - 0.25f) < 1.5f / 32)
            continue;
         const float want[4] = { px_u(px), px_v(py), 0.75f, 1 };
         if (!rwant(r, 0, px, py, x > 0.25f ? want : clear_col, 4, 1e-4f))
            return false;
      }
   return true;
}

/* ---- r_primid ----------------------------------------------------------------------------------------------------- */

static bool
rq_check(struct rrun *r)
{
   /* the quad's first triangle is (-1,-1) (1,-1) (-1,1): in pixels (y down) the half below the diagonal from the top
    * left to the bottom right, where y > x */
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float d = (px + 0.5f) - (py + 0.5f);   /* > 0: the second triangle */
         if (fabsf(d) < 2.5f)
            continue;
         const float want[4] = { d < 0 ? 0.0f : 1.0f, 1, 0, 1 };
         if (!rwant(r, 0, px, py, want, 4, 0))
            return false;
      }
   return true;
}

/* ---- r_fcin ------------------------------------------------------------------------------------------------------- */

/* the constants: fci_col true, fci_junk false (its attribute and its varying do not exist) */
static const struct fcset fc_fcin = { 2, { { 0, 1, { 1 } }, { 1, 1, { 0 } } } };

static void
rfi_vdesc(MTLVertexDescriptor *vd)
{
   vd.attributes[0].format = MTLVertexFormatFloat2;
   vd.attributes[0].offset = 0;
   vd.attributes[0].bufferIndex = 2;
   vd.attributes[1].format = MTLVertexFormatFloat4;
   vd.attributes[1].offset = 8;
   vd.attributes[1].bufferIndex = 2;
   vd.layouts[2].stride = 24;
   vd.layouts[2].stepFunction = MTLVertexStepFunctionPerVertex;
}

static void
rfi_setup(struct rrun *r)
{
   float v[6][6];
   for (unsigned i = 0; i < 6; i++) {
      const float u = quad[2 * i] * 0.5f + 0.5f, w = quad[2 * i + 1] * 0.5f + 0.5f;
      const float row[6] = { quad[2 * i], quad[2 * i + 1], u, w, 0.25f, 1 };
      memcpy(v[i], row, sizeof row);
   }
   r->vbuf[2] = rbuf(v, sizeof v);
}

static bool
rfi_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float want[4] = { px_u(px), px_v(py), 0.5f, 1 };
         if (!rwant(r, 0, px, py, want, 4, 1e-4f))
            return false;
      }
   return true;
}

/* ---- r_rleblit ---------------------------------------------------------------------------------------------------- */

/* CoreAnimation's buffer-to-image blit (Terminal's window): three rects, each a strip of 4 vertices of its own instance,
 * whose pixels are bytes of a 1.2 MB buffer at offset + x + y * bpr (the last rect's offset far into it) */
struct rbl_rect { float min[2], size[2], offset, bpr; };
static const struct rbl_rect rbl_rects[3] = {
   { { 4, 60 }, { 20, 24 }, 1234, 37 },
   { { 30, 60 }, { 30, 10 }, 40000, 64 },
   { { 8, 30 }, { 50, 26 }, 1100003, 61 },
};
#define RBL_BYTES (1200u * 1024u)

static uint8_t rbl_byte(unsigned i) { return (uint8_t)((i * 2654435761u) >> 13); }

static void
rbl_setup(struct rrun *r)
{
   const float mvp[16] = { 1.0f / 32, 0, 0, 0, 0, 1.0f / 32, 0, 0, 0, 0, 1, 0, -1, -1, 0, 1 };   /* columns */
   const float color[4] = { 1.0f / 255, 0.5f / 255, 0.25f / 255, 1.0f / 255 };
   r->vbuf[1] = rbuf(rbl_rects, sizeof rbl_rects);
   r->vbuf[2] = rbuf(mvp, sizeof mvp);
   r->fbuf[1] = rbuf(color, sizeof color);
   r->fbuf[9] = rbuf(NULL, RBL_BYTES);
   uint8_t *b = r->fbuf[9].contents;
   for (unsigned i = 0; i < RBL_BYTES; i++)
      b[i] = rbl_byte(i);
}

/* the same data in managed buffers, as CoreAnimation keeps them on a Mac with a discrete GPU */
static id<MTLBuffer>
rbuf_managed(const void *data, size_t n)
{
   id<MTLBuffer> b = [dev newBufferWithLength:n options:MTLResourceStorageModeManaged];
   if (!b) {
      fprintf(stderr, "plugin_run: newBufferWithLength:%zu (managed) failed\n", n);
      exit(2);
   }
   memcpy(b.contents, data, n);
   [b didModifyRange:NSMakeRange(0, n)];
   return b;
}

static void
rbl_setup_managed(struct rrun *r)
{
   rbl_setup(r);
   id<MTLBuffer> *slots[4] = { &r->vbuf[1], &r->vbuf[2], &r->fbuf[1], &r->fbuf[9] };
   for (unsigned i = 0; i < 4; i++) {
      id<MTLBuffer> m = rbuf_managed((*slots[i]).contents, (*slots[i]).length);
      [*slots[i] release];
      *slots[i] = m;
   }
}

/* both targets: a rect's pixel is its byte times the colour (BGRA8, 1 LSB), any other the clear colour */
static bool
rbl_check(struct rrun *r)
{
   for (unsigned att = 0; att < 2; att++)
      for (unsigned py = 0; py < RH; py++)
         for (unsigned px = 0; px < RW; px++) {
            float want[4] = { clear_col[0], clear_col[1], clear_col[2], clear_col[3] };
            for (unsigned k = 0; k < 3; k++) {
               const struct rbl_rect *q = &rbl_rects[k];
               const int x = (int)px - (int)q->min[0], y = (int)py - (64 - (int)q->min[1]);
               if (x < 0 || y < 0 || x >= (int)q->size[0] || y >= (int)q->size[1])
                  continue;
               const float v = rbl_byte((unsigned)(q->offset + x + y * q->bpr)) / 255.0f;
               want[0] = v; want[1] = v * 0.5f; want[2] = v * 0.25f; want[3] = v;
            }
            const uint8_t *g = (const uint8_t *)r->back[att].contents + ((size_t)py * RW + px) * 4;
            const float bgra[4] = { want[2], want[1], want[0], want[3] };
            for (unsigned c = 0; c < 4; c++) {
               const int q = (int)lrintf(bgra[c] * 255);
               if (abs((int)g[c] - q) > 1) {
                  snprintf(r->why, sizeof r->why, "attachment %u bgra8 pixel (%u,%u) byte %u: %u, want %d (1 LSB)", att, px, py,
                           c, g[c], q);
                  return false;
               }
            }
         }
   return true;
}

/* the same blit into one R8 target (CoreAnimation's 576x576 glyph and mask atlas in Terminal): the fragment's color(1)
 * has no attachment */
static bool
rbl8_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         float want = clear_col[0];
         for (unsigned k = 0; k < 3; k++) {
            const struct rbl_rect *q = &rbl_rects[k];
            const int x = (int)px - (int)q->min[0], y = (int)py - (64 - (int)q->min[1]);
            if (x >= 0 && y >= 0 && x < (int)q->size[0] && y < (int)q->size[1])
               want = rbl_byte((unsigned)(q->offset + x + y * q->bpr)) / 255.0f;
         }
         const uint8_t g = ((const uint8_t *)r->back[0].contents)[(size_t)py * RW + px];
         const int q = (int)lrintf(want * 255);
         if (abs((int)g - q) > 1) {
            snprintf(r->why, sizeof r->why, "r8 pixel (%u,%u): %u, want %d (1 LSB)", px, py, g, q);
            return false;
         }
      }
   return true;
}

/* the blit with a NaN offset in its third rect: Metal converts the NaN index to 0, so that rect shows byte 0. The road
 * before 3.0.24 converted it to 0x80000000, a read 2 GiB below the buffer: the MMU fault (Xid 31) Terminal hit */
static const struct rbl_rect rbl_nan_rects[3] = {
   { { 4, 60 }, { 20, 24 }, 1234, 37 },
   { { 30, 60 }, { 30, 10 }, 40000, 64 },
   { { 8, 30 }, { 50, 26 }, NAN, 61 },
};

static void
rbl_nan_setup(struct rrun *r)
{
   rbl_setup(r);
   [r->vbuf[1] release];
   r->vbuf[1] = rbuf(rbl_nan_rects, sizeof rbl_nan_rects);
}

static bool
rbl_nan_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         float want = clear_col[0];
         for (unsigned k = 0; k < 3; k++) {
            const struct rbl_rect *q = &rbl_nan_rects[k];
            const int x = (int)px - (int)q->min[0], y = (int)py - (64 - (int)q->min[1]);
            if (x >= 0 && y >= 0 && x < (int)q->size[0] && y < (int)q->size[1])
               want = rbl_byte(q->offset != q->offset ? 0u : (unsigned)(q->offset + x + y * q->bpr)) / 255.0f;
         }
         const uint8_t g = ((const uint8_t *)r->back[0].contents)[(size_t)py * RW + px];
         const int q = (int)lrintf(want * 255);
         if (abs((int)g - q) > 1) {
            snprintf(r->why, sizeof r->why, "r8 pixel (%u,%u): %u, want %d (1 LSB; a NaN index reads byte 0)", px, py, g, q);
            return false;
         }
      }
   return true;
}

/* the blit with its first rect at the start of a buffer whose rows are 256 MB apart, its top-left corner at an odd pixel:
 * the helper lanes of the quads along its top edge (uv.y extrapolated to -0.5, so y = -1) read 256 MB before the buffer.
 * The SPIR-V road's module returns its helper invocations at the entry (the plugin's nvmtl_independent_helpers.h); the
 * road's did not before 3.0.26, and the read before a glyph at the start of its buffer was Terminal's GPU fault */
#define RBH_BPR (256u << 20)
static const struct rbl_rect rbh_rects[2] = {
   { { 5, 41 }, { 5, 2 }, 0, (float)RBH_BPR },
   { { 21, 13 }, { 7, 3 }, 64, 9 },
};

static void
rbh_setup(struct rrun *r)
{
   const float mvp[16] = { 1.0f / 32, 0, 0, 0, 0, 1.0f / 32, 0, 0, 0, 0, 1, 0, -1, -1, 0, 1 };
   const float color[4] = { 1.0f / 255, 0.5f / 255, 0.25f / 255, 1.0f / 255 };
   r->vbuf[1] = rbuf(rbh_rects, sizeof rbh_rects);
   r->vbuf[2] = rbuf(mvp, sizeof mvp);
   r->fbuf[1] = rbuf(color, sizeof color);
   r->fbuf[9] = rbuf(NULL, RBH_BPR + 64);
   uint8_t *b = r->fbuf[9].contents;
   for (unsigned i = 0; i < 128; i++)
      b[i] = rbl_byte(i);
   for (unsigned i = 0; i < 64; i++)
      b[RBH_BPR + i] = rbl_byte(RBH_BPR + i);
}

static bool
rbh_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         float want = clear_col[0];
         for (unsigned k = 0; k < 2; k++) {
            const struct rbl_rect *q = &rbh_rects[k];
            const int x = (int)px - (int)q->min[0], y = (int)py - (64 - (int)q->min[1]);
            if (x >= 0 && y >= 0 && x < (int)q->size[0] && y < (int)q->size[1])
               want = rbl_byte((unsigned)(q->offset + x + y * q->bpr)) / 255.0f;
         }
         const uint8_t g = ((const uint8_t *)r->back[0].contents)[(size_t)py * RW + px];
         const int q = (int)lrintf(want * 255);
         if (abs((int)g - q) > 1) {
            snprintf(r->why, sizeof r->why, "r8 pixel (%u,%u): %u, want %d (1 LSB)", px, py, g, q);
            return false;
         }
      }
   return true;
}

/* ---- r_oob: a buffer read at an index the data gives, some past its end -------------------------------------------- */

#define ROB_PAL 16u
static void
rob_setup(struct rrun *r)
{
   setup_quad(r);
   float pal[ROB_PAL][4];
   for (unsigned i = 0; i < ROB_PAL; i++)
      for (unsigned c = 0; c < 4; c++)
         pal[i][c] = (i + 1) * 0.0625f - c * 0.01f;
   r->fbuf[0] = rbuf(pal, sizeof pal);
   uint32_t idx[RW * RH];
   for (unsigned i = 0; i < RW * RH; i++)
      idx[i] = (i % 7) == 3 ? ROB_PAL + 256 + i % 100 : i % ROB_PAL;   /* every 7th pixel 4 KB and more past the end */
   for (unsigned i = 5; i < RW * RH; i += 11)   /* byte offsets past 4 GiB: 32-bit, as the SPIR-V road's (k * 16 wraps) */
      idx[i] = 0x10000000u * (1 + i % 15) + i % (ROB_PAL + 4);
   r->fbuf[3] = rbuf(idx, sizeof idx);
}

/* the palette entry index k reads, or -1 for 0: in range, past the end, or past 4 GiB with the 32-bit byte offset the
 * SPIR-V road's descriptor access computes */
static int
rob_entry(uint32_t k)
{
   const uint32_t at = k * 16u;   /* mod 2^32 */
   return at / 16u < ROB_PAL ? (int)(at / 16u) : -1;
}

/* in range: the palette entry; past the end: 0, as the SPIR-V road's bounds-checked descriptor reads it */
static bool
rob_check(struct rrun *r)
{
   const uint32_t *idx = r->fbuf[3].contents;
   const float *pal = r->fbuf[0].contents;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const int e = rob_entry(idx[py * RW + px]);
         const float zero[4] = { 0, 0, 0, 0 };
         if (!rwant(r, 0, px, py, e >= 0 ? pal + e * 4 : zero, 4, 0))
            return false;
      }
   return true;
}

/* ---- r_oob2: a vertex function's buffer past its end, 8- and 16-bit elements at a buffer's tail ------------------- */

#define ROB2_B8 13u   /* bytes: 13 to 15 lie in the 4-byte rounding of the descriptor's range */
#define ROB2_H 5u     /* halves (10 bytes): half 5 lies in the rounding */
static const float rob2_quad[12] = { -1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1 };

static void
rob2_setup(struct rrun *r)
{
   r->vbuf[0] = rbuf(rob2_quad, sizeof rob2_quad);
   float vpal[8][4];
   for (unsigned i = 0; i < 8; i++)
      for (unsigned c = 0; c < 4; c++)
         vpal[i][c] = (float)(i * 4 + c) * 0.125f;
   r->vbuf[1] = rbuf(vpal, sizeof vpal);
   const uint32_t vidx[6] = { 5, 5, 5, 1000, 1000, 1000 };   /* the first triangle in range, the second past the end */
   r->vbuf[2] = rbuf(vidx, sizeof vidx);
   float pal[ROB_PAL][4];
   for (unsigned i = 0; i < ROB_PAL; i++)
      for (unsigned c = 0; c < 4; c++)
         pal[i][c] = (i + 1) * 0.0625f - c * 0.01f;
   r->fbuf[0] = rbuf(pal, sizeof pal);
   uint32_t idx[2 * RW * RH];
   for (unsigned i = 0; i < RW * RH; i++) {
      idx[i] = (i % 5) == 2 ? 0x10000000u * (1 + i % 15) + i % (ROB_PAL + 4) : (i % 9) == 4 ? ROB_PAL + i % 300 : i % ROB_PAL;
      idx[RW * RH + i] = i % 23;   /* 0 to 22: past both buffers' ends from 16 and 6 on */
   }
   r->fbuf[3] = rbuf(idx, sizeof idx);
   uint8_t b8[ROB2_B8];
   for (unsigned i = 0; i < ROB2_B8; i++)
      b8[i] = (uint8_t)(17 + 9 * i);
   r->fbuf[4] = rbuf(b8, sizeof b8);
   uint16_t h[ROB2_H];
   for (unsigned i = 0; i < ROB2_H; i++)
      h[i] = f2h(1.5f + (float)i * 0.25f);
   r->fbuf[5] = rbuf(h, sizeof h);
}

static bool
rob2_check(struct rrun *r)
{
   const float *vpal = r->vbuf[1].contents, *pal = r->fbuf[0].contents;
   const uint32_t *idx = r->fbuf[3].contents;
   const uint8_t *b8 = r->fbuf[4].contents;
   const uint16_t *h = r->fbuf[5].contents;
   unsigned checked = 0;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         /* the first triangle is where u < w (x + y < 0 in NDC); the diagonal is not compared */
         if (px + 2 > py && py + 2 > px)
            continue;
         const bool first = px < py;
         const unsigned i = py * RW + px;
         const int e = rob_entry(idx[i]);
         const unsigned k = idx[RW * RH + i];
         float want[4];
         for (unsigned c = 0; c < 4; c++)
            want[c] = first ? vpal[5 * 4 + c] : 0.0f;
         /* within the rounding the bytes past the buffer's length are read as they are: not compared */
         const bool b8_known = k < ROB2_B8 || k >= ((ROB2_B8 + 3) & ~3u), h_known = k < ROB2_H || k * 2 >= ((ROB2_H * 2 + 3) & ~3u);
         float got_b8 = rcol(r, 0, px, py)[0] - want[0], got_h = rcol(r, 0, px, py)[1] - want[1];
         want[0] += k < ROB2_B8 ? (float)b8[k] : b8_known ? 0.0f : got_b8;
         want[1] += k < ROB2_H ? h2f(h[k]) : h_known ? 0.0f : got_h;
         want[2] += e >= 0 ? pal[e * 4 + 1] : 0.0f;
         want[3] += e >= 0 ? pal[e * 4 + 3] : 0.0f;
         if (!rwant(r, 0, px, py, want, 4, 0))
            return false;
         checked++;
      }
   if (checked < 3000)
      return snprintf(r->why, sizeof r->why, "only %u pixels compared", checked), false;
   return true;
}

/* ---- r_bsearch: a binary search through pointers into a bounds-checked buffer ---------------------------------------- */

#define RBS_N 37u
static void
rbs_setup(struct rrun *r)
{
   setup_quad(r);
   float sorted[RBS_N];
   for (unsigned i = 0; i < RBS_N; i++)
      sorted[i] = (float)i * 1.5f + 0.25f;
   r->fbuf[0] = rbuf(sorted, sizeof sorted);
   const uint32_t n = RBS_N;
   r->fbuf[1] = rbuf(&n, sizeof n);
   float keys[RW * RH];
   for (unsigned i = 0; i < RW * RH; i++)
      keys[i] = (float)(rnd() % 1300) * 0.05f - 2.0f;   /* -2 to 63: past the last element (54.25) for some */
   r->fbuf[2] = rbuf(keys, sizeof keys);
}

static bool
rbs_check(struct rrun *r)
{
   const float *sorted = r->fbuf[0].contents, *keys = r->fbuf[2].contents;
   unsigned past = 0;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float key = keys[py * RW + px];
         unsigned first = 0;
         while (first < RBS_N && !(key < sorted[first]))
            first++;   /* the upper bound */
         const float v = first < RBS_N ? sorted[first] : 0.0f;
         past += first == RBS_N;
         const float want[4] = { v, v * 2.0f, 0, 1 };
         if (!rwant(r, 0, px, py, want, 4, 0))
            return false;
      }
   if (!past)
      return snprintf(r->why, sizeof r->why, "no key past the last element"), false;
   return true;
}

/* ---- r_pixfrag: pixel-coordinate samples in a fragment function --------------------------------------------------- */

#define RPF_N 12u
static float rpf_tex[RPF_N * RPF_N];

static void
rpf_setup(struct rrun *r)
{
   setup_quad(r);
   for (unsigned i = 0; i < RPF_N * RPF_N; i++)
      rpf_tex[i] = (float)((int)(rnd() % 801) - 400) * 0.01f;
   MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR32Float width:RPF_N height:RPF_N mipmapped:NO];
   td.usage = MTLTextureUsageShaderRead;
   td.storageMode = MTLStorageModePrivate;
   r->ftex[0] = [dev newTextureWithDescriptor:td];
   r->tstage[0] = rbuf(rpf_tex, sizeof rpf_tex);
   r->tw[0] = r->th[0] = RPF_N;
   r->tbpp[0] = 4;
}

/* the translator's linear emulation of a 2D pixel-coordinate sample (airrun_tests.h pxm_linear, for this texture) */
static float
rpf_linear(float cx, float cy, int ox, int oy, bool zero)
{
   const float c[2] = { cx, cy };
   const int off[2] = { ox, oy };
   int base[2];
   float w[2][2];
   for (unsigned i = 0; i < 2; i++) {
      const float f = fminf(fmaxf(c[i], -9.0f), (float)RPF_N + 9.0f), biased = f - 0.5f, fl = floorf(biased);
      base[i] = (int)fl + off[i];
      w[i][0] = 1.0f - (biased - fl);
      w[i][1] = biased - fl;
   }
   float acc = 0;
   for (unsigned tap = 0; tap < 4; tap++) {
      int x = base[0] + (int)(tap & 1), y = base[1] + (int)(tap >> 1);
      const bool in = x >= 0 && x < (int)RPF_N && y >= 0 && y < (int)RPF_N;
      x = x < 0 ? 0 : x >= (int)RPF_N ? (int)RPF_N - 1 : x;
      y = y < 0 ? 0 : y >= (int)RPF_N ? (int)RPF_N - 1 : y;
      const float v = zero && !in ? 0.0f : rpf_tex[y * RPF_N + x];
      const float weight = w[0][tap & 1] * w[1][tap >> 1];
      acc += weight == 0.0f ? 0.0f : v * weight;
   }
   return acc;
}

static bool
rpf_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float cx = (float)px * 0.25f - 1.5f, cy = (float)py * 0.25f - 1.5f;
         const float want[4] = { rpf_linear(cx, cy, 0, 0, false), rpf_linear(cx, cy, 0, 0, true), rpf_linear(cx, cy, 0, 0, false),
                                 rpf_linear(cx + 0.25f, cy + 0.25f, 1, -1, true) };
         const float *got = rcol(r, 0, px, py);
         static const float tol[4] = { 2e-6f, 2e-6f, 0.035f, 2e-6f };   /* the hardware filter's 8-bit weights for [2] */
         for (unsigned c = 0; c < 4; c++)
            if (!(fabsf(got[c] - want[c]) <= tol[c]))
               return snprintf(r->why, sizeof r->why, "pixel (%u,%u) component %u: %.7g, want %.7g (tolerance %g)", px, py, c,
                               got[c], want[c], tol[c]), false;
      }
   return true;
}

/* ---- r_fetch, r_fetch_mrt, r_fetch_noatt: framebuffer fetch -------------------------------------------------------- */

/* With NVMTL_NO_FBFETCH=1 (WindowServer's default) the plugin binds the zeroed dummy image for every [[color(n)]] read,
 * on either road: the references then read zeros. With NVMTL_FETCH_COPY=1 too, the reads see copies of the attachments
 * made before the draw (the SPIR-V road's emulation, which the road leaves to it): what the draw's earlier instances
 * wrote is not seen. */
static int
rfetch_mode(void)   /* 0 fetch, 1 zeros, 2 copies */
{
   const char *e = getenv("NVMTL_NO_FBFETCH"), *c = getenv("NVMTL_FETCH_COPY");
   return !(e && e[0] == '1') ? 0 : c && c[0] == '1' ? 2 : 1;
}
static bool rfetch_off(void) { return rfetch_mode() == 1; }

static float
q8(float x)
{
   return (float)lrintf(fminf(fmaxf(x, 0.0f), 1.0f) * 255.0f) / 255.0f;
}

static bool
rff_check(struct rrun *r)
{
   const bool off = rfetch_off(), copies = rfetch_mode() == 2;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float src0[4] = { px_u(px), px_w(py), 0.25f, 1 }, src1[4] = { px_u(px), px_w(py), 0.75f, 1 };
         float want[4];
         for (unsigned c = 0; c < 4; c++) {
            const float c1 = (off ? 0.0f : clear_col[c]) * 0.5f + src0[c] * 0.5f;
            want[c] = (off ? 0.0f : copies ? clear_col[c] : c1) * 0.5f + src1[c] * 0.5f;
         }
         if (!rwant(r, 0, px, py, want, 4, 1e-6f))
            return false;
      }
   return true;
}

static bool
rfm_check(struct rrun *r)
{
   const bool off = rfetch_off(), copies = rfetch_mode() == 2;
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_w(py);
         const float src0[4] = { u, w, 0.25f, 1 }, src1[4] = { u, w, 0.75f, 1 };
         float d[4], c0[4];
         for (unsigned c = 0; c < 4; c++)
            d[c] = off ? 0.0f : q8(clear_col[c]);
         static const unsigned sw[4] = { 2, 1, 0, 3 };   /* .bgra */
         for (unsigned c = 0; c < 4; c++)
            c0[c] = q8(d[sw[c]] * 0.5f + src0[c] * 0.5f);
         for (unsigned c = 0; c < 4; c++)
            d[c] = off ? 0.0f : copies ? q8(clear_col[c]) : c0[c];
         const uint8_t *g = (const uint8_t *)r->back[0].contents + ((size_t)py * RW + px) * 4;
         for (unsigned c = 0; c < 4; c++) {
            const int want = (int)lrintf((d[sw[c]] * 0.5f + src1[c] * 0.5f) * 255.0f);
            if (abs((int)g[c] - want) > 2) {
               snprintf(r->why, sizeof r->why, "rgba8 pixel (%u,%u) component %u: %u, want %d (2 LSB)", px, py, c, g[c], want);
               return false;
            }
         }
         const float f = ((const float *)r->back[1].contents)[py * RW + px];
         const float f0 = (off ? 0.0f : clear_col[0]) * 2 + u, fw = (off ? 0.0f : copies ? clear_col[0] : f0) * 2 + u;
         if (!(fabsf(f - fw) <= 1e-5f * fmaxf(fw, 1.0f))) {
            snprintf(r->why, sizeof r->why, "r32float pixel (%u,%u): %.7g, want %.7g (1e-5 relative)", px, py, f, fw);
            return false;
         }
         const uint32_t k = ((const uint32_t *)r->back[2].contents)[py * RW + px], kw = off ? 1u : (px + 7u) * 3u + 1u;
         if (k != kw && !copies) {   /* the copies' first value is the integer clear, which the reference does not model */
            snprintf(r->why, sizeof r->why, "r32uint pixel (%u,%u): %u, want %u", px, py, k, kw);
            return false;
         }
      }
   return true;
}

static bool
rfn_check(struct rrun *r)
{
   const bool off = rfetch_off();
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float src[4] = { px_u(px), px_w(py), 0.5f, 1 };
         float want[4];
         for (unsigned c = 0; c < 4; c++)
            want[c] = (off ? 0.0f : clear_col[c]) * 0.5f + src[c] * 0.5f;
         if (!rwant(r, 0, px, py, want, 4, 1e-6f))
            return false;
      }
   return true;
}

/* ---- r_linksamp: a linked visible function's constexpr sampler beside the fragment's own ---------------------------- */

static float
rls_texel(const struct rrun *r, int x, int y, unsigned c, bool repeat)
{
   if (repeat) {
      x = ((x % 16) + 16) % 16;
      y = ((y % 16) + 16) % 16;
   } else if (x < 0 || x > 15 || y < 0 || y > 15)
      return 0.0f;   /* clamp_to_zero: transparent black */
   return r->tex[(y * 16 + x) * 4 + c] / 255.0f;
}

static bool
rls_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         /* uv * 2 - 0.5 at the pixel's centre, in texels: never on a texel edge */
         const float tx = (px_u(px) * 2 - 0.5f) * 16, ty = (px_w(py) * 2 - 0.5f) * 16;
         const int x = (int)floorf(tx), y = (int)floorf(ty);
         float want[4];
         for (unsigned c = 0; c < 4; c++)
            want[c] = rls_texel(r, x, y, c, false) + 2 * rls_texel(r, x, y, c, true);
         if (!rwant(r, 0, px, py, want, 4, 1e-6f))
            return false;
      }
   return true;
}

/* ---- r_handlechoice: a texture call through a phi of argument handles chosen by a known value ------------------- */

static void
rhc_setup(struct rrun *r)
{
   setup_quad(r);
   for (unsigned k = 0; k < 3; k++) {
      for (unsigned i = 0; i < sizeof r->texn[k]; i++)
         r->texn[k][i] = (uint8_t)(rnd() >> 24);
      MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:16 mipmapped:NO];
      td.usage = MTLTextureUsageShaderRead;
      td.storageMode = MTLStorageModePrivate;
      r->ftex[k] = [dev newTextureWithDescriptor:td];
      r->tstage[k] = rbuf(r->texn[k], sizeof r->texn[k]);
      r->tw[k] = r->th[k] = 16;
      r->tbpp[k] = 4;
   }
   for (unsigned k = 0; k < 2; k++) {
      MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
      sd.minFilter = sd.magFilter = k ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
      sd.sAddressMode = sd.tAddressMode = MTLSamplerAddressModeClampToEdge;
      sd.normalizedCoordinates = YES;
      r->fsamp[k] = [dev newSamplerStateWithDescriptor:sd];
      [sd release];
   }
}

static float
rhc_texel(const struct rrun *r, unsigned k, int x, int y, unsigned c)
{
   x = x < 0 ? 0 : x > 15 ? 15 : x;
   y = y < 0 ? 0 : y > 15 ? 15 : y;
   return r->texn[k][(y * 16 + x) * 4 + c] / 255.0f;
}

static bool
rhc_check(struct rrun *r)
{
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_w(py);
         const float tx = u * 16 - 0.5f, ty = w * 16 - 0.5f;
         const int x0 = (int)floorf(tx), y0 = (int)floorf(ty);
         const float fx = tx - x0, fy = ty - y0;
         float want[4];
         for (unsigned c = 0; c < 4; c++) {
            /* pick(2): texture 2 through sampler 1 (linear); pick(0): texture 0 through sampler 0 (nearest) */
            const float l = (1 - fy) * ((1 - fx) * rhc_texel(r, 2, x0, y0, c) + fx * rhc_texel(r, 2, x0 + 1, y0, c)) +
                            fy * ((1 - fx) * rhc_texel(r, 2, x0, y0 + 1, c) + fx * rhc_texel(r, 2, x0 + 1, y0 + 1, c));
            want[c] = l + 4 * rhc_texel(r, 0, (int)(u * 16), (int)(w * 16), c);
         }
         if (!rwant(r, 0, px, py, want, 4, 1.0f / 128))
            return false;
      }
   return true;
}

/* ---- r_argbuf: argument buffers in the vertex and the fragment function ------------------------------------------ */

/* metal-render/r_argbuf.metal, as Metal lays the argument buffers out in memory (tier 2). Textures (16x16 rgba8, all
 * reached only through argument buffers): 0 and 1 vts, 2 the vertex Inner's, 3 and 4 the VItems', 5 tex, 6 to 8 texs,
 * 9 the fragment Inner's, 10 and 11 the Items'. Samplers: 0 vs (nearest clamp), 1 smp (nearest clamp), 2 smps[0]
 * (nearest repeat), 3 smps[1] (nearest clamp), 4 the fragment Inner's (nearest mirrored repeat). Vertex buffers: 0 VArgs,
 * 1 the two VItems, 2 sel, then reached through addresses: 3 the positions, 4 the vertex Inner, 5 its data, 6 the
 * VItems' vectors. Fragment buffers: 0 FArgs, 1 the two Items, 2 sel, then through addresses: 3 the fragment Inner,
 * 4 its data, 5 the palette, 6 the Items' vectors. */
struct rab_vargs { uint64_t pos; float scale; uint32_t pad; uint64_t vts[2], vs, inner; };
struct rab_vinner { uint64_t t, data; };
struct rab_vitem { uint64_t v, t; };
struct rab_finner { uint64_t t, s, data; };
struct rab_fargs { uint64_t tex, smp, texs[3], smps[2], inner, pal; };
struct rab_fitem { uint64_t t, v; };
static const uint32_t rab_vsel[2] = { 1, 1 }, rab_fsel[3] = { 2, 0, 1 };

static uint64_t rab_tex(struct rrun *r, unsigned i) { r->ftex_ab |= 1u << i; return r->ftex[i].gpuResourceID._impl; }
static uint64_t rab_samp(struct rrun *r, unsigned i) { r->fsamp_ab |= 1u << i; return r->fsamp[i].gpuResourceID._impl; }
static uint64_t rab_vaddr(struct rrun *r, unsigned i, uint64_t off) { r->vbuf_ab |= 1u << i; return r->vbuf[i].gpuAddress + off; }
static uint64_t rab_faddr(struct rrun *r, unsigned i, uint64_t off) { r->fbuf_ab |= 1u << i; return r->fbuf[i].gpuAddress + off; }

static float *
rab_floats(unsigned n)
{
   static float v[4][64];
   static unsigned next;
   float *f = v[next++ & 3];
   for (unsigned i = 0; i < n; i++)
      f[i] = rndf(-2, 2);
   return f;
}

static void
rab_setup(struct rrun *r)
{
   for (unsigned k = 0; k < 12; k++) {
      for (unsigned i = 0; i < sizeof r->abt[k]; i++)
         r->abt[k][i] = (uint8_t)(rnd() >> 24);
      MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:16 mipmapped:NO];
      td.usage = MTLTextureUsageShaderRead;
      td.storageMode = MTLStorageModePrivate;
      r->ftex[k] = [dev newTextureWithDescriptor:td];
      r->tstage[k] = rbuf(r->abt[k], sizeof r->abt[k]);
      r->tw[k] = r->th[k] = 16;
      r->tbpp[k] = 4;
   }
   static const MTLSamplerAddressMode modes[5] = { MTLSamplerAddressModeClampToEdge, MTLSamplerAddressModeClampToEdge,
      MTLSamplerAddressModeRepeat, MTLSamplerAddressModeClampToEdge, MTLSamplerAddressModeMirrorRepeat };
   for (unsigned k = 0; k < 5; k++) {
      MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
      sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterNearest;
      sd.sAddressMode = sd.tAddressMode = modes[k];
      sd.normalizedCoordinates = YES;
      sd.supportArgumentBuffers = YES;
      r->fsamp[k] = [dev newSamplerStateWithDescriptor:sd];
      [sd release];
   }
   float pos[12];
   for (unsigned i = 0; i < 12; i++)
      pos[i] = quad[i] * 0.5f;
   r->vbuf[3] = rbuf(pos, sizeof pos);
   r->vbuf[5] = rbuf(rab_floats(8), 8 * 4);
   r->vbuf[6] = rbuf(rab_floats(32), 32 * 4);
   const struct rab_vinner vin = { rab_tex(r, 2), rab_vaddr(r, 5, 0) };
   r->vbuf[4] = rbuf(&vin, sizeof vin);
   const struct rab_vargs va = { rab_vaddr(r, 3, 0), 2.0f, 0, { rab_tex(r, 0), rab_tex(r, 1) }, rab_samp(r, 0), rab_vaddr(r, 4, 0) };
   r->vbuf[0] = rbuf(&va, sizeof va);
   const struct rab_vitem vit[2] = { { rab_vaddr(r, 6, 0), rab_tex(r, 3) }, { rab_vaddr(r, 6, 64), rab_tex(r, 4) } };
   r->vbuf[1] = rbuf(vit, sizeof vit);
   r->vbuf[2] = rbuf(rab_vsel, sizeof rab_vsel);
   r->fbuf[4] = rbuf(rab_floats(32), 32 * 4);
   r->fbuf[5] = rbuf(rab_floats(16), 16 * 4);
   r->fbuf[6] = rbuf(rab_floats(32), 32 * 4);
   const struct rab_finner fin = { rab_tex(r, 9), rab_samp(r, 4), rab_faddr(r, 4, 0) };
   r->fbuf[3] = rbuf(&fin, sizeof fin);
   const struct rab_fargs fa = { rab_tex(r, 5), rab_samp(r, 1), { rab_tex(r, 6), rab_tex(r, 7), rab_tex(r, 8) },
                                 { rab_samp(r, 2), rab_samp(r, 3) }, rab_faddr(r, 3, 0), rab_faddr(r, 5, 0) };
   r->fbuf[0] = rbuf(&fa, sizeof fa);
   const struct rab_fitem fit[2] = { { rab_tex(r, 10), rab_faddr(r, 6, 0) }, { rab_tex(r, 11), rab_faddr(r, 6, 64) } };
   r->fbuf[1] = rbuf(fit, sizeof fit);
   r->fbuf[2] = rbuf(rab_fsel, sizeof rab_fsel);
}

static float
rab_texel(const struct rrun *r, unsigned k, int x, int y, unsigned c)
{
   return r->abt[k][(y * 16 + x) * 4 + c] / 255.0f;
}

/* texture k sampled nearest at (u, v): 0 clamp to edge, 1 repeat, 2 mirrored repeat */
static float
rab_nearest(const struct rrun *r, unsigned k, float u, float v, unsigned mode, unsigned c)
{
   int xy[2] = { (int)floorf(u * 16), (int)floorf(v * 16) };
   for (unsigned a = 0; a < 2; a++) {
      int i = xy[a];
      if (mode == 0)
         i = i < 0 ? 0 : i > 15 ? 15 : i;
      else if (mode == 1)
         i = ((i % 16) + 16) % 16;
      else {
         i = ((i % 32) + 32) % 32;
         i = i >= 16 ? 31 - i : i;
      }
      xy[a] = i;
   }
   return rab_texel(r, k, xy[0], xy[1], c);
}

static bool
rab_check(struct rrun *r)
{
   const float *vdata = r->vbuf[5].contents, *vv = r->vbuf[6].contents;
   const float *fdata = r->fbuf[4].contents, *pal = r->fbuf[5].contents, *fv = r->fbuf[6].contents;
   float vc[4];
   for (unsigned c = 0; c < 4; c++)
      vc[c] = rab_texel(r, rab_vsel[0], 3, 5, c) + rab_nearest(r, 0, 0.3f, 0.7f, 0, c) + rab_texel(r, 2, 2, 1, c) +
              vdata[rab_vsel[1] * 4 + c] + vv[rab_vsel[1] * 16 + c] + rab_texel(r, 3 + rab_vsel[1], 6, 6, c);
   for (unsigned py = 0; py < RH; py++)
      for (unsigned px = 0; px < RW; px++) {
         const float u = px_u(px), w = px_w(py);
         const unsigned k = (px + py) & 1u, it = rab_fsel[2] ^ k;
         float want[4];
         for (unsigned c = 0; c < 4; c++)
            want[c] = rab_nearest(r, 5, u, w, 0, c) + rab_texel(r, 6 + rab_fsel[k], px / 4, py / 4, c) * 2 +
                      rab_nearest(r, 7, u * 1.5f, w * 1.5f, k ? 0 : 1, c) + rab_nearest(r, 9, u * 2 - 0.5f, w * 2 - 0.5f, 2, c) * 0.5f +
                      fdata[(px & 7u) * 4 + c] + pal[(py & 3u) * 4 + c] + rab_texel(r, 10 + it, px / 4, py / 4, c) +
                      fv[it * 16 + (px & 3u) * 4 + c] + vc[c];
         if (!rwant(r, 0, px, py, want, 4, 4e-5f))
            return false;
      }
   return true;
}

/* ---- the table ---------------------------------------------------------------------------------------------------- */

#define F32 MTLPixelFormatRGBA32Float
#define NOC MTLPixelFormatInvalid
static const struct rtest rtests[] = {
   { "r_stage_in", "r_stage_in", "rs_vs", "rs_fs", { F32, F32, F32, F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     rs_vdesc, rs_setup, rs_check, "1e-4" },
   { "r_instanced", "r_instanced", "ri_vs", "ri_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 3, 6, 8, 2, NULL,
     NULL, ri_setup, ri_check, "exact" },
   { "r_link", "r_link", "rl_vs", "rl_fs", { F32, F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, setup_quad, rl_check, "1e-4" },
   { "r_interp", "r_interp", "rp_vs", "rp_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 3, 1, 0, NULL,
     NULL, rp_setup, rp_check, "smooth and no_perspective 2e-3, flat exact" },
   { "r_texture", "r_texture", "rt_vs", "rt_fs", { F32, F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, rt_setup, rt_check, "nearest 1e-6, linear 1/128" },
   { "r_discard", "r_discard", "rd_vs", "rd_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, setup_quad, rd_check, "1e-6" },
   { "r_depth", "r_depth", "rz_vs", "rz_fs", { F32 }, MTLPixelFormatDepth32Float, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, setup_quad, rz_check, "1e-6" },
   { "r_mrt", "r_mrt", "rm_vs", "rm_fs", { MTLPixelFormatRGBA8Unorm, MTLPixelFormatR32Float }, NOC, MTLPrimitiveTypeTriangle, 0, 6,
     1, 0, NULL, NULL, setup_quad, rm_check, "rgba8 1 LSB, r32float 1e-4 relative" },
   { "r_front", "r_front", "rf_vs", "rf_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, rf_setup, rf_check, "exact" },
   { "r_points", "r_points", "rpt_vs", "rpt_fs", { F32 }, NOC, MTLPrimitiveTypePoint, 0, 4, 1, 0, NULL,
     NULL, rpt_setup, rpt_check, "1e-5" },
   { "r_fc", "r_fc", "rfc_vs", "rfc_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, &fc_render,
     NULL, setup_quad, rfc_check, "1e-6" },
   { "r_buffers", "r_buffers", "rb_vs", "rb_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, rb_setup, rb_check, "exact" },
   { "r_nospace", "r_nospace", "rb_vs", "rb_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, rb_setup, rb_check, "exact; r_buffers without air.address_space in its metadata" },
   { "r_clip", "r_clip", "rc_vs", "rc_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, setup_quad, rc_check, "1e-4" },
   { "r_primid", "r_primid", "rq_vs", "rq_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, setup_quad, rq_check, "exact; must take the SPIR-V road", true },
   { "r_argpixel", "r_argpixel", "rap_vs", "rap_fs", { F32, F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL,
     NULL, rap_setup, rt_check, "nearest 1e-6, linear 1/128" },
   { "r_fcout", "r_fcout", "rfo_vs", "rfo_fs", { F32, F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, &fc_fcout,
     NULL, setup_quad, rfo_check, "1e-4; attachment 0 keeps its clear colour", false, true },
   { "r_fcin", "r_fcin", "rfi_vs", "rfi_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, &fc_fcin,
     rfi_vdesc, rfi_setup, rfi_check, "1e-4", false, true },
   { "r_rleblit", "r_rleblit", "rbl_vs", "rbl_fs", { MTLPixelFormatBGRA8Unorm, MTLPixelFormatBGRA8Unorm }, NOC,
     MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0, NULL, NULL, rbl_setup, rbl_check,
     "bgra8 1 LSB; CoreAnimation's buffer-to-image blit, a fragment byte read at buffer(9)" },
   { "r_qcblit", "qc_blit", "buffer_image_blit_vert_lpf", "buffer_image_blit_frag_lpf", { MTLPixelFormatBGRA8Unorm,
     MTLPixelFormatBGRA8Unorm }, NOC, MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0, NULL, NULL, rbl_setup, rbl_check,
     "bgra8 1 LSB; r_rleblit with QuartzCore's own two functions (PLUGIN_RUN_EXTRA_AIR names a folder with qc_blit.metallib)" },
   { "r_rleblit8", "r_rleblit", "rbl_vs", "rbl_fs", { MTLPixelFormatR8Unorm }, NOC, MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0,
     NULL, NULL, rbl_setup, rbl8_check, "r8 1 LSB; r_rleblit into one R8 target, its color(1) without an attachment" },
   { "r_qcblit8", "qc_blit", "buffer_image_blit_vert_lpf", "buffer_image_blit_frag_lpf", { MTLPixelFormatR8Unorm }, NOC,
     MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0, NULL, NULL, rbl_setup, rbl8_check,
     "r8 1 LSB; r_qcblit into one R8 target, its color(1) without an attachment" },
   { "r_qcblit8m", "qc_blit", "buffer_image_blit_vert_lpf", "buffer_image_blit_frag_lpf", { MTLPixelFormatR8Unorm }, NOC,
     MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0, NULL, NULL, rbl_setup_managed, rbl8_check,
     "r8 1 LSB; r_qcblit8 with its buffers managed (didModifyRange), as CoreAnimation keeps them" },
   { "r_rleblit_nan", "r_rleblit", "rbl_vs", "rbl_fs", { MTLPixelFormatR8Unorm }, NOC, MTLPrimitiveTypeTriangleStrip, 0, 4, 3, 0,
     NULL, NULL, rbl_nan_setup, rbl_nan_check,
     "r8 1 LSB; a NaN index reads byte 0 (before 3.0.24 the road read 2 GiB below the buffer: Terminal's Xid 31)" },
   { "r_rleblit_helpers", "r_rleblit", "rbl_vs", "rbl_fs", { MTLPixelFormatR8Unorm }, NOC, MTLPrimitiveTypeTriangleStrip, 0, 4,
     2, 0, NULL, NULL, rbh_setup, rbh_check,
     "r8 1 LSB; helper lanes above a rect at the start of its buffer, rows 256 MB apart, read before it unless they return" },
   { "r_qcblit_helpers", "qc_blit", "buffer_image_blit_vert_lpf", "buffer_image_blit_frag_lpf", { MTLPixelFormatR8Unorm }, NOC,
     MTLPrimitiveTypeTriangleStrip, 0, 4, 2, 0, NULL, NULL, rbh_setup, rbh_check,
     "r8 1 LSB; r_rleblit_helpers with QuartzCore's own two functions (Terminal's GPU fault on 3.0.25)" },
   { "r_oob", "r_oob", "ro_vs", "ro_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rob_setup, rob_check,
     "exact; a read past the end gives 0, one past 4 GiB wraps its 32-bit offset (the translator's bounds-checked "
     "descriptor, on both roads)" },
   { "r_oob_nospace", "r_oob_nospace", "ro_vs", "ro_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rob_setup,
     rob_check, "exact; r_oob without air.address_space in its metadata" },
   { "r_pixfrag", "r_pixfrag", "rpf_vs", "rpf_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rpf_setup, rpf_check,
     "2e-6; the hardware filter's sample at level(0) 0.035 (8-bit weights); the implicit-level ones the translator's fetch "
     "emulation" },
   { "r_bsearch", "r_bsearch", "rbs_vs", "rbs_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rbs_setup, rbs_check,
     "exact; a phi and a select of pointers into a bounds-checked buffer, the bound past its end reading 0" },
   { "r_oob2", "r_oob2", "ro2_vs", "ro2_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rob2_setup, rob2_check,
     "exact; a vertex function's buffer past its end, 8- and 16-bit elements at a buffer's tail (the 4-byte rounding "
     "not compared), offsets past 4 GiB" },
   { "r_fetch", "r_fetch", "rff_vs", "rff_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 2, 0, NULL, NULL, setup_quad, rff_check,
     "1e-6; [[color(0)]] read, two instances (the second reads the first's); NVMTL_NO_FBFETCH=1: zeros read" },
   { "r_fetch_mrt", "r_fetch", "rff_vs", "rfm_fs", { MTLPixelFormatRGBA8Unorm, MTLPixelFormatR32Float, MTLPixelFormatR32Uint }, NOC,
     MTLPrimitiveTypeTriangle, 0, 6, 2, 0, NULL, NULL, setup_quad, rfm_check,
     "rgba8 2 LSB, r32float 1e-5, r32uint exact; color(0..2) read as half4, float, uint" },
   { "r_fetch_noatt", "r_fetch", "rff_vs", "rfn_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, setup_quad, rfn_check,
     "1e-6; color(1) read with no attachment 1: the dummy image's zeros" },
   { "r_handlechoice", "r_handlechoice", "rhc_vs", "rhc_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rhc_setup,
     rhc_check, "1/128; textures and samplers chosen through a switch on a known value (built with -O0)", false, false, NULL, NULL,
     "the translator does not take the -O0 module (a texture operand from a local object it does not forward)" },
   { "r_linksamp", "r_linksamp", "rls_vs", "rls_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rt_setup, rls_check,
     "exact; the fragment's constexpr sampler (nearest, clamp_to_zero) and a linked visible function's (nearest, repeat), "
     "both @__air_sampler_state in their modules", false, false, "r_linksamp_fx", "rls_fx" },
   { "r_argbuf", "r_argbuf", "rab_vs", "rab_fs", { F32 }, NOC, MTLPrimitiveTypeTriangle, 0, 6, 1, 0, NULL, NULL, rab_setup, rab_check,
     "4e-5 (sums of nearest samples and buffer values); argument buffers in both stages: fields, arrays of textures and "
     "samplers indexed at run time, nested ones, arrays of them", false, false, NULL, NULL },
};
#undef F32
#undef NOC
