// A swapchain of the native Vulkan frontend that shows the newest frame (3.0.9): who owns each image, who waits for
// whom, and for how long. vkQueuePresentKHR hands the frame over and returns; a thread of the swapchain takes a
// drawable from the layer when the layer has one, copies the newest frame the application presented into it and
// presents it. A frame that was overtaken before a drawable came is never copied: its image goes back to the
// application once the GPU has finished rendering it.
//
// No Vulkan and no Metal here. bridge.mm gives the GPU and the layer as a Backend; a scripted Backend
// (tools/nvk_bridge_newest_fixture.cpp) runs this text on Linux, plain, under ThreadSanitizer and under
// AddressSanitizer with UndefinedBehaviorSanitizer.
//
// The states of an image:
//   Free      nobody uses it; vkAcquireNextImageKHR may hand it out.
//   App       the application acquired it and has not presented it.
//   Slot      presented, the newest frame, waiting for a drawable. At most one image.
//   Copying   the presentation thread took it: it waits for the frame's rendering, then copies. At most one image.
//   Retiring  presented and overtaken (or given up): it is Free again when its `done` fence has signalled, which
//             only vkAcquireNextImageKHR looks at.
// `done` is the fence vkQueuePresentKHR submits on the application's queue behind the frame's rendering. An image
// that is Free or App has no submission of that fence outstanding.
//
// The Backend, called with this class's lock NOT held unless it says so:
//   bool uncertain()                    the device's completion is uncertain (another swapchain may have said so)
//   void set_uncertain()                say so; nothing of the device is released afterwards
//   Wait wait_done(k, ns)               wait up to ns for image k's `done` fence (ns 0: look only)
//   uint64_t woke()                     a number that changes when the machine is seen to have woken from a sleep
//   Drawable next_drawable()            the layer's next drawable, or something false after its own timeout
//   Copy copy(k, drawable)              copy image k into the drawable, publish and present it
//   void hold()                         (lock held) the next present of the surface is to be synchronous
//   void dropped()                      (lock held) a presented frame will not be shown
//
// Locks. The device's submission lock (bridge.mm, Device::submit) is taken by Backend::copy. So nobody may wait for
// the presentation thread (quiesce, retire, and acquire, which waits for the thread's copy when the thread has every
// image the application does not hold) while holding it; bridge.mm calls all three before it takes it.
#ifndef NVK_BRIDGE_PRESENT_NEWEST_H
#define NVK_BRIDGE_PRESENT_NEWEST_H
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <utility>
#include <vector>

