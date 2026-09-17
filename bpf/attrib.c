#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>
#include <getopt.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include "attrib.skel.h"

struct blamed_key {
    __u64 cgid;
    __u32 vec;
    __u32 pad;
};

#define BLAMED_TABLE_SIZE 32768

struct blamed_entry {
    __u64 cgid;
    __u64 vec[10];
    bool used;
};

static struct blamed_entry *get_blamed_entry(struct blamed_entry *table, __u64 cgid)
{
    __u32 mask = BLAMED_TABLE_SIZE - 1;
    __u64 h = cgid;
    h ^= h >> 33;
    h *= 0xff51afd7ed558ccdULL;
    h ^= h >> 33;
    __u32 idx = (__u32)h & mask;

    while (table[idx].used) {
        if (table[idx].cgid == cgid)
            return &table[idx];
        idx = (idx + 1) & mask;
    }
    table[idx].used = true;
    table[idx].cgid = cgid;
    memset(table[idx].vec, 0, sizeof(table[idx].vec));
    return &table[idx];
}

static volatile sig_atomic_t stop = 0;

static void sig_handler(int sig)
{
    (void)sig;
    stop = 1;
}

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;
    return vfprintf(stderr, format, args);
}

static void print_usage_and_exit(const char *progname)
{
    fprintf(stderr, "Usage: %s [--interval N]\n", progname);
    exit(2);
}

