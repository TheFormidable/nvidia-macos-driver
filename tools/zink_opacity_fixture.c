/* zink_opacity_fixture.c: drives zink/nvk_opacity.h, as it is, with a clock and a reader of its own.
 * Built and run by tools/test_zink_opacity.py (also under AddressSanitizer and UndefinedBehaviorSanitizer where the
 * compiler has them, and against copies of the header with one rule broken, which must fail here):
 *   cc -std=gnu11 -Wall -Wextra -Werror -g -I zink tools/zink_opacity_fixture.c -o /tmp/op && /tmp/op
 * The clock is a number the arms set; the reader counts its asks and answers what the arm scripted. */
#include "nvk_opacity.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); fflush(stdout); if (!ok_) fails++; } while (0)
#define MS 1000000ull

/* What WindowServer would answer: the surface's opacity, and whether the ask succeeds. */
struct server { int asks, opacity, answers; };
static int ask(void *arg, int *opaque)
{
   struct server *s = arg;
   s->asks++;
   if (!s->answers) { *opaque = 0; return 0; }   /* what a failed ask leaves behind is not to be trusted */
   *opaque = s->opacity;
   return 1;
}

/* A bridge as calloc makes it. */
static struct nvk_opacity *fresh(void)
{
   struct nvk_opacity *o = calloc(1, sizeof(*o));
   if (!o) { printf("FAIL  no memory\n"); exit(2); }
   return o;
}

/* The answers of n swaps at now, now + step, ...; returns the asks they made. */
static int swaps(struct nvk_opacity *o, struct server *s, int keep, unsigned long long now, unsigned long long step,
                 int n, int *last)
{
   const int before = s->asks;
   for (int i = 0; i < n; ++i) *last = nvk_opacity_get(o, keep, now + (unsigned long long)i * step, ask, s);
   return s->asks - before;
}

