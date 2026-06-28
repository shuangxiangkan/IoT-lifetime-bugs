# `fuzzing_read_packet()` leaks the input buffer on every path

I found a memory leak in `fuzzing_read_packet()`: the heap buffer returned by
`fuzzing_read_bytes()` is never freed, on either the error path or the success
path.

File: `sys/fuzzing/fuzzing.c`

Function: `fuzzing_read_packet`

```c
int
fuzzing_read_packet(int fd, gnrc_pktsnip_t *pkt)
{
    size_t rsiz;

    /* can only be called once currently */
    assert(gnrc_pktbuf_fuzzptr == NULL);

    uint8_t *input = fuzzing_read_bytes(fd, &rsiz);     /* heap allocation */
    if (input == NULL) {
        return -errno;
    }

    if (gnrc_pktbuf_realloc_data(pkt, rsiz)) {
        return -ENOMEM;                                 /* input leaked */
    }

    memcpy(pkt->data, input, rsiz);                     /* input only copied */

    gnrc_pktbuf_fuzzptr = pkt;
    return 0;                                           /* input leaked */
}
```

`fuzzing_read_bytes()` returns heap memory (a `realloc`-ed buffer). After
`memcpy()`, `input` is no longer used and its ownership is not transferred:
`gnrc_pktbuf_fuzzptr` stores `pkt`, not `input`. So both the
`gnrc_pktbuf_realloc_data()` failure path and the normal `return 0` path leak
`input`.

The function is currently documented as callable only once, so the leak does
not accumulate unbounded, but it still shows up under LeakSanitizer and in
long-running fuzzing harnesses.

Suggested fix:

```c
    if (gnrc_pktbuf_realloc_data(pkt, rsiz)) {
        free(input);
        return -ENOMEM;
    }

    memcpy(pkt->data, input, rsiz);
    free(input);

    gnrc_pktbuf_fuzzptr = pkt;
    return 0;
```
