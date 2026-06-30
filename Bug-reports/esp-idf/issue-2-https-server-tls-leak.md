# esp_https_server: TLS session leaked when `transport_ctx` allocation fails in `httpd_ssl_open()`

## Summary

In `components/esp_https_server/src/https_server.c`, `httpd_ssl_open()` creates a
TLS object and completes the server-side TLS handshake, then allocates a small
`httpd_ssl_transport_ctx_t` to bind that TLS object to the HTTPD session. If the
`calloc()` fails, the function returns immediately without deleting the already
created TLS session, leaking the full TLS object and its internal handshake
resources.

## Affected code

`components/esp_https_server/src/https_server.c`, `httpd_ssl_open()`:

```c
    esp_tls_t *tls = esp_tls_init();
    if (!tls) {
        ...
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "performing session handshake");
    int ret = esp_tls_server_session_create(global_ctx->tls_cfg, sockfd, tls);
    if (ret != 0) {
        ESP_LOGE(TAG, "esp_tls_create_server_session failed, 0x%04x", -ret);
        goto fail;
    }

    httpd_ssl_transport_ctx_t *transport_ctx =
        (httpd_ssl_transport_ctx_t *)calloc(1, sizeof(httpd_ssl_transport_ctx_t));
    if (!transport_ctx) {
        esp_https_server_last_error_t last_error = {0};
        last_error.last_error = ESP_ERR_NO_MEM;
        http_dispatch_event_to_event_loop(HTTPS_SERVER_EVENT_ERROR, &last_error,
                                          sizeof(last_error));
        return ESP_ERR_NO_MEM;        /* tls leaked: not deleted, not registered */
    }
    transport_ctx->tls = tls;
    transport_ctx->global_ctx = global_ctx;

    httpd_sess_set_transport_ctx(server, sockfd, transport_ctx, httpd_ssl_close);
    ...
    return ESP_OK;
fail:
    {
        ...
        esp_tls_server_session_delete(tls);
    }
    return ESP_FAIL;
```

## Problem

At the `transport_ctx == NULL` branch:

- `tls` has already been created (`esp_tls_init()`) and its server handshake has
  already completed (`esp_tls_server_session_create()`).
- The branch returns directly, so it never calls
  `esp_tls_server_session_delete(tls)` the way the `fail:` label does.
- `httpd_ssl_close` (which would free both the TLS session and `transport_ctx`)
  is only registered *after* this point via
  `httpd_sess_set_transport_ctx(..., httpd_ssl_close)`, so the session-close path
  cannot reclaim `tls` either.

The leaked object is not a small context — it is a fully initialized TLS session.

## Trigger condition

1. A client completes (or nearly completes) the server-side TLS handshake.
2. The subsequent small `httpd_ssl_transport_ctx_t` allocation hits OOM.

This window requires memory pressure, but the leaked object is large; multiple
concurrent or repeated connections can amplify it into an "OOM causes a leak that
worsens OOM" feedback loop.

## Suggested fix

Route the failed branch through the existing `fail:` cleanup:

```c
    if (!transport_ctx) {
        esp_https_server_last_error_t last_error = {0};
        last_error.last_error = ESP_ERR_NO_MEM;
        http_dispatch_event_to_event_loop(HTTPS_SERVER_EVENT_ERROR, &last_error,
                                          sizeof(last_error));
        goto fail;
    }
```

If dispatching the TLS error event again from `fail:` is undesirable, instead
delete the session explicitly before returning:

```c
    if (!transport_ctx) {
        ...
        esp_tls_server_session_delete(tls);
        return ESP_ERR_NO_MEM;
    }
```

Either way, take care not to emit two error events with different meanings.

## Suggested verification

Use allocator fault injection so the second allocation in this function
(`calloc(1, sizeof(httpd_ssl_transport_ctx_t))`) fails. Verify:

- `esp_tls_server_session_delete()` is called exactly once.
- The session transport context remains `NULL`.
- The socket is subsequently closed by the HTTPD accept/session error path.
- ASan / heap tracing no longer reports leftover TLS allocations.
