/* nvmtl_cmd_life.h - which command buffers a dead image has to outlive (the image grace).

   A Metal app may release a texture while a command buffer that names it is still being encoded or is in flight; with
   the image grace the plugin keeps the image's memory and views until the GPU can no longer read them. Two kinds of
   command buffer can read a dead image: those already submitted when it died (they finish in queue order), and those
   begun but not yet submitted (they may be submitted later, and then read the slot or address they recorded).

   2.9 kept three counts - begun, submitted, abandoned - and held a dead image until the queue had finished sequence
   submitted + (begun - submitted - abandoned). That bound was wrong both ways. Command buffers that ended without a
   sequence of their own and without being counted as abandoned (the pre-submit prologue and the counter-reset command
   buffer, which ride their owner's single submit; the submit paths that fail before the queue) raised it by one for
   ever, each, so held memory grew without bound. And a command buffer begun after the death can be submitted before
   one that was open at the death and take the sequence the bound expected the older one to take: the image was then
   released while the older one could still be submitted and read it.

   The rule kept here. Every command buffer counted as begun is ended exactly once: by its own submit (it takes the next
   queue sequence), by riding another command buffer's submit, or by being dropped without a submit. A command buffer
   that was never counted is never ended. The tag in the command buffer's own record says "counted, not yet ended", so
   a second end, or the end of a record that was never begun (zeroed, made after a device loss, or moved out), counts
   nothing. Each command buffer belongs to a cohort: the cohort current when it was begun. Every death closes the
   current cohort and opens a new one. A dead image waits until every command buffer of its cohort and of the cohorts
   before it has ended - so exactly the ones that were open at its death, plus at most some begun after it when the
   cohort ring is full - and then until the queue has finished the last sequence handed out at the moment that was
   seen. A command buffer open at the death therefore cannot be submitted after the horizon, and the horizon stops
   moving as soon as those command buffers have ended: it cannot drift.

   One bound on top. A command buffer that is never ended - a record lost on a path nobody foresaw - would hold every
   later dead image for ever, which is worse than the drift this replaces. So a dead image whose open command buffers
   have not all ended by the time the queue has completed NVMTL_CL_PATIENCE submits after its death is released as
   though they had (counted in `forced`): from then on it is exactly as safe as no grace at all, as in 2.9 by default,
   and the memory a lost record can hold is what dies during that many submits.

   A holder keeps its marks in death order and, each time it looks, settles every one of them it has not settled yet
   (nvmtl_cl_settle) before it releases the due ones from the front: see nvmtl_cl_settle for why.

   Pure over its own state: the caller serialises every call with one lock (the plugin's grace lock), and
   tools/test_cmd_life.c drives it through every kind of command-buffer life the plugin has. */
#ifndef NVMTL_CMD_LIFE_H
#define NVMTL_CMD_LIFE_H
#include <stdint.h>

#define NVMTL_CL_COHORTS 64u
#define NVMTL_CL_PATIENCE 1024u

/* embedded in the command buffer's record; all zero = not counted */
typedef struct nvmtl_cl_tag { uint64_t cohort; uint32_t open; uint32_t pad; } nvmtl_cl_tag;

typedef struct nvmtl_cl_state {
    uint64_t begun;        /* command buffers counted as begun */
    uint64_t submitted;    /* queue sequence: every submit takes the next one, counted command buffer or not */
    uint64_t own;          /* counted command buffers ended by their own submit */
    uint64_t rode;         /* counted command buffers ended by riding another's submit */
    uint64_t dropped;      /* counted command buffers ended without any submit */
    uint64_t uncounted;    /* submits of a record that was not counted as begun: none are expected */
    uint64_t strays;       /* ends whose cohort was no longer kept: impossible by construction, counted to be seen */
    uint64_t merged;       /* deaths that found the cohort ring full and shared the current cohort (only holds longer) */
    uint64_t forced;       /* dead things released NVMTL_CL_PATIENCE completed submits after their death with a command
                              buffer of their cohort still open */
    uint64_t front;        /* id of the oldest cohort kept; cohorts front .. front + n - 1 are kept, the last is current */
    uint32_t n;            /* 0 before the first begin or death */
    uint32_t open[NVMTL_CL_COHORTS];   /* command buffers begun and not ended, per kept cohort (index id % NVMTL_CL_COHORTS) */
} nvmtl_cl_state;

/* what a dead image remembers: its cohort, then (once resolved) the last sequence it has to see finished */
typedef struct nvmtl_cl_mark { uint64_t cohort, seq; uint32_t resolved, pad; } nvmtl_cl_mark;

