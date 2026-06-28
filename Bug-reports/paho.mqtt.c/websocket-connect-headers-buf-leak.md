# Memory leak of `headers_buf` in `WebSocket_connect()` on handshake-buffer allocation failure

#### Description

I found a memory leak in `WebSocket_connect()`: when custom HTTP headers are present, `headers_buf` is allocated, but if the subsequent handshake-buffer allocation fails the function jumps to `exit` and skips the `free(headers_buf)` that is only on the normal path.

File: `src/WebSocket.c`

Function: `WebSocket_connect`

`headers_buf` is allocated first:

```c
headers_buf_len++;
if ((headers_buf = malloc(headers_buf_len)) == NULL)
    goto exit;                          /* headers_buf is NULL here, nothing to free */
headers_buf_cur = headers_buf;
/* ... build the headers into headers_buf ... */
```

Then the WebSocket handshake buffer is allocated, and on failure the function jumps to `exit`:

```c
if ((buf = malloc( buf_len )) == NULL)
{
    rc = PAHO_MEMORY_ERROR;
    goto exit;                          /* headers_buf already allocated -- leaked */
}
```

`headers_buf` is only released on the normal flow, after the handshake buffer has been built:

```c
if (headers_buf)
    free( headers_buf );
```

The `goto exit` from the handshake-buffer allocation failure jumps past this `free(headers_buf)`, and the `exit:` label does not release it either, so `headers_buf` is leaked whenever the second allocation fails after the first succeeded.

This requires at least one custom HTTP header to be configured (so `headers_buf` is actually allocated) and the handshake-buffer `malloc()` to fail (memory pressure).

A minimal fix is to free `headers_buf` before jumping:

```c
if ((buf = malloc( buf_len )) == NULL)
{
    rc = PAHO_MEMORY_ERROR;
    free(headers_buf);
    goto exit;
}
```

A cleaner option is to release the local buffers at the shared `exit:` label (guarded by `if (headers_buf)`) and remove the inline `free(headers_buf)` from the normal path, so no future early-return can miss it.

#### Steps to reproduce the issue

1. Configure at least one custom `httpHeaders` entry so `headers_buf` is allocated.
2. Let the `malloc(headers_buf_len)` succeed.
3. Force the subsequent `malloc(buf_len)` (the handshake buffer) to fail.

The function then returns `PAHO_MEMORY_ERROR` via `goto exit` with `headers_buf` still allocated.

#### Expected results

When the handshake-buffer allocation fails, `WebSocket_connect()` should release `headers_buf` before returning.

#### Actual results

`WebSocket_connect()` jumps to `exit:` without freeing `headers_buf`, leaking it. LeakSanitizer or an allocation counter shows the `headers_buf` allocation unfreed on this path.

#### Versions

Source-level issue in `src/WebSocket.c`, function `WebSocket_connect()`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
