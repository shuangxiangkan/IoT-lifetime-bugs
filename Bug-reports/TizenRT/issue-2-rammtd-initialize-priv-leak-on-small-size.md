# Memory leak in `rammtd_initialize()` when the RAM region is too small

`rammtd_initialize()` allocates its private state before validating that the
supplied RAM region contains at least one full erase block. When the region is
too small, it returns `NULL` without freeing that allocation.

Version checked: `926549785`

File: `os/fs/driver/mtd/rammtd/rammtd.c`

Function: `rammtd_initialize`

Relevant code:

```c
priv = (FAR struct ram_dev_s *)kmm_zalloc(sizeof(struct ram_dev_s));
if (!priv) {
    fdbg("Failed to allocate the RAM MTD state structure\n");
    return NULL;
}

/* Use memset to initialize when it started, to guarantees cleaned space for sw reset */
memset(start, CONFIG_RAMMTD_ERASESTATE, size);

/* Force the size to be an even number of the erase block size */

nblocks = size / CONFIG_RAMMTD_ERASESIZE;
if (nblocks < 1) {
    fdbg("Need to provide at least one full erase block\n");
    return NULL;
}
```

If `size < CONFIG_RAMMTD_ERASESIZE`, `nblocks` is zero and the function returns
`NULL` without freeing `priv`.

Unlike an out-of-memory path, this is reachable purely through the arguments: any
caller passing a buffer smaller than `CONFIG_RAMMTD_ERASESIZE` triggers it.

Suggested fix: free `priv` before returning from the `nblocks < 1` error path:

```c
if (nblocks < 1) {
    fdbg("Need to provide at least one full erase block\n");
    kmm_free(priv);
    return NULL;
}
```

## Related: the region is written before it is validated

`memset(start, CONFIG_RAMMTD_ERASESTATE, size)` runs before the `nblocks` check,
so a caller that passes an under-sized region has its buffer overwritten even
though the call then fails. Moving the size validation ahead of both the
allocation and the `memset()` would address the leak and this ordering issue at
once.
