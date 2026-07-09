# FTL initialization error paths leak internal buffers (`ftl.c` and `ftl_nand.c`)

Both FTL initialization functions allocate internal buffers into the device
structure and then, on later failure paths, free only the device structure. The
buffers they already allocated are leaked. The two files share the same pattern,
so they are reported together.

Version checked: `926549785`

## `ftl_nand_initialize()` leaks `block_map`

File: `os/fs/driver/mtd/ftl_nand.c`

```c
dev = (struct ftl_nand_s *)kmm_malloc(sizeof(struct ftl_nand_s));
if (dev) {
    ...
    dev->block_map = (int *)kmm_malloc(sizeof(int) * dev->geo.neraseblocks);
    if (!dev->block_map) {
        dbg("ERROR: Failed to allocate logical mapping of blocks\n");
        kmm_free(dev);
        return -ENOMEM;
    }

    ...

#ifdef CONFIG_FS_WRITABLE
    dev->eblock  = (FAR uint8_t *)kmm_malloc(dev->geo.erasesize);
    if (!dev->eblock) {
        dbg("ERROR: Failed to allocate an erase block buffer\n");
        kmm_free(dev);            /* leaks dev->block_map */
        return -ENOMEM;
    }
#endif

    ...

    ret = register_blockdriver(devname, &g_bops, 0, dev);
    if (ret < 0) {
        dbg("ERROR: register_blockdriver failed: %d\n", -ret);
        kmm_free(dev);            /* leaks dev->block_map, and dev->eblock */
    }
}
```

`dev->block_map` is allocated before the optional erase-block buffer and before
the block driver is registered:

- If the `dev->eblock` allocation fails under `CONFIG_FS_WRITABLE`, only `dev` is
  freed, leaking `dev->block_map`.
- If `register_blockdriver()` fails, only `dev` is freed. At that point
  `dev->block_map` is always allocated, and `dev->eblock` may be as well.

## `ftl_initialize()` leaks `eblock`

File: `os/fs/driver/mtd/ftl.c`

```c
dev = (struct ftl_struct_s *)kmm_malloc(sizeof(struct ftl_struct_s));
if (dev) {
    ...
#ifdef CONFIG_FS_WRITABLE
    dev->eblock  = (FAR uint8_t *)kmm_malloc(dev->geo.erasesize);
    if (!dev->eblock) {
        dbg("ERROR: Failed to allocate an erase block buffer\n");
        kmm_free(dev);
        return -ENOMEM;
    }
#endif

    ...

#ifdef FTL_HAVE_RWBUFFER
    ret = rwb_initialize(&dev->rwb);
    if (ret < 0) {
        dbg("ERROR: rwb_initialize failed: %d\n", ret);
        kmm_free(dev);            /* leaks dev->eblock */
        return ret;
    }
#endif

    ...

    ret = register_blockdriver(devname, &g_bops, 0, dev);
    if (ret < 0) {
        dbg("ERROR: register_blockdriver failed: %d\n", -ret);
        kmm_free(dev);            /* leaks dev->eblock */
    }
}
```

Under `CONFIG_FS_WRITABLE`, `dev->eblock` is allocated before the read/write
buffer setup and before the block driver is registered. Both the
`rwb_initialize()` failure path and the `register_blockdriver()` failure path
free only `dev`.

`register_blockdriver()` stores `dev` in `node->i_private` only on success, so on
these failure paths ownership has not been transferred and the initialization
function still owns everything it allocated.

## Why this looks unintentional

In both functions, the *earlier* failure paths free only `dev` correctly, because
they run before any internal buffer has been allocated. Only the paths that run
after an internal allocation are missing the corresponding release, which
suggests the cleanup was simply not extended when those allocations were added.

## Suggested fix

Release the internal buffers before freeing `dev` on each failure path that runs
after they have been allocated. For `ftl_nand_initialize()`, the
`register_blockdriver()` failure path should free `dev->block_map`, and also
`dev->eblock` under `CONFIG_FS_WRITABLE`, before freeing `dev`. For
`ftl_initialize()`, the `rwb_initialize()` and `register_blockdriver()` failure
paths should free `dev->eblock` before freeing `dev`.

If `rwb_initialize()` leaves partially initialized state behind on failure, that
should be unwound as well.

## Severity

Low. These paths run only at initialization and only when an allocation fails or
the block driver cannot be registered — situations in which the system is already
in trouble. The fix is small and self-contained, and the intent of the existing
cleanup is unambiguous.
