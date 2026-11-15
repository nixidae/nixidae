#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sys/socket.h>
#include <CoreFoundation/CoreFoundation.h>
int main(void) { printf("hi %ld\n", (long)CFAbsoluteTimeGetCurrent()); return 0; }
