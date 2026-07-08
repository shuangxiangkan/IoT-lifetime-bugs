# Title

protocomm Security1 leaks response objects when `psa_cipher_update()` fails

# Answers checklist

- [x] I have read the documentation ESP-IDF Programming Guide and the issue is not addressed there.
- [x] I have updated my IDF branch (master or release) to the latest version and checked that the issue is present there.
- [x] I have searched the issue tracker for a similar issue and not found a similar issue.

# IDF version

`v6.1-dev-5824-gfa8039b5cad`

# Espressif SoC revision

Not hardware-specific. This is a source-level resource cleanup issue in `components/protocomm/src/security/security1.c`.

# Operating System used

Linux x86_64. The issue was found by source inspection, not by an OS-specific build or runtime environment.

# How did you build your project?

Not applicable. No project build is required to observe the cleanup path in the source.

# If you are using Windows, please specify command line type

Not applicable.

# Development Kit

Not hardware-specific.

# Power Supply used

Not applicable.

# What is the expected behavior?

When `handle_session_command1()` allocates the Security1 response objects and a later error occurs, every locally-owned allocation should be released before returning an error.

In particular, if the second `psa_cipher_update()` call fails, the function should free `outbuf`, `out_resp`, and `out` before returning `ESP_FAIL`.

# What is the actual behavior?

In `components/protocomm/src/security/security1.c`, `handle_session_command1()` allocates `out` and `out_resp`, then allocates `outbuf`. If the second `psa_cipher_update()` call fails, the error path frees only `outbuf` and returns:

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
        return ESP_FAIL;
    }
```

At this point, `out` and `out_resp` have not yet been linked into `resp`:

```c
    out_resp->device_verify_data.data = outbuf;
    out_resp->device_verify_data.len = PUBLIC_KEY_LEN;
    ...
    out->sr1 = out_resp;
    resp->proto_case = SESSION_DATA__PROTO_SEC1;
    resp->sec1 = out;
```

Therefore `sec1_session_setup_cleanup()` cannot reclaim them. The outer request handler also returns immediately on setup failure and does not call the response cleanup function on this path.

This leaks two small heap objects each time this error path is taken.

# Steps to reproduce

This can be reproduced by injecting or stubbing a failure from the second `psa_cipher_update()` call in `handle_session_command1()`:

1. Start a Security1 session and reach `Session_Command1`.
2. Let the allocations for `out`, `out_resp`, and `outbuf` succeed.
3. Make the second `psa_cipher_update()` call return a value other than `PSA_SUCCESS`.
4. Observe that the function frees only `outbuf` and returns `ESP_FAIL`.
5. `out` and `out_resp` remain locally owned and are not reachable through `resp`, so they are leaked.

Suggested verification:

1. Record `heap_caps_get_free_size(MALLOC_CAP_8BIT)` before entering this path.
2. Inject the `psa_cipher_update()` failure.
3. Repeat the handshake.
4. Before the fix, free heap should trend downward. After the fix, it should remain stable for this path.

# Debug Logs

No runtime log is available. The issue was identified by source inspection of the error path.

The expected log line on the leaking path is:

```text
Failed at psa_cipher_update with error code : <status>
```

# Diagnostic report archive

Not available. This is a source-level cleanup issue and was not reproduced on a specific board.

# More Information

The allocation failure paths in the same block already free both `out` and `out_resp`, so the missing cleanup on the `psa_cipher_update()` failure path appears inconsistent.

A minimal fix is:

```c
    if (status != PSA_SUCCESS) {
        ESP_LOGE(TAG, "Failed at psa_cipher_update with error code : %d", status);
        free(outbuf);
        free(out_resp);
        free(out);
        return ESP_FAIL;
    }
```

A shared cleanup label would also work and may be more robust if more error paths are added later.
