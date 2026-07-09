# Possible event leak in `lwnl_add_event()` when there is no listener

I found a possible leak in the LWNL event queue when an event is posted while no
file descriptor is listening for that device type.

Version checked: `926549785`

File: `os/drivers/lwnl/lwnl_evt_queue.c`

Function: `lwnl_add_event`

Relevant code:

```c
struct lwnl_event *evt = (struct lwnl_event *)kmm_malloc(sizeof(struct lwnl_event));
if (!evt) {
    LWNL_LOGE(TAG, "fail to alloc lwnl event");
    return -1;
}

evt->refs = 0;
evt->data.status = type;
evt->data.data = NULL;
evt->data.data_len = 0;
if (buffer) {
    if (buf_len < 0) {
        evt->data.data = buffer;
        evt->data.data_len = -(buf_len);
    } else {
        char *output = kmm_malloc(buf_len);
        if (!output) {
            LWNL_LOGE(TAG, "fail to alloc buffer");
            kmm_free(evt);
            return -3;
        }
        memcpy(output, buffer, buf_len);
        evt->data.data = output;
        evt->data.data_len = buf_len;
    }
}

sq_addlast(&evt->entry, &g_event_queue[type.type]);
if (_lwnl_update_event_filep(evt) < 0) {
    ...
}
return 0;
```

`_lwnl_update_event_filep()` only links the event into per-file queues for
currently registered listeners:

```c
for (int i = 0; i < LWNL_NPOLLWAITERS; i++) {
    if (g_filep_list[i].filep && g_filep_list[i].type == dtype) {
        ...
        refs++;
    }
}
evt->refs = g_connected[dtype];
if (check == 1) {
    g_totalevt++;
}
return 0;
```

When there is no listener for `type.type`, `check` remains zero and
`g_connected[dtype]` is zero. However, `lwnl_add_event()` has already inserted
`evt` into `g_event_queue[type.type]` and still returns success.

Events are normally released from `_lwnl_remove_event()`, which is reached via
`lwnl_get_event()` or `lwnl_remove_listener()` through a listener's per-file
queue. With no listener, the event is not linked into any file queue, so there is
no later path that decrements/removes this zero-reference event. If the event
contains data, `evt->data.data` is retained as well.

This is reachable through `lwnl_postmsg()`, which calls `lwnl_add_event()` after
the driver has been registered. A producer can post an event before any user has
opened and bound `/dev/lwnl` for that device type.

Suggested fix: if `_lwnl_update_event_filep()` finds no matching listener, remove
the event from `g_event_queue[type.type]` and free `evt` plus `evt->data.data`
before returning. Alternatively, do not enqueue the event until after at least
one matching listener has been found.