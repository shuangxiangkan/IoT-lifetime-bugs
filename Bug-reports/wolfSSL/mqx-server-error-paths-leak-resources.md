# MQX server example leaks socket / CTX / accepted socket / SSL on error paths

#### Description

I found possible resource leaks in the MQX TLS server example. After the listening socket and the CTX are created, many error paths `return -1` directly, bypassing the cleanup at the end of the function, so the listening socket, the CTX, the accepted socket, and the `WOLFSSL` object are leaked depending on where the failure happens.

File: `IDE/MQX/server-tls.c`

The end-of-function cleanup is:

```c
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    close(sockfd);          /* listening socket */
```

But the error paths return directly without reaching it:

```c
    if ((sockfd = socket(AF_INET, SOCK_STREAM, 0)) == -1) { ... return -1; }   /* ok: nothing open */
    if ((ctx = wolfSSL_CTX_new(...)) == NULL) { ... return -1; }               /* leaks sockfd */
    /* cert load fails */                       return -1;                     /* leaks sockfd + ctx */
    /* key load fails */                        return -1;                     /* leaks sockfd + ctx */
    if (bind(sockfd, ...) == -1) { ... return -1; }                            /* leaks sockfd + ctx */
    if (listen(sockfd, 5) == -1) { ... return -1; }                            /* leaks sockfd + ctx */

    while (1) {
        if ((connd = accept(sockfd, ...)) == -1) { ... return -1; }            /* leaks sockfd + ctx */
        if ((ssl = wolfSSL_new(ctx)) == NULL) { ... return -1; }               /* leaks sockfd + ctx + connd */
        wolfSSL_set_fd(ssl, connd);
        if (wolfSSL_accept(ssl) != WOLFSSL_SUCCESS) { ... return -1; }         /* leaks sockfd + ctx + connd + ssl */
        if (wolfSSL_read(ssl, ...) == -1) { ... return -1; }                   /* leaks sockfd + ctx + connd + ssl */
        ...
        if (wolfSSL_write(ssl, ...) != len) { ... return -1; }                 /* leaks sockfd + ctx + connd + ssl */

        wolfSSL_free(ssl);
        close(connd);
    }
```

Once `socket()` succeeds, every subsequent `return -1` leaks at least the listening `sockfd`; after `wolfSSL_CTX_new()` it also leaks `ctx`; inside the loop after `accept()` it also leaks `connd`; and after `wolfSSL_new()` it also leaks the `WOLFSSL` object `ssl`.

A robust fix is two cleanup labels reached via `goto`, with the pointers/fds initialized (`ctx = NULL`, `ssl = NULL`, `sockfd = -1`, `connd = -1`) and guarded:

```c
connection_cleanup:
    wolfSSL_free(ssl); ssl = NULL;
    if (connd != -1) { close(connd); connd = -1; }

server_cleanup:
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    if (sockfd != -1) close(sockfd);
```

Connection-level failures go to `connection_cleanup` (then continue accepting or fall through to `server_cleanup`); setup failures go to `server_cleanup`.

#### Steps to reproduce the issue

Inject a failure at any of: `wolfSSL_CTX_new()`, certificate load, private-key load, `bind()`, `listen()`, `accept()`, `wolfSSL_new()`, `wolfSSL_accept()` (bad handshake), `wolfSSL_read()`, or `wolfSSL_write()`, and observe that the function returns `-1` without releasing the resources allocated up to that point.

#### Expected results

Each error path should release everything allocated so far — the listening socket, the CTX, the accepted socket, and the `WOLFSSL` object — and run `wolfSSL_Cleanup()` before returning.

#### Actual results

The error paths `return -1` directly, leaking the listening socket and CTX (setup failures) or additionally the accepted socket and `WOLFSSL` object (per-connection failures), and skipping `wolfSSL_Cleanup()`.

#### Versions

Source-level issue in `IDE/MQX/server-tls.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
