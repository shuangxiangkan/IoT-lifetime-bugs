# mgmt/settings: heap buffers leaked on access-hook and OOM paths in settings read/write/delete

## Summary

In `subsys/mgmt/mcumgr/grp/settings_mgmt/src/settings_mgmt.c`, the `read`,
`write`, and `delete` command handlers leak heap-allocated buffers on several
error paths when `CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP` is enabled. The
access-hook rejection path (`MGMT_CB_ERROR_RC`) returns directly, bypassing the
`end:` cleanup; `settings_mgmt_read()` also has an incomplete combined-NULL check.
`settings_mgmt_save()` in the same file handles the identical callback status
correctly, confirming these are cleanup inconsistencies rather than intended
ownership transfer.

These handlers are reachable by a remote management client, so an application
access hook that repeatedly denies requests turns these leaks into a
remotely-triggerable heap-exhaustion condition.

All issues are guarded by `CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP`; the
non-heap configuration uses stack arrays and is unaffected.

## Affected code

### `settings_mgmt_read()` — incomplete NULL check leaks `data`

```c
    key_name = (char *)k_malloc(key.len + 1);
    data = (uint8_t *)k_malloc(max_size);

    if (data == NULL || key_name == NULL) {
        if (key_name != NULL) {
            k_free(key_name);
        }
        return MGMT_ERR_ENOMEM;      /* if key_name==NULL, data!=NULL -> data leaked */
    }
```

### `settings_mgmt_read()` — access hook `MGMT_CB_ERROR_RC` leaks both buffers

```c
    if (status != MGMT_CB_OK) {
        if (status == MGMT_CB_ERROR_RC) {
            return ret_rc;           /* bypasses end: -> leaks key_name AND data */
        }
        ok = smp_add_cmd_err(zse, ret_group, (uint16_t)ret_rc);
        goto end;                    /* goto end frees both - correct */
    }
    ...
end:
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);
    k_free(data);
#endif
    return MGMT_RETURN_CHECK(ok);
```

### `settings_mgmt_write()` — access hook `MGMT_CB_ERROR_RC` leaks `key_name`

```c
    if (status != MGMT_CB_OK) {
        if (status == MGMT_CB_ERROR_RC) {
            return ret_rc;           /* bypasses end: k_free(key_name) */
        }
        ok = smp_add_cmd_err(zse, ret_group, (uint16_t)ret_rc);
        goto end;
    }
    ...
end:
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);
#endif
    return MGMT_RETURN_CHECK(ok);
```

### `settings_mgmt_delete()` — both callback error paths leak `key_name`

In `delete`, `key_name` is freed only inline after `settings_delete()`; the
`end:` label does **not** free it. So both the direct return *and* the `goto end`
leak:

```c
    if (status != MGMT_CB_OK) {
        if (status == MGMT_CB_ERROR_RC) {
            return ret_rc;           /* leaks key_name */
        }
        ok = smp_add_cmd_err(zse, ret_group, (uint16_t)ret_rc);
        goto end;                    /* also leaks: end: has no k_free */
    }

    rc = settings_delete(key_name);
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);                /* only the normal path frees */
#endif
    ...
end:
    return MGMT_RETURN_CHECK(ok);    /* no cleanup here */
```

## Correct contrast in the same file

`settings_mgmt_save()` frees `key_name` before returning on the same callback
status:

```c
    if (status == MGMT_CB_ERROR_RC) {
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
        k_free(key_name);
#endif
        return ret_rc;
    }
```

This shows the callback does not take ownership of `key_name`, so the missing
frees in read/write/delete are inconsistencies, not ownership transfer.

## Impact

The settings handlers are invoked by a remote management client. Whether callback
denial is triggerable depends on the application's registered access hook, but
denial is normal, supported access-control behavior — not a hardware fault or a
rare OOM. If an application uses an access hook that keeps denying certain keys:

- `read` leaks the key buffer and the value buffer each time;
- `write` / `delete` leak the key buffer each time;

so unauthorized requests can progressively exhaust the kernel heap. This makes
the severity higher than an ordinary init-time error-path leak.

## Suggested fix

Route every allocated buffer through the shared `end:` cleanup. For read/write,
replace the direct `return ret_rc` with:

```c
    if (status == MGMT_CB_ERROR_RC) {
        rc = ret_rc;            /* mind existing return-value / MGMT_RETURN_CHECK(ok) semantics */
        goto end;
    }
```

For `delete`, move the cleanup into `end:` and drop the inline free to avoid a
double free:

```c
end:
#ifdef CONFIG_MCUMGR_GRP_SETTINGS_BUFFER_TYPE_HEAP
    k_free(key_name);
#endif
    return MGMT_RETURN_CHECK(ok);
```

A lower-risk alternative is to free the buffers immediately before each direct
return.

## Suggested tests

| Handler | Injected condition | Expected |
|---|---|---|
| read | key allocation fails | `data` not leaked |
| read | data allocation fails | `key_name` not leaked |
| read | callback returns `MGMT_CB_ERROR_RC` | key/data both freed |
| read | callback returns other error | key/data both freed |
| write | callback returns `MGMT_CB_ERROR_RC` | key freed |
| write | callback returns other error | key freed |
| delete | callback returns `MGMT_CB_ERROR_RC` | key freed |
| delete | callback returns other error | key freed |
| all | callback OK | no double free |

Use a heap listener, ztest allocator fault injection, or wrapped `k_malloc`/
`k_free` for exact accounting.
