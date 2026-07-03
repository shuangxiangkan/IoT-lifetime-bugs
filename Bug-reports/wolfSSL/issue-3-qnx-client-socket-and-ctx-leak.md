# QNX client example leaks socket on connect errors and CTX on cert/key conversion errors

#### Description

I found two possible leaks in the QNX TLS client example, both caused by jumping to the wrong cleanup label:

1. After `socket()` succeeds, the address-parsing and `connect()` error paths `goto end`, skipping `socket_cleanup: close(sockfd)`.
2. After `wolfSSL_CTX_new()` succeeds, the certificate/private-key conversion error paths `goto socket_cleanup`, skipping `ctx_cleanup: wolfSSL_CTX_free(ctx)`.

File: `IDE/QNX/example-client/client-tls.c`

The cleanup labels are ordered so that `ctx_cleanup` falls through into `socket_cleanup`:

```c
ctx_cleanup:
    wolfSSL_CTX_free(ctx);   /* Free the wolfSSL context object */
socket_cleanup:
    close(sockfd);           /* Close the connection to the server */
end:
    return ret;
```

Socket leak — after `socket()` succeeds, these `goto end`:

```c
if ((sockfd = socket(AF_INET, SOCK_STREAM, 0)) == -1) { ... goto end; }   /* ok: nothing open */
...
if (inet_pton(AF_INET, argv[2], &servAddr.sin_addr) != 1) { ... goto end; }   /* sockfd leaked */
if ((ret = connect(sockfd, ...)) == -1) { ... goto end; }                     /* sockfd leaked */
```

`goto end` skips `socket_cleanup: close(sockfd)`, so the socket is leaked. These should `goto socket_cleanup`.

CTX leak — after `wolfSSL_CTX_new()` succeeds:

```c
if ((ctx = wolfSSL_CTX_new(wolfSSLv23_client_method())) == NULL) { ... goto socket_cleanup; }   /* ctx is NULL: ok */
...
/* certificate DER -> PEM conversion fails */   goto socket_cleanup;   /* ctx leaked */
...
/* private key DER -> PEM conversion fails */    goto socket_cleanup;   /* ctx leaked */
```

The conversion-failure paths run after `ctx` has been created, but `goto socket_cleanup` skips `ctx_cleanup: wolfSSL_CTX_free(ctx)`, so the CTX is leaked.

Suggested fix:

* Send the post-`socket()` address/`connect()` failures to `socket_cleanup`.
* Send the post-`wolfSSL_CTX_new()` certificate/key conversion failures to `ctx_cleanup`.
* Initialize `ctx = NULL`, `ssl = NULL`, `sockfd = -1` (and any temporary PEM buffer), and guard the cleanup; a single cleanup label that releases in reverse order based on validity is the most robust.

#### Steps to reproduce the issue

1. Run the client with an invalid IPv4 address (fails `inet_pton()`), or against an unreachable endpoint (fails `connect()`) — socket leak.
2. Provide input that makes the certificate or private-key DER→PEM conversion fail after `wolfSSL_CTX_new()` — CTX leak.

#### Expected results

Connect-time failures should close the socket; certificate/key conversion failures should free the CTX (and close the socket), reaching the correct cleanup level.

#### Actual results

Connect-time failures `goto end` and leak the socket; certificate/key conversion failures `goto socket_cleanup` and leak the CTX.

#### Versions

Source-level issue in `IDE/QNX/example-client/client-tls.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
