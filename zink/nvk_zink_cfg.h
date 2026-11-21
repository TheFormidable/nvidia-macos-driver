/* SPDX-License-Identifier: MIT */
#ifndef NVK_ZINK_CFG_H
#define NVK_ZINK_CFG_H
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The frontend's switches follow the rule the driver documents for a way back: a line "NAME=0" of the settings file,
 * or the process's own environment when that process set NVMTL_PROCESS_ENV=1 (plugin/nvmtl_env_knobs.h: exactly 1,
 * and never in a process that runs as root or setuid). A variable left behind in a shell does not change how a game
 * started from it presents, whether or not the Metal plugin has loaded into the process yet. As NVK's reader does
 * (nvmtl_cfg.h in its tree), the first 8191 bytes of the file are read and the last line that names the switch wins.
 * A line names it when the name is the first thing on the line; blanks before the name, around the "=" and before
 * the value are skipped. The Metal plugin's reader of the same file (plugin/NVMTLObjects.m) skips them before the
 * name and before the "=", and keeps a blank that follows the "=" as part of the value: every spelling that turns a
 * switch of the plugin off turns one of these off, and "NAME= 0" does so here only. A value from the process's own
 * environment has its leading blanks skipped the same way. A switch is on unless its value begins with 0. */
#ifndef NVK_ZINK_CFG_PATH
#define NVK_ZINK_CFG_PATH "/Library/GPUBundles/nvmtl/nvrm610.conf"
#endif

/* text: the file's first bytes, NUL-terminated. process_env: this process keeps its own variables. 1 = on. */
static inline int nvk_zink_cfg_on_in(const char *text, const char *name, int process_env)
{
   const char *value = process_env ? getenv(name) : NULL;
   if (!value) {
      const size_t length = strlen(name);
      for (const char *hit = strstr(text, name); hit; hit = strstr(hit + 1, name)) {
         const char *first = hit, *after = hit + length;
         while (first > text && (first[-1] == ' ' || first[-1] == '\t')) --first;
         while (*after == ' ' || *after == '\t') ++after;
         if ((first == text || first[-1] == '\n') && *after == '=') value = after + 1;
      }
   }
   while (value && (*value == ' ' || *value == '\t')) ++value;
   return !(value && value[0] == '0');
}

/* Not for two threads at once: its one caller reads the switches once, under pthread_once. */
static inline int nvk_zink_cfg_on(const char *name)
{
   static char text[8192];
   static int loaded, process_env;
   if (!loaded) {
      FILE *file = fopen(NVK_ZINK_CFG_PATH, "r");
      if (file) { text[fread(text, 1, sizeof text - 1, file)] = 0; fclose(file); }
      const char *own = getenv("NVMTL_PROCESS_ENV");
      process_env = own && !strcmp(own, "1") && !issetugid() && geteuid() != 0;
      loaded = 1;
   }
   return nvk_zink_cfg_on_in(text, name, process_env);
}
#endif
