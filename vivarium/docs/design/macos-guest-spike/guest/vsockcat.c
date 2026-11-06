/*
 * vsockcat: test virtio-vsock from a macOS guest.
 *
 *   vsockcat listen PORT            echo every connection back, one at a time
 *   vsockcat connect CID PORT TEXT  send TEXT, close the write side, print the reply
 *   vsockcat bulk CID PORT BYTES    send BYTES, print bytes echoed back and MB/s
 *
 * Build on the host: clang -O2 -mmacosx-version-min=14.0 -o vsockcat vsockcat.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/vsock.h>
#include <unistd.h>

static struct sockaddr_vm addr(unsigned cid, unsigned port)
{
    struct sockaddr_vm a = {0};

    a.svm_len = sizeof(a);
    a.svm_family = AF_VSOCK;
    a.svm_cid = cid;
    a.svm_port = port;
    return a;
}

static int listen_echo(unsigned port)
{
    struct sockaddr_vm a = addr(VMADDR_CID_ANY, port);
    char buf[65536];
    ssize_t n;
    int s = socket(AF_VSOCK, SOCK_STREAM, 0);

    if (s < 0 || bind(s, (struct sockaddr *)&a, sizeof(a)) < 0 || listen(s, 4) < 0) {
        perror("listen");
        return 1;
    }
    for (;;) {
        int c = accept(s, NULL, NULL);

        if (c < 0) {
            perror("accept");
            continue;
        }
        while ((n = read(c, buf, sizeof(buf))) > 0) {
            if (write(c, buf, n) != n) {
                break;
            }
        }
        close(c);
    }
}

static int connect_to(unsigned cid, unsigned port)
{
    struct sockaddr_vm a = addr(cid, port);
    int s = socket(AF_VSOCK, SOCK_STREAM, 0);

    if (s < 0 || connect(s, (struct sockaddr *)&a, sizeof(a)) < 0) {
        perror("connect");
        return -1;
    }
    return s;
}

static int send_text(unsigned cid, unsigned port, const char *text)
{
    char buf[4096];
    ssize_t n;
    int s = connect_to(cid, port);

    if (s < 0) {
        return 1;
    }
    if (write(s, text, strlen(text)) < 0) {
        perror("write");
        return 1;
    }
    shutdown(s, SHUT_WR);
    while ((n = read(s, buf, sizeof(buf))) > 0) {
        fwrite(buf, 1, n, stdout);
    }
    return 0;
}

static int bulk(unsigned cid, unsigned port, size_t total)
{
    static char buf[65536];
    struct timeval t0, t1;
    size_t sent = 0, got = 0;
    ssize_t n;
    int s = connect_to(cid, port);

    if (s < 0) {
        return 1;
    }
    memset(buf, 'x', sizeof(buf));
    gettimeofday(&t0, NULL);
    if (fork() == 0) {
        while (sent < total) {
            size_t want = total - sent < sizeof(buf) ? total - sent : sizeof(buf);

            n = write(s, buf, want);
            if (n <= 0) {
                _exit(1);
            }
            sent += n;
        }
        shutdown(s, SHUT_WR);
        _exit(0);
    }
    while ((n = read(s, buf, sizeof(buf))) > 0) {
        got += n;
    }
    gettimeofday(&t1, NULL);
    double secs = (t1.tv_sec - t0.tv_sec) + (t1.tv_usec - t0.tv_usec) / 1e6;
    printf("echoed %zu of %zu bytes in %.3f s, %.1f MB/s each way\n", got, total, secs,
           got / secs / 1e6);
    return got == total ? 0 : 1;
}

int main(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "listen")) {
        return listen_echo(strtoul(argv[2], NULL, 0));
    }
    if (argc == 5 && !strcmp(argv[1], "connect")) {
        return send_text(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), argv[4]);
    }
    if (argc == 5 && !strcmp(argv[1], "bulk")) {
        return bulk(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0),
                    strtoull(argv[4], NULL, 0));
    }
    fprintf(stderr, "usage: vsockcat listen PORT | connect CID PORT TEXT | bulk CID PORT BYTES\n");
    return 2;
}
