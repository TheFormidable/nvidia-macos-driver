#!/usr/bin/env python3
"""Since 3.0.9 an IMMEDIATE swapchain of the native Vulkan frontend shows the newest frame: vkQueuePresentKHR hands
the frame to a thread of the swapchain and returns, that thread copies the newest frame into a drawable when the layer
has one, and a frame that was overtaken first goes back to the application once the GPU has finished it
(tools/nvk-bridge/present_newest.h, used by bridge.mm). A mistake there hands the application an image the GPU still
writes, shows frames out of order, or leaves a game inside vkAcquireNextImageKHR for ever, and none of it can be seen
without a card. This test runs what can be run without one:

  the swapchain  tools/nvk_bridge_newest_fixture.cpp drives present_newest.h, as it is, with real threads against a
                 scripted GPU and a scripted layer: an application nothing limits, one the GPU limits, presents that
                 must be synchronous between the others, a swapchain replaced with frames in flight, a hidden window,
                 a copy that fails, a thread that ends on an exception, a GPU that stops, a machine that sleeps through
                 a wait, and what vkAcquireNextImageKHR answers in each case.
  again          under ThreadSanitizer, and under AddressSanitizer with UndefinedBehaviorSanitizer, where the compiler
                 has them.
  controls       copies of the header with one rule broken each, and the fixture's application with the device's
                 submission lock taken before it waits for the presentation thread (in a present, and in an acquire).
                 Every one of them has to fail the fixture in the arm that looks at its rule; a control that passes, or
                 fails somewhere else, means the fixture no longer looks at that rule.
  the text       the places of bridge.mm that the fixture's model stands for: nobody waits for a presentation thread
                 while holding the device's submission lock, that thread holds the lock for its copy's submission
                 alone, its waits for the GPU are whole ones that a sleep does not cut short, the present observer
                 never runs on it, and only an IMMEDIATE swapchain with a copy queue of the frontend's own gets one.

It touches no GPU and loads no Apple framework or Mesa.
usage: test_nvk_bridge_newest.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "tools" / "nvk-bridge"
HEADER = NB / "present_newest.h"
FIXTURE = ROOT / "tools" / "nvk_bridge_newest_fixture.cpp"
CXX = os.environ.get("CXX", "c++")
BASE = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-g", "-O1", "-pthread"]
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def build(out, include, flags=()):
    done = subprocess.run([CXX, *BASE, *flags, "-I", str(include), "-I", str(ROOT / "zink"), str(FIXTURE), "-o", str(out)],
                          capture_output=True, text=True)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:]


def run(binary, args=(), limit=300):
    """(exit status, output); a run that does not end in `limit` seconds is status 'timeout'."""
    try:
        done = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=limit)
        return done.returncode, (done.stdout + done.stderr)
    except subprocess.TimeoutExpired as late:
        out = late.stdout or ""
        return "timeout", (out.decode(errors="replace") if isinstance(out, bytes) else out)


# What the fixture is built with again. Undefined behaviour ends the run: it is not only reported.
SANITIZERS = {"thread": ["-fsanitize=thread"],
              "address": ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"]}


def flags_text(flags):
    return " ".join(flags)


def sanitizer(tmp, name):
    """The flags for one sanitizer, or None when this compiler cannot build and run a threaded program with them."""
    probe = tmp / f"probe_{name}.cpp"
    probe.write_text("#include <thread>\nint main() { std::thread t([] {}); t.join(); return 0; }\n")
    flags = SANITIZERS[name]
    done = subprocess.run([CXX, *BASE, *flags, str(probe), "-o", str(tmp / f"probe_{name}")], capture_output=True, text=True)
    return flags if done.returncode == 0 and run(tmp / f"probe_{name}")[0] == 0 else None


# One rule of present_newest.h broken each: (what, the text as it is, the text that breaks it, what the fixture has to
# say: the breach or the arm that looks at this rule). A fixture that stands still is ended by its watchdog, which names
# the arm.
CONTROLS = [
    ("the image of an overtaken frame is handed out before the GPU has finished the frame",
     "            if (answer==Wait::Signalled) {\n", "            if (answer!=Wait::Failed) {\n",
     "an image was handed out before the GPU had finished its frame"),
    ("an image the presentation thread is copying is handed out",
     "            if (state_[k]==Free) { state_[k]=App;", "            if (state_[k]==Free || state_[k]==Copying) { state_[k]=App;",
     ("an image was handed out while a copy or the application used it", "a copy reads an image the application or another copy uses",
      "an image was handed out before the GPU had finished its frame")),
    ("the copy does not wait for its frame's rendering",
     "      const Wait rendered=wait_rendered(k);\n", "      const Wait rendered=Wait::Signalled;\n",
     "a copy started before its frame's rendering had finished"),
    ("retire() does not wait for the copy in flight",
     "      work_.notify_all();changed_.notify_all();\n      wait_idle(lock);\n", "      work_.notify_all();changed_.notify_all();\n",
     "after retire() returned"),
    ("a synchronous present leaves the frame that waits for a drawable with the presentation thread",
     "      drop_slot();\n      wait_idle(lock);\n   }\n   // The synchronous path's two questions.",
     "      wait_idle(lock);\n   }\n   // The synchronous path's two questions.",
     "FAIL  quiesce gives up the frame that waits for a drawable"),
    ("a synchronous present does not wait for the copy in flight",
     "      drop_slot();\n      wait_idle(lock);\n   }\n   // The synchronous path's two questions.",
     "      drop_slot();\n   }\n   // The synchronous path's two questions.",
     "reached the layer after a newer one"),
    ("a layer that gives no drawable does not hold the application",
     "            drop_slot();\n            backend_.hold();\n", "            drop_slot();\n",
     "FAIL  a hidden window: the thread gets no drawable within the layer's timeout"),
    ("a copy that fails does not make the next present synchronous",
     "      if (result==Copy::Failed) backend_.hold();\n", "",
     "FAIL  a copy that fails: the next present is synchronous"),
    ("vkAcquireNextImageKHR does not notice a GPU that stops",
     "            if (answer==Wait::Failed || whole) { lose();return Acquire::Lost; }\n",
     "            if (answer==Wait::Failed) { lose();return Acquire::Lost; }\n",
     "FAIL  a hidden window and a GPU that stops: acquire gives up after the budget"),
    ("the image of a copy whose completion is uncertain is free again, and the device is not called uncertain",
     "      if (result==Copy::Uncertain) {\n", "      if (false) {\n",
     "FAIL  a copy whose completion is uncertain"),
    ("the image of a copy whose completion is uncertain is no longer the swapchain's",
     "         lose();\n         return stop();\n      }\n      state_[k]=Free;\n",
     "         state_[k]=Free;lose();\n         return stop();\n      }\n      state_[k]=Free;\n",
     "images the copy kept 1, of them still the swapchain's 0"),
    ("an overtaken frame's image is forgotten",
     "      state_[slot_]=Retiring;retiring_.push_back(slot_);slot_=kNone;\n", "      state_[slot_]=Retiring;slot_=kNone;\n",
     "(watchdog): what acquire answers"),
    ("a presentation thread that ended on an exception still counts as inside its copy",
     "      if (busy_) {\n         busy_=false;\n", "      if (busy_) {\n",
     "(watchdog): copies that fail"),
    ("a retired swapchain still hands out images",
     "         if (retired_) return Acquire::Retired;\n", "",
     "FAIL  a retired swapchain hands out nothing"),
    ("a wait the machine slept through is believed",
     "      if (after==before) return Wait::Pending;\n", "      if (after>=before) return Wait::Pending;\n",
     "FAIL  a whole wait is made once; again only when it ran out and the machine slept through it"),
    ("a wait that ran out is made again whether the machine slept or not",
     "      if (after==before) return Wait::Pending;\n", "      if (after<before) return Wait::Pending;\n",
     "(watchdog): the clocks and the whole waits"),
    ("the presentation thread's wait for a frame is not a whole wait",
     "      const Wait rendered=wait_rendered(k);\n", "      const Wait rendered=backend_.wait_done(k,limits_.done_ns);\n",
     "FAIL  the machine sleeps through the presentation thread's wait for a frame"),
    ("vkAcquireNextImageKHR's wait of a whole budget is not a whole wait",
     "            const Wait answer=whole?wait_rendered(k):backend_.wait_done(k,left);\n",
     "            const Wait answer=backend_.wait_done(k,whole?limits_.done_ns:left);\n",
     "FAIL  the machine sleeps through vkAcquireNextImageKHR's wait for an overtaken frame"),
    ("the clocks being apart at the first look counts as a sleep",
     "      if (looked_ && apart>apart_ && apart-apart_>=kSlept) ++wakes_;\n", "      if (apart>apart_ && apart-apart_>=kSlept) ++wakes_;\n",
     "FAIL  two clocks that are apart from the start"),
    ("any drift between the clocks counts as a sleep",
     "      if (looked_ && apart>apart_ && apart-apart_>=kSlept) ++wakes_;\n", "      if (looked_ && apart>apart_) ++wakes_;\n",
     "FAIL  two clocks that are apart from the start"),
    ("a reading taken before a sleep and arriving after it takes the sleep back, so that it is counted again",
     "      if (!looked_ || apart>apart_) apart_=apart;\n", "      apart_=apart;\n",
     "FAIL  the clocks move apart by a second or more"),
]


def body(text, start):
    """The text of the function that begins with `start`, up to its closing brace in the first column."""
    at = text.find(start)
    return "" if at < 0 else text[at:text.find("\n}\n", at)]


def wiring_problems(bridge):
    p = []

    def before(function, first, second, what):
        text = body(bridge, function)
        if first not in text or second not in text or text.index(first) > text.index(second):
            p.append(what)

    before("VkResult VKAPI_CALL queue_present(", "else c->newest->quiesce();",
           "std::lock_guard<std::recursive_mutex> lock(d->submit);",
           "queue_present waits for a presentation thread (quiesce) after it took the submission lock")
    before("VkResult VKAPI_CALL queue_present(", "c->newest->accepts(info->pImageIndices[j])",
           "std::lock_guard<std::recursive_mutex> lock(d->submit);",
           "queue_present decides who presents after it took the submission lock")
    before("void VKAPI_CALL destroy_swapchain(", "c->newest->retire();",
           "std::lock_guard<std::recursive_mutex> submit(c->dev->submit);",
           "destroy_swapchain retires the swapchain after it took the submission lock")
    before("void VKAPI_CALL destroy_swapchain(", "c->newest->pending()", "release_images(c);",
           "destroy_swapchain frees the images before the frames that were never copied have finished rendering")
    before("void VKAPI_CALL destroy_device(", "c->newest->retire();", "std::lock_guard<std::recursive_mutex> lock(d->submit);",
           "destroy_device retires the swapchains after it took the submission lock")
    before("VkResult VKAPI_CALL create_swapchain(", "if (old && old->newest) old->newest->retire();",
           "std::lock_guard<std::recursive_mutex> submit(d->submit);",
           "create_swapchain retires the old swapchain after it took the submission lock")
    before("VkResult VKAPI_CALL create_swapchain(", "if (old && old->newest) old->newest->retire();", "s->layer.drawableSize=",
           "create_swapchain changes the layer before the old swapchain stopped presenting")
    before("VkResult VKAPI_CALL create_swapchain(", "chains[c->handle]=c;", "start_presentation(c)",
           "create_swapchain starts the presentation thread of a swapchain that is not in the registry yet (a later failure "
           "would leave a thread nobody can retire)")
    before("VkResult VKAPI_CALL acquire_image(", "c->newest->acquire(timeout,index)",
           "std::lock_guard<std::recursive_mutex> submit(d->submit);",
           "acquire_image waits for a presentation thread (its copy) after it took the submission lock")
    before("nvkbridge::Copy NewestBackend::copy(", "if (fence_dropped && fence_dropped(c->dev->raw,c->images[k].done)==1) {",
           "copy_drawable(c,k,drawable,VK_NULL_HANDLE,consumed,true)",
           "a presentation thread copies a frame whose done fence a channel rebuild dropped")
    # The waits whose running out calls the device uncertain, on a presentation thread or for its frames: the whole
    # budget in one call, made again when the machine slept through it.
    whole = body(bridge, "VkResult wait_fence_whole(")
    if "nvkbridge::wait_whole([&] {\n      r=DP(d,WaitForFences)(d->raw,1,&fence,VK_TRUE,nvkbridge::NewestLimits{}.done_ns);" not in whole or \
       "},machine_woke);" not in whole or whole.count("WaitForFences") != 1:
        p.append("wait_fence_whole is not one wait of the whole budget, made again by wait_whole when the machine slept")
    # The clock that goes on is read first: a thread held up between the readings then sees them too close, which
    # counts for nothing, and never too far apart, which would count as a sleep.
    if "   const uint64_t all=clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n   const uint64_t awake=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);\n" \
       "   return watch.look(awake,all);" not in body(bridge, "uint64_t machine_woke("):
        p.append("machine_woke does not compare the clock that stands still in a sleep with the one that goes on, read in "
                 "that order (the one that goes on first)")
    if "uint64_t woke() { return machine_woke(); }" not in bridge:
        p.append("the swapchain's own waits (present_newest.h) are not told when the machine slept")
    if "VkResult r=wait_fence_whole(d.get(),c->images[k].done);" not in body(bridge, "void VKAPI_CALL destroy_swapchain("):
        p.append("destroy_swapchain's wait for the frames that were never copied is not a whole wait")
    waits = body(bridge, "nvkbridge::Wait NewestBackend::wait_done(")
    if "VkResult r=ns?DP(d,WaitForFences)(d->raw,1,&done,VK_TRUE,ns):DP(d,GetFenceStatus)(d->raw,done);" not in waits or waits.count("WaitForFences") != 1:
        p.append("NewestBackend::wait_done is not one wait for the time it was given")
    copy = body(bridge, "VkResult copy_drawable(")
    if "std::unique_lock<std::recursive_mutex> backend_lock(d->submit,std::defer_lock);\n   if(!threaded)backend_lock.lock();" not in copy:
        p.append("copy_drawable takes the submission lock at its start on a presentation thread too")
    if "   if(threaded) { backend_lock.lock();if(d->uncertain)return VK_ERROR_DEVICE_LOST; }\n   r=DP(d,QueueSubmit)(d->copy_queue,1,&submit,frame->fence);" not in copy:
        p.append("a presentation thread's copy is not submitted under the submission lock")
    if "   backend_lock.unlock();\n   auto start=std::chrono::steady_clock::now();\n   r=threaded?wait_fence_whole(d.get(),frame->fence):" \
       "DP(d,WaitForFences)(d->raw,1,&frame->fence,VK_TRUE,5000000000ull);\n   if(!threaded)backend_lock.lock();\n" \
       "   producer_ns+=elapsed(start);" not in copy:
        p.append("a presentation thread keeps (or takes again) the submission lock while it waits for its copy and publishes, "
                 "or its wait for the copy is not a whole wait")
    if "if (!threaded) if (auto observer=present_observer.load())" not in copy or bridge.count("present_observer.load()") != 2 or \
       "!present_observer.load() && !c->surface->held &&" not in body(bridge, "VkResult VKAPI_CALL queue_present("):
        p.append("the present observer can run on a presentation thread, or a present is handed over while one is set")
    if "if (threaded && fence_dropped && fence_dropped(d->raw,frame->fence)==1) return VK_ERROR_UNKNOWN;" not in copy or \
       copy.index("fence_dropped(d->raw") > copy.index("surface_dirty(d->raw"):
        p.append("a presentation thread publishes a copy that a channel rebuild dropped")
    create = body(bridge, "VkResult VKAPI_CALL create_swapchain(")
    if "if (info->presentMode==VK_PRESENT_MODE_IMMEDIATE_KHR && d->private_copy_queue && !forked && newest_wanted()) {" not in create or \
       bridge.count("std::make_unique<nvkbridge::Newest<NewestBackend>>") != 1:
        p.append("a swapchain other than IMMEDIATE with a private copy queue and the switch on can get a presentation thread")
    if "signalled.flags=VK_FENCE_CREATE_SIGNALED_BIT;" not in create:
        p.append("the images' done fences are not created signalled")
    thread = body(bridge, "void *newest_main(")
    if not re.search(r"for \(bool more=true;more;\) \{\n\s+@autoreleasepool \{\n[^@]*try \{ more=c->newest->step\(\); \}\n\s+catch \(\.\.\.\) \{", thread):
        p.append("the presentation thread's rounds are not each inside an autorelease pool and a catch of everything")
    start = body(bridge, "bool start_presentation(")
    if not re.search(r"pinned=dladdr\(reinterpret_cast<void \*>\(&vkGetInstanceProcAddr\),&self\) && self\.dli_fname &&\n\s+"
                     r"dlopen\(self\.dli_fname,RTLD_NOW\|RTLD_LOCAL\|RTLD_NODELETE\);\n   \}\);\n   if \(!pinned\) return false;\n", start):
        p.append("a presentation thread is started in a library that could not be pinned in the process")
    for need in ("pthread_attr_setdetachstate(&attr,PTHREAD_CREATE_DETACHED);", "pthread_attr_set_qos_class_np(&attr,QOS_CLASS_USER_INTERACTIVE,0);",
                 "sigfillset(&all);", "sigdelset(&all,SIGSEGV);sigdelset(&all,SIGBUS);sigdelset(&all,SIGILL);",
                 "sigdelset(&all,SIGFPE);sigdelset(&all,SIGTRAP);sigdelset(&all,SIGSYS);", "pthread_sigmask(SIG_SETMASK,&saved,nullptr);"):
        if need not in start:
            p.append(f"the presentation thread is not started as the Zink frontend's is: {need}")
    present = body(bridge, "VkResult VKAPI_CALL queue_present(")
    if "r=DP(d,QueueSubmit)(queue,1,&submit,done);if (r!=VK_SUCCESS) return r;\n      handed->newest->put(k);" not in present or \
       "VkResult r=DP(d,ResetFences)(d->raw,1,&done);if (r!=VK_SUCCESS) return r;" not in present:
        p.append("a frame is handed to the presentation thread without its done fence submitted behind its rendering")
    if "if (c->newest) valid=c->newest->app_owns(k);" not in present or "if (c->newest) c->newest->release(k);" not in present:
        p.append("the synchronous path does not ask the swapchain's own states about an image")
    if "if (r==VK_SUCCESS) c->surface->held=false;" not in body(bridge, "VkResult copy_frame("):
        p.append("a synchronous present that reaches the layer does not let the next one be handed over")
    return p


def dropped_problems(bridge, recover):
    p = []
    if not re.search(r'visibility\("default"\)\)\) int\n\+nvmtl_nvk_fence_dropped_v1\(VkDevice device, VkFence fence\)', recover):
        p.append("nvk-channel-recover.patch does not export int nvmtl_nvk_fence_dropped_v1(VkDevice, VkFence)")
    if "using FenceDropped = int (*)(VkDevice, VkFence);" not in bridge or 'dlsym(backend, "nvmtl_nvk_fence_dropped_v1")' not in bridge:
        p.append("bridge.mm does not look nvmtl_nvk_fence_dropped_v1 up with that signature")
    if "if (!backend_gipa || !stage_import || !surface_dirty) backend_gipa = nullptr;" not in bridge:
        p.append("bridge.mm refuses a renderer without nvmtl_nvk_fence_dropped_v1 (it is optional)")
    return p


def budget_problems(header, recover):
    """The time the frontend gives the GPU is a backstop behind NVK's own verdicts, which arrive through the same wait:
    longer than NVK goes on believing a suspended GPU, plus the window of its rebuilds."""
    mine = re.search(r"\n   uint64_t done_ns=(\d+)ull;\n", header)
    asleep = re.search(r"\n\+#define NVRM_REC_ASLEEP_NS\s+(\d+)ll\b", recover)
    window = re.search(r"\n\+#define NVRM_REC_WINDOW_NS\s+(\d+)ll\b", recover)
    if not mine or not asleep or not window:
        return ["NewestLimits::done_ns of present_newest.h, or NVRM_REC_ASLEEP_NS and NVRM_REC_WINDOW_NS of "
                "nvk-channel-recover.patch, is not where this test reads it"]
    if int(mine.group(1)) < int(asleep.group(1)) + int(window.group(1)):
        return [f"done_ns is {int(mine.group(1)) // 10**9} s: shorter than the {int(asleep.group(1)) // 10**9} s NVK goes on waiting "
                f"for a suspended GPU plus the {int(window.group(1)) // 10**9} s window of its rebuilds, so the frontend would call "
                "a device uncertain that NVK is still waiting for"]
    return []


def main():
    bridge = (NB / "bridge.mm").read_text()
    header = HEADER.read_text()

    # ---- the text ------------------------------------------------------------------------------------------------
    p = wiring_problems(bridge)
    check("bridge.mm: nobody waits for a presentation thread while holding the submission lock, the thread holds it for its "
          "copy's submission alone, its waits for the GPU are whole ones, the observer never runs on it, and only an "
          "IMMEDIATE swapchain with a private copy queue gets one", not p, p)
    # One place of bridge.mm broken each: (what, the text as it is, the text that breaks it), or several such pairs.
    for what, *changes in (
        ("a queue_present that waits for the presentation thread with the submission lock held",
         "      else c->newest->quiesce();\n   }\n   std::lock_guard<std::recursive_mutex> lock(d->submit);\n",
         "   }\n   std::lock_guard<std::recursive_mutex> lock(d->submit);\n      else c->newest->quiesce();\n"),
        ("a destroy_swapchain that retires under the submission lock",
         ("      c->newest->retire();\n      // Frames that were presented", "      // Frames that were presented"),
         ("   std::lock_guard<std::recursive_mutex> submit(c->dev->submit);\n   if (c->dev->uncertain) return;\n   release_images(c);\n",
          "   std::lock_guard<std::recursive_mutex> submit(c->dev->submit);\n   if (c->newest) c->newest->retire();\n"
          "   if (c->dev->uncertain) return;\n   release_images(c);\n")),
        ("a create_swapchain that does not stop the old swapchain first", "   if (old && old->newest) old->newest->retire();\n", ""),
        ("a presentation thread that holds the submission lock from the start of its copy",
         "   if(!threaded)backend_lock.lock();\n   if(d->uncertain)return VK_ERROR_DEVICE_LOST;\n   // Every frame",
         "   backend_lock.lock();\n   if(d->uncertain)return VK_ERROR_DEVICE_LOST;\n   // Every frame"),
        ("an observer called on the presentation thread", "   if (!threaded) if (auto observer=present_observer.load())",
         "   if (auto observer=present_observer.load())"),
        ("a FIFO swapchain with a presentation thread",
         "if (info->presentMode==VK_PRESENT_MODE_IMMEDIATE_KHR && d->private_copy_queue && !forked && newest_wanted()) {",
         "if (d->private_copy_queue && !forked && newest_wanted()) {"),
        ("a presentation thread that takes the application's signals", "   sigfillset(&all);\n", "   sigemptyset(&all);\n"),
        ("a frame handed over without its done fence",
         "      r=DP(d,QueueSubmit)(queue,1,&submit,done);if (r!=VK_SUCCESS) return r;\n", ""),
        ("an acquire_image that takes the submission lock before it asks the swapchain",
         "   auto d=c->dev;if (d->uncertain) return VK_ERROR_DEVICE_LOST;\n   if (c->newest) {\n",
         "   auto d=c->dev;if (d->uncertain) return VK_ERROR_DEVICE_LOST;\n"
         "   std::lock_guard<std::recursive_mutex> submit(d->submit);\n   if (c->newest) {\n"),
        ("a presentation thread started before its swapchain is in the registry",
         "      c->newest=std::make_unique<nvkbridge::Newest<NewestBackend>>(NewestBackend{c.get()},(uint32_t)c->images.size());\n",
         "      c->newest=std::make_unique<nvkbridge::Newest<NewestBackend>>(NewestBackend{c.get()},(uint32_t)c->images.size());\n"
         "      if (!start_presentation(c)) c->newest.reset();\n"),
        ("a presentation thread in a library that could not be pinned", "   if (!pinned) return false;\n", ""),
        ("a presentation thread whose wait for its copy ends when the machine wakes",
         "   r=threaded?wait_fence_whole(d.get(),frame->fence):DP(d,WaitForFences)", "   r=DP(d,WaitForFences)"),
        ("a wait of the whole budget cut in slices",
         "      r=DP(d,WaitForFences)(d->raw,1,&fence,VK_TRUE,nvkbridge::NewestLimits{}.done_ns);\n",
         "      for (int slice=0;slice<30 && r==VK_TIMEOUT;slice++) r=DP(d,WaitForFences)(d->raw,1,&fence,VK_TRUE,1000000000ull);\n"),
        ("a sleep looked for on one clock alone",
         "   const uint64_t all=clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n", "   const uint64_t all=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);\n"),
        ("the two clocks read in the order in which a thread held up between them invents a sleep",
         "   const uint64_t all=clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n   const uint64_t awake=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);\n",
         "   const uint64_t awake=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);\n   const uint64_t all=clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n"),
        ("a swapchain whose own waits are never told that the machine slept",
         "   uint64_t woke() { return machine_woke(); }\n", "   uint64_t woke() { return 0; }\n"),
        ("a destroy_swapchain whose wait for the frames in flight ends when the machine wakes",
         "         VkResult r=wait_fence_whole(d.get(),c->images[k].done);\n",
         "         VkResult r=DP(d,WaitForFences)(d->raw,1,&c->images[k].done,VK_TRUE,5000000000ull);\n"),
        ("a presentation thread that copies a frame whose done fence was dropped",
         "   if (fence_dropped && fence_dropped(c->dev->raw,c->images[k].done)==1) {\n", "   if (false) {\n"),
    ):
        pairs = [tuple(changes)] if isinstance(changes[0], str) else changes
        broken = bridge
        for old, new in pairs:
            if broken.count(old) != 1:
                check(f"control '{what}': the text it breaks is in bridge.mm once", False, repr(old))
                broken = None
                break
            broken = broken.replace(old, new)
        if broken is not None:
            check(f"control: {what} is caught", bool(wiring_problems(broken)), "the check found nothing wrong in a copy that is wrong")
    recover = (ROOT / "nvk" / "nvk-channel-recover.patch").read_text()
    p = dropped_problems(bridge, recover)
    check("bridge.mm asks NVK's exported nvmtl_nvk_fence_dropped_v1 with its signature, and works without it", not p, p)
    check("control: an NVK whose nvmtl_nvk_fence_dropped_v1 takes other arguments is caught", bool(dropped_problems(
        bridge, recover.replace("nvmtl_nvk_fence_dropped_v1(VkDevice device, VkFence fence)\n+{", "nvmtl_nvk_fence_dropped_v1(VkFence fence)\n+{", 1))),
        "the check found nothing wrong in a copy that is wrong")
    p = budget_problems(header, recover)
    check("the time a presentation thread gives the GPU is longer than NVK's own wait for a suspended GPU and its rebuilds", not p, p)
    check("control: a budget of 30 s is caught", bool(budget_problems(
        header.replace("   uint64_t done_ns=400000000000ull;\n", "   uint64_t done_ns=30000000000ull;\n", 1), recover)),
        "the check found nothing wrong in a copy that is wrong")
    check('bridge.mm includes the header the fixture runs ("present_newest.h"), and build.sh hashes its folder',
          '#include "present_newest.h"' in bridge and 'C_SRC="tools/nvk-bridge"' in (ROOT / "build.sh").read_text())

    # ---- the swapchain -------------------------------------------------------------------------------------------
    if not shutil.which(CXX):
        print(f"SKIP: no C++ compiler ({CXX}): the fixture is not built" if not fails else f"{fails} failed")
        return 1 if fails else 0
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        ok, out = build(tmp / "newest", NB)
        check("present_newest.h and its fixture compile without a warning", ok, out)
        if ok:
            status, out = run(tmp / "newest")
            check("the swapchain that shows the newest frame: every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
        for name in SANITIZERS:
            flags = sanitizer(tmp, name)
            if not flags:
                print(f"note the swapchain under {flags_text(SANITIZERS[name])}: not run, this compiler cannot")
                continue
            ok, out = build(tmp / f"newest_{name}", NB, flags)
            status, out = run(tmp / f"newest_{name}") if ok else ("not built", out)
            check(f"the swapchain under {flags_text(flags)}: every arm passes and nothing is reported",
                  status == 0 and "all arms passed" in out and "Sanitizer" not in out and "runtime error" not in out, out[-2500:])

        for what, old, new, says in CONTROLS:
            if header.count(old) != 1:
                check(f"control '{what}': the text it breaks is in present_newest.h once", False, repr(old))
                continue
            where = tmp / "broken"
            where.mkdir(exist_ok=True)
            (where / "present_newest.h").write_text(header.replace(old, new))
            ok, out = build(tmp / "newest_broken", where)
            status, out = run(tmp / "newest_broken", ["--first"], limit=120) if ok else ("not built", out)
            # 1: an arm failed. 3: the fixture's watchdog. Anything else (a signal, a timeout of this test) is not the
            # fixture saying that the rule is broken.
            check(f"control, the swapchain with one rule broken ({what}): the fixture fails where it looks at that rule",
                  ok and status in (1, 3) and any(text in out for text in ((says,) if isinstance(says, str) else says)),
                  f"status {status}, expected to read: {says}\n{out[-1500:]}")
        for what, flag, says in (
            ("in a present", "-DBREAK_LOCK_ORDER", "(watchdog): synchronous presents between the others"),
            ("in an acquire", "-DBREAK_ACQUIRE_ORDER", "(watchdog): an application nothing limits"),
        ):
            ok, out = build(tmp / "newest_order", NB, [flag])
            status, out = run(tmp / "newest_order", ["--first"], limit=120) if ok else ("not built", out)
            check(f"control, an application that takes the submission lock before it waits for the presentation thread {what}: "
                  "the fixture stands still there and its watchdog ends it", ok and status == 3 and says in out,
                  f"status {status}, expected to read: {says}\n{out[-1500:]}")

    print("all passed" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