namespace nvkbridge {

// A switch of the settings file, by the rule of zink/nvk_zink_cfg.h (nvk_zink_cfg_on_in without the process's own
// environment): the last line that names the switch wins; a line names it when the name is the first thing on the
// line; blanks before the name, around the "=" and before the value are skipped; on unless the value begins with 0.
// text: the file's first bytes, NUL-terminated.
inline bool settings_switch_on(const char *text,const char *name) {
   const char *value=nullptr;
   const size_t length=strlen(name);
   for (const char *hit=strstr(text,name);hit;hit=strstr(hit+1,name)) {
      const char *first=hit,*after=hit+length;
      while (first>text && (first[-1]==' ' || first[-1]=='\t')) --first;
      while (*after==' ' || *after=='\t') ++after;
      if ((first==text || first[-1]=='\n') && *after=='=') value=after+1;
   }
   while (value && (*value==' ' || *value=='\t')) ++value;
   return !(value && value[0]=='0');
}

enum class Wait { Signalled, Pending, Failed };
enum class Copy { Shown, Failed, Uncertain };
enum class Acquire { Got, NotReady, Timeout, Retired, Lost };

struct NewestLimits {
   // How long the GPU may take to finish a frame the application presented before the device's completion is called
   // uncertain. A backstop behind the renderer's own verdicts, which come back through the same wait and are not to
   // be forestalled. On a channel that stands still the renderer rebuilds the channel (the frame's fence then
   // signals) or loses the device (the wait fails), after 5 s awake with its channel recovery on (its default) and
   // after NVRM_FLUSH_TIMEOUT_MS, 5 s, on its sleep-counting clock with it off. It goes on waiting only for a GPU the
   // kext calls suspended for a system sleep or wake, for at most 360 s of awake time (NVRM_REC_ASLEEP_NS,
   // nvk/nvk-channel-recover.patch), and then runs the same sequence, at most three rebuilds in 30 s. So: longer than
   // those together. A wait the machine slept through is made again (wait_whole).
   uint64_t done_ns=400000000000ull;
   // How often a sleeper looks again at the device's flag, which a thread of another swapchain may set without
   // waking this one.
   uint64_t recheck_ns=100000000ull;
};

// Has the machine slept? awake: nanoseconds on a clock that stands still while the machine sleeps; all: on one that
// goes on. They move apart by the length of every sleep.
class WakeWatch {
public:
   // Two readings a moment apart do not make a sleep; a second does.
   static constexpr uint64_t kSlept=1000000000ull;
   // Look at the clocks. The answer changes when a sleep is seen to have ended since the last look, by any thread.
   uint64_t look(uint64_t awake,uint64_t all) {
      std::lock_guard<std::mutex> lock(mutex_);
      const uint64_t apart=all>awake?all-awake:0;
      if (looked_ && apart>apart_ && apart-apart_>=kSlept) ++wakes_;
      if (!looked_ || apart>apart_) apart_=apart;
      looked_=true;
      return wakes_;
   }
private:
   std::mutex mutex_;
   uint64_t apart_{},wakes_{};
   bool looked_{};
};

// A wait of a whole budget for the GPU, whose running out makes the device's completion uncertain. The renderer ends
// a wait at an absolute time on a clock that counts a sleep, and the GPU does nothing between the start of a suspend
// and the end of the resume: a wait that ran out is believed only when the machine did not sleep through it, and
// is made again otherwise. One call for the whole budget each time, never slices: the renderer reaches its own
// verdict on a channel that stands still inside one wait, and starts counting again in the next.
// wait(): Signalled, Pending (it ran out) or Failed. woke(): WakeWatch::look, or what stands for it.
template<class WaitFn,class WokeFn> Wait wait_whole(WaitFn wait,WokeFn woke) {
   for (uint64_t before=woke();;) {
      const Wait answer=wait();
      if (answer!=Wait::Pending) return answer;
      const uint64_t after=woke();
      if (after==before) return Wait::Pending;
      before=after;
   }
}

template<class Backend> class Newest {
public:
   static constexpr uint32_t kNone=UINT32_MAX;
   enum State : uint8_t { Free, App, Slot, Copying, Retiring };

   Newest(Backend backend,uint32_t images,NewestLimits limits={})
      : backend_(std::move(backend)),limits_(limits),state_(images,Free) {}
   Newest(const Newest &)=delete;
   Newest &operator=(const Newest &)=delete;

   // ---- The application's side. Vulkan has the application call these one at a time for one swapchain.

   // vkAcquireNextImageKHR. Waits for the GPU to finish an overtaken frame, or for the presentation thread when it
   // has every image the application does not hold (the frame waiting for a drawable, then its copy). An application
   // that holds one image at a time never waits for a drawable here.
   Acquire acquire(uint64_t timeout_ns,uint32_t *index) {
      using clock=std::chrono::steady_clock;
      const bool forever=timeout_ns==UINT64_MAX;
      const auto deadline=clock::now()+std::chrono::nanoseconds(std::min<uint64_t>(timeout_ns,INT64_MAX/2));
      std::unique_lock<std::mutex> lock(mutex_);
      for (;;) {
         if (retired_) return Acquire::Retired;
         if (backend_.uncertain()) return Acquire::Lost;
         const uint32_t count=static_cast<uint32_t>(state_.size());
         for (uint32_t j=0;j<count;j++) {
            const uint32_t k=(cursor_+j)%count;
            if (state_[k]==Free) { state_[k]=App;cursor_=(k+1)%count;*index=k;return Acquire::Got; }
         }
         uint64_t left=UINT64_MAX;
         if (!forever) {
            const auto now=clock::now();
            left=now>=deadline?0:static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(deadline-now).count());
         }
         if (!retiring_.empty()) {
            // The oldest overtaken frame. Only this function takes an image out of Retiring, so the image is still
            // there after the wait, which is made without the lock: the presentation thread goes on meanwhile.
            const uint32_t k=retiring_.front();
            const bool whole=left>=limits_.done_ns;
            lock.unlock();
            const Wait answer=whole?wait_rendered(k):backend_.wait_done(k,left);
            lock.lock();
            if (answer==Wait::Signalled) {
               if (!retiring_.empty() && retiring_.front()==k && state_[k]==Retiring) {
                  retiring_.erase(retiring_.begin());state_[k]=Free;
               }
               continue;
            }
            // The GPU did not finish that frame within its whole budget, or the wait itself failed: nothing of
            // this device is safe to hand out again.
            if (answer==Wait::Failed || whole) { lose();return Acquire::Lost; }
            return timeout_ns?Acquire::Timeout:Acquire::NotReady;
         }
         // Nothing is free and nothing was overtaken: what the application does not hold, the presentation thread
         // has (one frame waiting for a drawable, one in its copy).
         if (!left) return timeout_ns?Acquire::Timeout:Acquire::NotReady;
         changed_.wait_for(lock,std::chrono::nanoseconds(std::min(left,limits_.recheck_ns)));
      }
   }
   // The submission that tells the application about its acquired image failed: the image was not handed out.
   void unacquire(uint32_t k) {
      std::lock_guard<std::mutex> lock(mutex_);
      if (k<state_.size() && state_[k]==App) state_[k]=Free;
      changed_.notify_all();
   }
   // May vkQueuePresentKHR hand image k to the presentation thread?
   bool accepts(uint32_t k) {
      std::lock_guard<std::mutex> lock(mutex_);
      return running_ && !retired_ && k<state_.size() && state_[k]==App;
   }
   // vkQueuePresentKHR, after accepts(k) and after image k's `done` fence was submitted behind its rendering. The
   // frame that was waiting for a drawable is overtaken.
   void put(uint32_t k) {
      {
         std::lock_guard<std::mutex> lock(mutex_);
         drop_slot();
         slot_=k;state_[k]=Slot;
      }
      work_.notify_one();
   }
   // Before a synchronous present on this swapchain (and before the device's submission lock is taken): no frame of
   // the presentation thread may reach the layer after the synchronous one. The frame waiting for a drawable is
   // given up, and a copy in flight is waited for, unless the device's completion is uncertain: then nothing is
   // waited for, and every call on the device answers that it is lost.
   void quiesce() {
      std::unique_lock<std::mutex> lock(mutex_);
      drop_slot();
      wait_idle(lock);
   }
   // The synchronous path's two questions.
   bool app_owns(uint32_t k) {
      std::lock_guard<std::mutex> lock(mutex_);
      return !retired_ && k<state_.size() && state_[k]==App;
   }
   void release(uint32_t k) {
      std::lock_guard<std::mutex> lock(mutex_);
      if (k<state_.size() && state_[k]==App) state_[k]=Free;
      changed_.notify_all();
   }
   // The swapchain was replaced or is being destroyed (before the device's submission lock is taken). When this
   // returns the presentation thread starts no copy and presents nothing more, unless the device's completion is
   // uncertain (the copy in flight is then not waited for). The thread may still be inside the layer, asking for a
   // drawable it will give back.
   void retire() {
      std::unique_lock<std::mutex> lock(mutex_);
      retired_=true;
      drop_slot();
      work_.notify_all();changed_.notify_all();
      wait_idle(lock);
   }
   // The images whose `done` fence may not have signalled yet, for the swapchain's destruction after retire().
   std::vector<uint32_t> pending() {
      std::lock_guard<std::mutex> lock(mutex_);
      std::vector<uint32_t> out;
      for (uint32_t k=0;k<state_.size();k++)
         if (state_[k]==Slot || state_[k]==Copying || state_[k]==Retiring) out.push_back(k);
      return out;
   }

