/* libbpf stubs + test-only API for unit-testing ringbuf_shim.c.
 *
 * Compiled together with monitoring_tools/src/ringbuf_shim.c into a
 * separate libringbuf_shim_test.so so Python tests can load it via
 * ctypes and drive the production C code without a real eBPF map.
 *
 * The fake ring_buffer__poll:
 *   - mirrors real libbpf's "event consumed regardless of callback
 *     return" contract so the production shim's capacity-overflow
 *     behavior (event eaten but not copied) is faithfully reproduced
 *   - is mutex-protected so a producer (test thread) and a consumer
 *     (EventDispatcher.run thread) can interleave safely
 *   - sleeps briefly when the queue is empty so the dispatcher doesn't
 *     burn CPU between bursts in the stress test
 */

#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "aod_diag.h"

struct fake_rb {
	ring_buffer_sample_fn cb;
	void *cb_ctx;
};

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

static int g_bpf_obj_get_returns = 7;
static int g_ring_buffer_new_fails = 0;
static int g_poll_returns = 0;
static int g_idle_usleep_us = 1000;

#define MAX_PENDING 8192
static struct event g_pending_events[MAX_PENDING];
static size_t g_pending_sizes[MAX_PENDING];
static int g_pending_count = 0;

/* ------------------------------------------------------------------ */
/* libbpf stubs                                                        */
/* ------------------------------------------------------------------ */
int bpf_obj_get(const char *pathname)
{
	(void)pathname;
	return g_bpf_obj_get_returns;
}

struct ring_buffer *ring_buffer__new(int map_fd,
				     ring_buffer_sample_fn cb,
				     void *ctx,
				     const struct ring_buffer_opts *opts)
{
	(void)map_fd;
	(void)opts;
	if (g_ring_buffer_new_fails)
		return NULL;
	struct fake_rb *rb = calloc(1, sizeof(*rb));
	if (!rb)
		return NULL;
	rb->cb = cb;
	rb->cb_ctx = ctx;
	return (struct ring_buffer *)rb;
}

int ring_buffer__poll(struct ring_buffer *rb_, int timeout_ms)
{
	(void)timeout_ms;
	struct fake_rb *rb = (struct fake_rb *)rb_;

	pthread_mutex_lock(&g_lock);
	int n = g_pending_count;
	if (n == 0) {
		int idle = g_idle_usleep_us;
		int rc_idle = g_poll_returns;
		pthread_mutex_unlock(&g_lock);
		if (idle > 0)
			usleep(idle);
		return rc_idle;
	}
	/* Snapshot under lock so producers can keep appending. */
	struct event snap_events[MAX_PENDING];
	size_t snap_sizes[MAX_PENDING];
	memcpy(snap_events, g_pending_events, n * sizeof(struct event));
	memcpy(snap_sizes, g_pending_sizes, n * sizeof(size_t));
	pthread_mutex_unlock(&g_lock);

	int consumed = 0;
	for (int i = 0; i < n; ++i) {
		int r = rb->cb(rb->cb_ctx, &snap_events[i], snap_sizes[i]);
		consumed++;
		if (r < 0)
			break;
	}

	pthread_mutex_lock(&g_lock);
	int remaining = g_pending_count - consumed;
	if (remaining > 0) {
		memmove(g_pending_events, &g_pending_events[consumed],
			remaining * sizeof(struct event));
		memmove(g_pending_sizes, &g_pending_sizes[consumed],
			remaining * sizeof(size_t));
	}
	g_pending_count = remaining;
	int rc = g_poll_returns;
	pthread_mutex_unlock(&g_lock);
	return rc;
}

void ring_buffer__free(struct ring_buffer *rb_)
{
	free(rb_);
}

/* ------------------------------------------------------------------ */
/* Test-only API (called from Python via ctypes)                       */
/* ------------------------------------------------------------------ */
void test_set_bpf_obj_get_returns(int v) { g_bpf_obj_get_returns = v; }
void test_set_ring_buffer_new_fails(int v) { g_ring_buffer_new_fails = v; }

void test_set_poll_returns(int v)
{
	pthread_mutex_lock(&g_lock);
	g_poll_returns = v;
	pthread_mutex_unlock(&g_lock);
}

void test_reset(void)
{
	pthread_mutex_lock(&g_lock);
	g_bpf_obj_get_returns = 7;
	g_ring_buffer_new_fails = 0;
	g_poll_returns = 0;
	g_idle_usleep_us = 1000;
	g_pending_count = 0;
	pthread_mutex_unlock(&g_lock);
}

int test_queue_event(const struct event *e, size_t size)
{
	pthread_mutex_lock(&g_lock);
	if (g_pending_count >= MAX_PENDING) {
		pthread_mutex_unlock(&g_lock);
		return 0;
	}
	memcpy(&g_pending_events[g_pending_count], e, sizeof(struct event));
	g_pending_sizes[g_pending_count] = size;
	g_pending_count++;
	pthread_mutex_unlock(&g_lock);
	return 1;
}

int test_pending_count(void)
{
	pthread_mutex_lock(&g_lock);
	int n = g_pending_count;
	pthread_mutex_unlock(&g_lock);
	return n;
}

size_t test_sizeof_event(void) { return sizeof(struct event); }
