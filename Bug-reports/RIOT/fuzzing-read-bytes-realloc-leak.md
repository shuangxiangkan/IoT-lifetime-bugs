# `fuzzing_read_bytes()` leaks the buffer on `realloc` failure and on read error

I found memory leaks in `fuzzing_read_bytes()`. The function uses the
`buffer = realloc(buffer, n)` anti-pattern, which loses the original buffer when
`realloc()` fails, and it also returns without freeing `buffer` on a read error.

File: `sys/fuzzing/fuzzing.c`

Function: `fuzzing_read_bytes`

```c
uint8_t *
fuzzing_read_bytes(int fd, size_t *size)
{
    uint8_t *buffer = NULL;
    ssize_t r;
    size_t csiz, rsiz;

    csiz = 0;
    rsiz = FUZZING_BSIZE;
    if ((buffer = realloc(buffer, rsiz)) == NULL) {       /* ok: buffer is NULL here */
        return NULL;
    }

    while ((r = read(fd, &(buffer[csiz]), rsiz)) > 0) {
        assert((size_t)r <= rsiz);

        csiz += r;
        rsiz -= r;

        if (rsiz == 0) {
            if ((buffer = realloc(buffer, csiz + FUZZING_BSTEP)) == NULL) {
                return NULL;                              /* leak: old buffer lost */
            }
            rsiz += FUZZING_BSTEP;
        }
    }
    if (r == -1) {
        return NULL;                                      /* leak: buffer not freed */
    }

    /* shrink buffer to actual size */
    if ((buffer = realloc(buffer, csiz)) == NULL) {
        return NULL;                                      /* leak: old buffer lost */
    }

    *size = csiz;
    return buffer;
}
```

Three problems:

1. Line `buffer = realloc(buffer, csiz + FUZZING_BSTEP)` in the loop: when
   `realloc()` fails the original allocation is still valid, but the only
   pointer to it is overwritten with `NULL`, so it leaks.
2. `if (r == -1) return NULL;` returns on a read error without freeing the
   buffer that already holds the data read so far.
3. Line `buffer = realloc(buffer, csiz)` (the final shrink) has the same
   `realloc`-overwrite leak as (1).

(The very first `realloc(buffer, rsiz)` is fine because `buffer` is `NULL`
there, so nothing is lost on failure.)

Suggested fix: use a temporary for every reallocation and free the old buffer on
failure, and free the buffer on the read-error path:

```c
    uint8_t *new_buffer = realloc(buffer, new_size);
    if (new_buffer == NULL) {
        free(buffer);
        return NULL;
    }
    buffer = new_buffer;
```

```c
    if (r == -1) {
        free(buffer);
        return NULL;
    }
```

It is also worth handling the `csiz == 0` case explicitly: `realloc(buffer, 0)`
is implementation-defined, so an empty input and an allocation failure should
not be collapsed into the same `NULL` return.
