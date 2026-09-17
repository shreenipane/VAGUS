#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmissing-declarations"
#include "vmlinux.h"
#pragma GCC diagnostic pop
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

struct entry_val {
    __u64 ts;
    __u64 cgid;
    __u32 vec;
    __u32 pad;
};

struct blamed_key {
    __u64 cgid;
    __u32 vec;
    __u32 pad;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key, __u32);
    __type(value, struct entry_val);
    __uint(max_entries, 1);
} entry SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 10);
} vec_ns SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key, __u32);
    __type(value, __u32);
    __uint(max_entries, 1);
} in_netrx SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __type(key, __u64);
    __type(value, __u64);
    __uint(max_entries, 4096);
} rx_pkts SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_HASH);
    __type(key, struct blamed_key);
    __type(value, __u64);
    __uint(max_entries, 16384);
} blamed_ns SEC(".maps");

SEC("tp_btf/softirq_entry")
int BPF_PROG(softirq_entry, unsigned int vec_nr)
{
    __u32 zero = 0;
    struct entry_val *e = bpf_map_lookup_elem(&entry, &zero);
    if (!e)
        return 0;

    e->ts = bpf_ktime_get_ns();
    e->cgid = bpf_get_current_cgroup_id();
    e->vec = vec_nr;
    e->pad = 0;

    if (vec_nr == NET_RX_SOFTIRQ) {
        __u32 *flag = bpf_map_lookup_elem(&in_netrx, &zero);
        if (flag)
            *flag = 1;
    }

    return 0;
}

SEC("tp_btf/softirq_exit")
int BPF_PROG(softirq_exit, unsigned int vec_nr)
{
    __u32 zero = 0;
    struct entry_val *e = bpf_map_lookup_elem(&entry, &zero);
    if (!e || e->ts == 0)
        return 0;

    __u64 now = bpf_ktime_get_ns();
    __u64 delta = now - e->ts;
    __u32 vec = e->vec;
    __u64 cgid = e->cgid;

    if (vec < 10) {
        __u64 *vns = bpf_map_lookup_elem(&vec_ns, &vec);
        if (vns)
            *vns += delta;
    }

    struct blamed_key bkey = {
        .cgid = cgid,
        .vec = vec,
        .pad = 0,
    };
    __u64 init_val = 0;
    bpf_map_update_elem(&blamed_ns, &bkey, &init_val, BPF_NOEXIST);
    __u64 *bval = bpf_map_lookup_elem(&blamed_ns, &bkey);
    if (bval)
        *bval += delta;

    if (vec == NET_RX_SOFTIRQ || vec_nr == NET_RX_SOFTIRQ) {
        __u32 *flag = bpf_map_lookup_elem(&in_netrx, &zero);
        if (flag)
            *flag = 0;
    }

    e->ts = 0;
    return 0;
}

SEC("cgroup_skb/ingress")
int ingress(struct __sk_buff *skb)
{
    __u32 zero = 0;
    __u32 *flag = bpf_map_lookup_elem(&in_netrx, &zero);
    if (flag && *flag) {
        __u64 cgid = bpf_skb_cgroup_id(skb);
        if (cgid != 0) {
            __u64 init_val = 0;
            bpf_map_update_elem(&rx_pkts, &cgid, &init_val, BPF_NOEXIST);
            __u64 *pkts = bpf_map_lookup_elem(&rx_pkts, &cgid);
            if (pkts)
                *pkts += 1;
        }
    }
    return 1;
}
