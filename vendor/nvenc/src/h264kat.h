// h264kat.h: a known-answer test of the H.264 decoder on the card it is about to serve.
//
// The decoder drives Blackwell's NVDEC (class 0xcfb0) with the method list and the picture setup that are published
// for Ada, and reads its planes in a layout taken from Mesa. No public header says that this class accepts them, and
// VideoToolbox does not fall back once it has picked a decoder, so a decoder that registers and then gives wrong
// pictures is worse than none. The card can answer the question itself: H.264 decoding is exact by specification, so
// a few embedded pictures (h264kat_vectors.h) either come back with every byte right or they do not. The decoder
// registers only where they did.
#pragma once
#include "rm.h"
#include "h264kat_core.h"

// Decodes every embedded stream on rm's card with this library's own decoder (h264dec_open, _nal, _flush, _pop,
// _read_nv12, _release: what a VideoToolbox session calls) and requires, for each stream: every frame it decodes to
// given back exactly once (in any order), nothing else given back, no macroblock in error and no error status from
// the engine, all within five seconds. rm is an open client (rm_open); its address space is made here if it has none.
// Returns 0 when everything held, -1 otherwise; *r says what was seen either way. A frame whose bytes are wrong is
// read again in the other known plane layouts, and the result names the one in which it is right: that is for the
// log, the decoder goes on reading as it did.
int h264dec_selftest(Rm *rm, KatResult *r);
// The same for a caller's n streams and time limit; h264dec_selftest is this with the embedded streams and five
// seconds. tools/test_h264_kat_selftest.py runs it against an engine it plays itself.
int h264dec_selftest_streams(Rm *rm, const KatVector *v, unsigned n, unsigned limit_ms, KatResult *r);

// The verdict for this process: the first call opens its own RM client, runs the self-test, logs one line through
// syslog (found with: log show --predicate 'eventMessage CONTAINS "decode self-test"') and closes the client; later
// calls return the same verdict at once, and a caller that arrives while the test runs waits for it. card names the
// generation in the line ("Blackwell"). line, when not NULL, receives the line (n bytes at most, H264KAT_LINE holds
// all of it); *first, when not NULL, says whether this call was the one that ran the test.
enum { H264KAT_LINE = KAT_LINE + 160 };
bool h264kat_proven(const char *card, char *line, size_t n, bool *first);
