/* Stub <bpf/libbpf.h> for test builds of ringbuf_shim.c.
 *
 * Picked up ahead of the system header via -Itests/c/stubs so the
 * production C file links against our fakes instead of real libbpf.
 * Only the symbols ringbuf_shim.c actually uses are declared.
 */
#ifndef AODV2_TEST_BPF_LIBBPF_H
#define AODV2_TEST_BPF_LIBBPF_H

#include <stddef.h>
#include <linux/types.h>

struct ring_buffer;
struct ring_buffer_opts { size_t sz; };

typedef int (*ring_buffer_sample_fn)(void *ctx, void *data, size_t size);

struct ring_buffer *ring_buffer__new(int map_fd,
                                     ring_buffer_sample_fn sample_cb,
                                     void *ctx,
                                     const struct ring_buffer_opts *opts);
int ring_buffer__poll(struct ring_buffer *rb, int timeout_ms);
void ring_buffer__free(struct ring_buffer *rb);

#endif
