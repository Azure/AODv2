# Build the test variant of libringbuf_shim.so.
#
# This is NOT the production .so: production lives in monitoring_tools/
# and links against the real libbpf. Here we compile the same C source
# (monitoring_tools/src/ringbuf_shim.c) against stubbed bpf/libbpf.h
# headers so Python tests can exercise the C logic without a kernel.

CC       ?= cc
SHIM_SRC := ../../monitoring_tools/src/ringbuf_shim.c
COMMON   := ../../monitoring_tools/src/common
ARCH     := $(shell uname -m | sed 's/x86_64/x86/')
TARGET   := libringbuf_shim_test.so

CFLAGS   := -fPIC -shared -g -Wall -O0
INCLUDES := -Istubs -I$(COMMON) -I$(COMMON)/$(ARCH)
LIBS     := -lpthread

$(TARGET): ringbuf_shim_test.c $(SHIM_SRC) stubs/bpf/libbpf.h stubs/bpf/bpf.h
	$(CC) $(CFLAGS) $(INCLUDES) ringbuf_shim_test.c $(SHIM_SRC) -o $@ $(LIBS)

.PHONY: clean
clean:
	rm -f $(TARGET)
