# POSIX (GCC) port: thread marker leaked and thread mis-identified when `pthread_setspecific()` fails

## Summary

In `portable/ThirdParty/GCC/Posix/port.c`, `prvMarkAsFreeRTOSThread()` allocates
a one-byte thread marker and stores it in thread-specific storage via
`pthread_setspecific()`, but ignores the return value. If `pthread_setspecific()`
fails, the marker is neither stored (so the TLS destructor never frees it) nor
freed locally, leaking it — and the thread is left unmarked, so
`prvIsFreeRTOSThread()` misclassifies it.

## Affected code

`portable/ThirdParty/GCC/Posix/port.c`:

```c
static void prvThreadKeyDestructor( void * pvData )
{
    free( pvData );
}

/* ... key created once ... */
    pthread_key_create( &xThreadKey, prvThreadKeyDestructor );   /* return ignored */

/* ... per FreeRTOS pthread ... */
    uint8_t * pucThreadData = NULL;

    pucThreadData = malloc( 1 );
    configASSERT( pucThreadData != NULL );

    *pucThreadData = 1;

    pthread_setspecific( xThreadKey, pucThreadData );            /* return ignored */
```

## Problem

On the success path this is not a leak: ownership of the marker transfers to the
pthread TLS, and `prvThreadKeyDestructor()` frees it when the thread exits or is
cancelled.

On the failure path (`pthread_setspecific()` returns non-zero, e.g. `EINVAL` for
an invalid key or `ENOMEM` for insufficient resources):

- The pointer is not stored in thread-specific storage.
- The TLS destructor never receives it.
- The local variable goes out of scope on return.
- The one-byte allocation leaks.
- `prvIsFreeRTOSThread()` will incorrectly report that this thread is not a
  FreeRTOS thread.

The correctness impact (thread identity) is arguably larger than the one-byte
leak itself.

## Severity

Low as a leak: one byte plus allocator metadata per failure, only on an error
path that typically indicates the host is already resource-constrained. Worth
fixing together with the related robustness gaps below.

## Suggested fix

```c
    int xResult;

    xResult = pthread_setspecific( xThreadKey, pucThreadData );
    if( xResult != 0 )
    {
        free( pucThreadData );
        configASSERT( xResult == 0 );
    }
```

Because the function returns `void`, if `configASSERT` is compiled out a failure
policy is still needed — terminate the pthread, invoke a port-specific fatal
handler, or change the function to return status so the caller can abort
initialization. Freeing and continuing avoids the leak but leaves the thread
unmarked, which may lead to incorrect later control flow.

## Related robustness issues (same area)

- `configASSERT( pucThreadData != NULL )` is followed by an unconditional
  `*pucThreadData = 1;`. With `configASSERT` compiled out and `malloc()` failing,
  this is a NULL dereference.
- `pthread_key_create()`'s return value is also ignored; if key creation fails,
  `pthread_setspecific()` will subsequently fail. A complete fix should save and
  check the key initialization status.

## Suggested test

Use a wrapper or link-time substitution to make `pthread_setspecific()` return
`ENOMEM`, and verify:

- `pucThreadData` is freed.
- The thread is not treated as successfully marked.
- No double free occurs.
- The normal path still frees exactly once via the destructor on thread exit.
