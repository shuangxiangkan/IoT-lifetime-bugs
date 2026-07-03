# Accepted socket (`connd`) leaked on error paths in Azure Sphere server example

#### Description

I found a possible file descriptor leak in the Azure Sphere server example. After a client connection is accepted, the per-connection error paths call `util_Cleanup()`, which closes the *listening* socket `sockfd` but never closes the *accepted* socket `connd`, so `connd` is leaked.

File: `IDE/VS-AZURE-SPHERE/server/server.c`

The shared cleanup helper only closes the socket passed to it (`IDE/VS-AZURE-SPHERE/shared/util.h`):

```c
static void util_Cleanup(int sockfd, WOLFSSL_CTX* ctx, WOLFSSL* ssl)
{
    wolfSSL_free(ssl);
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    close(sockfd);          /* closes the listening socket only */
}
```

In the connection loop, `connd` is the accepted socket, but the error paths pass `sockfd` (the listening socket) to `util_Cleanup()`:

```c
    if ((connd = accept(sockfd, ...)) == -1) { ... }

    if ((ssl = wolfSSL_new(ctx)) == NULL) {
        util_Cleanup(sockfd, ctx, ssl);   /* connd not closed -> leak */
        return -1;
    }
    wolfSSL_set_fd(ssl, connd);
    ret = wolfSSL_accept(ssl);
    if (ret != WOLFSSL_SUCCESS) {
        util_Cleanup(sockfd, ctx, ssl);   /* connd not closed -> leak */
        return -1;
    }
    if (wolfSSL_read(ssl, buff, sizeof(buff)-1) == -1) {
        util_Cleanup(sockfd, ctx, ssl);   /* connd not closed -> leak */
        return -1;
    }
    ...
    if (wolfSSL_write(ssl, buff, (int)len) != len) {
        util_Cleanup(sockfd, ctx, ssl);   /* connd not closed -> leak */
        return -1;
    }
```

On each of these paths (`wolfSSL_new`, `wolfSSL_accept`, `wolfSSL_read`, `wolfSSL_write` failure), the accepted socket `connd` is left open. `util_Cleanup()` closes `sockfd`, not `connd`.

A minimal fix is to close the accepted socket before the shared cleanup on these paths:

```c
    if (connd >= 0) {
        close(connd);
        connd = -1;
    }
    util_Cleanup(sockfd, ctx, ssl);
    return -1;
```

Note: simply substituting `connd` for `sockfd` in the `util_Cleanup()` call would instead leak the listening socket, the CTX, and the global wolfSSL state, so the accepted socket needs its own close.

#### Steps to reproduce the issue

1. Build and run the Azure Sphere server example.
2. Have a client connect so `accept()` succeeds and `connd` is assigned.
3. Force one of the per-connection steps to fail (e.g. `wolfSSL_accept()` by sending a bad handshake, or `wolfSSL_read`/`wolfSSL_write`).

#### Expected results

When a per-connection step fails after `accept()`, the accepted socket `connd` should be closed before returning, so no file descriptor is leaked.

#### Actual results

The error paths call `util_Cleanup(sockfd, ctx, ssl)`, which closes the listening `sockfd` but not the accepted `connd`, leaking the accepted socket on each failing connection.

#### Versions

Source-level issue in `IDE/VS-AZURE-SPHERE/server/server.c` (with `util_Cleanup()` in `IDE/VS-AZURE-SPHERE/shared/util.h`).

I have not tied this to a specific build or device setup. Please let me know if a concrete version string or a runtime reproducer is required.
