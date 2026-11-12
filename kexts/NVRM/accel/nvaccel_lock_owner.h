/* nvaccel_lock_owner.h - which thread holds the accelerator lock, so a long wait can let go of it safely.
   The family calls acceleratorDidLock / acceleratorWillUnlock around its own locking of the accelerator. A page-off that
   must wait for the GPU used to wait with that lock held, and every RM call that needs the lock queued behind it. It may
   let go only when the CURRENT thread is the recorded owner, holds exactly one level, and has not pinned the lock: a
   thread that is not the owner holds nothing to give up, a thread holding it twice would still hold it after one unlock,
   and a pinned lock was taken by the driver's own code, which reads resource state after the page-off returns and must
   not have the lock let go under it.
   Pure (the thread identity is a pointer the caller supplies), so tools/test_accel_lock_owner.c proves the rules and the
   order of events on the host. */
#ifndef NVACCEL_LOCK_OWNER_H
#define NVACCEL_LOCK_OWNER_H
#include <stdint.h>
/* pinned: how many times the owner pinned the lock (nested). Only the owner changes it, with the lock held; a new owner
   starts at 0, so a pin left behind by an owner that unlocked without unpinning never outlives its hold. */
typedef struct { void *volatile owner; volatile int depth; volatile int pinned; } nvaccel_lock_owner;

static inline void nvaccel_owner_did_lock(nvaccel_lock_owner *o, void *self)
{
    /* the caller has just taken the lock, so no other thread is between its own did_lock and will_unlock */
    if (__atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) == self) __atomic_add_fetch(&o->depth, 1, __ATOMIC_RELAXED);
    else {
        __atomic_store_n(&o->depth, 1, __ATOMIC_RELAXED);
        __atomic_store_n(&o->pinned, 0, __ATOMIC_RELAXED);
        __atomic_store_n(&o->owner, self, __ATOMIC_RELEASE);
    }
}
static inline void nvaccel_owner_will_unlock(nvaccel_lock_owner *o, void *self)
{
    /* only the owner's unlock changes anything: a stray call from another thread must not clear the owner */
    if (__atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) != self) return;
    if (__atomic_sub_fetch(&o->depth, 1, __ATOMIC_RELAXED) <= 0) { __atomic_store_n(&o->depth, 0, __ATOMIC_RELAXED); __atomic_store_n(&o->owner, (void *)0, __ATOMIC_RELEASE); }
}
/* 1 when `self` may let go of the lock for a long wait: it is the owner, holds exactly one level and nothing pinned it. */
static inline int nvaccel_owner_may_release(const nvaccel_lock_owner *o, void *self)
{
    return __atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) == self && __atomic_load_n(&o->depth, __ATOMIC_RELAXED) == 1
        && __atomic_load_n(&o->pinned, __ATOMIC_RELAXED) == 0;
}
/* The owner forbids (pin) and allows again (unpin) letting go of the lock. Calls nest. A thread that is not the owner
   changes nothing, and an unpin with nothing pinned changes nothing. */
static inline void nvaccel_owner_pin(nvaccel_lock_owner *o, void *self)
{
    if (__atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) == self) __atomic_add_fetch(&o->pinned, 1, __ATOMIC_RELAXED);
}
static inline void nvaccel_owner_unpin(nvaccel_lock_owner *o, void *self)
{
    if (__atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) == self && __atomic_load_n(&o->pinned, __ATOMIC_RELAXED) > 0)
        __atomic_sub_fetch(&o->pinned, 1, __ATOMIC_RELAXED);
}
/* 1 when `self` owns the lock and has pinned it: why a page-off waits with the lock held (for the counters only). */
static inline int nvaccel_owner_pinned_by(const nvaccel_lock_owner *o, void *self)
{
    return __atomic_load_n(&o->owner, __ATOMIC_ACQUIRE) == self && __atomic_load_n(&o->pinned, __ATOMIC_RELAXED) > 0;
}
#endif
