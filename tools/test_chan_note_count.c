/* test_chan_note_count.c - the counting behind the channel note (kexts/NVRM/nvrm_chan_note_count.h, the file the kext
   compiles): every request counts what it should and nothing else; a record that is not version 1 or names an unknown
   request is not answered and not touched; a note cannot add more than 64 dropped submissions; a kind or reason the
   kext does not know is recorded as -1; the answer carries the power state and the Xid counters; and four threads
   counting Xids at once lose none. The controls at the end run the same checks against three broken copies of the
   note function, each of which must be caught.
   Build and run from the repository root:
     cc -std=c11 -O1 -pthread -Wall -Wextra -Werror -I kexts/NVRM tools/test_chan_note_count.c -o /tmp/test_chan_note_count && /tmp/test_chan_note_count */
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include "nvrm_chan_note_count.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

typedef int (*note_fn)(struct nvrm_cn_counts *, struct NVRMChanNoteEsc *, uint32_t);

static struct NVRMChanNoteEsc
rec(uint32_t what, uint32_t xid, uint32_t dropped, uint32_t kind, uint32_t why)
{
   struct NVRMChanNoteEsc e;
   memset(&e, 0, sizeof(e));
   e.version = NVRM_CHAN_NOTE_VERSION;
   e.what = what;
   e.xid = xid;
   e.dropped = dropped;
   e.kind = kind;
   e.why = why;
   return e;
}

static int
counts_eq(const struct nvrm_cn_counts *a, const struct nvrm_cn_counts *b)
{
   return memcmp(a, b, sizeof(*a)) == 0;
}

/* Each check returns 1 when the function behaves; the same checks run on the real function and on the broken ones. */

static int
check_versions(note_fn f)
{
   static const uint32_t bad[] = { 0u, 2u, 0x100u, 0xFFFFFFFFu };
   for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
      struct nvrm_cn_counts c;
      memset(&c, 0, sizeof(c));
      const struct nvrm_cn_counts c0 = c;
      struct NVRMChanNoteEsc e = rec(NVRM_CN_RECOVERED, 13, 1, 1, 0);
      e.version = bad[i];
      const struct NVRMChanNoteEsc e0 = e;
      if (f(&c, &e, NVRM_CN_PM_RUNNING) != 0 || !counts_eq(&c, &c0) || memcmp(&e, &e0, sizeof(e)) != 0)
         return 0;
   }
   return 1;
}

static int
check_whats(note_fn f)
{
   static const uint32_t bad[] = { NVRM_CN_WHAT_COUNT, 6u, 0x80000000u, 0xFFFFFFFFu };
   for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
      struct nvrm_cn_counts c;
      memset(&c, 0, sizeof(c));
      const struct nvrm_cn_counts c0 = c;
      struct NVRMChanNoteEsc e = rec(bad[i], 31, 5, 1, 3);
      const struct NVRMChanNoteEsc e0 = e;
      if (f(&c, &e, NVRM_CN_PM_RUNNING) != 0 || !counts_eq(&c, &c0) || memcmp(&e, &e0, sizeof(e)) != 0)
         return 0;
   }
   return 1;
}

static int
check_dropped_cap(note_fn f)
{
   struct nvrm_cn_counts c;
   memset(&c, 0, sizeof(c));
   struct NVRMChanNoteEsc e = rec(NVRM_CN_RECOVERED, 0, 0xFFFFFFFFu, 2, 0);
   if (f(&c, &e, 0) != 1 || c.dropped != 64 || c.recovered != 1)
      return 0;
   e = rec(NVRM_CN_RECOVERED, 0, 65, 2, 0);
   if (f(&c, &e, 0) != 1 || c.dropped != 128)
      return 0;
   e = rec(NVRM_CN_RECOVERED, 0, 64, 2, 0);
   if (f(&c, &e, 0) != 1 || c.dropped != 192)
      return 0;
   e = rec(NVRM_CN_RECOVERED, 0, 3, 2, 0);
   if (f(&c, &e, 0) != 1 || c.dropped != 195 || c.recovered != 4)
      return 0;
   /* dropped is read only from a RECOVERED note */
   e = rec(NVRM_CN_GAVE_UP, 0, 9, 1, 3);
   if (f(&c, &e, 0) != 1 || c.dropped != 195)
      return 0;
   return 1;
}