   // ---- The presentation thread.

   // One round: wait for a frame, take a drawable, copy the newest frame into it. false: the thread ends.
   bool step() {
      {
         std::unique_lock<std::mutex> lock(mutex_);
         for (;;) {
            if (retired_ || backend_.uncertain()) return stop();
            if (slot_!=kNone) break;
            work_.wait_for(lock,std::chrono::nanoseconds(limits_.recheck_ns));
         }
      }
      // The only wait for the layer, on this thread and with nothing held.
      auto drawable=backend_.next_drawable();
      uint32_t k=kNone;
      {
         std::lock_guard<std::mutex> lock(mutex_);
         if (retired_ || backend_.uncertain()) return stop();
         if (slot_==kNone) return true;          // given up meanwhile (quiesce): the drawable goes back unpresented
         if (!drawable) {
            // The layer gave nothing within its own timeout (a hidden window): this frame is not shown, and the
            // application is held by the layer again, as before 3.0.9, until a present reaches it.
            drop_slot();
            backend_.hold();
            changed_.notify_all();
            return true;
         }
         k=slot_;slot_=kNone;state_[k]=Copying;busy_=true;
      }
      // The frame's rendering first: the copy reads the image.
      const Wait rendered=wait_rendered(k);
      const Copy result=rendered==Wait::Signalled?backend_.copy(k,drawable):Copy::Uncertain;
      std::lock_guard<std::mutex> lock(mutex_);
      busy_=false;
      if (result==Copy::Uncertain) {
         // The image stays Copying: the GPU may still write or read it.
         lose();
         return stop();
      }
      state_[k]=Free;
      if (result==Copy::Failed) backend_.hold();
      changed_.notify_all();
      return true;
   }
   // The thread ends on an exception: the swapchain presents synchronously from here on.
   void abandon() {
      std::lock_guard<std::mutex> lock(mutex_);
      running_=false;
      if (busy_) {
         busy_=false;
         for (uint32_t k=0;k<state_.size();k++)
            if (state_[k]==Copying) { state_[k]=Retiring;retiring_.push_back(k); }
      }
      drop_slot();
      backend_.hold();
      changed_.notify_all();
   }

private:
   // The whole budget for image k's rendering, without the lock.
   Wait wait_rendered(uint32_t k) {
      return wait_whole([&] { return backend_.wait_done(k,limits_.done_ns); },[&] { return backend_.woke(); });
   }
   // The rest with the lock held.
   void drop_slot() {
      if (slot_==kNone) return;
      state_[slot_]=Retiring;retiring_.push_back(slot_);slot_=kNone;
      backend_.dropped();
   }
   void wait_idle(std::unique_lock<std::mutex> &lock) {
      while (busy_ && !backend_.uncertain()) changed_.wait_for(lock,std::chrono::nanoseconds(limits_.recheck_ns));
   }
   void lose() {
      backend_.set_uncertain();
      work_.notify_all();changed_.notify_all();
   }
   bool stop() {
      running_=false;
      changed_.notify_all();
      return false;
   }

   Backend backend_;
   const NewestLimits limits_;
   std::mutex mutex_;
   std::condition_variable work_;       // the presentation thread: a frame is in the slot, or the swapchain is retired
   std::condition_variable changed_;    // everyone else: an image is free, a frame was overtaken, the copy is over
   std::vector<uint8_t> state_;
   std::vector<uint32_t> retiring_;     // oldest first
   uint32_t slot_{kNone};
   uint32_t cursor_{};
   bool busy_{};                        // the presentation thread is between taking a frame and the end of its copy
   bool retired_{};
   bool running_{true};                 // the presentation thread serves this swapchain
};

}
#endif
