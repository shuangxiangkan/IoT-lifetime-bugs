# Antelope `index_load()`: fixed-pool slot leak on two error paths

## Summary

In `os/storage/antelope/index.c`, `index_load()` allocates an `index_t` from the
fixed-size `index_memb` pool, but two of its error paths return without calling
`memb_free()`. Each failed load permanently consumes one `index_memb` slot.
Because the pool is bounded by `DB_MAX_INDEXES`, repeated failures (e.g. from
corrupted or unsupported descriptors) exhaust the pool and make subsequent
legitimate index loads/creations fail forever.

## Affected code

`os/storage/antelope/index.c`, `index_load()`:

```c
  index = memb_alloc(&index_memb);
  if(index == NULL) {
    PRINTF("DB: No more index objects available\n");
    return DB_ALLOCATION_ERROR;
  }

  if(DB_ERROR(storage_get_index(index, rel, attr))) {
    PRINTF("DB: Failed load an index descriptor from storage\n");
    memb_free(&index_memb, index);          /* OK: slot freed */
    return DB_INDEX_ERROR;
  }

  index->rel = rel;
  index->attr = attr;
  index->opaque_data = NULL;

  api = find_index_api(index->type);
  if(api == NULL) {
    PRINTF("DB: No API for index type %d\n", index->type);
    return DB_INDEX_ERROR;                  /* LEAK: index never freed */
  }

  index->api = api;

  if(DB_ERROR(api->load(index))) {
    PRINTF("DB: Index-specific load failed\n");
    return DB_INDEX_ERROR;                  /* LEAK: index never freed */
  }

  list_push(indices, index);
  attr->index = index;
  index->flags = INDEX_READY;

  return DB_OK;
```

The `storage_get_index()` failure path correctly calls `memb_free()`; the two
later paths do not, which makes the inconsistency clear.

## Leak path 1 — unknown index type

`find_index_api(index->type)` returns `NULL` for an unsupported type. The slot
allocated at the top is leaked. A corrupted or attacker-supplied descriptor can
specify an unsupported `type` and leak one slot per load attempt.

## Leak path 2 — backend load failure

`api->load(index)` returns an error and the slot is leaked.

Additionally, `api->load()` may already have allocated backend state
(`opaque_data` or similar) before failing. A complete fix should consider the
backend's failure contract:

- Does `load()` guarantee it rolls back its own partial state on failure?
- Should `api->destroy(index)` be called before freeing the pool slot?
- Is there a dedicated partial-load cleanup path?

## Impact

`index_memb` is bounded by `DB_MAX_INDEXES`. Repeatedly loading descriptors that
are unknown-type, un-loadable by the backend, or otherwise corrupt/incompatible
will exhaust the pool. After exhaustion, legitimate index creation and recovery
fail persistently. On small IoT deployments, leaking even a few slots can fully
disable indexing.

## Suggested fix

Unknown-API path:

```c
  if(api == NULL) {
    PRINTF("DB: No API for index type %d\n", index->type);
    memb_free(&index_memb, index);
    return DB_INDEX_ERROR;
  }
```

Backend-load-failure path:

```c
  if(DB_ERROR(api->load(index))) {
    PRINTF("DB: Index-specific load failed\n");
    api->destroy(index);   /* only if the backend contract allows cleanup of partial state */
    memb_free(&index_memb, index);
    return DB_INDEX_ERROR;
  }
```

Or, more robustly, a single cleanup label:

```c
  db_result_t result = DB_INDEX_ERROR;
  bool backend_initialized = false;

  /* ... */

exit:
  if(result != DB_OK) {
    if(backend_initialized) {
      api->destroy(index);
    }
    memb_free(&index_memb, index);
  }
  return result;
```

## Suggested verification

Test 1 — unknown type:
1. Build a descriptor so `storage_get_index()` succeeds.
2. Set a `type` not supported by `find_index_api()`.
3. Call `index_load()` more than `DB_MAX_INDEXES` times.
4. After the fix, each call returns an error but `memb_numfree(&index_memb)` is unchanged.

Test 2 — backend load failure:
1. Use a valid `type`.
2. Mock `api->load()` to return an error.
3. Check both the top-level pool slot and any backend partial allocations are reclaimed.
4. Load successfully afterward and confirm the pool is not exhausted.
