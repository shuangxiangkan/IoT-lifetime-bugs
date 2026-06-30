# Antelope `index_create()`: use-after-free and dangling references on `storage_put_index()` failure path

## Summary

In `os/storage/antelope/index.c`, `index_create()` publishes the freshly allocated
`index_t` to both the owning attribute (`attr->index`) and the global `indices`
list **before** the last fallible initialization step (`storage_put_index()`).
When that step fails, the error path frees the `index_memb` pool slot but does
**not** roll back the two references it already published, and it reads a field
of the freed object in the log statement. This leaves a use-after-free read plus
two dangling references to a pool slot that is immediately returned to the
allocator.

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
           index->descriptor_file);             /* read of freed object  */
    return DB_INDEX_ERROR;                       /* attr->index and indices
                                                    still point to the slot */
  }
```

## Problems

The failure path leaves three defects, all because the object was published
before `storage_put_index()` could fail:

1. **Use-after-free read.** `index->descriptor_file` is read in the `PRINTF`
   *after* `memb_free(&index_memb, index)`. `memb_free()` does not clear the
   block, so on a single-threaded run the data is usually still intact, but the
   slot is formally released and may be re-allocated/overwritten by the next
   `memb_alloc()` (e.g. from an interrupting protothread). This is a latent UAF.

2. **Dangling `attr->index`.** `attr->index = index` is never reset to `NULL` on
   this path, so the attribute keeps pointing at a freed pool slot. Consequences:
   - The guard at the top of `index_create()` (`if(attr->index != NULL) ... already indexed`)
     wrongly believes the attribute already has an index.
   - Query paths dereference `attr->index->api` on a freed object.
   - A later `index_destroy()`/`index_release()` would free the same slot again
     (double free).

3. **Dangling global `indices` entry.** `list_push(indices, index)` is never
   matched by a `list_remove(indices, index)`, so any traversal of the global
   `indices` list visits a freed slot.

The intended cleanup ordering is visible in `index_release()`:

```c
  index->attr->index = NULL;
  list_remove(indices, index);
  memb_free(&index_memb, index);
```

The failure path in `index_create()` skips the first two steps.

## Trigger condition

The index backend creates successfully (`api->create()` returns OK) but the
descriptor persistence fails (`storage_put_index()` returns an error). Realistic
causes: storage write error, full backing store, corrupted/short descriptor
file, or a vendor storage backend returning failure.

## Impact

- Use-after-free read of the descriptor filename.
- Dangling `attr->index` → stale/incorrect "already indexed" state and
  dereference of a freed object from query paths.
- Dangling global `indices` list node.
- Potential double free of the same `index_memb` slot via a later release.
- Pool slot may be re-used while old references still point at it, corrupting the
  global list and query results.

On a fixed-size pool (`MEMB(index_memb, index_t, DB_MAX_INDEXES)`) the corruption
is stable and reproducible once the failing descriptor store is hit.

## Suggested fix

Preferred: publish the object only after every fallible step has succeeded
(persist the descriptor first, then assign `attr->index` and `list_push`):

```c
  if(index->descriptor_file[0] != '\0' &&
     DB_ERROR(storage_put_index(index))) {
    char descriptor_file[DB_MAX_FILENAME_LENGTH];

    strncpy(descriptor_file, index->descriptor_file, sizeof(descriptor_file));
    descriptor_file[sizeof(descriptor_file) - 1] = '\0';

    api->destroy(index);
    memb_free(&index_memb, index);

    PRINTF("DB: Failed to store index data in file \"%s\"\n", descriptor_file);
    return DB_INDEX_ERROR;
  }

  attr->index = index;
  list_push(indices, index);
```

Alternative (keep current publish order, but fully roll back and copy the
filename before freeing):

```c
  if(index->descriptor_file[0] != '\0' &&
     DB_ERROR(storage_put_index(index))) {
    char descriptor_file[DB_MAX_FILENAME_LENGTH];

    strncpy(descriptor_file, index->descriptor_file, sizeof(descriptor_file));
    descriptor_file[sizeof(descriptor_file) - 1] = '\0';

    attr->index = NULL;
    list_remove(indices, index);
    api->destroy(index);
    memb_free(&index_memb, index);

    PRINTF("DB: Failed to store index data in file \"%s\"\n", descriptor_file);
    return DB_INDEX_ERROR;
  }
```

## Suggested verification (fault injection)

1. Let `memb_alloc()` and `api->create()` succeed.
2. Force `storage_put_index()` to return an error.
3. Assert `attr->index == NULL`.
4. Assert the global `indices` list does not contain the freed address.
5. Assert `memb_numfree(&index_memb)` is restored to the pre-call value.
6. Create an index again and confirm the slot is safely re-usable.
7. Run under ASan / pool poisoning to confirm no read of the freed object.
