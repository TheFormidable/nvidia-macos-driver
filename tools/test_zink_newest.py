#!/usr/bin/env python3
"""Since 3.0.11 a swap of the Zink frontend with the swap interval at 0 leaves its frame parked and returns, and a
sender thread shows whatever frame is parked when the window's layer has room (zink/nvk_newest.h; nvk_zink_present_park
and nvk_zink_present_send in zink/nvk_zink.c; the sender in zink/nvk_cgl.c). A mistake in the agreement between the two
threads is a game that hangs in a swap, a window that never shows the last frame an application drew, or two threads
asking one layer at once. None of that needs a card to be seen, so this test runs it without one:

  the agreement  tools/zink_newest_fixture.c drives nvk_newest.h, as it is: a frame parked while another is being sent
                 is sent by the same sender; a stop waits for the frame being sent and leaves a parked one parked; a
                 sender that gave up is reported to the next swap and replaced after it; 20000 frames at an uneven
                 pace against a slow layer end with the last one sent, never two senders, nothing out of order. Again
                 under the thread sanitizer where the compiler has it.
  controls       copies of the header with one rule broken each. Every one has to fail the fixture.
  the text       the swap parks only after a swap that presented by itself and only with the interval at 0; every
                 path that presents, resizes or destroys by itself stops the sender first; the sender calls nothing
                 of CGL's; the switch is read by name with the two it depends on.

It touches no GPU and loads no Apple framework.
usage: test_zink_newest.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZINK = ROOT / "zink"
FIXTURE = ROOT / "tools" / "zink_newest_fixture.c"
CC = os.environ.get("CC", "cc")
BASE = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-g", "-O1", "-pthread"]
fails = 0

CONTROLS = (
    ("a kick does not mark the sender before it starts it", "      n->sending = true;\n      ops->start(arg);", "      ops->start(arg);"),
    ("the sender leaves after one frame", "      if (failed) n->failed = failed;\n", "      if (failed) n->failed = failed;\n      break;\n"),
    ("a stop does not wait for the sender", "   while (n->sending) pthread_cond_wait(&n->idle, &n->lock);\n", ""),
    ("the sender does not look at a stop", "n->stop || n->failed ? 0", "n->failed ? 0"),
    ("a kick does not look at a sender that gave up", "const int going = !n->failed;", "const int going = 1;"),
    ("a stop does not forget what the sender gave up with", "   n->failed = 0;\n", ""),
)


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def build(out, include, flags=()):
    done = subprocess.run([CC, *BASE, *flags, "-I", str(include), str(FIXTURE), "-o", str(out)], capture_output=True, text=True)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:]


def run(binary, limit=120):
    try:
        done = subprocess.run([str(binary)], capture_output=True, text=True, timeout=limit)
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return "timeout", ""


def function(text, head):
    start = text.index(head)
    return text[start:text.index("\n}\n", start) + 3]


def main():
    if not shutil.which(CC):
        print(f"SKIP: no C compiler ({CC})")
        return 77
    header = (ZINK / "nvk_newest.h").read_text()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ok, out = build(tmp / "plain", ZINK)
        check("the header and its fixture compile without a warning", ok, out)
        if ok:
            status, out = run(tmp / "plain")
            check("the agreement: every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
        ok, out = build(tmp / "tsan", ZINK, ["-fsanitize=thread"])
        if not ok:
            print("note the agreement under -fsanitize=thread: not run, this compiler cannot")
        else:
            status, out = run(tmp / "tsan", 300)
            check("the agreement under -fsanitize=thread: every arm passes and nothing is reported",
                  status == 0 and "all arms passed" in out and "WARNING: ThreadSanitizer" not in out, out[-2500:])
        for what, old, new in CONTROLS:
            if header.count(old) != 1:
                check(f"control '{what}': the line it breaks is in the header once", False, repr(old))
                continue
            there = tmp / "control"
            shutil.rmtree(there, ignore_errors=True)
            there.mkdir()
            (there / "nvk_newest.h").write_text(header.replace(old, new))
            ok, out = build(there / "fixture", there)
            status, out = run(there / "fixture", 60) if ok else ("did not compile", out)
            check(f"control, the header with one rule broken ({what}): the fixture fails", ok and status != 0, f"status {status}\n{out[-1500:]}")

    # ---- the text --------------------------------------------------------------------------------------------------
    cgl = (ZINK / "nvk_cgl.c").read_text()
    frontend = (ZINK / "nvk_zink.c").read_text()
    present = function(cgl, "static int present_drawable(CGLContextObj context)\n{")
    spots = [present.find(piece) for piece in ("CGLGetParameter(context, kCGLCPSwapInterval, &interval)",
                                               "b->native_present && !interval && atomic_load(&b->newest_failures) < NEWEST_FAILURES",
                                               "nvk_zink_present_park(b->zink, opaque)", "nvk_newest_kick(&b->newest, &newest_ops, b)",
                                               "newest_stop(b);", "nvk_zink_present_drain(b->zink)", "nvk_native_acquire(",
                                               "nvk_zink_present_frame(")]
    check("a swap parks only with the interval at 0; otherwise it stops the sender before it waits, asks the layer or "
          "presents", -1 not in spots and spots == sorted(spots), str(spots))
    frame = function(frontend, "NVK_EXPORT int nvk_zink_present_frame(")
    check("the frontend parks only after a swap's frame was handed over one frame ahead, and not once the context was "
          "told it has no drawable or let its presentation state go",
          frontend.count("p_atomic_set(&ctx->parked.armed, 1);") == 1 and
          0 < frame.find("if (ctx) p_atomic_set(&ctx->parked.armed, 0);") < frame.find("nvk_zink_present_native(") and
          "if (result == 2 && !p_atomic_read(&ctx->screen->uncertain)) p_atomic_set(&ctx->parked.armed, 1);" in frame and
          "p_atomic_set(&ctx->parked.armed, 0);" in function(frontend, "NVK_EXPORT void nvk_zink_present_idle(") and
          "p_atomic_set(&ctx->parked.armed, 0);" in function(frontend, "NVK_EXPORT void nvk_zink_present_release(") and
          "!p_atomic_read(&ctx->parked.armed)" in function(frontend, "NVK_EXPORT int nvk_zink_present_park("))
    for head in ("EXPORT int nvk_cgl_resize(", "static void release_gpu_present(struct nvk_cgl_bridge *b)\n",
                 "EXPORT void nvk_cgl_destroy_gld(", "EXPORT int nvk_cgl_detach("):
        body = function(cgl, head)
        first = min((body.find(piece) for piece in ("nvk_zink_resize(", "nvk_native_remove(", "nvk_zink_present_release(",
                                                    "nvk_zink_destroy(") if piece in body), default=-1)
        check(f"{head.split('(')[0].split()[-1]} stops the sender before it resizes, releases or destroys",
              0 < body.find("newest_stop(b);") < first, f"stop at {body.find('newest_stop(b);')}, first use at {first}")
    sender = function(cgl, "static int newest_send(void *arg, unsigned seen)")
    spots = [sender.find(piece) for piece in ("nvk_zink_present_drain(b->zink)", "nvk_zink_present_parked(b->zink, &width",
                                              "nvk_native_acquire(b->context", "nvk_zink_present_send(b->zink")]
    check("the sender waits for the frame in flight, then reads what is parked, then asks the layer, then hands over",
          -1 not in spots and spots == sorted(spots), str(spots))
    check("the sender and the layer's acquire call nothing of CGL's and nothing of GL's",
          not re.search(r"\b(CGL|gl)[A-Z]\w*\(", sender) and
          "CGLGetParameter" not in (ZINK / "native_drawable.m").read_text())
    park = function(frontend, "NVK_EXPORT int nvk_zink_present_park(")
    spots = [park.find(piece) for piece in ("nvk_present_newest_enabled()", "parked_lock(ctx)", "blit_default(ctx, image, 0, opaque)",
                                            "st_context_flush(ctx->st, 0, &fence", "pthread_mutex_unlock(&ctx->parked.lock);",
                                            "fence_finish(screen, ctx->st->pipe, before")]
    check("park: the switch, then the image's lock around the blit and its flush, then the wait for the frame before",
          -1 not in spots and spots == sorted(spots), str(spots))
    send = function(frontend, "NVK_EXPORT int nvk_zink_present_send(")
    spots = [send.find(piece) for piece in ("nvk_present_ahead_begin(", "parked_lock(ctx)", "ctx->parked.image = NULL;",
                                            "&ctx->parked.lock,\n", "surface, NULL, -1, done, arg);")]
    check("send: the queue, then the image's lock, the frame taken, and a handover that is given the lock and never waits",
          -1 not in spots and spots == sorted(spots) and "st_" not in send and "finish(" not in send, str(spots))
    present_c = (ZINK / "nvk_present.c").read_text()
    submit = function(present_c, "int nvk_present_ahead_submit(")
    check("the handover lets go of the image's lock on each of its ways out, and before anything is waited for",
          submit.count("if (image_lock) pthread_mutex_unlock(image_lock);") == 2 and
          0 < submit.find("if (image_lock) pthread_mutex_unlock(image_lock);") < submit.find("nvk_pq_submit(") and
          submit.find("refused:\n   if (image_lock) pthread_mutex_unlock(image_lock);") > 0)
    check("the switch is read once, by name, and needs the frame ahead and no observer",
          present_c.count('nvk_zink_cfg_on("NVK_ZINK_PRESENT_NEWEST")') == 1 and
          "return ahead_on && newest_on && !atomic_load(&present_observer);" in present_c)
    print("all passed" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
