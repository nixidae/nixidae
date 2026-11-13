// Zero the guest's free pages, so the saved RAM is sparse where nothing lives.
//
//   zerofree [MIB]
//
// Touches MIB of anonymous memory (default: the free pages less 64 MiB), then
// exits. The kernel zero-fills each page on first touch and puts it back on the
// free list zeroed. QEMU's migration skips a zero page, so ram.bin gets a hole.
// More than the free pages evicts file cache to make room.
#include <mach/mach.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

int main(int argc, char **argv) {
    vm_statistics64_data_t vm;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t)&vm, &count) != KERN_SUCCESS) {
        fprintf(stderr, "host_statistics64 failed\n");
        return 1;
    }
    size_t page = (size_t)getpagesize();
    size_t free_bytes = (size_t)vm.free_count * page;
    size_t want = argc > 1 ? (size_t)strtoull(argv[1], NULL, 10) << 20
                           : (free_bytes > (64u << 20) ? free_bytes - (64u << 20) : 0);
    char *p = mmap(NULL, want, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) {
        perror("mmap");
        return 1;
    }
    for (size_t off = 0; off < want; off += page) {
        p[off] = 0;
    }
    printf("free %zu MiB, touched %zu MiB\n", free_bytes >> 20, want >> 20);
    return 0;
}
