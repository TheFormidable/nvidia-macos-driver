/* Crash handlers for a program of tools/ that tools/card/checks_zink.py runs as it is (tools/glbench/glbench.c): the
 * row's build adds "-include tools/card/zink_crash_guard.h", so the program's own file stays unchanged.
 *
 * A constructor installs handlers for SIGSEGV, SIGBUS, SIGILL, SIGFPE and SIGABRT on their own stack. Each writes
 * "CRASH <signal>" and a backtrace to standard error and ends the process with exit status 70, as the new programs of
 * tools/card do (zink_common.h), so that a crash ends as an exit code that the runner reports as CRASH and macOS writes
 * no crash report (believed; the first run tells). There is no alarm here: the row's limit kills a program that hangs,
 * and the runner reports that as a time-out. Not yet run on a Mac. */
#ifndef ZINK_CRASH_GUARD_H
#define ZINK_CRASH_GUARD_H

#include <execinfo.h>
#include <signal.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t zk_guard_crashing;

static void zk_guard_write(const char *s)
{
   size_t left = strlen(s);
   while (left) {
      ssize_t n = write(STDERR_FILENO, s, left);
      if (n <= 0) return;
      s += n;
      left -= (size_t)n;
   }
}

static void zk_guard_on_crash(int sig)
{
   if (zk_guard_crashing) _exit(70);
   zk_guard_crashing = 1;
   const char *name = sig == SIGSEGV ? "SIGSEGV" : sig == SIGBUS ? "SIGBUS" : sig == SIGILL ? "SIGILL"
                    : sig == SIGFPE ? "SIGFPE" : sig == SIGABRT ? "SIGABRT" : "signal";
   zk_guard_write("CRASH ");
   zk_guard_write(name);
   zk_guard_write("\n");
   void *frames[64];
   int count = backtrace(frames, 64);
   backtrace_symbols_fd(frames, count, STDERR_FILENO);
   _exit(70);
}

__attribute__((constructor)) static void zk_guard_install(void)
{
   static char stack[1 << 16];
   stack_t alt;
   memset(&alt, 0, sizeof(alt));
   alt.ss_sp = stack;
   alt.ss_size = sizeof(stack);
   sigaltstack(&alt, NULL);
   void *warm[4];
   (void)backtrace(warm, 4); /* binds backtrace's lazy symbols now, outside a signal handler */
   const int sigs[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
   for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); ++i) {
      struct sigaction sa;
      memset(&sa, 0, sizeof(sa));
      sa.sa_handler = zk_guard_on_crash;
      sa.sa_flags = SA_ONSTACK;
      sigemptyset(&sa.sa_mask);
      sigaction(sigs[i], &sa, NULL);
   }
}

#endif
