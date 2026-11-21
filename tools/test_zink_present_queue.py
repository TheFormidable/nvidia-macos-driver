#!/usr/bin/env python3
"""Since 3.0.7 a context of the Zink frontend presents one frame ahead: the swap submits the copy into the drawable's
surface and returns, a thread of the process waits for the GPU, marks the surface, tags it and presents the drawable,
and the import of a surface's video memory is kept across frames (zink/nvk_present_queue.c, the second half of
zink/nvk_present.c). A mistake there shows the wrong frame, frees memory the GPU is still writing, or hangs a game in
its swap, and none of it can be seen without a card. This test runs what can be run without one:

  the queue    tools/zink_present_queue_fixture.c drives nvk_present_queue.c, as it is, with real threads: who may be
               inside, who waits for whom and for how long, teardown, two threads presenting one context, a fork.
  the frame    tools/zink_present_ahead_fixture.c runs the second half of nvk_present.c, cut out of the file as it
               is, against a scripted GPU: the order (fence, mark, tag, drawable), kept imports and their eviction,
               a block that moved under a kept import, a GPU that does not finish, every refusal, both switches, an
               NVK and a kernel from before 3.0.7.
  both         again under ThreadSanitizer and AddressSanitizer where the compiler has them.
  controls     copies of the two sources with one rule broken each. Every one of them has to fail its fixture;
               a control that passes means the fixture no longer looks at that rule.
  the drawable the lines of the CGL adapter that decide who presents and who releases a drawable (the end of
               present_drawable and native_frame_done in zink/nvk_cgl.c, cut out as they are), for every answer the
               frontend can give: exactly one release, at most one present, none after an uncertain frame.
  the text     the calls the frontend and the CGL adapter make, in the order the design depends on, and that the
               path every way back leads to (nvk_zink_copy_vram, nvk_zink_present_native) is 3.0.6's text.

It touches no GPU and loads no Apple framework or Mesa.
usage: test_zink_present_queue.py"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZINK = ROOT / "zink"
TOOLS = ROOT / "tools"
CC = os.environ.get("CC", "cc")
BASE = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-g", "-pthread", "-DNVK_PQ_WAIT_MS=400"]
MARK = "/* ---- One frame ahead (3.0.7)"
fails = 0

# The CGL adapter's side of a frame, around the lines cut out of zink/nvk_cgl.c. The frontend is a script: what
# nvk_zink_present_frame answers, and whether it (the queue) called `done` before it returned.
DRAWABLE = r"""
#include <stdio.h>
typedef void *IOSurfaceRef; typedef void *CGLContextObj;
struct nvk_zink_context { int unused; };
struct nvk_cgl_bridge { struct nvk_zink_context *zink; };
static int published, released, failed, swapped, publish_answer = 1;
static int fail(const char *error) { (void)error; failed++; return 0; }
static int nvk_native_publish(void *frame) { (void)frame; published++; return publish_answer; }
static void nvk_native_release(void *frame) { (void)frame; released++; }
static int nvk_zink_swap(struct nvk_zink_context *z) { (void)z; swapped++; return 1; }
static int opaque_drawable(CGLContextObj c) { (void)c; return 0; }
static int script_result, script_done, done_answer;   /* script_done: -1 `done` is not called, else done(arg, it) */
static int nvk_zink_present_frame(struct nvk_zink_context *z, IOSurfaceRef s, int opaque, int (*done)(void *, int), void *arg)
{ (void)z; (void)s; (void)opaque; if (script_done >= 0) done_answer = done(arg, script_done); return script_result; }