static int
check_enums(note_fn f)
{
   struct nvrm_cn_counts c;
   memset(&c, 0, sizeof(c));
   struct NVRMChanNoteEsc e = rec(NVRM_CN_GAVE_UP, 0, 0, 7, 13);
   if (f(&c, &e, 0) != 1 || c.lastKind != 7 || c.lastWhy != 13 || c.gaveUp != 1)
      return 0;
   e = rec(NVRM_CN_GAVE_UP, 0, 0, 8, 14);
   if (f(&c, &e, 0) != 1 || c.lastKind != -1 || c.lastWhy != -1 || c.gaveUp != 2)
      return 0;
   e = rec(NVRM_CN_GAVE_UP, 0, 0, 0xFFFFFFFFu, 0x80000000u);
   if (f(&c, &e, 0) != 1 || c.lastKind != -1 || c.lastWhy != -1)
      return 0;
   e = rec(NVRM_CN_GAVE_UP, 0, 0, 0, 2);
   if (f(&c, &e, 0) != 1 || c.lastKind != 0 || c.lastWhy != 2)
      return 0;
   /* kind and reason are those of the last give-up: a RECOVERED note's kind does not replace them */
   e = rec(NVRM_CN_RECOVERED, 0, 1, 1, 0);
   if (f(&c, &e, 0) != 1 || c.lastKind != 0 || c.lastWhy != 2)
      return 0;
   return 1;
}

#define XID_EACH 200000

static void *
xid_worker(void *p)
{
   for (int i = 0; i < XID_EACH; i++)
      nvrm_cn_xid((struct nvrm_cn_counts *)p, (uint32_t)i);
   return NULL;
}

/* The broken copies: the real function with one defect put around it. */

static int
broken_any_version(struct nvrm_cn_counts *c, struct NVRMChanNoteEsc *e, uint32_t pm)
{
   e->version = NVRM_CHAN_NOTE_VERSION;
   return nvrm_cn_note(c, e, pm);
}

static int
broken_unknown_what(struct nvrm_cn_counts *c, struct NVRMChanNoteEsc *e, uint32_t pm)
{
   if (e->what >= NVRM_CN_WHAT_COUNT)
      e->what = NVRM_CN_KICK;
   return nvrm_cn_note(c, e, pm);
}

static int
broken_no_cap(struct nvrm_cn_counts *c, struct NVRMChanNoteEsc *e, uint32_t pm)
{
   const int r = nvrm_cn_note(c, e, pm);
   if (r && e->what == NVRM_CN_RECOVERED && e->dropped > NVRM_CN_DROPPED_MAX)
      c->dropped += (int64_t)(e->dropped - NVRM_CN_DROPPED_MAX);
   return r;
}

static int
broken_raw_kind(struct nvrm_cn_counts *c, struct NVRMChanNoteEsc *e, uint32_t pm)
{
   const int r = nvrm_cn_note(c, e, pm);
   if (r && e->what == NVRM_CN_GAVE_UP)
      c->lastKind = (int32_t)e->kind;
   return r;
}

