# FreeRTOS compatibility `xQueueCreate()`: queue descriptor / backing memory / semaphore leaked when semaphore creation fails

## Summary

In `utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c`, `xQueueCreate()`
allocates a queue descriptor (`p_queue`) and its backing memory (`p_mem`), then
creates two ThreadX semaphores. If either `tx_semaphore_create()` call fails, the
function returns `NULL` without freeing the already-allocated resources. Because
no queue handle is returned, the caller cannot call `vQueueDelete()` to clean up.

## Affected code

`utility/rtos_compatibility_layers/FreeRTOS/tx_freertos.c`, `xQueueCreate()`:

```c
    p_queue = txfr_malloc(sizeof(txfr_queue_t));
    if(p_queue == NULL) {
        return NULL;
    }

    mem_size = uxQueueLength*(uxItemSize);
    p_mem = txfr_malloc(mem_size);
    if(p_mem == NULL) {
        txfr_free(p_queue);           /* this path cleans up correctly */
        return NULL;
    }
    ...
    ret = tx_semaphore_create(&p_queue->read_sem, "", 0u);
    if(ret != TX_SUCCESS) {
        return NULL;                  /* leaks p_mem and p_queue */
    }

    ret = tx_semaphore_create(&p_queue->write_sem, "", uxQueueLength);
    if(ret != TX_SUCCESS) {
        return NULL;                  /* leaks read_sem, p_mem and p_queue */
    }

    return p_queue;
```

## Problem

Two error paths skip cleanup:

1. **`read_sem` creation fails** → returns without freeing `p_mem` and `p_queue`.
2. **`write_sem` creation fails** → returns without deleting the already created
   `read_sem`, and without freeing `p_mem` and `p_queue`. Besides the byte-pool
   leak, this leaves a ThreadX semaphore control block in an unreachable state.

The `p_mem == NULL` branch already demonstrates the correct pattern
(`txfr_free(p_queue)`), and `vQueueDelete()` shows the full teardown
(`tx_semaphore_delete(read_sem)`, `tx_semaphore_delete(write_sem)`,
`vPortFree(p_mem)`, `vPortFree(p_queue)`). Because the failure paths never return
a handle, the caller cannot invoke `vQueueDelete()`, so this is a definite
error-path leak.

## Trigger condition

`tx_semaphore_create()` fails (e.g. invalid control block state, disallowed
caller/context, or a ThreadX configuration/system error). The backing storage
size is proportional to `uxQueueLength * uxItemSize`, so the leaked amount can be
substantially larger than the descriptor itself.

## Suggested fix

Layered cleanup:

```c
    ret = tx_semaphore_create(&p_queue->read_sem, "", 0u);
    if(ret != TX_SUCCESS) {
        txfr_free(p_mem);
        txfr_free(p_queue);
        return NULL;
    }

    ret = tx_semaphore_create(&p_queue->write_sem, "", uxQueueLength);
    if(ret != TX_SUCCESS) {
        (void)tx_semaphore_delete(&p_queue->read_sem);
        txfr_free(p_mem);
        txfr_free(p_queue);
        return NULL;
    }
```

Cleanup labels would also work and guard against future init steps being added.

## Suggested verification

Inject (1) a first `tx_semaphore_create()` failure and (2) a first-success /
second-failure case. Confirm the byte pool's available space is restored, and in
case (2) that `tx_semaphore_delete(&p_queue->read_sem)` is called exactly once.
