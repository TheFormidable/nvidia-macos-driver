/* SPDX-License-Identifier: MIT */
#ifndef NVK_CGL_GLD_H
#define NVK_CGL_GLD_H
/* Private GLD lifecycle interface. These arguments are observed GLI and GLD
 * contexts with the private layout observed on x86_64 macOS 25G241,
 * not application CGL handles. OS build numbers are not gated. */
int nvk_cgl_prepare_gld(void *engine, void *driver);
void nvk_cgl_destroy_gld(void *driver);
#endif
