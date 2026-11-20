/* test_dlss_views.c - DLSS's view cache (it grows, never leaks, and forgets an image whose backing went) and Frame
   Generation's priming decision (nvmtl_dlss_views.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_dlss_views.c -o /tmp/test_dlss_views && /tmp/test_dlss_views
   The cache is driven the way plugin/nvmtl_vk.c drives it (dlss_view: find, else create and add; image destroy, grave
   and re-back: take and destroy), against a model that counts the views made and destroyed. Each arm is a sentence
   about a failure the rule must produce or refuse. */
#include <stdio.h>
#include <stdint.h>
#include "nvmtl_dlss_views.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

/* A view is a counter value; made - destroyed is what a process would hold. */
static uintptr_t g_next = 1; static long g_made, g_destroyed;
static void *mk(void) { g_made++; return (void *)(g_next++ << 4); }
static void *view_of(nvmtl_dvc *c, const void *img, uint32_t fmt, uint32_t aspect, int rw)
{
    void *v = nvmtl_dvc_find(c, img, fmt, aspect, rw);
    if (v) return v;
    v = mk();
    nvmtl_dvc_ent e = { img, fmt, aspect, rw, v };
    if (nvmtl_dvc_add(c, &e)) { /* not cached: the old leak path */ }
    return v;
}
static void drop(nvmtl_dvc *c, const void *img)
{
    void *v[8]; uint32_t n;
    while ((n = nvmtl_dvc_take(c, img, v, 8))) g_destroyed += n;
}
static const void *IMG(uintptr_t k) { return (const void *)(0x100000u + (k << 8)); }

