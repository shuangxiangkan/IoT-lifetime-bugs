# Antelope `index_create()`: stale attribute and list references after `storage_put_index()` failure

## Summary

In `os/storage/antelope/index.c`, `index_create()` publishes the freshly allocated
`index_t` to both the owning attribute (`attr->index`) and the global `indices`
list **before** the last fallible initialization step (`storage_put_index()`).
When that step fails, the error path frees the `index_memb` pool slot but does
**not** roll back the two references it already published. The attribute and
global list consequently retain pointers to a slot that has been returned to
the allocator and can be reused by a later `memb_alloc()`.

## Affected code

`os/storage/antelope/index.c`, `index_create()`:

```c
  attr->index = index;          /* published to attribute  */
  list_push(indices, index);    /* published to global list */

  if(index->descriptor_file[0] != '\0' &&
     DB_ERROR(storage_put_index(index))) {
    api->destroy(index);
    memb_free(&index_memb, index);              /* slot returned to pool */
    PRINTF("DB: Failed to store index data in file \"%s\"\n",
           index->descriptor_file);             /* access after pool release */
    return DB_INDEX_ERROR;                       /* attr->index and indices
                                                    still point to the slot */
  }
```

## Problems

The failure path leaves two persistent stale references because the object was
published before `storage_put_index()` could fail:

1. **Stale `attr->index`.** `attr->index = index` is never reset to `NULL` on
   this path, so the attribute keeps pointing at a freed pool slot. Consequences:
   - The guard at the top of `index_create()` (`if(attr->index != NULL) ... already indexed`)
     wrongly believes the attribute already has an index.
   - Insert and query paths can use an index whose backend resources have already
     been destroyed.
   - If the pool slot is reused, the old attribute can silently refer to a
     different index object.

2. **Stale global `indices` entry.** `list_push(indices, index)` is never
   matched by a `list_remove(indices, index)`, so any traversal of the global
   `indices` list visits a slot that the pool considers free. A later allocation
   may overwrite that slot while it is still linked into the list.

The log statement also accesses `index->descriptor_file` after returning the
slot to `index_memb`. Contiki-NG's `memb_free()` only clears the pool's
allocation bitmap; it does not release or clear the static backing storage.
Therefore this is not a conventional heap use-after-free, but the access is
poorly ordered and relies on data in a slot that is no longer owned by
`index_create()`.

The intended cleanup ordering is visible in `index_release()`:

```c
  index->attr->index = NULL;
  list_remove(indices, index);
  memb_free(&index_memb, index);
```

The failure path in `index_create()` skips the first two steps.

## Trigger condition

The index backend creates successfully (`api->create()` returns OK), sets a
non-empty `descriptor_file`, but descriptor persistence fails
(`storage_put_index()` returns an error). With the built-in components this
principally affects max-heap indexes; inline indexes leave `descriptor_file`
empty and skip this call. Realistic causes include failure to open the relation
metadata file, a full backing store, or a failed/short metadata write.

