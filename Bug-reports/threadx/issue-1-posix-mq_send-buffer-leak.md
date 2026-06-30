# POSIX compatibility `mq_send()`: message buffer leaked when `tx_queue_send()` fails

## Summary

In `utility/rtos_compatibility_layers/posix/px_mq_send.c`, `mq_send()` allocates
a private copy of the caller's message from the queue's own byte pool, then posts
a pointer to that copy into the ThreadX queue. If `tx_queue_send()` fails, the
function returns `ERROR` without releasing the copy. Because the pointer never
entered the queue, no receiver can ever free it, so each failure permanently
consumes space in that queue's byte pool.

## Affected code

`utility/rtos_compatibility_layers/posix/px_mq_send.c`, `mq_send()`:

```c
    /* Allocate memory to save the message from the queue's byte pool. */
    temp1 = tx_byte_allocate((TX_BYTE_POOL *)&(q_ptr->vq_message_area), &bp,
                             msg_len, TX_NO_WAIT);
    ...
    /* Copy the message into the private buffer. */
    ...
    msg[0] = (ULONG)source;   /* msg carries the address of bp */
    ...

    /* Attempt to post the message to the queue. */
    temp1 = tx_queue_send(Queue, msg, TX_WAIT_FOREVER);
    if (temp1 != TX_SUCCESS)
    {
        /* POSIX doesn't have error for this, hence give default. */
        posix_errno = EINTR;
        posix_set_pthread_errno(EINTR);
        return(ERROR);        /* bp is never released */
    }

    return(OK);
```

## Problem

On the success path, the buffer's ownership transfers to the queue, and the
receiver frees it (`tx_byte_release()`) after dequeuing the message. On the
`tx_queue_send()` failure path:

- `bp` was successfully allocated.
- The message never entered the queue, so no receiver will see the pointer.
- The sender returns without calling `tx_byte_release(bp)`.

Each failure permanently consumes one allocation in `q_ptr->vq_message_area`.

## Trigger condition

`tx_queue_send()` can return a non-success status (e.g. queue pointer/state
error, or an aborted wait). The code uses `TX_WAIT_FOREVER` so a full queue does
not fail outright, but the ThreadX API still returns a status and the existing
code explicitly handles the non-success case, so this branch cannot be assumed
unreachable. Repeated failures exhaust the per-queue message storage; even after
the queue itself recovers, later `mq_send()` calls fail because they can no
longer allocate a message copy.

## Suggested fix

Release the not-yet-transferred copy before returning:

```c
    temp1 = tx_queue_send(Queue, msg, TX_WAIT_FOREVER);
    if (temp1 != TX_SUCCESS)
    {
        tx_byte_release(bp);
        posix_errno = EINTR;
        posix_set_pthread_errno(EINTR);
        return(ERROR);
    }
```

The `tx_byte_release()` return value may be checked, but even on release failure
the original `tx_queue_send()` error semantics should be preserved.

## Note (separate robustness issue, out of scope)

Earlier in the same function, when `tx_byte_allocate()` fails the code calls
`posix_internal_error(9999)` and then continues to use `bp` anyway (copying into
it and posting it). That is an independent defect not covered by this report, but
worth addressing in the same area.

## Suggested verification

Stub `tx_byte_allocate()` to succeed and `tx_queue_send()` to fail. Verify
`tx_byte_release()` is called exactly once with `bp`. Loop the failing path and
confirm the queue byte pool's available bytes stop decreasing.
