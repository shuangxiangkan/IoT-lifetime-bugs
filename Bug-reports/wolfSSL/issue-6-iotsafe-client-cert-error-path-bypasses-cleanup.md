# IoT SAFE client example bypasses cleanup when loading the client certificate fails

#### Description

I found a possible resource leak in the IoT SAFE Raspberry Pi client example. Most error paths in this function use `goto exit` to run a shared cleanup block, but the client-certificate load failure uses `return -1` directly, skipping that cleanup.

File: `IDE/iotsafe-raspberrypi/client-tls13.c`

The shared `exit:` block releases the socket, the SSL/CTX objects, the RNG, and the global wolfSSL state:

```c
exit:
    if (sockfd != -1)
        close(sockfd);
    if (ssl != NULL)
        wolfSSL_free(ssl);
    if (ctx != NULL)
        wolfSSL_CTX_free(ctx);
    wc_FreeRng(&rng);
    wolfSSL_Cleanup();
    return ret;
```

But the certificate-load failure returns directly instead of going to `exit`:

```c
    /* Load client certificate */
    if ((ret = wolfSSL_CTX_use_certificate_buffer(ctx, cert_buffer,
            cert_buffer_sz, WOLFSSL_FILETYPE_ASN1)) != WOLFSSL_SUCCESS) {
        fprintf(stderr, "ERROR: Failed to load client certificate\n");
        return -1;                /* bypasses the exit: cleanup */
    }
```

By the time this runs, the function has already created the socket and connected, called `wolfSSL_Init()`, `wc_InitRng()`, and `wolfSSL_CTX_new()`, so `return -1` here leaks the socket, the CTX, and the RNG, and skips `wolfSSL_Cleanup()`.

Suggested fix — route this failure through the shared cleanup like the others:

```c
        fprintf(stderr, "ERROR: Failed to load client certificate\n");
        ret = -1;
        goto exit;
```

#### Steps to reproduce the issue

1. Make `wolfSSL_CTX_use_certificate_buffer()` fail (e.g. provide an invalid certificate buffer) after the socket, RNG, and CTX have been set up.

#### Expected results

The certificate-load failure should go to the shared `exit:` cleanup so the socket, CTX, and RNG are released and `wolfSSL_Cleanup()` runs.

#### Actual results

The certificate-load failure does `return -1` directly, leaking the socket, CTX, and RNG and skipping `wolfSSL_Cleanup()`.

#### Versions

Source-level issue in `IDE/iotsafe-raspberrypi/client-tls13.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
