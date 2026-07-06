# Azure Sphere server example double-frees `ssl` and leaks accepted sockets

## Summary

`IDE/VS-AZURE-SPHERE/server/server.c` has two related resource-lifecycle
problems:

1. On the normal `"shutdown"` path, the current `WOLFSSL` object is freed at
   the bottom of the connection loop and then freed again by `util_Cleanup()`.
2. On errors after `accept()` succeeds, `util_Cleanup()` closes the listening
   socket but not the accepted socket.

The double free is the primary issue: it is deterministically reachable through
the example's normal shutdown command and can corrupt the allocator or crash the
application. The accepted-socket leak is a secondary cleanup defect.

## Double-free path

The shared helper frees `ssl`:

```c
static void util_Cleanup(int sockfd, WOLFSSL_CTX* ctx, WOLFSSL* ssl)
{
    wolfSSL_free(ssl);
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    close(sockfd);
}
```

After a successful connection, the loop also frees `ssl` without clearing it:

```c
if (strncmp(buff, "shutdown", 8) == 0) {
    shutdown = 1;
}

/* send reply */

wolfSSL_free(ssl);
close(connd);
```

When `"shutdown"` was received, the loop exits and passes the dangling pointer
to the helper:

```c
util_Cleanup(sockfd, ctx, ssl);
```

`wolfSSL_free(NULL)` is safe, but a stale non-NULL pointer is not:
`wolfSSL_free()` dereferences the object to obtain `ssl->ctx->heap`.

There is another stale-pointer path. After a normal connection frees `ssl`, a
subsequent `accept()` failure calls `util_Cleanup()` before assigning a new
value to `ssl`. A subsequent `wolfSSL_new()` failure does **not** have this
specific problem because its assignment sets `ssl` to NULL before cleanup.

## Accepted-socket leak

After `connd = accept(...)` succeeds, failures in `wolfSSL_new()`,
`wolfSSL_accept()`, `wolfSSL_read()`, or `wolfSSL_write()` call:

```c
util_Cleanup(sockfd, ctx, ssl);
return -1;
```

The helper closes `sockfd`, the listening socket, but never closes `connd`.
The application then exits, so the platform will normally reclaim the
descriptor, but the example still fails to release the resource it owns.

## Suggested fix

Use separate connection- and server-level cleanup and invalidate resources
immediately after release:

```c
int sockfd = -1;
int connd = -1;
WOLFSSL *ssl = NULL;

/* connection cleanup */
wolfSSL_free(ssl);
ssl = NULL;
if (connd >= 0) {
    close(connd);
    connd = -1;
}

/* server cleanup */
wolfSSL_CTX_free(ctx);
wolfSSL_Cleanup();
if (sockfd >= 0) {
    close(sockfd);
}
```

All failures after `accept()` should run connection cleanup before final server
cleanup.

## Reproduction

For the double free:

1. Run the Azure Sphere server example.
2. Complete a TLS connection and send `"shutdown"`.
3. The loop frees `ssl`, exits, and `util_Cleanup()` frees the same pointer.

For the descriptor leak, connect and then fail the TLS handshake. The error path
closes `sockfd` but not `connd`.

## Severity and reporting value

This is an Azure Sphere example issue, not a wolfSSL library-core bug. The
double free is nevertheless worth reporting because it is deterministic on the
normal shutdown path. The descriptor leak should be fixed in the same change
rather than reported separately.
