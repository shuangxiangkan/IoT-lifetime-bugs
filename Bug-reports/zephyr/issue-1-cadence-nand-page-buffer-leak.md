# drivers/flash: Cadence NAND `cdns_nand_read()` leaks two page buffers on the success path

## Summary

In `drivers/flash/flash_cadence_nand_ll.c`, the multi-page read branch of
`cdns_nand_read()` allocates two temporary page buffers (`first_end_page`,
`last_end_page`) but does not free them on the normal success path. It also leaks
one buffer on the partial-allocation-failure path. The three read-error paths in
the same branch free both buffers correctly, which confirms the omission is a bug
rather than an intentional ownership transfer.

## Affected code

`drivers/flash/flash_cadence_nand_ll.c`, `cdns_nand_read()`:

```c
    } else if ((check_page_last == 0) && (check_page_first == 0) && (page_count > 2)) {
        first_end_page = (char *)k_malloc(sizeof(char) * (params->page_size));
        last_end_page = (char *)k_malloc(sizeof(char) * (params->page_size));
        if ((first_end_page != NULL) && (last_end_page != NULL)) {
            memset(first_end_page, 0xFF, sizeof(char) * (params->page_size));
            memset(last_end_page, 0xFF, sizeof(char) * (params->page_size));
        } else {
            LOG_ERR("Memory allocation error occurred %s", __func__);
            return -ENOSR;                    /* leaks whichever alloc succeeded */
        }
        ret = cdns_read_data(params, start_page_number, first_end_page, 1);
        if (ret != 0) {
            k_free(first_end_page);
            k_free(last_end_page);
            return ret;                        /* error paths free both - correct */
        }
        ...
        ret = cdns_read_data(params, end_page_number, last_end_page, 1);
        if (ret != 0) {
            k_free(last_end_page);
            k_free(first_end_page);
            return ret;
        }
        ...
        ret = cdns_read_data(params, (++start_page_number), ((char *)buffer + bytes_dif),
                             (page_count - 2));
        if (ret != 0) {
            k_free(last_end_page);
            k_free(first_end_page);
            return ret;
        }
        memcpy((char *)buffer, first_end_page + r_bytes, bytes_dif);
        memcpy(((char *)buffer + bytes_dif + ((page_count - 2) * (params->npages_per_block))),
               last_end_page, lp_bytes_dif);
    }

    return 0;                                  /* success: neither buffer freed */
```

## Problems

This branch is entered when the read is unaligned at both ends and spans more
than two NAND pages (`check_page_last == 0 && check_page_first == 0 &&
page_count > 2`).

1. **Success-path double leak.** After all three `cdns_read_data()` calls succeed
   and the two `memcpy()`s run, the function falls through to `return 0` without
   `k_free(first_end_page)` / `k_free(last_end_page)`. The buffers are not stored
   in driver state and not returned to the caller, so both page-sized allocations
   leak on every successful read of this shape. With a 2 KiB or 4 KiB page size,
   that is 4 KiB or 8 KiB leaked per call.

2. **Partial-allocation-failure leak.** The combined NULL check
   (`first_end_page != NULL && last_end_page != NULL`) returns `-ENOSR` on the
   `else` branch without freeing the allocation that did succeed (e.g.
   `first_end_page != NULL, last_end_page == NULL` leaks `first_end_page`, and
   vice-versa).

The three read-error branches all free both buffers, confirming these two paths
are missing cleanup, not deliberate ownership transfer.

## Suggested fix

Free both buffers on all exits. `k_free(NULL)` is allowed in Zephyr, so the NULL
check can be simplified:

```c
    first_end_page = k_malloc(sizeof(char) * (params->page_size));
    last_end_page = k_malloc(sizeof(char) * (params->page_size));

    if ((first_end_page == NULL) || (last_end_page == NULL)) {
        k_free(first_end_page);
        k_free(last_end_page);
        LOG_ERR("Memory allocation error occurred %s", __func__);
        return -ENOSR;
    }
    ...
    memcpy(...);
    memcpy(...);

    k_free(last_end_page);
    k_free(first_end_page);
```

A single cleanup label is even cleaner:

```c
    ret = cdns_read_data(...);
    if (ret != 0) {
        goto free_end_pages;
    }
    ...
    ret = 0;
free_end_pages:
    k_free(last_end_page);
    k_free(first_end_page);
    return ret;
```
