// mount9p UUID DIR [ro]: mount_9p's mount(2), at any directory.
// Layout read from mount_9p's disassembly (macOS 27 /sbin/mount_9p).
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: mount9p UUID DIR [ro]\n");
        return 2;
    }
    unsigned char data[0x30] = {0};
    uint32_t magic = 0x41563950;
    memcpy(data, &magic, 4);
    strlcpy((char *)data + 4, argv[1], 0x25);
    uint32_t ro = argc > 3;
    memcpy(data + 0x2c, &ro, 4);
    if (mount("AppleVirtIO9P", argv[2], 0, data) != 0) {
        perror("mount");
        return 1;
    }
    return 0;
}
