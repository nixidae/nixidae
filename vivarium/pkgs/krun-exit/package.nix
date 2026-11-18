# A krun guest ends with a reset whatever it meant, so it tells libkrun
# which: libkrun's process exits with the code the guest last set through
# virtio-fs ioctl 0x7602 on any file of a share, as libkrun's own init
# does. `KrunExit` in vivarium_runner/backends/krun.py reads `codes`.
#
#     vivarium-krun-exit PATH CODE
{ writeCBin }:
(writeCBin "vivarium-krun-exit" ''
  #include <fcntl.h>
  #include <stdio.h>
  #include <stdlib.h>
  #include <sys/ioctl.h>

  int main(int argc, char **argv) {
    if (argc != 3) {
      fprintf(stderr, "usage: %s PATH CODE\n", argv[0]);
      return 2;
    }
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) {
      perror(argv[1]);
      return 1;
    }
    if (ioctl(fd, 0x7602, atoi(argv[2])) < 0) {
      perror("ioctl 0x7602");
      return 1;
    }
    return 0;
  }
'').overrideAttrs
  (old: {
    passthru = (old.passthru or { }) // {
      codes = {
        poweroff = 0;
        crash = 70;
        reboot = 71;
      };
    };
  })
