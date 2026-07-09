# `sys/fuzzing`: input buffers are leaked in `fuzzing_read_bytes()` and `fuzzing_read_packet()`

#### Description

I found several related input-buffer lifetime bugs in RIOT's fuzzing helper
code. They are all in `sys/fuzzing/fuzzing.c` and affect the heap buffer used
to read fuzzer input from a file descriptor.

File: `sys/fuzzing/fuzzing.c`

Functions: `fuzzing_read_bytes`, `fuzzing_read_packet`

First, `fuzzing_read_bytes()` overwrites its only pointer with the result of
`realloc()`:

```c
while ((r = read(fd, &(buffer[csiz]), rsiz)) > 0) {
    ...
    if (rsiz == 0) {
        if ((buffer = realloc(buffer, csiz + FUZZING_BSTEP)) == NULL) {
            return NULL;
        }
        rsiz += FUZZING_BSTEP;
    }
}
...
if ((buffer = realloc(buffer, csiz)) == NULL) {
    return NULL;
}
```

If either `realloc()` fails, the original allocation is still valid, but the
only pointer to it has already been overwritten with `NULL`. The buffer is then
leaked. The read-error path has a similar cleanup omission:

```c
if (r == -1) {
    return NULL;
}
```

At this point `buffer` may already contain data read from the file descriptor,
but it is returned without being freed.

Second, `fuzzing_read_packet()` leaks the buffer returned by
`fuzzing_read_bytes()`:

```c
uint8_t *input = fuzzing_read_bytes(fd, &rsiz);
if (input == NULL) {
    return -errno;
}

if (gnrc_pktbuf_realloc_data(pkt, rsiz)) {
    return -ENOMEM;
}

memcpy(pkt->data, input, rsiz);

gnrc_pktbuf_fuzzptr = pkt;
return 0;
```

`fuzzing_read_bytes()` returns heap memory. `fuzzing_read_packet()` only copies
that memory into `pkt->data`; ownership is not transferred. The
`gnrc_pktbuf_fuzzptr` global stores `pkt`, not `input`, so it does not clean up
the temporary input buffer. As a result, `input` is leaked both when
`gnrc_pktbuf_realloc_data()` fails and on the normal success path.

This is fuzzing support code rather than a normal firmware runtime path, so the
impact is mainly on sanitizer/fuzzing runs. Still, the leaks can produce
LeakSanitizer noise and make fuzzing failures harder to interpret.

#### Suggested fix

Use a temporary pointer for reallocations in `fuzzing_read_bytes()` and free the
existing buffer on failure:

```c
uint8_t *new_buffer = realloc(buffer, new_size);
if (new_buffer == NULL) {
    free(buffer);
    return NULL;
}
buffer = new_buffer;
```

The read-error path should also free the partially filled buffer:

```c
if (r == -1) {
    free(buffer);
    return NULL;
}
```

In `fuzzing_read_packet()`, free the temporary input buffer after it has been
copied, and also before returning from the packet-buffer resize failure path:

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

It may also be worth handling the zero-length input case explicitly, so
`realloc(buffer, 0)` does not make a valid empty input indistinguishable from an
allocation failure on platforms where it returns `NULL`.

#### Steps to reproduce the issue

1. Run a RIOT fuzzing target with LeakSanitizer enabled, or use allocation fault
   injection around `sys/fuzzing/fuzzing.c`.
2. For `fuzzing_read_packet()`, pass an input that is read successfully and
   reaches the normal `memcpy()` path.
3. For `fuzzing_read_bytes()`, force either the growth `realloc()`, the final
   shrink `realloc()`, or `read()` to fail after the initial buffer allocation.

#### Expected results

Temporary input buffers should be freed on all error paths and after
`fuzzing_read_packet()` copies the input into the packet buffer.

#### Actual results

`fuzzing_read_bytes()` can lose the only pointer to its buffer on `realloc()`
failure and can return on read error without freeing it. `fuzzing_read_packet()`
never frees the heap buffer returned by `fuzzing_read_bytes()`.

#### Versions

Source-level issue in `sys/fuzzing/fuzzing.c`.
