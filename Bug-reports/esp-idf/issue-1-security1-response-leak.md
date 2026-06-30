# protocomm Security1: `out`/`out_resp` leaked when `psa_cipher_update()` fails

## Summary

In `components/protocomm/src/security/security1.c`, the Security1 session
command-1 handler allocates two protobuf response objects (`out` and `out_resp`)
before encrypting the device public key. If `psa_cipher_update()` fails, the
error path frees only the cipher output buffer (`outbuf`) and returns, leaking
both `out` and `out_resp`. They have not yet been linked into `resp`, so no
upper-layer cleanup can reclaim them.

## Affected code

`components/protocomm/src/security/security1.c`, `handle_session_command1()`:

```c
    Sec1Payload *out = (Sec1Payload *) malloc(sizeof(Sec1Payload));
    SessionResp1 *out_resp = (SessionResp1 *) malloc(sizeof(SessionResp1));
    if (!out || !out_resp) {
        ESP_LOGE(TAG, "Error allocating memory for response1");
        free(out);
        free(out_resp);
        return ESP_ERR_NO_MEM;
    }

    sec1_payload__init(out);
    session_resp1__init(out_resp);
    out_resp->status = STATUS__Success;

    uint8_t *outbuf = (uint8_t *) malloc(PUBLIC_KEY_LEN);
    if (!outbuf) {
        ESP_LOGE(TAG, "Error allocating ciphertext buffer");
        free(out);
        free(out_resp);
        return ESP_ERR_NO_MEM;
    }

    size_t outlen = 0;
    status = psa_cipher_update(&cur_session->ctx_aes, cur_session->client_pubkey,
                              sizeof(cur_session->client_pubkey), outbuf,
                              PUBLIC_KEY_LEN, &outlen);
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "Failed at psa_cipher_update with error code : %d", status);
        free(outbuf);
        return ESP_FAIL;        /* out and out_resp leaked */
    }

    out_resp->device_verify_data.data = outbuf;     /* ownership linked only here */
    out_resp->device_verify_data.len = PUBLIC_KEY_LEN;
    ...
    out->sr1 = out_resp;
    resp->proto_case = SESSION_DATA__PROTO_SEC1;
    resp->sec1 = out;                               /* and here */
```

## Problem

`out` and `out_resp` are only attached to the response tree *after* the
`psa_cipher_update()` check (`out->sr1 = out_resp; resp->sec1 = out;`). On the
encryption-failure path they are still local, unreferenced allocations, so:

- The function does not free them before returning `ESP_FAIL`.
- The normal teardown (`sec1_session_setup_cleanup()`, which frees
  `out_resp->device_verify_data.data`, `out_resp`, then `out`) never sees them,
  because nothing was linked into `resp`.

The other two error paths in the same block (allocation failures) correctly free
`out` and `out_resp`; only the `psa_cipher_update()` failure path omits them,
making the inconsistency clear.

## Trigger condition

1. A Security1 handshake reaches `Session_Command1`.
2. All three heap allocations succeed.
3. `psa_cipher_update()` returns something other than `PSA_SUCCESS` (invalid
   cipher state, PSA driver error, hardware crypto failure).

Each occurrence leaks two small heap objects. A peer that can repeatedly start
and interrupt provisioning handshakes could gradually exhaust the heap on a
constrained device.

## Suggested fix

Free the local objects on the failure path:

```c
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "Failed at psa_cipher_update with error code : %d", status);
        free(outbuf);
        free(out_resp);
        free(out);
        return ESP_FAIL;
    }
```

A single `goto cleanup` label that frees `outbuf`/`out_resp`/`out` would be more
robust against future error branches being added.

## Suggested verification

Inject a single `psa_cipher_update()` failure and record
`heap_caps_get_free_size(MALLOC_CAP_8BIT)` before and after the call. Repeat the
handshake: before the fix the free heap trends downward; after the fix it stays
stable.
