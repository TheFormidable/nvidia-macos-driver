// nvmtl_switch_defaults.h: two of the plugin's speed switches that shipped off and are on by default from 3.0.1, each
// with one way back in the settings file. Pure, so tools/test_switch_defaults.c holds every value against these rules.
//
// NVMTL_ASYNC_TEX_UPLOAD. replaceRegion submitted its copy and waited for the GPU before it returned, so an app that
// uploads many textures per frame stalled once per upload. The asynchronous path (its own staging buffer and command
// buffer, the handle finished before the image is overwritten, read back or freed: nvmtl_tex_upload.h) has run by
// default for CPU-written IOSurface uploads outside WindowServer since 2.6.3 (NVMTL_ASYNC_SURFACE_UPLOAD). It now runs
// for every region upload outside WindowServer as well, whose uploads stay synchronous as its surfaces' do. "1" turns
// it on in any process, WindowServer included, as before; any other value turns it off (NVMTL_ASYNC_TEX_UPLOAD=0:
// replaceRegion waits for its copy, as in 3.0).
//
// NVMTL_PRETRANSLATE. A library's shaders were translated on the thread that first asked for each one, so the first run
// of a game's level stopped for every new shader. In a game (the games category or LSSupportsGameMode in its
// Info.plist, the test macOS's own Game Mode uses: nvmtl_process_is_game) every entry point of a new library is now
// translated ahead by the small low-priority pool (NVMTLLibraryLoad.m), as the Cyberpunk profile already asked for.
// Other processes are left as they were: an app that creates big framework libraries would translate all of them for
// nothing. "1" turns it on in any process, as before; any other value turns it off (NVMTL_PRETRANSLATE=0: shaders are
// translated when they are first asked for, as in 3.0).
#ifndef NVMTL_SWITCH_DEFAULTS_H
#define NVMTL_SWITCH_DEFAULTS_H
#include <string.h>

// env: the variable's value after the settings file was read (NULL when unset); progname: getprogname().
static inline int nvmtl_async_tex_upload_default(const char *env, const char *progname)
{
    if (env) return env[0] == '1';
    return !(progname && !strcmp(progname, "WindowServer"));
}

// env as above; is_game: nvmtl_process_is_game().
static inline int nvmtl_pretranslate_default(const char *env, int is_game)
{
    if (env) return !strcmp(env, "1");
    return is_game != 0;
}
#endif
