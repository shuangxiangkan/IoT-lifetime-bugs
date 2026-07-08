# [Bug] GICv3 ITS cleanup path removes list node after freeing it

## RT-Thread Version

`v5.0.2-2673-gac6dc197a0`

## Hardware Type/Architectures

Likely affects ARM platforms using the GICv3 ITS driver. The issue is in:

`components/drivers/pic/pic-gicv3-its.c`

## Develop Toolchain

Not toolchain-specific. The issue was found by source inspection of the cleanup path.

## Describe the bug

In `components/drivers/pic/pic-gicv3-its.c`, the `_free_all` cleanup path in `gicv3_its_ofw_probe()` frees each `struct gicv3_its` object before removing its embedded list node:

```c
_free_all:
    rt_list_for_each_entry_safe(its, its_next, &its_nodes, list)
    {
        rt_free(its);
        rt_list_remove(&its->list);
    }

    return err;
```

After `rt_free(its)`, accessing `its->list` in `rt_list_remove(&its->list)` is a use-after-free.

The same file already has a helper that uses the safe order and also releases associated resources:

```c
static void its_init_fail(struct gicv3_its *its)
{
    if (its->base)
    {
        rt_iounmap(its->base);
    }

    if (its->cmd_base)
    {
        rt_free_align(its->cmd_base);
    }

    for (int i = 0; i < RT_ARRAY_SIZE(its->tbls); ++i)
    {
        struct its_table *tbl = &its->tbls[i];

        if (tbl->base)
        {
            rt_free_align(tbl->base);
        }
    }

    rt_list_remove(&its->list);
    rt_free(its);
}
```

The `_free_all` branch should not free `its` before using `its->list`.

### 1. Steps to reproduce the behavior

1. Use a configuration/platform that probes GICv3 ITS nodes.
2. Let at least one ITS object be allocated and inserted into `its_nodes`.
3. Trigger an error after insertion that jumps to `_free_all`, for example an error from `its_lpi_table_init(rt_ofw_data(np))`.
4. `_free_all` calls `rt_free(its)` and then accesses `its->list`.

### 2. Expected behavior

The driver should remove the list node before freeing the object, and should release the same associated resources as other ITS initialization failure paths.

One possible fix is to reuse the existing helper:

```c
_free_all:
    rt_list_for_each_entry_safe(its, its_next, &its_nodes, list)
    {
        its_init_fail(its);
    }

    return err;
```

If the helper is not appropriate for all states, the minimal ordering fix is:

```c
rt_list_remove(&its->list);
rt_free(its);
```

### 3. Add screenshot / media if you have them

No screenshot. This is a source-level cleanup bug.

## Other additional context

The issue was detected while scanning RT-Thread with a lightweight resource lifetime checker and then manually confirmed against the source.

Besides the use-after-free, the `_free_all` path currently bypasses `its_init_fail()`, so it may also skip cleanup such as `rt_iounmap(its->base)` for ITS objects that were already partially initialized.
