/* test_accel_lock_owner.c - the accelerator-lock owner record, its pin and the unlocked page-off wait, proven on the host.
   Build and run anywhere:  cc -pthread -I kexts/NVRM/accel tools/test_accel_lock_owner.c -o /tmp/t && /tmp/t */
#include "nvaccel_lock_owner.h"
#include <pthread.h>
#include <stdio.h>
#include <string.h>
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

/* a model of the page-off: lock, wait without the lock when allowed, take it back */
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER; static nvaccel_lock_owner own;
static void lock(void *self) { pthread_mutex_lock(&lk); nvaccel_owner_did_lock(&own, self); }
static void unlock(void *self) { nvaccel_owner_will_unlock(&own, self); pthread_mutex_unlock(&lk); }
static int other_got_lock_during_wait;
static void *other(void *arg)
{
    (void)arg; lock((void *)2); other_got_lock_during_wait = 1; unlock((void *)2); return 0;
}
static void pageoff_wait(void *self, pthread_t *t)
{
    if (nvaccel_owner_may_release(&own, self)) {
        unlock(self);
        pthread_create(t, 0, other, 0); pthread_join(*t, 0);   /* the lock is free for the whole wait: the other thread gets it */
        lock(self);
    }
}
int main(void)
{
    void *A = (void *)1, *B = (void *)2; pthread_t t;
    ARM("a thread that never locked may not release", !nvaccel_owner_may_release(&own, A));
    lock(A);
    ARM("the owner holding one level may release", nvaccel_owner_may_release(&own, A));
    ARM("another thread may not release a lock it does not hold", !nvaccel_owner_may_release(&own, B));
    nvaccel_owner_will_unlock(&own, B);
    ARM("a stray unlock from another thread does not clear the owner", nvaccel_owner_may_release(&own, A));
    pageoff_wait(A, &t);
    ARM("while the owner waits unlocked, another thread can take the lock", other_got_lock_during_wait == 1);
    ARM("the owner holds the lock again after the wait", nvaccel_owner_may_release(&own, A));
    unlock(A);
    ARM("after the final unlock nobody is the owner", !nvaccel_owner_may_release(&own, A) && own.owner == 0 && own.depth == 0);
    /* a recursive take is two levels: releasing one would leave the thread still holding the lock */
    memset(&own, 0, sizeof own);
    nvaccel_owner_did_lock(&own, A); nvaccel_owner_did_lock(&own, A);
    ARM("a thread holding two levels may not release", !nvaccel_owner_may_release(&own, A));
    nvaccel_owner_will_unlock(&own, A);
    ARM("after one level is dropped it may", nvaccel_owner_may_release(&own, A));
    nvaccel_owner_will_unlock(&own, A);
    ARM("and the record is empty after the last", own.owner == 0);

    /* The pin: a lock the driver took itself (nvFamLock) is never let go inside a page-off reached from under it. */
    memset(&own, 0, sizeof own);
    nvaccel_owner_did_lock(&own, A);
    nvaccel_owner_pin(&own, A);
    ARM("a pinned owner at one level may not release", !nvaccel_owner_may_release(&own, A));
    ARM("the pin is reported for the owner", nvaccel_owner_pinned_by(&own, A) && !nvaccel_owner_pinned_by(&own, B));
    /* negative control: the rule before the pin (owner at one level) would have let go of this lock */
    ARM("control: owner-and-depth alone would allow it", own.owner == A && own.depth == 1);
    nvaccel_owner_pin(&own, B);
    ARM("a pin from a thread that is not the owner changes nothing", own.pinned == 1);
    nvaccel_owner_pin(&own, A);
    nvaccel_owner_unpin(&own, A);
    ARM("pins nest: one unpin of two leaves it pinned", !nvaccel_owner_may_release(&own, A));
    nvaccel_owner_unpin(&own, B);
    ARM("an unpin from a thread that is not the owner changes nothing", own.pinned == 1);
    nvaccel_owner_unpin(&own, A);
    ARM("the last unpin allows letting go again", nvaccel_owner_may_release(&own, A));
    nvaccel_owner_unpin(&own, A);
    ARM("an unpin with nothing pinned does not go below zero", own.pinned == 0 && nvaccel_owner_may_release(&own, A));
    /* the driver's own lock: pin after did_lock, unpin before will_unlock, as nvFamLock / nvFamUnlock do */
    nvaccel_owner_pin(&own, A);
    nvaccel_owner_will_unlock(&own, A);
    ARM("after the final unlock nobody owns it (a pin left behind is not anyone's)", own.owner == 0 && !nvaccel_owner_pinned_by(&own, A));
    nvaccel_owner_did_lock(&own, B);
    ARM("a new owner starts unpinned, even after a pin left behind", own.pinned == 0 && nvaccel_owner_may_release(&own, B));
    nvaccel_owner_will_unlock(&own, B);
    /* the unlocked page-off: it relocks through nvFamLock (which pins) and unpins at once, so the family's caller gets
       back an owner record exactly as it was before the wait: one level, unpinned */
    nvaccel_owner_did_lock(&own, A);
    ARM("the family's caller holds the lock at one level, unpinned", nvaccel_owner_may_release(&own, A));
    nvaccel_owner_will_unlock(&own, A);                              /* nvFamUnlock (its unpin had nothing to undo) */
    nvaccel_owner_did_lock(&own, B); nvaccel_owner_pin(&own, B);     /* another thread's own nvFamLock meanwhile */
    nvaccel_owner_unpin(&own, B); nvaccel_owner_will_unlock(&own, B);
    nvaccel_owner_did_lock(&own, A); nvaccel_owner_pin(&own, A);     /* the relock */
    nvaccel_owner_unpin(&own, A);
    ARM("after the relock and its unpin the record is the caller's again", own.owner == A && own.depth == 1 && own.pinned == 0);
    nvaccel_owner_will_unlock(&own, A);
    return fails ? 1 : 0;
}
