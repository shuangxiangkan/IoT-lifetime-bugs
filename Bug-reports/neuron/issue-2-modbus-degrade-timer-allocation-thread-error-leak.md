# Missing error handling in `set_skip_timer()` can leak timer state

I found a resource-lifetime issue in the Modbus degradation timer setup path.

Version checked: `main-daily`, commit `26d8505b`

File: `plugins/modbus/modbus_req.c`

Function: `set_skip_timer`

Relevant code:

```c
typedef struct {
    uint8_t  slave_id;
    uint16_t degrade_time;
} degrade_timer_data_t;

void *degrade_timer(void *arg)
{
    degrade_timer_data_t *data = (degrade_timer_data_t *) arg;

    struct timespec t1 = { .tv_sec = data->degrade_time, .tv_nsec = 0 };
    struct timespec t2 = { 0 };
    nanosleep(&t1, &t2);

    skip[data->slave_id] = false;

    free(data);
    return NULL;
}

void set_skip_timer(uint8_t slave_id, uint32_t degrade_time)
{
    degrade_timer_data_t *data =
        (degrade_timer_data_t *) malloc(sizeof(degrade_timer_data_t));
    data->slave_id     = slave_id;
    data->degrade_time = degrade_time;

    failed_cycles[slave_id] = 0;

    pthread_t timer_thread;
    pthread_create(&timer_thread, NULL, degrade_timer, data);

    pthread_detach(timer_thread);
}
```

`data` is allocated for the timer thread and is normally freed by
`degrade_timer()`. However, `set_skip_timer()` does not check either allocation
or thread creation:

```c
degrade_timer_data_t *data =
    (degrade_timer_data_t *) malloc(sizeof(degrade_timer_data_t));
data->slave_id = slave_id;
...
pthread_create(&timer_thread, NULL, degrade_timer, data);
pthread_detach(timer_thread);
```

If `malloc()` fails, the function immediately dereferences `data`. If
`pthread_create()` fails, the timer thread never runs, so `data` is never freed.
The related `skip[slave_id]` recovery logic also does not run in that case.

Suggested fix: check `malloc()` before writing through `data`; check the return
value from `pthread_create()` and free `data` if the thread was not created.
Only call `pthread_detach()` when `pthread_create()` succeeds.
