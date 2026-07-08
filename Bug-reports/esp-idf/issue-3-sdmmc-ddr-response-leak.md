# Title

sdmmc leaks DMA response buffer when DDR bus-mode switch fails

# Answers checklist

- [x] I have read the documentation ESP-IDF Programming Guide and the issue is not addressed there.
- [x] I have updated my IDF branch (master or release) to the latest version and checked that the issue is present there.
- [x] I have searched the issue tracker for a similar issue and not found a similar issue.

# IDF version

`v6.1-dev-5824-gfa8039b5cad`

# Espressif SoC revision

Not hardware-specific. This is a source-level cleanup issue in `components/sdmmc/sdmmc_sd.c`.

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

`sdmmc_enter_higher_speed_mode()` allocates a DMA-capable response buffer for the CMD6 switch-function response. Every error path after that allocation should release the buffer before returning.

# What is the actual behavior?

In `components/sdmmc/sdmmc_sd.c`, `sdmmc_enter_higher_speed_mode()` allocates `response` with `heap_caps_malloc(sizeof(*response), MALLOC_CAP_DMA)` and normally frees it at the shared `out:` label:

```c
    sdmmc_switch_func_rsp_t *response = NULL;
    esp_err_t err = ESP_FAIL;
    response = heap_caps_malloc(sizeof(*response), MALLOC_CAP_DMA);
    if (!response) {
        ESP_LOGE(TAG, "%s: not enough mem, err=0x%x", __func__, ESP_ERR_NO_MEM);
        return ESP_ERR_NO_MEM;
    }
    ...
out:
    free(response);
    return err;
```

Most error paths in the function use `goto out;`, but the UHS-I DDR50 branch returns directly when the host driver's `set_bus_ddr_mode()` callback fails:

```c
    if (((card->host.flags & SDMMC_HOST_FLAG_DDR) != 0) && (card->is_uhs1 == 1)) {
        ...
        card->is_ddr = 1;
        err = (*card->host.set_bus_ddr_mode)(card->host.slot, true);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
            return err;
        }
    }
```

This direct return bypasses `out:` and leaks the DMA-capable `response` buffer.

# Steps to reproduce

This can be reproduced with a host callback that fails the DDR bus-mode switch:

1. Use an SD card/path where `sdmmc_enter_higher_speed_mode()` enters the UHS-I DDR50 branch:
   - `card->host.flags` includes `SDMMC_HOST_FLAG_DDR`.
   - `card->is_uhs1 == 1`.
   - The card reports support for `SD_ACCESS_MODE_DDR50`.
2. Let the card-side `sdmmc_send_cmd_switch_func(... SD_ACCESS_MODE_DDR50 ...)` call succeed.
3. Make `card->host.set_bus_ddr_mode(card->host.slot, true)` return an error.
4. The function returns that error directly instead of jumping to `out:`.
5. The `response` buffer allocated with `MALLOC_CAP_DMA` is not freed.

Suggested verification:

1. Provide a `set_bus_ddr_mode` stub that always returns an error.
2. Call `sdmmc_enter_higher_speed_mode()` on the DDR50 path.
3. Verify that the original error code is returned.
4. Track `heap_caps_get_free_size(MALLOC_CAP_DMA)` or the relevant heap accounting across repeated calls.
5. Before the fix, DMA-capable free memory should decrease. After the fix, it should remain stable for this path.

# Debug Logs

No runtime log is available. The issue was identified by source inspection of the error path.

The expected log line on the leaking path is:

```text
sdmmc_enter_higher_speed_mode: failed to switch bus to DDR mode (<err>)
```

# Diagnostic report archive

Not available. This is a source-level cleanup issue and was not reproduced on a specific board.

# More Information

Replacing the direct return with the existing shared cleanup path should fix the leak:

```c
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "%s: failed to switch bus to DDR mode (0x%x)", __func__, err);
            goto out;
        }
```

There may also be a separate state consistency question because `card->is_ddr` is set before the host-side switch succeeds. This report is only about the leaked DMA response buffer.
