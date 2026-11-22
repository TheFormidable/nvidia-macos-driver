// Host fixture of tools/nvk-bridge/present_newest.h: the swapchain that shows the newest frame, as it is, against a
// scripted GPU and a scripted layer, with real threads. tools/test_nvk_bridge_newest.py builds and runs it, again
// under ThreadSanitizer and under AddressSanitizer with UndefinedBehaviorSanitizer, and against copies of the header
// with one rule broken each.
//
// The model. A GPU that finishes frames one after another (or never). A layer with as many drawables as bridge.mm
// gives it (three, or two for a swapchain of two images), which come back when a compositor thread composes (or
// never: a hidden window). The device's submission lock, which the copy takes as bridge.mm's does. A machine that
// sleeps through a wait when the script says so. An application thread that acquires, "renders" and presents the way
// bridge.mm's vkAcquireNextImageKHR and vkQueuePresentKHR do: handed to the presentation thread, or synchronously
// after quiesce().
//
// What is held, in every arm (a breach is counted and fails the arm):
//   - an image is handed to the application only when the GPU has finished what was presented from it and no copy
//     reads it;
//   - a copy starts only after its frame's rendering has finished;
//   - frames reach the layer in the order they were presented, whoever presents them;
//   - nothing is copied or presented for a swapchain after retire() returned;
//   - every call returns: a watchdog ends the process when an arm stands still.
#include "present_newest.h"
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <stdexcept>
#include <string>
#include <thread>

#ifdef __linux__
extern "C" int issetugid(void) { return 0; }
#endif
#define NVK_ZINK_CFG_PATH "/nonexistent/nvk-bridge-newest-fixture.conf"
extern "C" {
#include "nvk_zink_cfg.h"
}

using Clock=std::chrono::steady_clock;
using std::chrono::microseconds;
using std::chrono::milliseconds;
using nvkbridge::Acquire;
using nvkbridge::Copy;
using nvkbridge::Wait;

