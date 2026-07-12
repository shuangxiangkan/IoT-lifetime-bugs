# BLE GATT service registration misses allocation checks and failure cleanup

I found possible allocation and cleanup issues in BLE GATT service registration.
The code allocates UUID objects for services and characteristics, but does not
check allocation failures before writing to the returned pointer. It also
returns on later GATT configuration failures without releasing the UUID objects
already allocated during the current registration attempt.

File: `src/tal_bluetooth/nimble/tkl_bluetooth.c`

Function: `tkl_ble_gatts_service_add`

Relevant code:

```c
if (!tuya_gatt_svcs) {
    tuya_gatt_svcs = tuya_ble_hs_malloc(TKL_BLE_GATT_SERVICE_MAX_NUM * sizeof(struct ble_gatt_svc_def));
}

memset(tuya_gatt_svcs, 0, TKL_BLE_GATT_SERVICE_MAX_NUM * sizeof(struct ble_gatt_svc_def));
memset(tuya_gatt_chars, 0, 2 * TUYA_BLE_GATT_CHAR_MAX_NUM * sizeof(struct ble_gatt_chr_def));
```

`tuya_gatt_svcs` is not checked for `NULL` before the `memset()`.

The service UUID allocations have the same pattern:

```c
if (p_service->p_service[i].svc_uuid.uuid_type == TKL_BLE_UUID_TYPE_16) {
    tuya_gatt_svcs[i].uuid = (ble_uuid_t *)tal_malloc(sizeof(ble_uuid16_t));
    memcpy((uint8_t *)tuya_gatt_svcs[i].uuid,
           (uint8_t *)BLE_UUID16_DECLARE(p_service->p_service[i].svc_uuid.uuid.uuid16),
           (uint32_t)sizeof(ble_uuid16_t));
} else if (p_service->p_service[i].svc_uuid.uuid_type == TKL_BLE_UUID_TYPE_32) {
    tuya_gatt_svcs[i].uuid = (ble_uuid_t *)tal_malloc(sizeof(ble_uuid32_t));
    memcpy((uint8_t *)tuya_gatt_svcs[i].uuid,
           (uint8_t *)BLE_UUID32_DECLARE(p_service->p_service[i].svc_uuid.uuid.uuid32),
           (uint32_t)sizeof(ble_uuid32_t));
}
```

and characteristic UUID allocations do as well:

```c
if (p_char[j].char_uuid.uuid_type == TKL_BLE_UUID_TYPE_16) {
    tuya_gatt_svcs[i].characteristics[j].uuid = (ble_uuid_t *)tal_malloc(sizeof(ble_uuid16_t));
    memcpy((uint8_t *)tuya_gatt_svcs[i].characteristics[j].uuid,
           (uint8_t *)BLE_UUID16_DECLARE(p_char[j].char_uuid.uuid.uuid16), (uint32_t)sizeof(ble_uuid16_t));
} else if (p_char[j].char_uuid.uuid_type == TKL_BLE_UUID_TYPE_32) {
    tuya_gatt_svcs[i].characteristics[j].uuid = (ble_uuid_t *)tal_malloc(sizeof(ble_uuid32_t));
    memcpy((uint8_t *)tuya_gatt_svcs[i].characteristics[j].uuid,
           (uint8_t *)BLE_UUID32_DECLARE(p_char[j].char_uuid.uuid.uuid32), (uint32_t)sizeof(ble_uuid32_t));
}
```

If any of these allocations fail, the next `memcpy()` dereferences a `NULL`
pointer.

There are also failure returns after UUIDs may already have been allocated:

```c
if (index > TKL_BLE_GATT_CHAR_MAX_NUM) {
    return OPRT_INVALID_PARM;
}

...

rc = ble_gatts_count_cfg(tuya_gatt_svcs);
if (rc != 0) {
    BLE_HS_LOG(INFO, "rc = %d\n", rc);
    return OPRT_INVALID_PARM;
}

rc = ble_gatts_add_svcs(tuya_gatt_svcs);
if (rc != 0) {
    BLE_HS_LOG(INFO, "rc = %d\n", rc);
    return OPRT_INVALID_PARM;
}
```

The deinit path frees these UUID objects:

```c
for (i = 0; i < TKL_BLE_GATT_SERVICE_MAX_NUM; i++) {
    if (tuya_gatt_svcs[i].uuid) {
        tuya_ble_hs_free((void *)tuya_gatt_svcs[i].uuid);
    }
}

for (i = 0; i < TUYA_BLE_GATT_CHAR_MAX_NUM; i++) {
    if (tuya_gatt_chars[0][i].uuid) {
        tuya_ble_hs_free((void *)tuya_gatt_chars[0][i].uuid);
    }
    if (tuya_gatt_chars[1][i].uuid) {
        tuya_ble_hs_free((void *)tuya_gatt_chars[1][i].uuid);
    }
}
```

But the registration failure paths above return directly and do not run this
cleanup. `tuya_ble_hs_malloc()` and `tuya_ble_hs_free()` are wrappers over
`tal_malloc()` and `tal_free()`, so the same cleanup helper is suitable for the
UUID allocations.

Suggested fix: check every UUID allocation before copying into it, and route all
post-allocation validation / `ble_gatts_*` failures through a common cleanup path
that frees any UUIDs allocated during the failed registration attempt.
