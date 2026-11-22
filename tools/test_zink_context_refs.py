#!/usr/bin/env python3
"""CGL may destroy an OpenGL context on one thread after another thread switched CGL away from it. Nothing unbinds
Mesa on that other thread, so the Zink context stays in its Mesa current-context slot, and that thread's next bind
reads and flushes it. zink/nvk_zink.c therefore counts references: the owner's, one per thread on which the context is
Mesa-current, and one per caller that holds it across a borrowed bind. This test takes the reference code out of
nvk_zink.c as written (destroy_now through nvk_zink_destroy, and nvk_zink_get_current), compiles it for the host with
stand-ins for the few Mesa calls it makes, and runs the sequences on two threads. The stand-ins record a read or flush
of a freed context as a fault. It also reads zink/nvk_cgl.c: the per-thread bridge cache of bridge_current is checked
without reading the cached bridge, which another thread may have freed. It touches no GPU and loads no Apple framework
or Mesa.
usage: test_zink_context_refs.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZINK = ROOT / "zink"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


PRELUDE = r"""
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#define NVK_EXPORT
#define ST_ATTACHMENT_COUNT 4
#define p_atomic_read(p) __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define p_atomic_set(p, v) __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
#define p_atomic_inc(p) ((void)__atomic_add_fetch((p), 1, __ATOMIC_SEQ_CST))
#define p_atomic_dec(p) ((void)__atomic_sub_fetch((p), 1, __ATOMIC_SEQ_CST))
#define p_atomic_dec_zero(p) (__atomic_sub_fetch((p), 1, __ATOMIC_SEQ_CST) == 0)
struct pipe_resource;
struct pipe_frontend_drawable { int unused; };
struct nvk_zink_context;
struct st_context { struct nvk_zink_context *frontend_context; int dead; };
struct nvk_zink_screen { int refs; int uncertain; };
struct nvk_zink_context {
   struct pipe_frontend_drawable drawable;
   struct nvk_zink_screen *screen;
   int refs;
   struct st_context *st;
   struct pipe_resource *attachments[ST_ATTACHMENT_COUNT];
   struct pipe_resource *iosurface_staging;
   struct { pthread_mutex_t lock; unsigned long long drawn, sent; pid_t pid; } parked;   /* 3.0.11: what destroy_now reads of it */
   int freed;
};
/* zink/nvk_zink.h declares these in the real build */
int nvk_zink_make_current(struct nvk_zink_context *ctx);
struct nvk_zink_context *nvk_zink_get_current(void);
void nvk_zink_destroy(struct nvk_zink_context *ctx);
void nvk_zink_ref(struct nvk_zink_context *ctx);
void nvk_zink_unref(struct nvk_zink_context *ctx);
static int faults, frees;
static _Thread_local struct st_context *mesa_current;   /* Mesa's current-context slot of this thread */
static void fault(const char *what) { ++faults; fprintf(stderr, "fault: %s\n", what); }
/* GET_CURRENT_CONTEXT then ctx->st: a read of the current context */
static struct st_context *st_api_get_current(void)
{
   if (mesa_current && mesa_current->dead) fault("read of a freed current context");
   return mesa_current;
}
/* _mesa_make_current flushes the context it leaves */
static bool st_api_make_current(struct st_context *st, struct pipe_frontend_drawable *d,
                                struct pipe_frontend_drawable *r)
{
   (void)d; (void)r;
   if (mesa_current && mesa_current != st && mesa_current->dead) fault("flush of a freed context at a bind");
   if (st && st->dead) fault("bind of a freed context");
   mesa_current = st;
   return true;
}
static int nvk_iosurface_group_release(struct nvk_zink_context *ctx)
{
   if (ctx->freed) fault("release of freed");
   return 1;
}
static void nvk_zink_present_release(struct nvk_zink_context *ctx)
{
   if (ctx->freed) fault("present release of freed");
}
static void pipe_resource_reference(struct pipe_resource **p, struct pipe_resource *q) { (void)p; (void)q; }
static void st_api_destroy_drawable(struct pipe_frontend_drawable *d) { (void)d; }
static void st_destroy_context(struct st_context *st) { if (st->dead) fault("double destroy"); st->dead = 1; }
/* free() marks instead of freeing, so that a later use is seen */
static void mark_freed(struct nvk_zink_context *ctx) { if (ctx->freed) fault("double free"); ctx->freed = 1; ++frees; }
#define free(p) mark_freed(p)
"""

DRIVER = r"""
#undef free
static struct nvk_zink_screen screen;
static struct nvk_zink_context *make(void)
{
   struct nvk_zink_context *c = calloc(1, sizeof(*c));
   c->st = calloc(1, sizeof(*c->st));
   c->st->frontend_context = c;
   c->screen = &screen;
   c->refs = 1;   /* as nvk_zink_create_drawable_profile sets it */
   return c;
}
static int failed;
static void expect(const char *name, int ok) { printf("%s %s\n", ok ? "ok  " : "FAIL", name); failed |= !ok; }
static struct nvk_zink_context *victim;
static void *destroy_elsewhere(void *arg) { (void)arg; nvk_zink_destroy(victim); return NULL; }
static void on_other_thread(void *(*fn)(void *))
{
   pthread_t t; pthread_create(&t, NULL, fn, NULL); pthread_join(t, NULL);
}