int main(void)
{
    nvmtl_dvc c = { 0 };
    /* 200 live scaler images (each with an input and an output view), 100 evaluations each */
    for (int ev = 0; ev < 100; ev++)
        for (uintptr_t k = 0; k < 200; k++) { view_of(&c, IMG(k), 97, 1, 0); view_of(&c, IMG(k), 97, 1, 1); }
    arm("200 live images x 100 evaluations make one view per (image, read/write): 400, not one per evaluation", g_made == 400);
    arm("the table grew past the 48 the fixed one had and holds every view", c.n == 400 && c.cap >= 400);
    void *a = view_of(&c, IMG(7), 97, 1, 0), *b = view_of(&c, IMG(7), 97, 1, 0);
    arm("the same key returns the same view every evaluation (the core keeps its handle)", a == b && g_made == 400);
    arm("input and output views of one image are distinct", view_of(&c, IMG(7), 97, 1, 1) != a);
    arm("another format or aspect of the same image is another view", view_of(&c, IMG(7), 98, 1, 0) != a && view_of(&c, IMG(7), 97, 2, 0) != a);
    drop(&c, IMG(7));
    arm("dropping an image destroys all of its views (4 here) and nothing else", g_destroyed == 4 && c.n == 398);
    arm("after the drop no lookup returns a view of that image", !nvmtl_dvc_find(&c, IMG(7), 97, 1, 0) && !nvmtl_dvc_find(&c, IMG(7), 97, 1, 1));
    long before = g_made;
    void *again = view_of(&c, IMG(7), 97, 1, 0);
    arm("a new image with the reused handle value gets a new view, not the dead one", g_made == before + 1 && again != a);
    arm("every other image still finds its own view after the hole was filled", nvmtl_dvc_find(&c, IMG(199), 97, 1, 1) && nvmtl_dvc_find(&c, IMG(0), 97, 1, 0));
    /* churn: images created and destroyed (or re-backed) forever stay bounded by the live set */
    const uint32_t cap0 = c.cap;
    for (uintptr_t k = 1000; k < 101000; k++) { view_of(&c, IMG(k), 97, 1, 0); view_of(&c, IMG(k), 97, 1, 1); drop(&c, IMG(k)); }
    arm("100000 images made and dropped leave the table at its live size", c.n == 399 && c.cap == cap0);
    arm("every view made for a dropped image was destroyed", g_made - g_destroyed == (long)c.n);
    for (uintptr_t k = 0; k < 200; k++) drop(&c, IMG(k));
    arm("dropping every image empties the table and destroys every view", c.n == 0 && g_made == g_destroyed);
    drop(&c, IMG(5)); drop(&c, NULL);
    arm("dropping an image with no views, or no image, does nothing", c.n == 0 && g_made == g_destroyed);
    arm("a NULL image or view is never cached", !nvmtl_dvc_find(&c, NULL, 0, 0, 0)
        && nvmtl_dvc_add(&c, &(nvmtl_dvc_ent){ NULL, 1, 1, 0, (void *)16 }) == -1 && nvmtl_dvc_add(&c, &(nvmtl_dvc_ent){ IMG(1), 1, 1, 0, NULL }) == -1);
    { void *v[2]; nvmtl_dvc d = { 0 };
      for (int k = 0; k < 5; k++) nvmtl_dvc_add(&d, &(nvmtl_dvc_ent){ IMG(3), (uint32_t)k, 1, 0, (void *)(uintptr_t)(16 * (k + 1)) });
      uint32_t t1 = nvmtl_dvc_take(&d, IMG(3), v, 2), t2 = nvmtl_dvc_take(&d, IMG(3), v, 2), t3 = nvmtl_dvc_take(&d, IMG(3), v, 2), t4 = nvmtl_dvc_take(&d, IMG(3), v, 2);
      arm("take hands back at most the buffer's size and the rest on the next calls", t1 == 2 && t2 == 2 && t3 == 1 && t4 == 0 && d.n == 0);
      free(d.e); }
    free(c.e);
    { nvmtl_dvc g = { 0 }; uint32_t caps[3] = { 0 };
      for (uint32_t k = 0; k < 200; k++) {
          nvmtl_dvc_add(&g, &(nvmtl_dvc_ent){ IMG(k), 97, 1, 0, (void *)16 });
          caps[0] = k == 0 ? g.cap : caps[0]; caps[1] = k == 48 ? g.cap : caps[1]; caps[2] = k == 96 ? g.cap : caps[2];
      }
      arm("the table starts at 48 and doubles (49 entries take 96, 97 take 192), not one realloc per view", caps[0] == 48 && caps[1] == 96 && caps[2] == 192);
      free(g.e); }

    /* Frame Generation priming */
    const nvmtl_dlss_src A = { IMG(1), 97, 1920, 1080, 1 }, B = { IMG(2), 97, 1920, 1080, 1 }, Z = { 0 };
    nvmtl_dlss_src A2 = A; A2.fmt = 98; nvmtl_dlss_src Ah = A; Ah.h = 540;
    arm("a reset never primes", !nvmtl_dlss_fg_prime(1, 1, 1, 0, B, A, A));
    arm("no previous colour texture never primes", !nvmtl_dlss_fg_prime(0, 0, 1, 0, Z, A, A));
    arm("no history yet always primes", nvmtl_dlss_fg_prime(0, 1, 0, 1, A, A, A) && nvmtl_dlss_fg_prime(0, 1, 0, 0, A, A, A));
    arm("the same object as the last colour is not primed (as before)", !nvmtl_dlss_fg_prime(0, 1, 1, 1, A, A, A));
    arm("the same object is not primed even after a re-back moved it (as before)", !nvmtl_dlss_fg_prime(0, 1, 1, 1, B, A, B));
    arm("another object for the same image the core was handed is not primed again", !nvmtl_dlss_fg_prime(0, 1, 1, 0, A, A, A));
    arm("a different image (a copy into a previous texture) is primed", nvmtl_dlss_fg_prime(0, 1, 1, 0, B, A, A));
    arm("the same image in another format is primed (the core gets another view)", nvmtl_dlss_fg_prime(0, 1, 1, 0, A2, A, A));
    arm("the same image at another size is primed", nvmtl_dlss_fg_prime(0, 1, 1, 0, Ah, A, A));
    { nvmtl_dlss_src As = A; As.samples = 4;
      arm("the same image with another sample count is primed", nvmtl_dlss_fg_prime(0, 1, 1, 0, As, A, A)); }
    arm("the last colour given a new backing since: a texture now carrying the old handle value is primed", nvmtl_dlss_fg_prime(0, 1, 1, 0, A, A, B));
    arm("the last colour gone (nil) primes", nvmtl_dlss_fg_prime(0, 1, 1, 0, A, A, Z));
    arm("nothing remembered (a failed encode) primes", nvmtl_dlss_fg_prime(0, 1, 1, 0, Z, Z, Z) && nvmtl_dlss_fg_prime(0, 1, 1, 0, A, Z, A));

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
