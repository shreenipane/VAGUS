BPFTOOL ?= $(shell command -v bpftool || echo /usr/sbin/bpftool)
CLANG ?= clang
CC ?= cc

.PHONY: all clean

all: attrib

vmlinux.h:
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h

attrib.bpf.o: attrib.bpf.c vmlinux.h
	$(CLANG) -g -O2 -target bpf -D__TARGET_ARCH_x86 -c attrib.bpf.c -o attrib.bpf.o

attrib.skel.h: attrib.bpf.o
	$(BPFTOOL) gen skeleton attrib.bpf.o > attrib.skel.h

attrib: attrib.c attrib.skel.h
	$(CC) -O2 -Wall -Wextra attrib.c -lbpf -lelf -lz -o attrib

clean:
	rm -f vmlinux.h attrib.bpf.o attrib.skel.h attrib