namespace {
const auto kNever=Clock::time_point::max();
const milliseconds kDrawableTimeout{300};     // the layer's own timeout (1 s on a Mac)
const nvkbridge::NewestLimits kLimits{400000000ull,5000000ull};   // 400 ms for the GPU, look again every 5 ms

struct World {
   explicit World(uint32_t images) : done_at(images,Clock::time_point::min()),frame_of(images,0),owner(images,0),
      layer_max(static_cast<int>(std::min(3u,images))),drawables(layer_max) {}
   std::mutex m;
   // GPU
   std::vector<Clock::time_point> done_at;     // when the frame presented from image k has finished rendering
   Clock::time_point gpu_free{Clock::time_point::min()};
   std::vector<uint64_t> frame_of;             // the number of the frame presented from image k
   std::vector<int> owner;                     // 0 nobody, 1 the application renders into it, 2 a copy reads it
   // layer: as many drawables as bridge.mm gives it (maximumDrawableCount)
   const int layer_max;
   int drawables;
   bool hidden{};
   std::condition_variable layer;
   uint64_t last_shown{};
   // script
   std::atomic<int> gpu_us{200},copy_us{300};
   std::atomic<bool> gpu_stalled{},fail_wait{};
   std::atomic<bool> gpu_suspended{};          // the GPU finishes nothing until the machine has slept and woken
   std::atomic<int> fail_copies{},uncertain_copies{},throw_copies{};
   std::atomic<int> sleeps{};                  // the machine sleeps through this many of the next whole waits
   std::atomic<uint64_t> wakes{};              // what Backend::woke answers
   std::atomic<uint64_t> whole_waits{};        // waits of the whole budget that were begun
   // what happened
   std::atomic<bool> uncertain{},held{},retire_returned{};
   std::atomic<uint64_t> queued{},shown{},shown_sync{},failed{},dropped{},breaches{};
   std::recursive_mutex submit;                // the device's submission lock
   std::string first_breach;
   void breach(const char *what) {             // m held or not: only the first text is kept, under its own lock
      static std::mutex text;
      std::lock_guard<std::mutex> lock(text);
      if (!breaches++) first_breach=what;
   }
};

struct Drawable {
   World *w{};
   bool presented{};
   Drawable()=default;
   explicit Drawable(World *world) : w(world) {}
   Drawable(Drawable &&o) noexcept : w(o.w),presented(o.presented) { o.w=nullptr; }
   Drawable &operator=(Drawable &&)=delete;
   explicit operator bool() const { return w!=nullptr; }
   // A drawable that was not presented goes straight back to the layer; a presented one when the compositor composes.
   ~Drawable() {
      if (!w || presented) return;
      { std::lock_guard<std::mutex> lock(w->m);w->drawables++; }
      w->layer.notify_all();
   }
};

Drawable take_drawable(World *w) {
   std::unique_lock<std::mutex> lock(w->m);
   if (!w->layer.wait_for(lock,kDrawableTimeout,[&] { return w->drawables>0 && !w->hidden; })) return Drawable();
   w->drawables--;
   return Drawable(w);
}

struct Backend {
   World *w;
   bool uncertain() const { return w->uncertain.load(); }
   void set_uncertain() { w->uncertain=true; }
   void hold() { w->held=true; }
   void dropped() { w->dropped++; }
   Wait wait_done(uint32_t k,uint64_t ns) {
      if (w->fail_wait) return Wait::Failed;
      if (ns==kLimits.done_ns) {
         w->whole_waits++;
         int left=w->sleeps.load();
         while (left>0 && !w->sleeps.compare_exchange_weak(left,left-1)) {}
         if (left>0) {
            // The script's sleep. A wait ends at a time on a clock that counts a sleep, so it has run out when the
            // machine wakes, with the frame where it was. A GPU that was suspended for the sleep goes on from there:
            // what it was given finishes soon, in the order it was given.
            std::lock_guard<std::mutex> lock(w->m);
            w->wakes++;
            if (w->gpu_suspended.exchange(false)) {
               std::vector<uint32_t> waiting;
               for (uint32_t i=0;i<w->done_at.size();i++) if (w->done_at[i]==kNever) waiting.push_back(i);
               std::sort(waiting.begin(),waiting.end(),[&](uint32_t a,uint32_t b) { return w->frame_of[a]<w->frame_of[b]; });
               w->gpu_free=Clock::now();
               for (uint32_t i : waiting) { w->gpu_free+=milliseconds(30);w->done_at[i]=w->gpu_free; }
            }
            return Wait::Pending;
         }
      }
      const auto until=Clock::now()+std::chrono::nanoseconds(ns);
      for (;;) {
         Clock::time_point at;
         { std::lock_guard<std::mutex> lock(w->m);at=w->done_at[k]; }
         const auto now=Clock::now();
         if (now>=at) return Wait::Signalled;
         if (now>=until) return Wait::Pending;
         std::this_thread::sleep_until(std::min(at,std::min(until,now+milliseconds(2))));
      }
   }
   uint64_t woke() { return w->wakes.load(); }
   Drawable next_drawable() { return take_drawable(w); }
   Copy copy(uint32_t k,Drawable &drawable) {
      uint64_t frame;
      {
         std::lock_guard<std::mutex> lock(w->m);
         if (w->retire_returned) w->breach("a copy started after retire() returned");
         if (Clock::now()<w->done_at[k]) w->breach("a copy started before its frame's rendering had finished");
         if (w->owner[k]) w->breach("a copy reads an image the application or another copy uses");
         w->owner[k]=2;frame=w->frame_of[k];
      }
      if (w->throw_copies>0) { w->throw_copies--; { std::lock_guard<std::mutex> lock(w->m);w->owner[k]=0; } throw std::runtime_error("scripted"); }
      { std::lock_guard<std::recursive_mutex> submit(w->submit); }     // the copy's own submission
      std::this_thread::sleep_for(microseconds(w->copy_us.load()));
      Copy result=Copy::Shown;
      if (w->uncertain_copies>0) { w->uncertain_copies--;result=Copy::Uncertain; }
      else if (w->fail_copies>0) { w->fail_copies--;result=Copy::Failed; }
      std::lock_guard<std::mutex> lock(w->m);
      if (result==Copy::Uncertain) return result;                      // the image stays the copy's
      w->owner[k]=0;
      if (result==Copy::Failed) { w->failed++;return result; }
      if (w->retire_returned) w->breach("a frame was presented after retire() returned");
      if (frame<=w->last_shown) w->breach("a frame reached the layer after a newer one");
      w->last_shown=frame;drawable.presented=true;w->shown++;
      return result;
   }
};
using Chain=nvkbridge::Newest<Backend>;

// The compositor: every `period` it composes, which gives back the drawables that were presented (at most two, and
// never more than the layer has).
struct Compositor {
   World *w;std::atomic<bool> stop{};std::thread thread;
   Compositor(World *world,milliseconds period) : w(world),thread([this,period] {
      while (!stop) {
         std::this_thread::sleep_for(period);
         { std::lock_guard<std::mutex> lock(w->m);w->drawables=std::min(w->layer_max,w->drawables+2); }
         w->layer.notify_all();
      }
   }) {}
   ~Compositor() { stop=true;thread.join(); }
};
// The presentation thread, as bridge.mm runs it.
struct Presenter {
   std::atomic<bool> ended{};std::thread thread;
   explicit Presenter(Chain *c) : thread([this,c] {
      try { while (c->step()) {} } catch (...) { c->abandon(); }
      ended=true;
   }) {}
   ~Presenter() { thread.join(); }
};

std::atomic<int> watchdog_ms{};
std::atomic<const char *> standing{"nothing"};
// An arm begins: its name for the watchdog, and how long it may take.
void starts(const char *what,int ms) { standing=what;watchdog_ms=ms; }
void watchdog_main() {
   for (;;) {
      std::this_thread::sleep_for(milliseconds(50));
      if ((watchdog_ms-=50)<=0) { printf("FAIL  the arm stood still (watchdog): %s\n",standing.load());fflush(stdout);_Exit(3); }
   }
}

int fails;
bool stop_at_first;      // a control only has to fail once
void arm(const char *name,bool ok,const std::string &detail="") {
   printf("%s  %s%s%s\n",ok?"ok  ":"FAIL",name,ok||detail.empty()?"":"\n      ",ok?"":detail.c_str());
   if (!ok) fails++;
   if (!ok && stop_at_first) { printf("FAILED\n");fflush(stdout);_Exit(1); }
}

// The application's side of bridge.mm.
struct App {
   World *w;Chain *c;uint64_t seq{},presents{},synchronous{},out_of_date{};
   // vkAcquireNextImageKHR: the swapchain's answer first, then the submission that tells the application.
   bool acquire(uint32_t *k,uint64_t timeout=UINT64_MAX,Acquire *why=nullptr) {
#ifdef BREAK_ACQUIRE_ORDER
      std::lock_guard<std::recursive_mutex> submit(w->submit);
      const Acquire a=c->acquire(timeout,k);
#else
      const Acquire a=c->acquire(timeout,k);
      std::lock_guard<std::recursive_mutex> submit(w->submit);
#endif
      if (why) *why=a;
      if (a!=Acquire::Got) return false;
      std::lock_guard<std::mutex> lock(w->m);
      if (w->owner[*k]) w->breach("an image was handed out while a copy or the application used it");
      if (Clock::now()<w->done_at[*k]) w->breach("an image was handed out before the GPU had finished its frame");
      w->owner[*k]=1;
      return true;
   }
   // The frame's rendering goes to the GPU, behind what it already has.
   void submit_rendering(uint32_t k) {
      std::lock_guard<std::mutex> lock(w->m);
      w->owner[k]=0;w->frame_of[k]=++seq;
      if (w->gpu_stalled || w->gpu_suspended) { w->done_at[k]=kNever;return; }
      w->gpu_free=std::max(w->gpu_free,Clock::now())+microseconds(w->gpu_us.load());
      w->done_at[k]=w->gpu_free;
   }
   // vkQueuePresentKHR. true: accepted (handed over, or shown synchronously).
   bool present(uint32_t k,bool force_sync=false) {
      presents++;
      const bool newest=!force_sync && !w->held && c->accepts(k);
#ifdef BREAK_LOCK_ORDER
      std::lock_guard<std::recursive_mutex> submit(w->submit);
      if (!newest) c->quiesce();
#else
      if (!newest) c->quiesce();
      std::lock_guard<std::recursive_mutex> submit(w->submit);
#endif
      if (w->uncertain) return false;
      if (newest) { submit_rendering(k);w->queued++;c->put(k);return true; }
      // The synchronous path: a drawable, the rendering, the copy, the present, all on this thread.
      bool shown=false;
      if (c->app_owns(k)) {
         synchronous++;
         submit_rendering(k);
         Drawable drawable=take_drawable(w);
         Backend backend{w};
         const Wait rendered=backend.wait_done(k,kLimits.done_ns);
         if (rendered!=Wait::Signalled) { w->uncertain=true;return false; }
         if (drawable) {
            std::lock_guard<std::mutex> lock(w->m);
            if (w->frame_of[k]<=w->last_shown) w->breach("a synchronous frame reached the layer after a newer one");
            w->last_shown=w->frame_of[k];drawable.presented=true;w->shown_sync++;w->held=false;shown=true;
         } else out_of_date++;
      }
      if (!w->uncertain) c->release(k);
      return shown;
   }
   void render(int us) { if (us) std::this_thread::sleep_for(microseconds(us)); }
};

std::string counts(World &w,const App &app) {
   char text[320];
   snprintf(text,sizeof text,"presents %llu, handed over %llu, shown %llu, shown synchronously %llu, dropped %llu, failed %llu, "
      "breaches %llu%s%s",(unsigned long long)app.presents,(unsigned long long)w.queued.load(),(unsigned long long)w.shown.load(),
      (unsigned long long)w.shown_sync.load(),(unsigned long long)w.dropped.load(),(unsigned long long)w.failed.load(),
      (unsigned long long)w.breaches.load(),w.breaches?": ":"",w.first_breach.c_str());
   return text;
}

// The application runs as fast as it can for `ms`; the layer composes every 10 ms.
void uncapped(uint32_t images) {
   starts("an application nothing limits",20000);
   World w(images);Chain c(Backend{&w},images,kLimits);App app{&w,&c};
   {
      Compositor compositor(&w,milliseconds(10));Presenter presenter(&c);
      const auto end=Clock::now()+milliseconds(400);
      while (Clock::now()<end) {
         uint32_t k;
         if (!app.acquire(&k)) break;
         app.render(100);
         app.present(k);
      }
      c.retire();w.retire_returned=true;
      std::this_thread::sleep_for(milliseconds(30));
   }
   const std::string what=counts(w,app);
   char name[160];
   snprintf(name,sizeof name,"%u images, nothing limits the application: it presents several times what the layer shows",images);
   arm(name,!w.breaches && !w.uncertain && w.shown>=5 && app.presents>3*w.shown && w.queued==app.presents,what);
   snprintf(name,sizeof name,"%u images: every frame handed over was shown or dropped, none twice",images);
   arm(name,w.queued==w.shown+w.dropped+w.failed && !w.shown_sync,what);
}

// The GPU needs 4 ms a frame and the application nothing: acquire holds it to the GPU.
void gpu_bound(uint32_t images) {
   starts("an application the GPU limits",20000);
   World w(images);w.gpu_us=4000;Chain c(Backend{&w},images,kLimits);App app{&w,&c};
   const auto begin=Clock::now();
   {
      Compositor compositor(&w,milliseconds(10));Presenter presenter(&c);
      const auto end=begin+milliseconds(400);
      while (Clock::now()<end) {
         uint32_t k;
         if (!app.acquire(&k)) break;
         app.present(k);
      }
      c.retire();w.retire_returned=true;
   }
   const double took=std::chrono::duration<double>(Clock::now()-begin).count();
   const uint64_t most=static_cast<uint64_t>(took/0.004)+images+2;
   char name[160];
   snprintf(name,sizeof name,"%u images, a GPU of 4 ms a frame: the application is held to the GPU, at most %u frames ahead",images,images);
   arm(name,!w.breaches && !w.uncertain && app.presents<=most && app.presents>=20,counts(w,app)+", at most "+std::to_string(most));
}

// The GPU stops finishing frames.
void stall_seen_by_the_thread() {
   starts("a GPU that stops, seen by the thread",20000);
   World w(3);Chain c(Backend{&w},3,kLimits);App app{&w,&c};
   Compositor compositor(&w,milliseconds(10));Presenter presenter(&c);
   uint32_t k;
   for (int i=0;i<20 && app.acquire(&k);i++) { app.render(100);app.present(k); }
   const uint64_t before=w.shown;
   w.gpu_stalled=true;
   const auto begin=Clock::now();
   Acquire why=Acquire::Got;int handed=0;
   while (app.acquire(&k,UINT64_MAX,&why)) { handed++;if (!app.present(k)) break; }
   if (why==Acquire::Got) app.acquire(&k,UINT64_MAX,&why);
   const auto took=Clock::now()-begin;
   arm("a GPU that stops: the device is called uncertain within the budget and acquire answers that it is lost",
       w.uncertain && why==Acquire::Lost && took<milliseconds(3000) && handed<=3 && !w.breaches,
       counts(w,app)+", handed out after the stall "+std::to_string(handed));
   const auto t0=Clock::now();
   c.quiesce();c.retire();
   arm("after that, quiesce and retire return at once and the thread ends",
       Clock::now()-t0<milliseconds(500) && w.shown<=before+2);
   for (int i=0;i<100 && !presenter.ended;i++) std::this_thread::sleep_for(milliseconds(10));
   arm("the presentation thread of an uncertain device ends",presenter.ended.load());
   const auto left=c.pending();
   arm("an uncertain device keeps its images: none of the frames in flight was freed",!left.empty());
}
void stall_seen_by_acquire() {
   starts("a GPU that stops, seen by acquire",20000);
   World w(2);w.hidden=true;Chain c(Backend{&w},2,kLimits);App app{&w,&c};
   Presenter presenter(&c);
   w.gpu_stalled=true;
   uint32_t k;Acquire why=Acquire::Got;
   // Two frames presented, the layer gives nothing: each is given up in turn, and their rendering never ends.
   bool ok=app.acquire(&k) && app.present(k);
   for (int i=0;i<300 && !w.held;i++) std::this_thread::sleep_for(milliseconds(10));
   ok=ok && w.held && app.acquire(&k);
   w.held=false;ok=ok && app.present(k);
   const auto begin=Clock::now();
   const bool got=app.acquire(&k,UINT64_MAX,&why);
   const auto took=Clock::now()-begin;
   arm("a hidden window and a GPU that stops: acquire gives up after the budget and answers that the device is lost",
       ok && !got && why==Acquire::Lost && w.uncertain && took>=milliseconds(390) && took<milliseconds(3000) && !w.breaches,
       counts(w,app));
   c.retire();
}
void wait_failure() {
   starts("a wait that fails",20000);
   World w(2);w.hidden=true;Chain c(Backend{&w},2,kLimits);App app{&w,&c};
   Presenter presenter(&c);
   w.gpu_us=100000;
   uint32_t k;Acquire why=Acquire::Got;
   bool ok=app.acquire(&k) && app.present(k);
   for (int i=0;i<300 && !w.held;i++) std::this_thread::sleep_for(milliseconds(10));
   ok=ok && w.held && app.acquire(&k);
   w.held=false;ok=ok && app.present(k);
   w.fail_wait=true;
   const bool got=app.acquire(&k,UINT64_MAX,&why);
   arm("a wait for the GPU that fails makes the device uncertain: the image is not handed out",
       ok && !got && why==Acquire::Lost && w.uncertain && !w.breaches,counts(w,app));
   c.retire();
}

// A hidden window: the layer gives nothing, the application is held by the layer again, and is let go when it shows.
void hidden_window() {
   starts("a hidden window",30000);
   World w(3);Chain c(Backend{&w},3,kLimits);App app{&w,&c};
   Compositor compositor(&w,milliseconds(10));Presenter presenter(&c);
   uint32_t k;
   for (int i=0;i<30 && app.acquire(&k);i++) { app.render(100);app.present(k); }
   const uint64_t shown_before=w.shown;
   { std::lock_guard<std::mutex> lock(w.m);w.hidden=true; }
   // Presents go on at full speed until the thread has asked the layer once and got nothing.
   const auto begin=Clock::now();
   uint64_t fast=0;
   while (!w.held && Clock::now()-begin<milliseconds(2000) && app.acquire(&k)) { app.present(k);fast++; }
   arm("a hidden window: the thread gets no drawable within the layer's timeout and asks for synchronous presents",
       w.held && fast>0 && !w.breaches,counts(w,app));
   const uint64_t before=app.out_of_date;
   const auto t0=Clock::now();
   int tries=0;
   for (;tries<3 && app.acquire(&k);tries++) app.present(k);
   const auto each=(Clock::now()-t0)/3;
   arm("while it is hidden every present waits for the layer on the application's thread and is refused, as before 3.0.9",
       tries==3 && app.out_of_date==before+3 && each>=kDrawableTimeout-milliseconds(10) && w.held && w.shown<=shown_before+3 && !w.breaches,
       counts(w,app));
   { std::lock_guard<std::mutex> lock(w.m);w.hidden=false; }
   w.layer.notify_all();
   const uint64_t sync_before=w.shown_sync,queued_before=w.queued;
   for (int i=0;i<40 && app.acquire(&k);i++) { app.render(100);app.present(k); }
   arm("shown again: one synchronous present reaches the layer, and the presents after it are handed over again",
       w.shown_sync==sync_before+1 && w.queued>=queued_before+30 && !w.held && !w.breaches,counts(w,app));
   c.retire();w.retire_returned=true;
}

// Presents that have to be synchronous (an observer, two swapchains in one call) between the others.
void mixed(uint32_t images) {
   starts("synchronous presents between the others",15000);
   World w(images);Chain c(Backend{&w},images,kLimits);App app{&w,&c};
   uint64_t forced=0;
   {
      Compositor compositor(&w,milliseconds(3));Presenter presenter(&c);
      uint32_t k;unsigned r=12345;
      for (int i=0;i<1500 && app.acquire(&k);i++) {
         r=r*1103515245u+12345u;
         const bool sync=(r>>16)%7==0;
         if ((r>>20)%3==0) app.render(150);
         app.present(k,sync);forced+=sync;
      }
      c.retire();w.retire_returned=true;
      std::this_thread::sleep_for(milliseconds(20));
   }
   char name[200];
   snprintf(name,sizeof name,"%u images, one present in seven synchronous: frames reach the layer in order and nobody waits for ever",images);
   arm(name,!w.breaches && !w.uncertain && app.presents==1500 && app.synchronous>=forced && w.shown_sync+app.out_of_date==app.synchronous && w.shown>0 &&
       w.queued==w.shown+w.dropped+w.failed,counts(w,app)+", synchronous "+std::to_string(forced));
}

// The swapchain is replaced while frames are in flight.
void retire_in_flight() {
   starts("a swapchain replaced with frames in flight",30000);
   bool ok=true;std::string what;
   for (int round=0;round<40 && ok;round++) {
      World w(3);w.copy_us=2000;Chain c(Backend{&w},3,kLimits);App app{&w,&c};
      Compositor compositor(&w,milliseconds(2));Presenter presenter(&c);
      uint32_t k;
      for (int i=0;i<5+round%7 && app.acquire(&k);i++) { app.render(round%3?200:0);app.present(k); }
      c.retire();w.retire_returned=true;
      Acquire why=Acquire::Got;
      const bool after=app.acquire(&k,UINT64_MAX,&why);
      std::this_thread::sleep_for(milliseconds(6));
      // What is left is waited for without the swapchain's thread: the frames' rendering ends.
      Backend backend{&w};
      for (uint32_t left : c.pending()) ok=ok && backend.wait_done(left,kLimits.done_ns)==Wait::Signalled;
      ok=ok && !after && why==Acquire::Retired && !w.breaches && !c.accepts(0) && w.queued==w.shown+w.dropped+w.failed;
      if (!ok) what=counts(w,app);
   }
   arm("replaced with frames in flight, 40 times: after retire() nothing is copied or presented, acquire answers that the "
       "swapchain is retired, and what is left of the frames ends",ok,what);
   // Retired while its thread waits for a drawable that does not come.
   World w(3);w.hidden=true;Chain c(Backend{&w},3,kLimits);App app{&w,&c};
   Presenter presenter(&c);
   uint32_t k;
   const bool put=app.acquire(&k) && app.present(k);
   std::this_thread::sleep_for(milliseconds(20));
   const auto t0=Clock::now();
   c.retire();
   const auto took=Clock::now()-t0;
   for (int i=0;i<100 && !presenter.ended;i++) std::this_thread::sleep_for(milliseconds(10));
   arm("retired while its thread waits for the layer: retire() does not wait for the layer, and the thread ends by itself",
       put && took<kDrawableTimeout/2 && presenter.ended && !w.breaches && w.shown==0,counts(w,app));
}

// A copy that fails (the layer changed size), and one whose thread ends on an exception.
void failures() {
   starts("copies that fail",15000);
   {
      World w(3);Chain c(Backend{&w},3,kLimits);App app{&w,&c};
      Compositor compositor(&w,milliseconds(3));Presenter presenter(&c);
      uint32_t k;
      for (int i=0;i<10 && app.acquire(&k);i++) { app.render(200);app.present(k); }
      w.fail_copies=1;
      // The application goes on presenting; the present after the failed copy is the synchronous one.
      const auto begin=Clock::now();
      while (!w.shown_sync && Clock::now()-begin<milliseconds(2000) && app.acquire(&k)) { app.render(200);app.present(k); }
      const uint64_t sync_first=w.shown_sync,queued_before=w.queued;
      for (int i=0;i<30 && app.acquire(&k);i++) { app.render(200);app.present(k); }
      c.retire();w.retire_returned=true;
      arm("a copy that fails: the next present is synchronous and says what is wrong, the image is not lost, and the "
          "presents after it are handed over again",
          sync_first==1 && w.failed==1 && w.shown_sync==1 && w.queued==queued_before+30 && !w.held && !w.breaches &&
          w.queued==w.shown+w.dropped+w.failed,counts(w,app));
   }
   {
      World w(2);Chain c(Backend{&w},2,kLimits);App app{&w,&c};
      Compositor compositor(&w,milliseconds(3));Presenter presenter(&c);
      uint32_t k;
      for (int i=0;i<10 && app.acquire(&k);i++) { app.render(200);app.present(k); }
      w.throw_copies=1;
      const auto begin=Clock::now();
      while (!presenter.ended && Clock::now()-begin<milliseconds(2000) && app.acquire(&k)) { app.render(200);app.present(k); }
      const bool ended=presenter.ended;
      const uint64_t sync_before=w.shown_sync,queued_before=w.queued;
      int handed=0;
      for (int i=0;i<30 && app.acquire(&k);i++) { handed++;w.held=false;app.present(k); }
      arm("a presentation thread that ends on an exception: the swapchain presents synchronously from then on and every "
          "image comes back",
          ended && handed==30 && w.queued==queued_before && w.shown_sync==sync_before+30 && !c.accepts(k) && !w.breaches,
          counts(w,app));
      c.retire();
   }
   {
      World w(3);Chain c(Backend{&w},3,kLimits);App app{&w,&c};
      Compositor compositor(&w,milliseconds(3));Presenter presenter(&c);
      uint32_t k;
      for (int i=0;i<10 && app.acquire(&k);i++) { app.render(200);app.present(k); }
      w.uncertain_copies=1;
      Acquire why=Acquire::Got;
      const auto begin=Clock::now();
      while (Clock::now()-begin<milliseconds(2000) && app.acquire(&k,UINT64_MAX,&why)) { app.render(200);if (!app.present(k)) break; }
      for (int i=0;i<100 && !presenter.ended;i++) std::this_thread::sleep_for(milliseconds(10));
      // The image the copy read stays the swapchain's: it is among those whose GPU work may not have ended.
      const auto left=c.pending();
      std::lock_guard<std::mutex> lock(w.m);
      int kept=0,listed=0;
      for (uint32_t i=0;i<w.owner.size();i++)
         if (w.owner[i]==2) { kept++;listed+=std::find(left.begin(),left.end(),i)!=left.end(); }
      arm("a copy whose completion is uncertain: the device is uncertain, the thread ends, and the image it read stays "
          "with the swapchain, never handed out again",w.uncertain && presenter.ended && kept==1 && listed==1 && !w.breaches,
          "images the copy kept "+std::to_string(kept)+", of them still the swapchain's "+std::to_string(listed));
   }
}

// The machine sleeps while somebody waits for the GPU: the wait runs out when it wakes, and proves nothing. The GPU is
// suspended from the start and goes on when the machine wakes, so no arm here depends on when a thread gets to run.
void slept_through() {
   starts("a machine that sleeps through a wait",20000);
   {
      // Seen by the thread.
      World w(3);w.gpu_suspended=true;w.sleeps=1;Chain c(Backend{&w},3,kLimits);App app{&w,&c};
      Compositor compositor(&w,milliseconds(10));Presenter presenter(&c);
      uint32_t k;
      const auto begin=Clock::now();
      const bool put=app.acquire(&k) && app.present(k);
      while (!w.shown && !w.uncertain && Clock::now()-begin<milliseconds(5000)) std::this_thread::sleep_for(milliseconds(5));
      arm("the machine sleeps through the presentation thread's wait for a frame: the wait is made again, the frame is "
          "shown and the device is not called uncertain",
          put && w.shown==1 && !w.uncertain && w.wakes==1 && w.whole_waits==2 && !presenter.ended && !w.breaches,
          counts(w,app)+", whole waits "+std::to_string(w.whole_waits.load()));
      c.retire();w.retire_returned=true;
   }
   {
      // Seen by acquire: a hidden window and an overtaken frame.
      World w(2);w.hidden=true;w.gpu_suspended=true;Chain c(Backend{&w},2,kLimits);App app{&w,&c};
      Presenter presenter(&c);
      uint32_t first=9,second=9,again=9;Acquire why=Acquire::Got;
      bool ok=app.acquire(&first) && app.present(first);
      for (int i=0;i<300 && !w.held;i++) std::this_thread::sleep_for(milliseconds(10));
      ok=ok && w.held && app.acquire(&second);
      w.held=false;ok=ok && app.present(second);
      w.sleeps=1;
      const bool got=app.acquire(&again,UINT64_MAX,&why);
      arm("the machine sleeps through vkAcquireNextImageKHR's wait for an overtaken frame: the wait is made again and "
          "the image is handed out once its frame has finished",
          ok && got && again==first && !w.uncertain && w.wakes==1 && w.whole_waits==2 && !w.breaches,
          counts(w,app)+", whole waits "+std::to_string(w.whole_waits.load()));
      c.retire();
   }
   {
      // The GPU has stopped for good: one sleep buys one more wait, not an endless one.
      World w(2);w.hidden=true;w.gpu_stalled=true;Chain c(Backend{&w},2,kLimits);App app{&w,&c};
      Presenter presenter(&c);
      uint32_t k;Acquire why=Acquire::Got;
      bool ok=app.acquire(&k) && app.present(k);
      for (int i=0;i<300 && !w.held;i++) std::this_thread::sleep_for(milliseconds(10));
      ok=ok && w.held && app.acquire(&k);
      w.held=false;ok=ok && app.present(k);
      w.sleeps=1;
      const auto begin=Clock::now();
      const bool got=app.acquire(&k,UINT64_MAX,&why);
      const auto took=Clock::now()-begin;
      arm("a GPU that stops and a machine that sleeps once: the wait is made once more and then the device is called "
          "uncertain",
          ok && !got && why==Acquire::Lost && w.uncertain && w.wakes==1 && w.whole_waits==2 && took>=milliseconds(390) && !w.breaches,
          counts(w,app)+", whole waits "+std::to_string(w.whole_waits.load()));
      c.retire();
   }
}
// Seeing that the machine slept, from two clocks: one that stands still while it sleeps and one that goes on.
void wake_watch() {
   starts("the clocks and the whole waits",5000);
   const uint64_t s=1000000000ull;
   nvkbridge::WakeWatch watch;
   // The clocks are already apart when the process first looks (the machine slept before): that is no sleep.
   bool ok=watch.look(5*s,100*s)==0;
   ok=ok && watch.look(6*s,101*s+500000)==0;               // half a millisecond between the two readings
   ok=ok && watch.look(7*s,102*s+999999999ull)==0;         // just under a second more, in all
   arm("two clocks that are apart from the start and move by less than a second: the machine did not sleep",ok);
   ok=watch.look(8*s,112*s)==1;                            // nine seconds
   ok=ok && watch.look(9*s,113*s)==1 && watch.look(10*s,113*s+900000000ull)==1;
   ok=ok && watch.look(11*s,116*s)==2;
   // A reading that was taken before that sleep and arrives after it (another thread's) does not take the sleep back:
   // the next reading does not count it again.
   ok=ok && watch.look(12*s,115*s)==2 && watch.look(13*s,117*s)==2;
   arm("the clocks move apart by a second or more: one sleep each time, counted once, also when an older reading "
       "arrives after it",ok);
   nvkbridge::WakeWatch other;
   ok=other.look(50*s,20*s)==0 && other.look(51*s,21*s)==0 && other.look(52*s,53*s+1)==1;
   arm("a clock that reads behind the other is no sleep, and is not counted against a later one",ok);
   // wait_whole: what it does with the answers of one wait.
   // Atomic, so that a header that repeats a wait for ever does so in a way the watchdog can end (a loop that does
   // nothing a compiler has to keep is not one).
   std::atomic<int> waits{0};std::atomic<uint64_t> woke{0};
   const auto asked=[&] { return woke.load(); };
   ok=nvkbridge::wait_whole([&] { waits++;return Wait::Signalled; },asked)==Wait::Signalled && waits==1;
   waits=0;
   ok=ok && nvkbridge::wait_whole([&] { waits++;return Wait::Failed; },[&] { return woke++; })==Wait::Failed && waits==1;
   waits=0;woke=0;
   ok=ok && nvkbridge::wait_whole([&] { waits++;return Wait::Pending; },asked)==Wait::Pending && waits==1;
   waits=0;woke=0;
   ok=ok && nvkbridge::wait_whole([&] { if (++waits<3) woke++;return waits<4?Wait::Pending:Wait::Signalled; },asked)==Wait::Pending && waits==3;
   waits=0;woke=0;
   ok=ok && nvkbridge::wait_whole([&] { if (++waits<3) woke++;return waits<3?Wait::Pending:Wait::Signalled; },asked)==Wait::Signalled && waits==3;
   arm("a whole wait is made once; again only when it ran out and the machine slept through it; and a failure is final",ok,
       "waits "+std::to_string(waits.load()));
}

// What acquire answers without a presentation thread in the way.
void answers() {
   starts("what acquire answers",8000);
   World w(2);w.hidden=true;w.gpu_us=200000;Chain c(Backend{&w},2,kLimits);App app{&w,&c};
   uint32_t a=9,b=9,k=9;Acquire why=Acquire::Got;
   bool ok=app.acquire(&a) && app.acquire(&b) && a==0 && b==1;
   ok=ok && !app.acquire(&k,0,&why) && why==Acquire::NotReady;
   const auto t0=Clock::now();
   ok=ok && !app.acquire(&k,20000000ull,&why) && why==Acquire::Timeout && Clock::now()-t0>=milliseconds(19);
   arm("both images with the application: acquire answers not ready at once, and a timeout after the time it was given",ok);
   c.unacquire(b);
   { std::lock_guard<std::mutex> lock(w.m);w.owner[b]=0; }
   ok=app.acquire(&k) && k==b;
   arm("an acquire whose own submission failed gives the image back: the next acquire hands out the same one",ok);
   // No thread serves this swapchain here, so frames stay where present puts them.
   ok=c.accepts(a) && !c.accepts(7);
   app.submit_rendering(a);c.put(a);
   ok=ok && !c.accepts(a) && !c.app_owns(a);
   c.release(a);                                             // a present of an image that is not the application's
   ok=ok && c.pending().size()==1;
   app.submit_rendering(b);c.put(b);                          // overtakes a, whose rendering needs 200 ms
   ok=ok && w.dropped==1 && c.pending().size()==2;
   arm("a second present of the same image is refused and frees nothing; a frame that is overtaken is counted",ok);
   ok=!app.acquire(&k,0,&why) && why==Acquire::NotReady;
   ok=ok && !app.acquire(&k,5000000ull,&why) && why==Acquire::Timeout && !w.uncertain;
   const auto t1=Clock::now();
   ok=ok && app.acquire(&k) && k==a && Clock::now()-t1>=milliseconds(30) && !w.breaches;
   arm("an overtaken frame the GPU has not finished: not ready, then a timeout that does not call the device lost, then "
       "the image once its rendering has ended",ok,counts(w,app));
   c.quiesce();
   arm("quiesce gives up the frame that waits for a drawable",c.pending().size()==1 && w.dropped==2);
   c.retire();
   ok=!app.acquire(&k,0,&why) && why==Acquire::Retired && !c.app_owns(a) && !c.accepts(a);
   arm("a retired swapchain hands out nothing",ok);
}

// The settings file's rule is the one of zink/nvk_zink_cfg.h.
void settings() {
   const char *name="NVK_BRIDGE_PRESENT_NEWEST";
   const char *texts[]={"","NVK_BRIDGE_PRESENT_NEWEST=0\n","NVK_BRIDGE_PRESENT_NEWEST=1\n","NVK_BRIDGE_PRESENT_NEWEST=0",
      "  NVK_BRIDGE_PRESENT_NEWEST = 0\n","\tNVK_BRIDGE_PRESENT_NEWEST\t=\t0\n","# NVK_BRIDGE_PRESENT_NEWEST=0\n",
      "XNVK_BRIDGE_PRESENT_NEWEST=0\n","NVK_BRIDGE_PRESENT_NEWEST_X=0\n","NVK_BRIDGE_PRESENT_NEWEST=0\nNVK_BRIDGE_PRESENT_NEWEST=1\n",
      "NVK_BRIDGE_PRESENT_NEWEST=1\nNVK_BRIDGE_PRESENT_NEWEST=0\n","NVK_BRIDGE_PRESENT_NEWEST=00\n","NVK_BRIDGE_PRESENT_NEWEST=\n",
      "NVK_BRIDGE_PRESENT_NEWEST=off\n","A=1\nNVK_BRIDGE_PRESENT_NEWEST=0\nB=2\n","NVK_ZINK_PRESENT_AHEAD=0\n",
      "NVK_BRIDGE_PRESENT_NEWEST 0\n","A=NVK_BRIDGE_PRESENT_NEWEST=0\n","NVK_BRIDGE_PRESENT_NEWEST= 0\n","NVK_BRIDGE_PRESENT_NEWEST=-0\n",
      "\nNVK_BRIDGE_PRESENT_NEWEST=0"};
   bool same=true;int off=0;std::string where;
   for (const char *text : texts) {
      const bool mine=nvkbridge::settings_switch_on(text,name);
      if (mine!=!!nvk_zink_cfg_on_in(text,name,0)) { same=false;where=text; }
      off+=!mine;
   }
   arm("the switch is read from the settings file by the rule of zink/nvk_zink_cfg.h, for every spelling tried",same && off==9,
       "differs for: "+where+" (off for "+std::to_string(off)+")");
}
}

int main(int argc,char **argv) {
   stop_at_first=argc>1 && !strcmp(argv[1],"--first");
   std::thread(watchdog_main).detach();
   starts("the settings file",20000);
   settings();
   wake_watch();
   answers();
   for (uint32_t images : {2u,3u,8u}) uncapped(images);
   for (uint32_t images : {2u,3u}) mixed(images);
   retire_in_flight();
   failures();
   hidden_window();
   stall_seen_by_acquire();
   slept_through();
   for (uint32_t images : {2u,3u}) gpu_bound(images);
   stall_seen_by_the_thread();
   wait_failure();
   watchdog_ms=1<<30;
   printf("%s\n",fails?"FAILED":"all arms passed");
   return fails?1:0;
}
