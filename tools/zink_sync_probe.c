/* SPDX-License-Identifier: MIT */
#include "util/u_queue.h"
#include "util/u_thread.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

static util_barrier barrier;
static atomic_uint serial_returns, jobs_done;
static int barrier_worker(void *unused)
{
   (void)unused;
   for (unsigned i=0;i<1000;++i)
      if (util_barrier_wait(&barrier)) atomic_fetch_add(&serial_returns,1);
   return 0;
}
static void job(void *unused,void *global,int index)
{
   (void)unused;(void)global;(void)index;
   atomic_fetch_add(&jobs_done,1);
}
int main(void)
{
   thrd_t workers[4];
   util_barrier_init(&barrier,4);
   for (unsigned i=0;i<4;++i) if(thrd_create(&workers[i],barrier_worker,NULL)!=thrd_success) return 1;
   for (unsigned i=0;i<4;++i) thrd_join(workers[i],NULL);
   util_barrier_destroy(&barrier);
   if(atomic_load(&serial_returns)!=1000) { fputs("FAIL: barrier must have exactly one serial return per generation\n",stderr);return 1; }
   struct util_queue queue;
   if(!util_queue_init(&queue,"sync-test",8,4,UTIL_QUEUE_INIT_RESIZE_IF_FULL,NULL)) return 1;
   for(unsigned i=0;i<100;++i) {
      util_queue_add_job(&queue,&queue,NULL,job,NULL,0);
      util_queue_finish(&queue);
   }
   util_queue_destroy(&queue);
   if(atomic_load(&jobs_done)!=100) return 1;
   puts("PASS: 1000 four-thread barrier generations, one serial return each; 100 queue completion/teardown cycles");
   return 0;
}