@DONE@
static int tail(struct nvk_cgl_bridge *b, CGLContextObj context, IOSurfaceRef surface, void *frame)
{
@TAIL@}

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
static int go(int result, int done, int publish)
{
    static struct nvk_zink_context z; static struct nvk_cgl_bridge b = { &z }; static int frame;
    published = released = failed = swapped = 0; done_answer = -1;
    script_result = result; script_done = done; publish_answer = publish;
    return tail(&b, NULL, NULL, &frame);
}
int main(void)
{
    static int frame;
    ARM("handed over and still on its way when the swap returns: the swap succeeds and touches the drawable no more",
        go(2, -1, 1) == 1 && published == 0 && released == 0 && failed == 0 && swapped == 1);
    ARM("when its pixels are in the surface the queue presents it and releases it, once",
        native_frame_done(&frame, 1) == 1 && published == 1 && released == 1);
    ARM("handed over and already published when the swap returns: one present, one release, both the queue's",
        go(2, 1, 1) == 1 && published == 1 && released == 1 && done_answer == 1 && failed == 0);
    ARM("handed over and not published: released unpresented by the queue; the swap had no way to know",
        go(2, 0, 1) == 1 && published == 0 && released == 1 && done_answer == 0);
    ARM("waited for and not published (an observer, a front-buffer flush): the swap fails and releases nothing again",
        go(3, 0, 1) == 0 && published == 0 && released == 1 && failed == 1 && swapped == 0);
    ARM("uncertain: the swap fails and the drawable is neither presented nor released", go(-1, -1, 1) == 0 && published == 0 &&
        released == 0 && failed == 1);
    ARM("uncertain after the queue was done with the frame: not released a second time", go(-1, 0, 1) == 0 && released == 1 && published == 0);
    ARM("3.0.6's path put the pixels in the surface: the swap presents and releases, once each",
        go(1, -1, 1) == 1 && published == 1 && released == 1 && failed == 0 && swapped == 1);
    ARM("the layer refuses the drawable: the swap fails, the drawable is still released once", go(1, -1, 0) == 0 && released == 1 && failed == 1);
    ARM("3.0.6's path could not publish: no present, one release, the swap fails", go(0, -1, 1) == 0 && published == 0 && released == 1 && failed == 1);
    ARM("the queue's answer is the layer's: a present the layer refused is not a published frame",
        (publish_answer = 0, published = released = 0, native_frame_done(&frame, 1)) == 0 && published == 1 && released == 1);
    printf("%s\n", fails ? "FAILED" : "all arms passed");
    return fails ? 1 : 0;
}
"""


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def build(out, sources, flags=()):
    done = subprocess.run([CC, *BASE, *flags, *map(str, sources), "-o", str(out)], capture_output=True, text=True)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:]


def run(binary, args=(), limit=240):
    """(exit status, output); a run that does not end in `limit` seconds is status 'timeout'."""
    try:
        done = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=limit)
        return done.returncode, (done.stdout + done.stderr)
    except subprocess.TimeoutExpired as late:
        return "timeout", ((late.stdout or b"").decode(errors="replace") if isinstance(late.stdout, bytes) else (late.stdout or ""))


def mutate(text, old, new, what):
    if text.count(old) != 1:
        check(f"control '{what}': the line it breaks is in the source once", False, repr(old))
        return None
    return text.replace(old, new)


def sanitizer(tmp, name):
    """The flags for one sanitizer, or None when this compiler cannot build and run a threaded program with it."""
    probe = tmp / f"probe_{name}.c"
    probe.write_text("#include <pthread.h>\nstatic void *f(void *p) { return p; }\n"
                     "int main(void) { pthread_t t; pthread_create(&t, 0, f, 0); pthread_join(t, 0); return 0; }\n")
    flags = [f"-fsanitize={name}"]
    ok, _ = build(tmp / f"probe_{name}", [probe], flags)
    return flags if ok and run(tmp / f"probe_{name}")[0] == 0 else None


def main():
    if not shutil.which(CC):
        print(f"SKIP: no C compiler ({CC})")
        return 0
    queue_c = ZINK / "nvk_present_queue.c"
    present_c = (ZINK / "nvk_present.c").read_text()
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        sanitizers = {name: sanitizer(tmp, name) for name in ("thread", "address")}

        # ---- the queue -------------------------------------------------------------------------------------------
        fixture = TOOLS / "zink_present_queue_fixture.c"
        ok, out = build(tmp / "pq", [fixture, queue_c], ["-I", str(ZINK)])
        check("the queue and its fixture compile without a warning", ok, out)
        if ok:
            status, out = run(tmp / "pq")
            check("the queue: every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
        for name, flags in sanitizers.items():
            if not flags:
                print(f"note the queue under -fsanitize={name}: not run, this compiler cannot")
                continue
            ok, out = build(tmp / f"pq_{name}", [fixture, queue_c], ["-I", str(ZINK), "-DPQ_NO_FORK", *flags])
            status, out = run(tmp / f"pq_{name}") if ok else ("not built", out)
            check(f"the queue under -fsanitize={name}: every arm passes and nothing is reported",
                  status == 0 and "all arms passed" in out and "Sanitizer" not in out, out[-2500:])

        queue_text = queue_c.read_text()
        controls = [
            ("a claim does not wait for the frame in flight",
             "if (q->state == NVK_PQ_FREE) break;", "if (q->state == NVK_PQ_FREE || q->state == NVK_PQ_QUEUED) break;"),
            ("a teardown waits for its own thread",
             "(!wait_for_owner || pthread_equal(q->owner, pthread_self()))", "(!wait_for_owner)"),
            ("an uncertain frame is handed back as a finished one",
             "q->state = result == NVK_PQ_WORK_UNCERTAIN ? NVK_PQ_UNCERTAIN : NVK_PQ_DONE;", "q->state = NVK_PQ_DONE;"),
            ("the frame is answered before its work ran",
             "      pthread_mutex_unlock(&pq_lock);\n      int result = work(frame);\n      pthread_mutex_lock(&pq_lock);\n",
             "      q->result = 0; q->state = NVK_PQ_DONE; pthread_cond_broadcast(&pq_changed);\n"
             "      pthread_mutex_unlock(&pq_lock);\n      int result = work(frame);\n      pthread_mutex_lock(&pq_lock);\n"),
            ("the presentation thread takes the application's signals", "sigfillset(&all);", "sigemptyset(&all);"),
            ("a fault on the presentation thread is blocked",
             "   sigdelset(&all, SIGSEGV); sigdelset(&all, SIGBUS); sigdelset(&all, SIGILL);\n", ""),
            ("a fork's child believes it has the thread", "   pq_thread_state = -1;\n}", "}"),
            ("a frame is given up by how long its claimer waited, whatever the presentation thread did",
             "const long long since = q->state == NVK_PQ_QUEUED ? pq_progress_ms : began;",
             "const long long since = q->state == NVK_PQ_QUEUED && pq_progress_ms < 0 ? pq_progress_ms : began;"),
            ("a frame handed to an idle presentation thread is charged with the time that thread had nothing to do",
             "      if (!pq_head && !pq_working) pq_progress_ms = pq_now_ms();   /* an idle thread is handed its next frame */\n", ""),
            ("a finished frame does not count for the frames behind it",
             "      pq_working = 0;\n      pq_progress_ms = pq_now_ms();\n", "      pq_working = 0;\n"),
        ]
        for what, old, new in controls:
            broken = mutate(queue_text, old, new, what)
            if broken is None:
                continue
            copy = tmp / "broken_queue.c"
            copy.write_text(broken)
            ok, out = build(tmp / "pq_broken", [fixture, copy], ["-I", str(ZINK)])
            status, out = run(tmp / "pq_broken", limit=60) if ok else ("not built", out)
            check(f"control, the queue with one rule broken ({what}): the fixture fails",
                  ok and status != 0, f"status {status}\n{out[-1500:]}")

        # ---- the frame -------------------------------------------------------------------------------------------
        check("nvk_present.c has the half that presents one frame ahead, after the 3.0.6 path", MARK in present_c)
        if MARK not in present_c:
            print(f"{fails} failed")
            return 1
        lifted = present_c[present_c.index(MARK):]
        fixture = TOOLS / "zink_present_ahead_fixture.c"
        conf = tmp / "settings.conf"

        def build_frame(out, text, extra=()):
            where = tmp / f"lift_{out}"
            where.mkdir(exist_ok=True)
            (where / "ahead_lifted.inc").write_text(text)
            return build(tmp / out, [fixture, queue_c],
                         ["-I", str(where), "-I", str(ZINK), f'-DNVK_ZINK_CFG_PATH="{conf}"', *extra])

        sets = [("keep", ""), ("nokeep", "# a comment\nNVK_ZINK_SURFACE_KEEP=0\n"), ("oldnvk", ""), ("oldkernel", ""),
                ("off", "NVK_ZINK_SURFACE_KEEP=1\nNVK_ZINK_PRESENT_AHEAD=0\n"), ("cfg", ""), ("fork", "")]
        ok, out = build_frame("pa", lifted)
        check("the frame's half and its fixture compile without a warning", ok, out)
        if ok:
            for name, settings in sets:
                conf.write_text(settings)
                status, out = run(tmp / "pa", [name])
                check(f"the frame, set '{name}': every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
            # the settings file is what decides: the same sets against the wrong file must not pass
            conf.write_text("")
            check("control: without its line in the settings file the set 'nokeep' fails", run(tmp / "pa", ["nokeep"])[0] == 1)
            check("control: without its line in the settings file the set 'off' fails", run(tmp / "pa", ["off"])[0] == 1)
        for name, flags in sanitizers.items():
            if not flags:
                print(f"note the frame under -fsanitize={name}: not run, this compiler cannot")
                continue
            ok, out = build_frame(f"pa_{name}", lifted, ["-DPA_NO_FORK", *flags])
            if not ok:
                check(f"the frame builds under -fsanitize={name}", False, out)
                continue
            for set_name, settings in sets[:4]:
                conf.write_text(settings)
                status, out = run(tmp / f"pa_{name}", [set_name])
                check(f"the frame under -fsanitize={name}, set '{set_name}': every arm passes and nothing is reported",
                      status == 0 and "all arms passed" in out and "Sanitizer" not in out, out[-2500:])

        wait = re.search(r"VkResult result = screen->vk\.WaitForFences\(screen->dev, 1, &a->fence, VK_TRUE,\n[^\n]*\n", lifted)
        check("the presentation thread's wait for the copy's fence is where the controls expect it", wait is not None)
        controls = [
            ("the surface is marked without waiting for the GPU", wait.group(0) if wait else "?", "VkResult result = VK_SUCCESS;\n"),
            ("a copy that never ran is published",
             "if (observed && !(a->dropped && a->dropped(screen->dev, a->fence) == 1)) {", "if (observed) {"),
            ("a mark refused for a kept block is not sent again",
             "      if (status && a->import->cookie) {\n", "      if (0) {\n"),
            ("the one fence is used again without a reset",
             "       screen->vk.ResetFences(screen->dev, 1, &a->fence) != VK_SUCCESS ||\n", ""),
            ("every kept block is marked with the first one's name",
             "a->dirty_at(screen->dev, id, 0, a->import->cookie)", "a->dirty_at(screen->dev, id, 0, a->kept[0].cookie)"),
            ("a context that stopped keeping goes on using what it kept",
             "            for (unsigned i = 0; i < AHEAD_KEPT; ++i) ahead_import_free(a, &a->kept[i]);\n", ""),
            ("an import is let go one present late", "a->presents - a->kept[i].used >= AHEAD_AGE)", "a->presents - a->kept[i].used > AHEAD_AGE)"),
            ("an import is let go one present early", "a->presents - a->kept[i].used >= AHEAD_AGE)", "a->presents - a->kept[i].used >= AHEAD_AGE - 1)"),
            ("a kept block is marked without its name",
             "const uint32_t status = a->import->cookie ? a->dirty_at(screen->dev, id, 0, a->import->cookie)\n"
             "                                                : a->dirty(screen->dev, id, 0);",
             "const uint32_t status = a->dirty(screen->dev, id, 0);"),
            ("the block's name is read after the import's stage was cleared",
             "   if (result == VK_SUCCESS && a->keep) made.cookie = a->cookie();\n   a->stage(NULL);\n",
             "   a->stage(NULL);\n   if (result == VK_SUCCESS && a->keep) made.cookie = a->cookie();\n"),
            ("Zink is not told that the frame finished", "   if (a->zink_fence) {\n", "   if (0) {\n"),
            ("an import nobody uses is kept for good", "#define AHEAD_AGE 64 ", "#define AHEAD_AGE 1000000 "),
            ("a quarantined context is torn down",
             "   const int inside = ahead_enter(queue, uncertain, 1, NULL);\n   if (inside <= 0) return 0;\n",
             "   const int inside = ahead_enter(queue, uncertain, 1, NULL);\n   if (inside == 0) return 0;\n"),
            ("a front-buffer flush does not wait for its frame",
             "if (synchronous || atomic_load(&present_observer)) {", "if ((synchronous && 0) || atomic_load(&present_observer)) {"),
            ("a frame that was sent again is not presented",
             "         publish = again == 1;\n", "         publish = 0;\n"),
            ("a refused frame is sent again in a share group that is uncertain",
             "         if (p_atomic_read(a->uncertain)) return NVK_PQ_WORK_UNCERTAIN;\n", ""),
        ]
        conf.write_text("")
        for what, old, new in controls:
            broken = mutate(lifted, old, new, what)
            if broken is None:
                continue
            ok, out = build_frame("pa_broken", broken)
            status, out = run(tmp / "pa_broken", ["keep"], limit=60) if ok else ("not built", out)
            check(f"control, the frame with one rule broken ({what}): the fixture fails",
                  ok and status != 0, f"status {status}\n{out[-1500:]}")

    # ---- the text ------------------------------------------------------------------------------------------------
    old_path = present_c[present_c.index("struct copy_frame {"):present_c.index(MARK)].rstrip("\n")
    check("nvk_zink_copy_vram and what it uses are 3.0.6's text: the way back is the path that ran on a card",
          hashlib.sha256(old_path.encode()).hexdigest() == "b2b74e0234ca1a767555f80f3bd633e017a093cfd48cf8d821519253d84e9887",
          "zink/nvk_present.c changed between 'struct copy_frame {' and the 3.0.7 half; if that is meant, say so in "
          "the changelog and pin the new text here")
    frontend = (ZINK / "nvk_zink.c").read_text()

    def function(text, head):
        start = text.index(head)
        return text[start:text.index("\n}\n", start) + 3]

    native = function(frontend, "NVK_EXPORT int nvk_zink_present_native(")
    check("nvk_zink_present_native is 3.0.6's text",
          hashlib.sha256(native.encode()).hexdigest() == "1374a3aee4e9ea36eba4cb4c1e3b658088cdb0ef641aac3219299ad171d72d46")
    frame = function(frontend, "NVK_EXPORT int nvk_zink_present_frame(")
    order = [frame.find(piece) for piece in ("nvk_present_ahead_enabled()", "nvk_present_ahead_begin(", "blit_default(",
                                             "st_context_flush(ctx->st, 0, &fence", "nvk_present_ahead_submit(")]
    check("nvk_zink_present_frame: the switch, then the queue, then the blit, then the flush, then the handover",
          -1 not in order and order == sorted(order), str(order))
    handed = frame[:frame.find("nvk_present_ahead_submit(")]
    check("nothing on the way to the handover waits for the GPU (no finish(), no fence_finish)",
          "finish(" not in handed and "nvk_zink_copy_vram" not in handed)
    refused = frame[frame.find("if (result == -2)"):]
    check("a frame NVK did not take is finished the way 3.0.6 does: its fence let go, the GPU waited for, then the copy",
          0 < refused.find("fence_reference(screen, &fence, NULL)") < refused.find("finish(ctx) ? nvk_zink_copy_vram("))
    spots = [refused.find(piece) for piece in ("ctx->present_old_only = true;",
                                               "nvk_present_ahead_trim(&ctx->present_queue, &ctx->ahead, &ctx->screen->uncertain);",
                                               "finish(ctx) ? nvk_zink_copy_vram(")]
    check("a context that goes back to 3.0.6's path for good lets its kept imports go, before that frame is sent",
          -1 not in spots and spots == sorted(spots), str(spots))
    # the limit the programs above run with is 0.4 s; the one that ships has to hold a frame's two waits for the GPU
    limit = re.search(r"#ifndef NVK_PQ_WAIT_MS\n#define NVK_PQ_WAIT_MS (\d+)\n#endif", (ZINK / "nvk_present_queue.h").read_text())
    waits = [int(n) // 1000000 for n in re.findall(r"WaitForFences\([^;]*?(\d+)ull\);", present_c, re.S)]
    check("the limit that ships holds the two waits for the GPU a frame's work can make (5 s each) and 2 s more",
          limit is not None and waits == [5000, 5000] and int(limit.group(1)) >= sum(waits) + 2000,
          f"limit {limit.group(1) if limit else None}, waits {waits}")
    check("a context that is not current, or whose share group is quarantined, hands nothing over",
          frame.find("st_api_get_current() != ctx->st || p_atomic_read(&ctx->screen->uncertain)") in range(1, order[1]))
    release = function(frontend, "NVK_EXPORT void nvk_zink_present_release(")
    spots = [release.find(piece) for piece in ("nvk_present_ahead_destroy(", "finish(ctx)", "&ctx->native_image, NULL")]
    check("nvk_zink_present_release ends the frame in flight before it waits for Zink and drops the scratch image",
          -1 not in spots and spots == sorted(spots) and "if (!nvk_present_ahead_destroy(" in release, str(spots))

    cgl = (ZINK / "nvk_cgl.c").read_text()
    drawable = function(cgl, "static int present_drawable(CGLContextObj context)\n{")
    spots = [drawable.find(piece) for piece in ("nvk_zink_present_drain(b->zink)", "nvk_native_acquire(",
                                                "nvk_zink_present_idle(b->zink)", "nvk_zink_present_frame(",
                                                "if (copied < 0)", "if (copied == 3) return fail(",
                                                "if (copied == 2) return nvk_zink_swap(b->zink);",
                                                "nvk_native_release(frame);")]
    check("present_drawable: the frame in flight is waited for before the next drawable is asked for, and a frame "
          "that was handed over is not presented or released here", -1 not in spots and spots == sorted(spots), str(spots))
    check("present_drawable releases a frame itself only on the path where it kept it",
          drawable.count("nvk_native_release(frame)") == 1 and drawable.count("nvk_native_publish(frame)") == 1)
    done = function(cgl, "static int native_frame_done(void *frame, int publish)\n{")
    check("native_frame_done presents only a published frame and releases the drawable once, either way",
          "publish && nvk_native_publish(frame)" in done and done.count("nvk_native_release(frame)") == 1)

    # ---- the drawable: who presents it and who releases it, run for every answer of the frontend ------------------
    first, last = "      int copied = nvk_zink_present_frame(", "      return nvk_zink_swap(b->zink);\n"
    check("the end of present_drawable is where this test cuts it out",
          drawable.count(first) == 1 and drawable.count(last) == 1 and drawable.find(first) < drawable.find(last))
    if drawable.count(first) == 1 and drawable.count(last) == 1:
        tail = drawable[drawable.index(first):drawable.index(last) + len(last)]
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)

            def drawable_run(name, done_text, tail_text):
                source = tmp / f"{name}.c"
                source.write_text(DRAWABLE.replace("@DONE@", done_text).replace("@TAIL@", tail_text))
                ok, out = build(tmp / name, [source])
                return run(tmp / name, limit=60) if ok else ("not built", out)

            status, out = drawable_run("drawable", done, tail)
            check("the drawable, with the adapter's lines as they are: every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
            controls = [
                ("a frame that was handed over is presented and released by the swap too", "tail",
                 "      if (copied == 2) return nvk_zink_swap(b->zink);\n", ""),
                ("a frame that was waited for and not published is released twice", "tail",
                 '      if (copied == 3) return fail("Native NVK drawable handoff failed");\n', ""),
                ("the drawable of an uncertain frame is released", "tail",
                 '         return fail("Native NVK completion uncertain; drawable quarantined");',
                 '         { nvk_native_release(frame); return fail("Native NVK completion uncertain; drawable quarantined"); }'),
                ("the queue presents a frame whose pixels are not in the surface", "done",
                 "publish && nvk_native_publish(frame)", "(publish || 1) && nvk_native_publish(frame)"),
                ("the queue never releases the drawable", "done", "   nvk_native_release(frame);\n", ""),
            ]
            for what, where, old, new in controls:
                broken = mutate(tail if where == "tail" else done, old, new, what)
                if broken is None:
                    continue
                status, out = drawable_run("drawable_broken", broken if where == "done" else done, broken if where == "tail" else tail)
                check(f"control, the adapter with one rule broken ({what}): the arms fail", status == 1, f"status {status}\n{str(out)[-1500:]}")

    meson = (ZINK / "meson.build").read_text()
    prepare = (ROOT / "build" / "prepare_zink.py").read_text()
    check("the build compiles the queue and copies the new files into Mesa's tree",
          "'nvk_present_queue.c'" in meson and all(f"'{name}'" in prepare for name in
                                                   ("nvk_present_queue.c", "nvk_present_queue.h", "nvk_zink_cfg.h")))
    both = present_c[present_c.index(MARK):]
    check("each switch is read by its name, once per process",
          both.count('nvk_zink_cfg_on("NVK_ZINK_PRESENT_AHEAD")') == 1 and both.count('nvk_zink_cfg_on("NVK_ZINK_SURFACE_KEEP")') == 1
          and "pthread_once(&ahead_once, ahead_read_switches)" in both)

    print("all passed" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
