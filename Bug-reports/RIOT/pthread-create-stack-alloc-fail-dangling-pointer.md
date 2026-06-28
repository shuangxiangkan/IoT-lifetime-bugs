# `pthread_create()` leaves a freed `pt` in the global thread table on stack-alloc failure

I found a dangling-pointer / use-after-free hazard in `pthread_create()`. When
the thread stack allocation fails, the `pthread_thread_t` is freed but its
slot in the global `pthread_sched_threads[]` table is not cleared, so the table
keeps pointing at freed memory.

File: `sys/posix/pthread/pthread.c`

Function: `pthread_create`

`pt` is allocated and immediately registered into the global table by
`insert()`:

```c
pthread_thread_t *pt = calloc(1, sizeof(pthread_thread_t));   /* line 125 */
if (pt == NULL) {
    return -ENOMEM;
}

kernel_pid_t pthread_pid = insert(pt);                        /* line 131 */
```

```c
static int insert(pthread_thread_t *pt)
{
    ...
    for (int i = 0; i < MAXTHREADS; i++){
        if (!pthread_sched_threads[i]) {
            pthread_sched_threads[i] = pt;                    /* pt is now in the table */
            result = i+1;
            break;
        }
    }
    ...
}
```

The stack-allocation failure path then frees `pt` but does **not** remove it
from the table:

```c
void *stack = autofree ? malloc(stack_size) : attr->ss_sp;

if (stack == NULL) {
    free(pt);                                                 /* line 147 */
    return -ENOMEM;
    /* missing: pthread_sched_threads[pthread_pid - 1] = NULL; */
}
```

After this, `pthread_sched_threads[pthread_pid - 1]` still points at the freed
`pt`. A later `pthread_join()` / `pthread_detach()` or thread-id reuse can read
that dangling pointer (use-after-free), and the slot can no longer be reused by
`insert()`.

That this is an omission (not intentional) is clear from the other two failure
paths in the same function, which **do** clear the slot:

```c
    if (!pid_is_valid(pid)) {
        free(pt->stack);
        free(pt);
        pthread_sched_threads[pthread_pid-1] = NULL;          /* reaper-create failure */
        mutex_unlock(&pthread_mutex);
        return -1;
    }
```

```c
    if (!pid_is_valid(pt->thread_pid)) {
        free(pt->stack);
        free(pt);
        pthread_sched_threads[pthread_pid-1] = NULL;          /* thread-create failure */
        return -1;
    }
```

Suggested fix:

```c
if (stack == NULL) {
    free(pt);
    pthread_sched_threads[pthread_pid - 1] = NULL;
    return -ENOMEM;
}
```

As a related hardening, consider writing `*newthread = pthread_pid;` only after
the thread is fully created, so a failed `pthread_create()` does not hand the
caller a thread id that maps to a freed/empty slot.