int main(void)
{
   /* 1. T draws with A, CGL switches away (Mesa keeps A), U destroys A, T binds B. */
   struct nvk_zink_context *a = make(), *b = make();
   nvk_zink_make_current(a);
   victim = a; on_other_thread(destroy_elsewhere);
   expect("deferred_alive", !a->freed && a->refs == 1);
   int f0 = faults;
   nvk_zink_make_current(b);
   expect("deferred_freed_at_next_bind", a->freed && a->st->dead && faults == f0);
   expect("deferred_new_current", nvk_zink_get_current() == b && b->refs == 2);

   /* 2. The owner destroys the context current on its own thread: unbound and freed at once. */
   nvk_zink_destroy(b);
   expect("own_thread_freed_now", b->freed && mesa_current == NULL);

   /* 3. A saved current context held across a borrowed bind, destroyed elsewhere in between, restored live. */
   struct nvk_zink_context *c = make(), *d = make();
   nvk_zink_make_current(c);
   struct nvk_zink_context *saved = nvk_zink_get_current();
   nvk_zink_ref(saved);
   nvk_zink_make_current(d);
   victim = c; on_other_thread(destroy_elsewhere);
   expect("held_alive_while_borrowed", !c->freed);
   f0 = faults;
   nvk_zink_make_current(saved);
   nvk_zink_unref(saved);
   expect("held_restored_live", !c->freed && faults == f0 && nvk_zink_get_current() == c && c->refs == 1);
   nvk_zink_make_current(NULL);
   expect("held_freed_at_unbind", c->freed && faults == f0);
   nvk_zink_destroy(d);
   expect("borrowed_target_freed", d->freed);

   /* 4. A context never made current is freed by the owner's drop alone. */
   struct nvk_zink_context *e = make();
   nvk_zink_destroy(e);
   expect("unbound_freed_by_owner", e->freed);

   /* 5. Uncertain GPU work: the owner's drop leaves the context allocated (quarantine). */
   struct nvk_zink_context *g = make();
   screen.uncertain = 1;
   nvk_zink_destroy(g);
   expect("uncertain_kept", !g->freed);
   screen.uncertain = 0;

   /* control: the stand-ins see a bind that flushes a freed context */
   struct nvk_zink_context *h = make();
   mesa_current = h->st; h->st->dead = 1;
   f0 = faults;
   st_api_make_current(NULL, NULL, NULL);
   expect("control_fault_is_seen", faults == f0 + 1);

   expect("no_fault_outside_control", faults == f0 + 1);
   printf("%s\n", failed ? "SOME CASES FAILED" : "ALL CASES PASSED");
   return failed ? 1 : 0;
}
"""


def extract(src):
    start = src.find("static void destroy_now(struct nvk_zink_context *ctx)")
    m = re.search(r"NVK_EXPORT void nvk_zink_destroy\(struct nvk_zink_context \*ctx\)\n\{.*?\n\}\n", src, re.S)
    getter = re.search(r"NVK_EXPORT struct nvk_zink_context \*nvk_zink_get_current\(void\)\n\{.*?\n\}\n", src, re.S)
    if start < 0 or not m or not getter or m.end() <= start:
        return None
    return getter.group(0) + src[start:m.end()]


def main():
    src = (ZINK / "nvk_zink.c").read_text()
    body = extract(src)
    check("nvk_zink.c has destroy_now, nvk_zink_ref, nvk_zink_unref, nvk_zink_make_current and nvk_zink_destroy in "
          "one run, and nvk_zink_get_current", body is not None and all(
              f in (body or "") for f in ("nvk_zink_ref(", "nvk_zink_unref(", "nvk_zink_make_current(")))
    check("nvk_zink_create_drawable_profile gives a new context the owner's reference",
          re.search(r"ctx = calloc\(1, sizeof\(\*ctx\)\);\n\s*if \(!ctx\) return NULL;\n\s*ctx->refs = 1;", src)
          is not None)
    cgl = (ZINK / "nvk_cgl.c").read_text()
    ios = (ZINK / "nvk_iosurface.c").read_text()
    check("the borrowed binds of nvk_cgl.c hold the saved Mesa context until they restore it",
          cgl.count("nvk_zink_ref(previous_zink);") == 2 and cgl.count("nvk_zink_unref(previous_zink);") == 2)
    check("nvk_iosurface_group_release holds the saved context across its borrowed bind",
          "nvk_zink_ref(previous);" in ios and ios.count("nvk_zink_unref(previous);") == 3)
    fast = re.search(r"static struct nvk_cgl_bridge \*bridge_current\(void \*engine\)\n\{(.*?)pthread_mutex_lock", cgl,
                     re.S)
    check("bridge_current decides whether its cached bridge holds by the cached context and engine, before it reads "
          "the bridge", fast is not None and "thread_context != context" in fast.group(1)
          and "thread_engine != engine" in fast.group(1) and "thread_bridge->" not in fast.group(1),
          fast.group(1) if fast else "bridge_current not found")
    if body is None:
        print(f"{fails} failed")
        return 1
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        check("a C compiler is available (CC, cc, gcc or clang)", False, "none found")
        print(f"{fails} failed")
        return 1
    with tempfile.TemporaryDirectory(prefix="zinkrefs-") as t:
        c_file = Path(t) / "refs.c"
        c_file.write_text(PRELUDE + body + DRIVER)
        binary = Path(t) / "refs"
        cp = subprocess.run([cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-pthread", str(c_file), "-o",
                             str(binary)], capture_output=True, text=True)
        check("the reference code compiles for the host with -Wall -Wextra -Werror", cp.returncode == 0,
              cp.stderr.strip())
        if cp.returncode != 0:
            print(f"{fails} failed")
            return 1
        rp = subprocess.run([str(binary)], capture_output=True, text=True)
        print(rp.stdout, end="")
        if rp.returncode != 0 and rp.stderr.strip():   # the control's own fault is expected and not printed
            print(rp.stderr, end="")
        cases = {ln.split()[1] for ln in rp.stdout.splitlines()
                 if ln.startswith(("ok", "FAIL")) and len(ln.split()) > 1}
        expected = {"deferred_alive", "deferred_freed_at_next_bind", "deferred_new_current", "own_thread_freed_now",
                    "held_alive_while_borrowed", "held_restored_live", "held_freed_at_unbind",
                    "borrowed_target_freed", "unbound_freed_by_owner", "uncertain_kept", "control_fault_is_seen",
                    "no_fault_outside_control"}
        check("every sequence ran", expected.issubset(cases), sorted(expected - cases))
        bad = [ln for ln in rp.stdout.splitlines() if ln.startswith("FAIL")]
        check("no context is read, bound or flushed after it is freed, and none is freed early", not bad,
              "\n".join(bad))
        check("the C program reports all cases passed", "ALL CASES PASSED" in rp.stdout and rp.returncode == 0)
    print(f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
