# drivers/interrupt_controller: GICv3 ITS leaks the ITT when the MAPD command fails

## Summary

In `drivers/interrupt_controller/intc_gicv3_its.c`,
`gicv3_its_init_device_id()` allocates an Interrupt Translation Table (ITT) with
`k_aligned_alloc()` and then issues a MAPD command to map it to the device ID. If
`its_send_mapd_cmd()` fails, the function returns without freeing the ITT. The
allocation is not recorded in driver state, so the only pointer to it is lost.

## Affected code

`drivers/interrupt_controller/intc_gicv3_its.c`, `gicv3_its_init_device_id()`:

```c
    /* ITT must be of power of 2 */
    nr_ites = MAX(2, nites);
    alloc_size = ROUND_UP(nr_ites * entry_size, 256);
    ...
    itt = k_aligned_alloc(256, alloc_size);
    if (!itt) {
        return -ENOMEM;
    }
    memset(itt, 0, alloc_size);
#ifdef CONFIG_GIC_V3_ITS_DMA_NONCOHERENT
    arch_dcache_flush_and_invd_range(itt, alloc_size);
#endif

    /* size is log2(ites) - 1, equivalent to (fls(ites) - 1) - 1 */
    ret = its_send_mapd_cmd(data, device_id, fls_z(nr_ites) - 2, (uintptr_t)itt, true);
    if (ret) {
        LOG_ERR("Failed to map device id %x ITT table", device_id);
        return ret;                  /* itt leaked */
    }

    return 0;
```

## Problem

On the MAPD failure path:

- `itt` was successfully allocated.
- The command did not map the table into the GIC ITS.
- The driver does not store `itt` anywhere.
- The function returns and drops the only pointer.

So this allocation is not a successful hardware ownership transfer; it is an
error-path leak.

## Impact

The ITT is at least 256 bytes (`ROUND_UP(nr_ites * entry_size, 256)`, with
`nr_ites = MAX(2, nites)`) and grows with the number of interrupt vectors. A
failed MAPD usually indicates an abnormal ITS command-queue or hardware state,
so it is not typically application-triggerable, but initialization retries could
leak aligned heap repeatedly.

## Suggested fix

```c
    ret = its_send_mapd_cmd(data, device_id, fls_z(nr_ites) - 2, (uintptr_t)itt, true);
    if (ret) {
        LOG_ERR("Failed to map device id %x ITT table", device_id);
        k_free(itt);
        return ret;
    }
```

Before applying, confirm that a failed `its_send_mapd_cmd()` cannot take effect
asynchronously later. Given the command's synchronous return semantics and the
fact that the driver records no ITT address on failure, keeping the allocation
would not help any later teardown anyway.

## Suggested tests

Stub `its_send_mapd_cmd()` to return an error and verify:

- `k_free(itt)` is called exactly once.
- The original error code is returned unchanged.
- Under `CONFIG_GIC_V3_ITS_DMA_NONCOHERENT`, the cache flush does not interfere
  with the free.
- The success path does not free an ITT still in use by the hardware.
