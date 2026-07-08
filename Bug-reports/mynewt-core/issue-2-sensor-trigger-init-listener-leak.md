# Memory leak in `sensor_trigger_init()` when `sensor_register_listener()` fails

I found a memory leak in `sensor_trigger_init()`: the listener allocated for the
trigger is not freed when `sensor_register_listener()` returns an error.

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

`sensor_register_listener()` only takes ownership of the listener on success — it
inserts it into `sensor->s_listener_list`. On failure it returns early without
inserting or freeing:

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

So when `sensor_lock()` fails (e.g. mutex timeout), `sensor_trigger_init()`
returns on the `if (rc)` path and leaks `sensor_trig_lner`, which was never
inserted into the listener list.

Suggested fix: free `sensor_trig_lner` before returning on the
`sensor_register_listener()` failure path.
