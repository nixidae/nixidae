/*
 * vivarium-krun: one libkrun guest, in this process.
 *
 *   vivarium-krun --vmlinux PATH --initrd PATH --cmdline TEXT --cpus N --memory MIB
 *                 --disk QCOW2 --store DIR --dax-window BYTES --agent-fd FD
 *                 --uplink FD,MAC [--lan FD,MAC]... [--artifacts DIR] [--nested] [--acpi]
 *
 * libkrun takes the process over: the guest's reset ends it with the exit
 * code the guest last set (vivarium_runner.backend.KrunExit). The kernel's
 * console is a serial port on this process's stdout, which the runner reads
 * as it reads QEMU's -serial stdio. VIVARIUM_KRUN_LOG_LEVEL is libkrun's log
 * level: 0 off to 5 trace, errors by default.
 *
 * A C program and not Python over ctypes: libkrun 2.0's C API passes structs
 * by value, and on macOS the process that calls it carries the hypervisor
 * entitlement, which an interpreter does not (nixidae/nixidae#1163).
 */

#include <errno.h>
#include <getopt.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libkrun.h>

/*
 * Receive offloads only. libkrun advertises neither VIRTIO_NET_F_MTU nor
 * mergeable receive buffers, so without a GUEST_TSO feature the driver posts
 * 1518-byte receive buffers and drops every frame above that: passt's 65520
 * and a segment's 65000. With one, it posts 64 KiB "big packets". No transmit
 * offload: the virtio-net header does not cross a segment or passt, so a
 * guest must send whole frames with their checksums done.
 */
/* Feature bit numbers from the virtio specification; libkrun's net device
 * takes the bits as they are, and its header names none of them. */
#define VIRTIO_NET_F_GUEST_CSUM 1
#define VIRTIO_NET_F_GUEST_TSO4 7
#define VIRTIO_NET_F_GUEST_TSO6 8
#define NET_FEATURES ((1u << VIRTIO_NET_F_GUEST_CSUM) | (1u << VIRTIO_NET_F_GUEST_TSO4) | (1u << VIRTIO_NET_F_GUEST_TSO6))
#define AGENT_PORT "vivarium-agent"
#define MAX_LAN 16

static bool push_to_stderr(void *userdata, KrunStr s)
{
    (void)userdata;
    fwrite(s.data, 1, s.len, stderr);
    return true;
}

static KrunVtableHandle stderr_writer =
    KRUN_VTABLE_HANDLE(KRUN_PUSH_STR_TYPE_TAG, ((KrunPushStrVtable){.drop = NULL, .push = push_to_stderr}), NULL);

#define CHECK(call)                                       \
    err = NULL;                                           \
    call;                                                 \
    if (err) {                                            \
        fprintf(stderr, "vivarium-krun: %s: ", #call);    \
        krun_error_message(err, &stderr_writer);          \
        fputc('\n', stderr);                              \
        return 1;                                         \
    }

struct nic {
    int fd;
    uint8_t mac[6];
};

static bool parse_long(const char *text, long min, long max, long *out)
{
    char *end;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno || *end != '\0' || end == text || value < min || value > max)
        return false;
    *out = value;
    return true;
}

/* FD,MAC with MAC as six hex octets separated by colons. */
static bool parse_nic(char *arg, struct nic *nic)
{
    char *comma = strchr(arg, ',');
    long fd;
    if (comma == NULL)
        return false;
    *comma = '\0';
    if (!parse_long(arg, 0, INT32_MAX, &fd))
        return false;
    nic->fd = (int)fd;
    unsigned int octets[6];
    char tail;
    if (sscanf(comma + 1, "%2x:%2x:%2x:%2x:%2x:%2x%c", &octets[0], &octets[1], &octets[2], &octets[3],
               &octets[4], &octets[5], &tail) != 6)
        return false;
    for (int i = 0; i < 6; i++)
        nic->mac[i] = (uint8_t)octets[i];
    return true;
}

