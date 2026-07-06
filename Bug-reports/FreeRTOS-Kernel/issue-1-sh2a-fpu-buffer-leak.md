# POSIX port ignores TLS initialization failures, causing thread misclassification and a memory leak

## Summary

In `portable/Renesas/SH2A_FPU/port.c`, `xPortUsesFloatingPoint()` dynamically
allocates a 72-byte FPU register save buffer and hands ownership to the task by
storing a pointer to it in the task's application tag (`pxTaskTag`). The port
never defines `portCLEAN_UP_TCB`, so when a task that uses the FPU is deleted the
buffer is never freed and becomes unreachable. Calling the function twice on the
same task also leaks the first buffer immediately.

## Affected code

`portable/Renesas/SH2A_FPU/port.c`, `xPortUsesFloatingPoint()`:

```c
#define portFLOP_REGISTERS_TO_STORE    ( 18 )
#define portFLOP_STORAGE_SIZE          ( portFLOP_REGISTERS_TO_STORE * 4 )   /* 72 bytes */

    /* Allocate a buffer large enough to hold all the flop registers. */
    pulFlopBuffer = ( uint32_t * ) pvPortMalloc( portFLOP_STORAGE_SIZE );

    if( pulFlopBuffer != NULL )
    {
        memset( ( void * ) pulFlopBuffer, 0x00, portFLOP_STORAGE_SIZE );
        *pulFlopBuffer = get_fpscr();

        /* Use the task tag to point to the flop buffer.  Pass pointer to just
         * above the buffer because the flop save routine uses a pre-decrement. */
        vTaskSetApplicationTaskTag( xTask,
            ( void * ) ( pulFlopBuffer + portFLOP_REGISTERS_TO_STORE ) );
        xReturn = pdPASS;
    }
    else
    {
        xReturn = pdFAIL;
    }

    return xReturn;
```

The tag stores a pointer to *just past the end* of the buffer (the assembly save
routine pre-decrements), so the only reference to the original allocation base is
`pxTaskTag - portFLOP_REGISTERS_TO_STORE`.

`portable/Renesas/SH2A_FPU/portmacro.h` uses `pxTaskTag` exclusively for the FPU
context:

```c
#define traceTASK_SWITCHED_OUT()  do { if( pxCurrentTCB->pxTaskTag != NULL ) vPortSaveFlopRegisters( pxCurrentTCB->pxTaskTag ); } while( 0 )
#define traceTASK_SWITCHED_IN()   do { if( pxCurrentTCB->pxTaskTag != NULL ) vPortRestoreFlopRegisters( pxCurrentTCB->pxTaskTag ); } while( 0 )
```

## Problem

The buffer is not a leak on successful return — ownership legitimately escapes to
the task. But that transfer requires a matching destructor when the task is
deleted, and none exists:

- `prvDeleteTCB()` calls `portCLEAN_UP_TCB( pxTCB )` precisely so a port can free
  task-specific memory.
- `portmacro.h` for SH2A_FPU does **not** define `portCLEAN_UP_TCB`, so the
  kernel default is used (`include/FreeRTOS.h`):

  ```c
  #ifndef portCLEAN_UP_TCB
      #define portCLEAN_UP_TCB( pxTCB )    ( void ) ( pxTCB )
  #endif
  ```

- The kernel does not free the application task tag as generic memory (in other
  ports it may hold a user callback).

Result: deleting any task that called `xPortUsesFloatingPoint()` leaks 72 bytes.

### Secondary: repeated calls leak immediately

`xPortUsesFloatingPoint()` does not check whether the task already has an FPU
buffer. A second call allocates a new buffer, overwrites `pxTaskTag`, and drops
the only pointer to the previous allocation — no task deletion required.

## Trigger condition

- Target is `portable/Renesas/SH2A_FPU`.
- Dynamic allocation is enabled.
- A task calls `xPortUsesFloatingPoint()`.
- The task is deleted (or the function is called twice).

Each occurrence leaks 72 bytes. In a system that repeatedly creates, FPU-enables,
runs and deletes worker tasks, the leak accumulates steadily and can eventually
cause `pvPortMalloc()` to fail. Embedded systems have no process-exit reclamation,
so the loss persists until reboot.

## Suggested fix

Define `portCLEAN_UP_TCB` for this port, recovering the base pointer from the
stored end pointer:

```c
#define portCLEAN_UP_TCB( pxTCB )                                    \
    do                                                               \
    {                                                                \
        if( ( pxTCB )->pxTaskTag != NULL )                           \
        {                                                            \
            uint32_t * pulBufferEnd = ( uint32_t * ) ( pxTCB )->pxTaskTag; \
            vPortFree( pulBufferEnd - portFLOP_REGISTERS_TO_STORE );  \
            ( pxTCB )->pxTaskTag = NULL;                             \
        }                                                            \
    } while( 0 )
```

Implementation notes:

- `portFLOP_REGISTERS_TO_STORE` is currently defined in `port.c`; if the macro
  lives in `portmacro.h`, the constant must be shared/moved.
- The cleanup must only free tags created by this port's FPU init. Reusing the
  application task tag makes this ambiguous if `configUSE_APPLICATION_TASK_TAG`
  lets users set the tag. A cleaner design stores the allocation base in a
  port-specific TCB extension field instead of overloading `pxTaskTag`.

For the repeated-call issue, guard the allocation (only safe if the tag is known
to be port-owned):

```c
    if( xTaskGetApplicationTaskTag( xTask ) != NULL )
    {
        return pdPASS;
    }
```