int main(int argc, char **argv)
{
    int interval = 5;
    static const struct option long_options[] = {
        {"interval", required_argument, NULL, 'i'},
        {NULL, 0, NULL, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
        switch (opt) {
        case 'i': {
            char *endptr = NULL;
            long val = strtol(optarg, &endptr, 10);
            if (*optarg == '\0' || *endptr != '\0' || val < 1 || val > 60) {
                print_usage_and_exit(argv[0]);
            }
            interval = (int)val;
            break;
        }
        default:
            print_usage_and_exit(argv[0]);
        }
    }
    if (optind < argc) {
        print_usage_and_exit(argv[0]);
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = sig_handler;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    libbpf_set_print(libbpf_print_fn);

    struct attrib_bpf *skel = attrib_bpf__open();
    if (!skel) {
        fprintf(stderr, "Failed to open BPF skeleton\n");
        return 1;
    }

    int err = attrib_bpf__load(skel);
    if (err) {
        fprintf(stderr, "Failed to load BPF skeleton: %d\n", err);
        attrib_bpf__destroy(skel);
        return 1;
    }

    skel->links.softirq_entry = bpf_program__attach(skel->progs.softirq_entry);
    if (!skel->links.softirq_entry) {
        fprintf(stderr, "Failed to attach softirq_entry: %s\n", strerror(errno));
        attrib_bpf__destroy(skel);
        return 1;
    }

    skel->links.softirq_exit = bpf_program__attach(skel->progs.softirq_exit);
    if (!skel->links.softirq_exit) {
        fprintf(stderr, "Failed to attach softirq_exit: %s\n", strerror(errno));
        attrib_bpf__destroy(skel);
        return 1;
    }

    int cgroup_fd = open("/sys/fs/cgroup", O_RDONLY | O_DIRECTORY);
    if (cgroup_fd < 0) {
        fprintf(stderr, "Failed to open /sys/fs/cgroup: %s\n", strerror(errno));
        attrib_bpf__destroy(skel);
        return 1;
    }

    skel->links.ingress = bpf_program__attach_cgroup(skel->progs.ingress, cgroup_fd);
    if (!skel->links.ingress) {
        fprintf(stderr, "Failed to attach ingress: %s\n", strerror(errno));
        close(cgroup_fd);
        attrib_bpf__destroy(skel);
        return 1;
    }

    int ncpu = libbpf_num_possible_cpus();
    if (ncpu <= 0) {
        fprintf(stderr, "Invalid number of possible CPUs: %d\n", ncpu);
        close(cgroup_fd);
        attrib_bpf__destroy(skel);
        return 1;
    }

    int fd_vec = bpf_map__fd(skel->maps.vec_ns);
    int fd_rx = bpf_map__fd(skel->maps.rx_pkts);
    int fd_blamed = bpf_map__fd(skel->maps.blamed_ns);

    if (fd_vec < 0 || fd_rx < 0 || fd_blamed < 0) {
        fprintf(stderr, "Failed to get map file descriptors\n");
        close(cgroup_fd);
        attrib_bpf__destroy(skel);
        return 1;
    }

    __u64 *vec_buf = malloc((size_t)ncpu * sizeof(__u64));
    __u64 *rx_buf = malloc((size_t)ncpu * sizeof(__u64));
    __u64 *blamed_val_buf = malloc((size_t)ncpu * sizeof(__u64));
    struct blamed_entry *blamed_table = calloc(BLAMED_TABLE_SIZE, sizeof(*blamed_table));

    if (!vec_buf || !rx_buf || !blamed_val_buf || !blamed_table) {
        fprintf(stderr, "Memory allocation failed\n");
        free(vec_buf);
        free(rx_buf);
        free(blamed_val_buf);
        free(blamed_table);
        close(cgroup_fd);
        attrib_bpf__destroy(skel);
        return 1;
    }

    while (!stop) {
        unsigned int remaining = (unsigned int)interval;
        while (remaining > 0 && !stop) {
            remaining = sleep(remaining);
        }
        if (stop)
            break;

        struct timespec ts;
        if (clock_gettime(CLOCK_REALTIME, &ts) != 0) {
            fprintf(stderr, "clock_gettime failed: %s\n", strerror(errno));
            break;
        }

        long ms = (ts.tv_nsec + 500000L) / 1000000L;
        long long sec = (long long)ts.tv_sec;
        if (ms >= 1000) {
            sec++;
            ms -= 1000;
        }

        printf("{\"ts\": %lld.%03ld, \"ncpu\": %d, ", sec, ms, ncpu);

        /* vec_ns */
        printf("\"vec_ns\": [");
        for (__u32 v = 0; v < 10; v++) {
            if (v > 0)
                printf(", ");
            if (bpf_map_lookup_elem(fd_vec, &v, vec_buf) != 0) {
                memset(vec_buf, 0, (size_t)ncpu * sizeof(__u64));
            }
            printf("[");
            for (int c = 0; c < ncpu; c++) {
                if (c > 0)
                    printf(", ");
                printf("%llu", (unsigned long long)vec_buf[c]);
            }
            printf("]");
        }
        printf("], ");

        /* rx_pkts */
        printf("\"rx_pkts\": {");
        bool first_rx = true;
        __u64 rx_key, next_rx_key;
        const void *cur_rx_ptr = NULL;
        while (bpf_map_get_next_key(fd_rx, cur_rx_ptr, &next_rx_key) == 0) {
            rx_key = next_rx_key;
            cur_rx_ptr = &rx_key;
            if (bpf_map_lookup_elem(fd_rx, &rx_key, rx_buf) == 0) {
                if (!first_rx)
                    printf(", ");
                first_rx = false;
                printf("\"%llu\": [", (unsigned long long)rx_key);
                for (int c = 0; c < ncpu; c++) {
                    if (c > 0)
                        printf(", ");
                    printf("%llu", (unsigned long long)rx_buf[c]);
                }
                printf("]");
            }
        }
        printf("}, ");

        /* blamed_ns */
        memset(blamed_table, 0, BLAMED_TABLE_SIZE * sizeof(*blamed_table));
        struct blamed_key bkey, next_bkey;
        const void *cur_bkey_ptr = NULL;
        while (bpf_map_get_next_key(fd_blamed, cur_bkey_ptr, &next_bkey) == 0) {
            bkey = next_bkey;
            cur_bkey_ptr = &bkey;
            if (bpf_map_lookup_elem(fd_blamed, &bkey, blamed_val_buf) == 0) {
                if (bkey.vec < 10) {
                    __u64 sum = 0;
                    for (int c = 0; c < ncpu; c++) {
                        sum += blamed_val_buf[c];
                    }
                    struct blamed_entry *be = get_blamed_entry(blamed_table, bkey.cgid);
                    be->vec[bkey.vec] += sum;
                }
            }
        }

        printf("\"blamed_ns\": {");
        bool first_blamed = true;
        for (size_t i = 0; i < BLAMED_TABLE_SIZE; i++) {
            if (blamed_table[i].used) {
                if (!first_blamed)
                    printf(", ");
                first_blamed = false;
                printf("\"%llu\": [", (unsigned long long)blamed_table[i].cgid);
                for (int v = 0; v < 10; v++) {
                    if (v > 0)
                        printf(", ");
                    printf("%llu", (unsigned long long)blamed_table[i].vec[v]);
                }
                printf("]");
            }
        }
        printf("}}\n");
        fflush(stdout);
    }

    free(vec_buf);
    free(rx_buf);
    free(blamed_val_buf);
    free(blamed_table);
    close(cgroup_fd);
    attrib_bpf__destroy(skel);

    return stop ? 0 : 1;
}