static void usage(void)
{
    fprintf(stderr,
            "usage: vivarium-krun --vmlinux PATH --initrd PATH --cmdline TEXT --cpus N --memory MIB\n"
            "                     --disk QCOW2 --store DIR --dax-window BYTES --agent-fd FD\n"
            "                     --uplink FD,MAC [--lan FD,MAC]... [--artifacts DIR] [--nested] [--acpi]\n");
}

int main(int argc, char *argv[])
{
    enum { VMLINUX = 256, INITRD, CMDLINE, CPUS, MEMORY, DISK, STORE, DAX, AGENT, UPLINK, LAN, ARTIFACTS, NESTED, ACPI };
    static const struct option options[] = {
        {"vmlinux", required_argument, NULL, VMLINUX},
        {"initrd", required_argument, NULL, INITRD},
        {"cmdline", required_argument, NULL, CMDLINE},
        {"cpus", required_argument, NULL, CPUS},
        {"memory", required_argument, NULL, MEMORY},
        {"disk", required_argument, NULL, DISK},
        {"store", required_argument, NULL, STORE},
        {"dax-window", required_argument, NULL, DAX},
        {"agent-fd", required_argument, NULL, AGENT},
        {"uplink", required_argument, NULL, UPLINK},
        {"lan", required_argument, NULL, LAN},
        {"artifacts", required_argument, NULL, ARTIFACTS},
        {"nested", no_argument, NULL, NESTED},
        {"acpi", no_argument, NULL, ACPI},
        {NULL, 0, NULL, 0},
    };
    const char *vmlinux = NULL, *initrd = NULL, *cmdline = NULL, *disk = NULL, *store = NULL, *artifacts = NULL;
    long cpus = 0, memory = 0, dax_window = -1, agent_fd = -1;
    bool nested = false, acpi = false, have_uplink = false;
    struct nic uplink = {0};
    struct nic lan[MAX_LAN] = {0};
    int nlan = 0, c;

    while ((c = getopt_long(argc, argv, "", options, NULL)) != -1) {
        bool ok = true;
        switch (c) {
        case VMLINUX: vmlinux = optarg; break;
        case INITRD: initrd = optarg; break;
        case CMDLINE: cmdline = optarg; break;
        case DISK: disk = optarg; break;
        case STORE: store = optarg; break;
        case ARTIFACTS: artifacts = optarg; break;
        case CPUS: ok = parse_long(optarg, 1, 255, &cpus); break;
        case MEMORY: ok = parse_long(optarg, 1, UINT32_MAX, &memory); break;
        case DAX: ok = parse_long(optarg, 0, INT64_MAX, &dax_window); break;
        case AGENT: ok = parse_long(optarg, 0, INT32_MAX, &agent_fd); break;
        case UPLINK: ok = have_uplink = parse_nic(optarg, &uplink); break;
        case LAN: ok = nlan < MAX_LAN && parse_nic(optarg, &lan[nlan++]); break;
        case NESTED: nested = true; break;
        case ACPI: acpi = true; break;
        default: ok = false; break;
        }
        if (!ok) {
            usage();
            return 2;
        }
    }
    if (optind != argc || !vmlinux || !initrd || !cmdline || !disk || !store || !cpus || !memory || dax_window < 0 ||
        agent_fd < 0 || !have_uplink) {
        usage();
        return 2;
    }

    long level = KRUN_LOG_LEVEL_ERROR;
    const char *level_env = getenv("VIVARIUM_KRUN_LOG_LEVEL");
    if (level_env != NULL && !parse_long(level_env, KRUN_LOG_LEVEL_OFF, KRUN_LOG_LEVEL_TRACE, &level)) {
        fprintf(stderr, "vivarium-krun: VIVARIUM_KRUN_LOG_LEVEL is 0 to 5, not %s\n", level_env);
        return 2;
    }

    KrunError err;
    CHECK(krun_init_log(STDERR_FILENO, (uint32_t)level, KRUN_LOG_STYLE_AUTO, 0, &err));

    KrunMmioDeviceManager devices = krun_mmio_device_manager_new();

    /* The agent's channel. A port that is not a console never becomes hvc0;
     * udev links it as /dev/virtio-ports/<name>, which modules/qemu.nix names. */
    {
        KrunConsoleBuilder cb = krun_console_device_builder();
        uint32_t port;
        CHECK(krun_console_builder_add_inout_port(cb, KRUN_STR(AGENT_PORT), (int)agent_fd, (int)agent_fd, &port, &err));
        CHECK(KrunConsoleDevice console = krun_console_builder_build(cb, &err));
        krun_mmio_device_manager_add(devices, console);
    }

    /* The store is read-only and no path in it ever changes, so the guest's
     * cache of it never goes stale: Always. Auto never keeps the cache, and a
     * DAX mapping in use dropped on open is a SIGBUS (smol-machines/smolvm#1741). */
    {
        CHECK(KrunFsDevice fs = krun_fs_device_new_read_only(KRUN_STR("nix"), KRUN_STR(store), &err));
        krun_fs_device_set_dax_window_size(fs, (uint64_t)dax_window);
        CHECK(krun_fs_device_set_cache_policy(fs, KRUN_CACHE_POLICY_ALWAYS, &err));
        krun_mmio_device_manager_add(devices, fs);
    }
    if (artifacts != NULL) {
        CHECK(KrunFsDevice fs = krun_fs_device_new(KRUN_STR("artifacts"), KRUN_STR(artifacts), &err));
        krun_mmio_device_manager_add(devices, fs);
    }

    CHECK(KrunBlockDevice blk = krun_block_device_new(KRUN_STR("root"), KRUN_STR(disk), KRUN_DISK_FORMAT_QCOW2, &err));
    krun_mmio_device_manager_add(devices, blk);

    CHECK(KrunNetDevice up = krun_net_device_new_unixstream_fd(KRUN_STR("uplink"), uplink.fd, KRUN_BYTES(uplink.mac),
                                                               NET_FEATURES, 0, &err));
    krun_mmio_device_manager_add(devices, up);
    for (int i = 0; i < nlan; i++) {
        char id[16];
        snprintf(id, sizeof id, "lan%d", i);
        CHECK(KrunNetDevice nic = krun_net_device_new_unixgram_fd(KRUN_STR(id), lan[i].fd, KRUN_BYTES(lan[i].mac),
                                                                  NET_FEATURES, 0, &err));
        krun_mmio_device_manager_add(devices, nic);
    }

    CHECK(KrunRngDevice rng = krun_rng_device_new(&err));
    krun_mmio_device_manager_add(devices, rng);
    /* Free page reporting: the guest's freed pages go back to the host. */
    CHECK(KrunBalloonDevice balloon = krun_balloon_device_new(&err));
    krun_mmio_device_manager_add(devices, balloon);

    CHECK(KrunPayload payload = krun_payload_load_external(KRUN_STR(vmlinux), KRUN_KERNEL_FORMAT_ELF, KRUN_STR(initrd),
                                                           KRUN_STR(cmdline), &err));

    KrunVmmBuilder builder = krun_vmm_builder_new();
    CHECK(krun_vmm_builder_vcpus(&builder, (uint8_t)cpus, &err));
    CHECK(krun_vmm_builder_ram_mib(&builder, (uint32_t)memory, &err));
    krun_vmm_builder_payload(&builder, payload);
    krun_vmm_builder_devices(&builder, devices);
    krun_vmm_builder_nested_virt(&builder, nested);
    if (acpi) {
        CHECK(krun_vmm_builder_acpi(&builder, true, &err));
    }

    /* The runner's stdin is /dev/null, and libkrun stops the guest when the
     * serial port's input reaches end of file (measured on 1.x). A pipe whose
     * write end this process holds never does. */
    int console_in[2];
    if (pipe(console_in) != 0) {
        perror("vivarium-krun: pipe");
        return 1;
    }
    CHECK(krun_vmm_builder_add_serial_console(&builder, console_in[0], STDOUT_FILENO, &err));

    CHECK(KrunVmm vmm = krun_vmm_builder_build(&builder, &err));
    krun_vmm_run(vmm);
    fprintf(stderr, "vivarium-krun: the VMM's event loop ended\n");
    return 1;
}