int
main(void)
{
   /* ---- the Xid hook ---- */
   {
      struct nvrm_cn_counts c;
      memset(&c, 0, sizeof(c));
      nvrm_cn_xid(&c, 13);
      nvrm_cn_xid(&c, 31);
      T("xid: two Xids are counted and the last one is kept", c.xidCount == 2 && c.xidLast == 31);
      nvrm_cn_xid(&c, 0xFFFFFFFFu);
      T("xid: any 32-bit value is stored (as the int the sysctl shows)", c.xidCount == 3 && c.xidLast == -1);
      struct nvrm_cn_counts d = c;
      d.xidCount = 0; d.xidLast = 0;
      struct nvrm_cn_counts z;
      memset(&z, 0, sizeof(z));
      T("xid: the hook touches no channel counter", counts_eq(&d, &z));
   }

   /* ---- each request counts its own counter and nothing else ---- */
   {
      struct nvrm_cn_counts c, z;
      memset(&c, 0, sizeof(c));
      memset(&z, 0, sizeof(z));
      struct NVRMChanNoteEsc e = rec(NVRM_CN_QUERY, 0, 7, 3, 4);
      T("QUERY: answered", nvrm_cn_note(&c, &e, NVRM_CN_PM_SUSPENDED) == 1 && e.status == NVRM_CN_OK);
      T("QUERY: counts nothing (its dropped, kind and reason are ignored)", counts_eq(&c, &z));
      T("QUERY: answers the power state it was given", e.pm == NVRM_CN_PM_SUSPENDED);

      e = rec(NVRM_CN_KICK, 0, 0, 0, 0);
      T("KICK: answered", nvrm_cn_note(&c, &e, 0) == 1 && e.status == NVRM_CN_OK);
      z.kicks = 1;
      T("KICK: kicks + 1 and nothing else", counts_eq(&c, &z));

      e = rec(NVRM_CN_RECOVERED, 43, 2, 1, 0);
      T("RECOVERED: answered", nvrm_cn_note(&c, &e, 0) == 1);
      z.recovered = 1; z.dropped = 2; z.lastXid = 43;
      T("RECOVERED: recovered + 1, dropped + 2, last channel Xid 43, nothing else", counts_eq(&c, &z));

      e = rec(NVRM_CN_GAVE_UP, 79, 0, 5, 2);
      T("GAVE_UP: answered", nvrm_cn_note(&c, &e, NVRM_CN_PM_LOST) == 1 && e.pm == NVRM_CN_PM_LOST);
      z.gaveUp = 1; z.lastKind = 5; z.lastWhy = 2; z.lastXid = 79;
      T("GAVE_UP: gave_up + 1, kind 5, reason 2, last channel Xid 79, nothing else", counts_eq(&c, &z));

      e = rec(NVRM_CN_INJECTED, 0, 0, 0, 0);
      T("INJECTED: answered", nvrm_cn_note(&c, &e, 0) == 1);
      z.injected = 1;
      T("INJECTED: injected + 1; an Xid of 0 leaves the last channel Xid as it was", counts_eq(&c, &z));
   }

   /* ---- the answer ---- */
   {
      struct nvrm_cn_counts c;
      memset(&c, 0, sizeof(c));
      for (int i = 0; i < 5; i++)
         nvrm_cn_xid(&c, 109);
      struct NVRMChanNoteEsc e = rec(NVRM_CN_QUERY, 0, 0, 0, 0);
      e.reserved = 0x5A5A5A5Au;
      (void)nvrm_cn_note(&c, &e, NVRM_CN_PM_RUNNING);
      T("answer: xidCount and xidLast are the RM's Xid counters", e.xidCount == 5 && e.xidLast == 109);
      T("answer: the input fields and the reserved word are left as sent",
        e.version == NVRM_CHAN_NOTE_VERSION && e.what == NVRM_CN_QUERY && e.reserved == 0x5A5A5A5Au);
      T("answer: a channel note does not count as an Xid", c.xidCount == 5);
   }

   T("an unknown version is not answered and changes nothing", check_versions(nvrm_cn_note));
   T("an unknown request is not answered and changes nothing", check_whats(nvrm_cn_note));
   T("one note adds at most 64 dropped submissions, and only a RECOVERED note adds any", check_dropped_cap(nvrm_cn_note));
   T("kind and reason outside the known ranges are recorded as -1", check_enums(nvrm_cn_note));

   /* ---- threads ---- */
   {
      static struct nvrm_cn_counts c;
      enum { THREADS = 4 };
      pthread_t t[THREADS];
      int ok = 1;
      for (int i = 0; i < THREADS; i++)
         ok &= pthread_create(&t[i], NULL, xid_worker, &c) == 0;
      for (int i = 0; i < THREADS; i++)
         pthread_join(t[i], NULL);
      T("threads: four threads counting 200000 Xids each lose none", ok && c.xidCount == (int64_t)THREADS * XID_EACH);
   }

   /* ---- controls ---- */
   T("control: a copy that answers any version is caught", !check_versions(broken_any_version));
   T("control: a copy that counts an unknown request as a kick is caught", !check_whats(broken_unknown_what));
   T("control: a copy without the 64 cap is caught", !check_dropped_cap(broken_no_cap));
   T("control: a copy that stores any kind is caught", !check_enums(broken_raw_kind));

   printf(fails ? "FAILED: %d\n" : "PASS\n", fails);
   return fails ? 1 : 0;
}