static inline uint64_t nvmtl_cl_current(nvmtl_cl_state *s)
{
    if (!s->n) { s->n = 1; s->open[s->front % NVMTL_CL_COHORTS] = 0; }
    return s->front + s->n - 1;
}
/* cohorts older than the current one that have no open command buffer are forgotten */
static inline void nvmtl_cl_trim(nvmtl_cl_state *s)
{
    while (s->n > 1 && !s->open[s->front % NVMTL_CL_COHORTS]) { s->front++; s->n--; }
}
/* open command buffers counted now: begun less every kind of end */
static inline uint64_t nvmtl_cl_open(const nvmtl_cl_state *s) { return s->begun - s->own - s->rode - s->dropped; }

/* A command buffer was begun (and its record zeroed before): count it. Returns its begin ordinal (1, 2, ...). */
static inline uint64_t nvmtl_cl_begin(nvmtl_cl_state *s, nvmtl_cl_tag *t)
{
    const uint64_t c = nvmtl_cl_current(s);
    s->open[c % NVMTL_CL_COHORTS]++;
    t->cohort = c; t->open = 1; t->pad = 0;
    return ++s->begun;
}
/* the one place an end is counted: 1 when the tag was open (and is now closed), 0 when there was nothing to end */
static inline int nvmtl_cl_end_(nvmtl_cl_state *s, nvmtl_cl_tag *t)
{
    if (!t->open) return 0;
    t->open = 0;
    const uint32_t i = (uint32_t)(t->cohort % NVMTL_CL_COHORTS);
    if (t->cohort < s->front || t->cohort - s->front >= s->n || !s->open[i]) { s->strays++; return 1; }
    s->open[i]--;
    nvmtl_cl_trim(s);
    return 1;
}
/* The record went to the queue in its own submit: returns the sequence that submit takes. A record that was not
   counted (never begun here) still takes a sequence - the queue really ran a submit - and is counted apart. */
static inline uint64_t nvmtl_cl_submit(nvmtl_cl_state *s, nvmtl_cl_tag *t)
{
    if (nvmtl_cl_end_(s, t)) s->own++; else s->uncounted++;
    return ++s->submitted;
}
/* The record went to the queue inside another record's submit (same vkQueueSubmit, same sequence). */
static inline void nvmtl_cl_ride(nvmtl_cl_state *s, nvmtl_cl_tag *t) { if (nvmtl_cl_end_(s, t)) s->rode++; }
/* The record is dropped without being submitted (abandoned, failed before the queue, prologue with nothing in it). */
static inline void nvmtl_cl_drop(nvmtl_cl_state *s, nvmtl_cl_tag *t) { if (nvmtl_cl_end_(s, t)) s->dropped++; }

/* Something died now. Its mark waits for every command buffer open now; later begins go to a new cohort. */
static inline void nvmtl_cl_death(nvmtl_cl_state *s, nvmtl_cl_mark *m)
{
    const uint64_t c = nvmtl_cl_current(s);
    m->cohort = c; m->seq = s->submitted; m->resolved = 0; m->pad = 0;
    if (s->n < NVMTL_CL_COHORTS) { s->n++; s->open[(c + 1) % NVMTL_CL_COHORTS] = 0; nvmtl_cl_trim(s); }
    else s->merged++;   /* later begins join cohort c: this mark waits for them too, which is only longer */
}
/* The oldest cohort that still has an open command buffer; one past the current cohort when none has. */
static inline uint64_t nvmtl_cl_oldest_open(const nvmtl_cl_state *s)
{
    if (s->n && s->open[s->front % NVMTL_CL_COHORTS]) return s->front;
    return s->front + (s->n ? s->n : 1);   /* trim leaves an empty front only when it is the sole, current cohort */
}
/* Resolves the mark when it can be, and says whether it is resolved: the first time every command buffer of its cohort
   and older has ended, it starts waiting for the queue to finish the last sequence handed out at that moment, which
   covers every one of them that was submitted and every submit that was in flight at the death.
   The sequence is the one current when this is called, so a holder must settle EVERY mark it keeps each time it looks,
   in death order, stopping at the first that does not settle (marks settle in death order: cohorts and death sequences
   only grow). Settling only the oldest held mark, and the next one only once the oldest is released, gives each mark
   the sequence handed out when its predecessor went, one more round trip of the queue per held mark: with work in
   flight a steady rate of deaths is then never caught up with, and held memory grows without bound. */
static inline int nvmtl_cl_settle(nvmtl_cl_state *s, nvmtl_cl_mark *m, uint64_t finished)
{
    if (!m->resolved && nvmtl_cl_oldest_open(s) > m->cohort) {
        m->resolved = 1;
        if (s->submitted > m->seq) m->seq = s->submitted;
    }
    if (!m->resolved && finished >= m->seq + NVMTL_CL_PATIENCE) { m->resolved = 1; s->forced++; }   /* seq stays the death's */
    return (int)m->resolved;
}
/* May the thing marked m be released, with finished the highest sequence the queue has completed? */
static inline int nvmtl_cl_due(nvmtl_cl_state *s, nvmtl_cl_mark *m, uint64_t finished)
{
    return nvmtl_cl_settle(s, m, finished) && m->seq <= finished;
}
#endif
