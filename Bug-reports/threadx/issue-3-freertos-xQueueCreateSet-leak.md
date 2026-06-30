# FreeRTOS compatibility `xQueueCreateSet()`: descriptor and backing memory leaked when `tx_queue_create()` fails

## Summary

In `utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c`, `xQueueCreateSet()`
allocates a queue-set descriptor (`p_set`) and backing memory (`p_mem`), then
calls `tx_queue_create()`. If that fails, the function invokes
`TX_FREERTOS_ASSERT_FAIL()` and returns `NULL` without freeing either allocation.
Under the default configuration `TX_FREERTOS_ASSERT_FAIL()` is an empty macro, so
execution continues to the `return NULL` and both allocations are lost.

## Affected code

`utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c`, `xQueueCreateSet()`:

```c
    p_set = txfr_malloc(sizeof(txfr_queueset_t));
    if(p_set == NULL) {
        return NULL;
    }

    queue_size = sizeof(void *) * uxEventQueueLength;
    p_mem = txfr_malloc(queue_size);
    if(p_mem == NULL) {
        txfr_free(p_set);             /* this path cleans up correctly */
        return NULL;
    }

    ret = tx_queue_create(&p_set->queue, "", sizeof(void *) / sizeof(UINT),
                          p_mem, queue_size);
    if(ret != TX_SUCCESS) {
        TX_FREERTOS_ASSERT_FAIL();
        return NULL;                  /* leaks p_set and p_mem */
    }

    return p_set;
```

## Problem

When `tx_queue_create()` fails, neither `p_set` nor `p_mem` is freed.

`TX_FREERTOS_ASSERT_FAIL()` does not save this path, because it is an empty macro
by default:

```c
/* FreeRTOS.h */
#define TX_FREERTOS_ASSERT_FAIL()

/* config_template/FreeRTOSConfig.h */
#define TX_FREERTOS_ASSERT_FAIL()
/* #define TX_FREERTOS_ASSERT_FAIL() {taskDISABLE_INTERRUPTS(); for(;;) {};} */
```

The non-returning (infinite-loop) variant is commented out. So in the default
configuration the code falls through to `return NULL` and leaks both allocations.
The `p_mem == NULL` branch above shows the correct cleanup pattern.

## Trigger condition

`tx_queue_create()` returns a non-success status. The leaked amount is:

```
sizeof(txfr_queueset_t) + uxEventQueueLength * sizeof(void *)
```

An attacker cannot usually force the underlying queue creation to fail directly,
but under memory pressure or abnormal object state this error path further
consumes the ThreadX byte pool.

## Suggested fix

Free both allocations on failure:

```c
    if(ret != TX_SUCCESS) {
        TX_FREERTOS_ASSERT_FAIL();
        txfr_free(p_mem);
        txfr_free(p_set);
        return NULL;
    }
```

If the project may configure `TX_FREERTOS_ASSERT_FAIL()` as a non-returning
infinite loop, place the cleanup *before* the assert so memory is reclaimed in
both configurations:

```c
    if(ret != TX_SUCCESS) {
        txfr_free(p_mem);
        txfr_free(p_set);
        TX_FREERTOS_ASSERT_FAIL();
        return NULL;
    }
```

## Suggested verification

Make `tx_queue_create()` return an error and confirm:

- `txfr_free(p_mem)` is called.
- `txfr_free(p_set)` is called.
- The function still returns `NULL`.
- Behavior is well-defined under both the empty-assert and custom-assert configs.
