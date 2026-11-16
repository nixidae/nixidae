/*
 * krun-router: one lab guest under libkrun (E2). Not vivarium-krun: no
 * disk, no store share, no agent. The guest's root is the initrd.
 *
 *   krun-router VMLINUX INITRD CMDLINE CPUS MIB NET_FD [VSOCK_PORT VSOCK_PATH]
 *
 * NET_FD is a unixgram socket that carries whole Ethernet frames. A guest
 * connection to vsock CID 2, VSOCK_PORT becomes a connection to the unix
 * socket VSOCK_PATH on the host. libkrun listens on IN_PATH, and a host
 * connection there becomes one to the guest's vsock IN_PORT.
 */

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include <libkrun.h>

/* As vivarium-krun.c: receive offloads, so the driver posts 64 KiB buffers. */
#define NET_FEATURES ((1u << 1) | (1u << 7) | (1u << 8))

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
        fprintf(stderr, "krun-router: %s: ", #call);      \
        krun_error_message(err, &stderr_writer);          \
        fputc('\n', stderr);                              \
        return 1;                                         \
    }

int main(int argc, char *argv[])
{
    if (argc != 7 && argc != 9 && argc != 11) {
        fprintf(stderr, "usage: krun-router VMLINUX INITRD CMDLINE CPUS MIB NET_FD [VSOCK_PORT VSOCK_PATH [IN_PORT IN_PATH]]\n");
        return 2;
    }
    const char *vmlinux = argv[1], *initrd = argv[2], *cmdline = argv[3];
    int cpus = atoi(argv[4]), mib = atoi(argv[5]), net_fd = atoi(argv[6]);
    uint8_t mac[6] = {0x02, 0x00, 0x00, 0x00, 0x00, (uint8_t)getpid()};

    KrunError err;
    CHECK(krun_init_log(STDERR_FILENO, KRUN_LOG_LEVEL_ERROR, KRUN_LOG_STYLE_AUTO, 0, &err));
    KrunMmioDeviceManager devices = krun_mmio_device_manager_new();

    CHECK(KrunNetDevice net =
              krun_net_device_new_unixgram_fd(KRUN_STR("lan0"), net_fd, KRUN_BYTES(mac), NET_FEATURES, 0, &err));
    krun_mmio_device_manager_add(devices, net);

    if (argc >= 9) {
        CHECK(KrunVsockDevice vsock = krun_vsock_device_new(3, 0, &err));
        krun_vsock_device_add_unix_port(vsock, (uint32_t)atoi(argv[7]), KRUN_STR(argv[8]), false);
        if (argc == 11)
            krun_vsock_device_add_unix_port(vsock, (uint32_t)atoi(argv[9]), KRUN_STR(argv[10]), true);
        krun_mmio_device_manager_add(devices, vsock);
    }

    CHECK(KrunPayload payload =
              krun_payload_load_external(KRUN_STR(vmlinux), KRUN_KERNEL_FORMAT_ELF, KRUN_STR(initrd), KRUN_STR(cmdline), &err));
    KrunVmmBuilder builder = krun_vmm_builder_new();
    CHECK(krun_vmm_builder_vcpus(&builder, (uint8_t)cpus, &err));
    CHECK(krun_vmm_builder_ram_mib(&builder, (uint32_t)mib, &err));
    krun_vmm_builder_payload(&builder, payload);
    krun_vmm_builder_devices(&builder, devices);

    /* libkrun stops the guest when the serial input reaches end of file. */
    int console_in[2];
    if (pipe(console_in) != 0) {
        perror("krun-router: pipe");
        return 1;
    }
    CHECK(krun_vmm_builder_add_serial_console(&builder, console_in[0], STDOUT_FILENO, &err));
    CHECK(KrunVmm vmm = krun_vmm_builder_build(&builder, &err));
    krun_vmm_run(vmm);
    return 1;
}
