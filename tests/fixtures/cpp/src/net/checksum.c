#include "Session.h"

/* Adds every byte of buf. */
static unsigned add_bytes(const unsigned char* buf, int len)
{
    unsigned sum = 0;
    for (int i = 0; i < len; ++i)
        sum += buf[i];
    return sum;
}

struct Packet { int size; };
