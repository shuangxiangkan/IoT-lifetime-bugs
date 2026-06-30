# sdmmc: DMA response buffer leaked when DDR bus-mode switch fails in `sdmmc_enter_higher_speed_mode()`

## Summary

In `components/sdmmc/sdmmc_sd.c`, `sdmmc_enter_higher_speed_mode()` allocates a
DMA-capable response buffer for the CMD6 switch-function response and frees it at
a shared `out:` label. Every error path in the function jumps to `out:` — except
the UHS-I DDR50 branch, which returns directly when the host's
`set_bus_ddr_mode()` fails, leaking the DMA buffer.

## Affected code

`components/sdmmc/sdmmc_sd.c`, `sdmmc_enter_higher_speed_mode()`:

```c
    sdmmc_switch_func_rsp_t *response = NULL;
    esp_err_t err = ESP_FAIL;
    response = heap_caps_malloc(sizeof(*response), MALLOC_CAP_DMA);
    if (!response) {
        ESP_LOGE(TAG, "%s: not enough mem, err=0x%x", __func__, ESP_ERR_NO_MEM);
        return ESP_ERR_NO_MEM;
    }
    ...
    if (((card->host.flags & SDMMC_HOST_FLAG_DDR) != 0) && (card->is_uhs1 == 1)) {
        ...
        card->is_ddr = 1;
        err = (*card->host.set_bus_ddr_mode)(card->host.slot, true);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
            return err;          /* bypasses out: free(response) -> DMA buffer leaked */
        }
    } else if (...) {
        ...
    }

out:
    free(response);
    return err;
```

## Problem

Every other failure branch in this function uses `goto out;`, which runs
`free(response)`. The DDR50 `set_bus_ddr_mode()` failure path is the only one
that does `return err;` directly, so `response` (allocated with `MALLOC_CAP_DMA`)
is never freed on that path.

## Trigger condition

1. The SD card supports SWITCH_FUNC and DDR50.
2. The card-side DDR50 switch (CMD6) succeeds.
3. The host driver's `set_bus_ddr_mode(slot, true)` returns an error.

Each occurrence leaks one `sdmmc_switch_func_rsp_t` DMA buffer. DMA-capable
memory is typically scarcer than ordinary heap, so even a small per-occurrence
leak permanently reduces DMA memory available to other peripherals.

## Note (out of scope for this report)

On this path the card-side mode has already been changed to DDR50 while the
host-side switch failed, so callers may also need to consider device-state
rollback. This report only covers the memory leak and does not claim the
protocol-state mismatch as a confirmed bug.

## Suggested fix

Replace the direct return with the shared cleanup:

```c
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
            goto out;
        }
```

## Suggested verification

Provide a `set_bus_ddr_mode` stub that always returns an error. Verify:

- The original error code is still returned.
- `response` is freed.
- Repeated calls do not progressively reduce `MALLOC_CAP_DMA` free space.
