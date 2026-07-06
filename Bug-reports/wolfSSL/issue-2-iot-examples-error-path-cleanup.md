# IoT/IDE examples: several error paths bypass resource cleanup

## Summary

Three platform examples share the same class of defect: setup/connection error
paths do not unwind the resources acquired so far, so on failure they leak the
listening/connected socket, the `WOLFSSL_CTX`, per-connection `WOLFSSL` objects,
and/or leave `wolfSSL_Init()` unbalanced. Each example already has a correct
cleanup sequence on its normal path (or an `exit:`/`*_cleanup:` block); the error
paths simply fail to route through it.

These are example-code issues, not wolfSSL library-core bugs. They are grouped
here because they are the same mistake in copy-pasted templates and are best fixed
in one change. (The Azure Sphere server example has an additional, more serious
deterministic double free — tracked separately in
`issue-4-azure-sphere-server-resource-lifecycle-bugs.md`.)

Reviewed revision: `38a4143a480b642464074a42bfd6c09666556e8b`

Affected examples:

- `IDE/MQX/server-tls.c`
- `IDE/QNX/example-client/client-tls.c`
- `IDE/iotsafe-raspberrypi/client-tls13.c`

---

## 1. `IDE/MQX/server-tls.c` — every error path is a bare `return -1`

After `wolfSSL_Init()`, the socket, `WOLFSSL_CTX`, accepted socket, and per-connection
`WOLFSSL` object are all acquired, but every error path returns directly. Only the
normal shutdown path cleans up:

```c
/* normal path only */
wolfSSL_free(ssl);
close(connd);
...
wolfSSL_CTX_free(ctx);
wolfSSL_Cleanup();
close(sockfd);
```

Each of these instead abandons everything acquired so far:

- `socket()` failure — skips `wolfSSL_Cleanup()`;
- `wolfSSL_CTX_new()` failure — also leaks `sockfd`;
- certificate/key load, `bind()`, `listen()` failure — abandons `ctx` and `sockfd`;
- `accept()` failure — abandons `ctx` and `sockfd`;
- `wolfSSL_new()` failure — additionally abandons `connd`;
- `wolfSSL_accept()` / `wolfSSL_read()` / `wolfSSL_write()` failure — additionally
  abandons `ssl` and `connd`.

MQX is an RTOS, so the example must not assume Unix process-style teardown on return.

**Fix:** initialize `sockfd`/`connd` to `-1` and `ctx`/`ssl` to `NULL`, and route
failures through a connection-level and a server-level cleanup block:

```c
connection_cleanup:
    wolfSSL_free(ssl); ssl = NULL;
    if (connd >= 0) { close(connd); connd = -1; }
server_cleanup:
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    if (sockfd >= 0) close(sockfd);
```

Connection failures clean up and either continue accepting or fall through to
`server_cleanup`; setup failures go straight to `server_cleanup`.

---

## 2. `IDE/QNX/example-client/client-tls.c` — error paths jump to the wrong cleanup level

The cleanup ladder is:

```c
cleanup:
    wolfSSL_free(ssl);
ctx_cleanup:
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
socket_cleanup:
    close(sockfd);
end:
    return ret;
```

Two classes of jumps land at the wrong level:

- After `socket()` succeeds, `inet_pton()` and `connect()` failures `goto end`,
  skipping `close(sockfd)`. They should go to `socket_cleanup`.
- After `wolfSSL_Init()` and `wolfSSL_CTX_new()` succeed, the client-cert DER→PEM
  convert/load, the private-key DER→PEM convert/load, and the CA file open / buffer
  allocation failures all `goto socket_cleanup`, skipping `wolfSSL_CTX_free(ctx)` and
  `wolfSSL_Cleanup()`. They should go to `ctx_cleanup`. (The very next call,
  `wolfSSL_CTX_load_verify_buffer()`, already `goto ctx_cleanup` correctly — which is
  what makes the others clearly wrong.)

**Fix:** route post-socket address/connect failures through `socket_cleanup`, and all
post-context cert/key/CA failures through `ctx_cleanup`; balance a successful
`wolfSSL_Init()` even when `wolfSSL_CTX_new()` fails. A single guarded cleanup block
with `sockfd = -1`, `ctx = NULL`, `ssl = NULL` is preferable to the multi-level ladder.

---

## 3. `IDE/iotsafe-raspberrypi/client-tls13.c` — two cert-loading failures `return -1` instead of `goto exit`

Almost all errors use `goto exit`, but the client- and server-certificate loads
return directly:

```c
if ((ret = wolfSSL_CTX_use_certificate_buffer(ctx, cert_buffer,
        cert_buffer_size, WOLFSSL_FILETYPE_ASN1)) != WOLFSSL_SUCCESS) {
    fprintf(stderr, "ERROR: Failed to load client certificate\n");
    return -1;                 /* bypasses exit: cleanup */
}
...
if ((ret = wolfSSL_CTX_trust_peer_buffer(ctx, cert_buffer,
        cert_buffer_size, WOLFSSL_FILETYPE_ASN1)) != WOLFSSL_SUCCESS) {
    fprintf(stderr, "ERROR: Failed to load server certificate\n");
    return -1;                 /* bypasses exit: cleanup */
}
```

By those points the function owns a connected socket, an initialized RNG, and a
`WOLFSSL_CTX`, and has called `wolfSSL_Init()`. Both returns bypass:

```c
exit:
    if (sockfd != -1) close(sockfd);
    if (ssl != NULL) wolfSSL_free(ssl);
    if (ctx != NULL) wolfSSL_CTX_free(ctx);
    wc_FreeRng(&rng);
    wolfSSL_Cleanup();
    return ret;
```

**Fix:** replace both `return -1;` with `ret = -1; goto exit;`, matching the
surrounding cert-extraction, IoT SAFE, socket, and TLS error paths.

---

## Severity and reporting value

All three are confined to platform examples, so none is a wolfSSL core vulnerability
and none should go through a security/CVE channel — a normal cleanup PR is the right
venue. The value is that these examples are widely copied as templates by IoT
developers, so the leaked-socket / leaked-context / unbalanced-`wolfSSL_Init()`
patterns propagate into real products. Fixing all three in one PR (each is a small,
local change) is the least-friction way to land them.
