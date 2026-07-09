# `sensor_trigger_init()` should free its listener if registration fails

I found a small cleanup issue in `sensor_trigger_init()`: the function allocates
an internal listener for trigger notifications, but does not free it if
`sensor_register_listener()` returns an error.

File: `hw/sensor/src/sensor.c`

Function: `sensor_trigger_init`

Relevant code:

```c
    sensor_trig_lner = malloc(sizeof(struct sensor_listener));
    assert(sensor_trig_lner != NULL);

    sensor_trig_lner->sl_func = sensor_generate_trig;
    sensor_trig_lner->sl_sensor_type = type;
    sensor_trig_lner->sl_arg = (void *)notify;

    rc = sensor_register_listener(sensor, sensor_trig_lner);
    if (rc) {
        return;
    }
```

The ownership boundary appears to be that `sensor_register_listener()` takes
ownership only after it successfully inserts the listener into
`sensor->s_listener_list`. If registration fails before the insert, the listener
is not attached to the sensor and there is no later owner that can release it:

```c
sensor_register_listener(struct sensor *sensor, struct sensor_listener *listener)
{
    int rc;

    rc = sensor_lock(sensor);
    if (rc != 0) {
        goto err;
    }

    SLIST_INSERT_HEAD(&sensor->s_listener_list, listener, sl_next);
    sensor_unlock(sensor);
    return (0);
err:
    return (rc);
}
```

So if `sensor_register_listener()` ever returns an error,
`sensor_trigger_init()` returns on the `if (rc)` path and loses
`sensor_trig_lner`.

This is a low-severity robustness issue. `sensor_lock()` uses
`OS_TIMEOUT_NEVER`, so ordinary mutex contention should not make this path fail
by timeout. The failure path is still worth cleaning up because the listener was
allocated locally and has not been inserted into the list.

A minimal fix is to free the listener before returning:

```c
    rc = sensor_register_listener(sensor, sensor_trig_lner);
    if (rc) {
        free(sensor_trig_lner);
        return;
    }
```
