# Double free of `ssl` in Azure Sphere server example on the shutdown path

#### Description

I found a possible double free of the `WOLFSSL *ssl` object in the Azure Sphere server example. On the normal shutdown path, `ssl` is freed at the end of the connection loop without being set to `NULL`, and then freed again by `util_Cleanup()` after the loop exits.

File: `IDE/VS-AZURE-SPHERE/server/server.c`

The shared cleanup helper frees `ssl` (`IDE/VS-AZURE-SPHERE/shared/util.h`):

```c
static void util_Cleanup(int sockfd, WOLFSSL_CTX* ctx, WOLFSSL* ssl)
{
    wolfSSL_free(ssl);      /* Free the wolfSSL object  */
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    close(sockfd);
}
```

Inside the connection loop, each completed connection frees `ssl` but does not clear the pointer:

```c
    /* ... handle one connection ... */
    if (strncmp(buff, "shutdown", 8) == 0) {
        ...
        shutdown = 1;
    }
    ...
    wolfSSL_free(ssl);      /* Free the wolfSSL object */
    close(connd);           /* Close the connection    */
}   /* end of while (!shutdown) */

util_Cleanup(sockfd, ctx, ssl);   /* frees ssl again */
```

When a `"shutdown"` command is received, `shutdown` is set, the reply is written, then `wolfSSL_free(ssl)` runs at the bottom of the loop body. Because `ssl` is not set to `NULL`, the loop exits and `util_Cleanup(sockfd, ctx, ssl)` calls `wolfSSL_free(ssl)` a second time on the same pointer — a double free.

The same dangling-`ssl` problem also affects error handling on later iterations: after the loop body frees `ssl` without nulling it, if the next iteration's `accept()` or `wolfSSL_new()` fails, `util_Cleanup(sockfd, ctx, ssl)` is called with the already-freed `ssl`.

A minimal fix is to clear the pointers after the per-connection cleanup:

```c
    wolfSSL_free(ssl);
    ssl = NULL;
    close(connd);
    connd = -1;
```

With `ssl == NULL`, the final `util_Cleanup()` call is a no-op for `ssl` (`wolfSSL_free(NULL)` is safe).

#### Steps to reproduce the issue

1. Build and run the Azure Sphere server example.
2. Connect a client, complete the TLS handshake, and send the message `"shutdown"`.
3. The server replies, frees `ssl` at the end of the loop, exits the loop, and calls `util_Cleanup(sockfd, ctx, ssl)`.

#### Expected results

`ssl` should be freed exactly once. After the per-connection `wolfSSL_free(ssl)`, the pointer should be cleared so the final `util_Cleanup()` does not free it again.

#### Actual results

On the shutdown path, `wolfSSL_free(ssl)` runs at the end of the loop body and again inside `util_Cleanup()`, freeing the same `WOLFSSL` object twice. With a hardened allocator / ASan this is a detectable double free.

#### Versions

Source-level issue in `IDE/VS-AZURE-SPHERE/server/server.c` (with `util_Cleanup()` in `IDE/VS-AZURE-SPHERE/shared/util.h`).

I have not tied this to a specific build or device setup. Please let me know if a concrete version string or a runtime reproducer is required.