int main(void)
{
   int answer = -1;
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 1 };
      ARM("a context's first swap asks, and is given what was answered",
          swaps(o, &s, 1, 5000 * MS, 0, 1, &answer) == 1 && answer == 0);
      ARM("the swaps within 100 ms of that ask do not ask, and keep its answer",
          swaps(o, &s, 1, 5000 * MS + 1, 10 * MS, 10, &answer) == 0 && answer == 0);
      ARM("one nanosecond before 100 ms is still within them", swaps(o, &s, 1, 5100 * MS - 1, 0, 1, &answer) == 0);
      s.opacity = 1;
      ARM("an application's change is seen by the first swap at 100 ms, which asks once",
          swaps(o, &s, 1, 5100 * MS, 0, 1, &answer) == 1 && answer == 1);
      ARM("and the 100 ms start again from that ask", swaps(o, &s, 1, 5100 * MS, 1 * MS, 99, &answer) == 0 && answer == 1 &&
          swaps(o, &s, 1, 5200 * MS, 0, 1, &answer) == 1);
      ARM("a swap every 10 ms for one second asks 10 times", swaps(o, &s, 1, 5210 * MS, 10 * MS, 100, &answer) == 10);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 1, 1 };
      swaps(o, &s, 1, 1000 * MS, 0, 1, &answer);
      s.opacity = 0;
      nvk_opacity_changed(o);
      ARM("a change of the output makes the next swap ask at once, 1 ms after the last ask",
          swaps(o, &s, 1, 1001 * MS, 0, 1, &answer) == 1 && answer == 0);
      ARM("and only that swap", swaps(o, &s, 1, 1002 * MS, 1 * MS, 50, &answer) == 0);
      nvk_opacity_changed(o);
      nvk_opacity_changed(o);
      ARM("two changes before one swap are one ask", swaps(o, &s, 1, 1060 * MS, 1 * MS, 5, &answer) == 1);
      nvk_opacity_changed(o);
      ARM("a change asks at once with the switch off too, and once",
          swaps(o, &s, 0, 1070 * MS, 0, 1, &answer) == 1);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 1 };
      ARM("with NVK_ZINK_OPACITY_KEEP=0 every swap asks, exactly once",
          swaps(o, &s, 0, 1000 * MS, 1 * MS, 200, &answer) == 200 && answer == 0);
      ARM("with the switch off, swaps at the same instant ask each", swaps(o, &s, 0, 1000 * MS, 0, 7, &answer) == 7);
      s.opacity = 1;
      ARM("and see an application's change at the next swap, as 3.0.7 did",
          swaps(o, &s, 0, 1000 * MS + 1, 0, 1, &answer) == 1 && answer == 1);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 0 };
      ARM("an ask that fails answers opaque", swaps(o, &s, 1, 1000 * MS, 0, 1, &answer) == 1 && answer == 1);
      ARM("and is not kept: the next swap asks again", swaps(o, &s, 1, 1000 * MS + 1, 1 * MS, 3, &answer) == 3 && answer == 1);
      s.answers = 1;
      ARM("once it is answered, that answer is the one kept",
          swaps(o, &s, 1, 1010 * MS, 0, 1, &answer) == 1 && answer == 0 && swaps(o, &s, 1, 1011 * MS, 1 * MS, 5, &answer) == 0 &&
          answer == 0);
      s.answers = 0;
      ARM("a failed ask after a kept transparent answer answers opaque, not the old answer",
          swaps(o, &s, 1, 1200 * MS, 0, 1, &answer) == 1 && answer == 1);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 7, 1 };
      ARM("any opacity but 0 is opaque", swaps(o, &s, 1, 1000 * MS, 0, 1, &answer) == 1 && answer == 1);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 1 };
      swaps(o, &s, 1, 5000 * MS, 0, 1, &answer);
      s.opacity = 1;
      ARM("a clock that went back asks at once, by any amount",
          swaps(o, &s, 1, 5000 * MS - 1, 0, 1, &answer) == 1 && answer == 1);
      ARM("and keeps the answer from there for 100 ms", swaps(o, &s, 1, 5000 * MS - 1, 1 * MS, 100, &answer) == 0 &&
          swaps(o, &s, 1, 5100 * MS - 1, 0, 1, &answer) == 1);
      swaps(o, &s, 1, 1 * MS, 0, 1, &answer);
      ARM("a clock that went back to near 0 asks at once and then keeps", s.asks == 4 &&
          swaps(o, &s, 1, 2 * MS, 1 * MS, 50, &answer) == 0);
      free(o);
   }
   {
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 1 };
      const unsigned long long top = ~0ull - 20 * MS;   /* 20 ms before the clock wraps */
      swaps(o, &s, 1, top, 0, 1, &answer);
      ARM("across the clock's wrap, 60 ms after the ask, nothing is asked",
          swaps(o, &s, 1, top + 60 * MS, 0, 1, &answer) == 0 && answer == 0);
      s.opacity = 1;
      ARM("across the clock's wrap, 100 ms after the ask, one ask",
          swaps(o, &s, 1, top + 100 * MS, 0, 1, &answer) == 1 && answer == 1);
      free(o);
   }
   {
      /* a clock that only goes back: every swap is before the last ask, and every swap asks */
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 0, 1 };
      int asks = 0;
      for (unsigned long long t = 9000 * MS; t > 8000 * MS; t -= 1 * MS) asks += swaps(o, &s, 1, t, 0, 1, &answer);
      ARM("a clock that keeps going back never keeps an answer", asks == 1000);
      free(o);
   }
   {
      /* the switch off still costs one ask per swap, never two */
      struct nvk_opacity *o = fresh();
      struct server s = { 0, 1, 1 };
      int most = 0;
      for (int i = 0; i < 64; ++i) {
         if (i % 3 == 0) nvk_opacity_changed(o);
         const int n = swaps(o, &s, i % 2, 1000 * MS + (unsigned long long)i * 37 * MS, 0, 1, &answer);
         if (n > most) most = n;
      }
      ARM("no swap asks more than once, whatever the switch, the time or the changes", most == 1);
      free(o);
   }
   printf("%s\n", fails ? "FAILED" : "all arms passed");
   return fails ? 1 : 0;
}
