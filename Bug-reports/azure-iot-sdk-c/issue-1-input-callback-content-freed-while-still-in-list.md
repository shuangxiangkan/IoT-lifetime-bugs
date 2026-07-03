# Input message callback freed while still referenced by `event_callbacks` list (use-after-free / double free)

#### Description

I found two places in the input-message-callback handling where a callback object is freed with `delete_event()` while it is still referenced by the `handleData->event_callbacks` linked list, leaving a dangling list node. A later message dispatch reads the freed object (use-after-free), and client destroy frees it again (double free). Both share one root cause: the callback *content* is freed without keeping it in sync with the *list node* lifetime.

File: `iothub_client/src/iothub_client_core_ll.c`

The list stores `IOTHUB_EVENT_CALLBACK *` pointers; the object and its nested fields are freed by:

```c
static void delete_event(IOTHUB_EVENT_CALLBACK* event_callback)
{
    STRING_delete(event_callback->inputName);
    free(event_callback->userContextCallbackEx);
    free(event_callback);
}
```

Consumers assume a list entry always points to a live object — message dispatch dereferences it directly:

```c
IOTHUB_EVENT_CALLBACK* event_callback = (IOTHUB_EVENT_CALLBACK*)singlylinkedlist_item_get_value(item_handle);
...
result = event_callback->callbackAsyncEx(messageHandle, event_callback->userContextCallbackEx);
```

and client teardown frees every entry again via `singlylinkedlist_foreach(..., delete_event_callback, ...)`, where `delete_event_callback` calls `delete_event()`.

**Problem 1 — `create_event_handler_callback()`: updating an existing callback frees the in-list object on context-allocation failure.**

When `inputName` already has a callback, the existing object is taken from the list and `add_to_list` stays `false`:

```c
else
{
    event_callback = (IOTHUB_EVENT_CALLBACK*)singlylinkedlist_item_get_value(item_handle);  /* already in the list */
    ...
}
```

The old context is freed, then a new one is allocated; on allocation failure the whole object is deleted:

```c
    free(event_callback->userContextCallbackEx);
    event_callback->userContextCallbackEx = NULL;
    ...
    if ((userContextCallbackEx != NULL) &&
        (NULL == (event_callback->userContextCallbackEx = malloc(userContextCallbackExLength))))
    {
        LogError("Unable to allocate userContextCallback");
        delete_event(event_callback);            /* frees an object still in event_callbacks */
        result = IOTHUB_CLIENT_ERROR;
    }
    else if ((add_to_list == true) && (NULL == singlylinkedlist_add(handleData->event_callbacks, event_callback)))
    {
        delete_event(event_callback);            /* OK: guarded by add_to_list, object not yet in the list */
        result = IOTHUB_CLIENT_ERROR;
    }
```

The `singlylinkedlist_add()`-failure branch is correctly guarded by `add_to_list == true` (the object was never added). But the `malloc()`-failure branch runs regardless of `add_to_list`, so when updating an existing callback it calls `delete_event()` on an object that is still in `event_callbacks`. The list node now points at freed memory.

A fix is to make the update transactional — allocate the new context first, and only replace the old one once it succeeds, so a failure leaves the existing in-list callback intact:

```c
void* new_context = NULL;
if (userContextCallbackEx != NULL)
{
    if ((new_context = malloc(userContextCallbackExLength)) == NULL)
    {
        result = IOTHUB_CLIENT_ERROR;
        /* leave the existing callback and its context untouched */
        ...
    }
    else
    {
        memcpy(new_context, userContextCallbackEx, userContextCallbackExLength);
    }
}
/* only now mutate the existing object */
free(event_callback->userContextCallbackEx);
event_callback->userContextCallbackEx = new_context;
event_callback->callbackAsync   = callbackSync;
event_callback->callbackAsyncEx = callbackSyncEx;
event_callback->userContextCallback = userContextCallback;
```

A newly created object (`add_to_list == true`) can still be `delete_event()`'d on failure, since it is not yet in the list.

**Problem 2 — `remove_event_unsubscribe_if_needed()`: frees the object before removing the node.**

```c
delete_event(event_callback);
if (singlylinkedlist_remove(handleData->event_callbacks, item_handle) != 0)
{
    LogError("singlylinkedlist_remove failed");
    result = IOTHUB_CLIENT_ERROR;
}
```

If `singlylinkedlist_remove()` fails, the object has already been freed but the node remains, so the list again holds a dangling pointer. The safe order is remove-then-free:

```c
if (singlylinkedlist_remove(handleData->event_callbacks, item_handle) != 0)
{
    LogError("singlylinkedlist_remove failed");
    result = IOTHUB_CLIENT_ERROR;
}
else
{
    delete_event(event_callback);
    ...
    result = IOTHUB_CLIENT_OK;
}
```

(`item_handle` here was just returned by `singlylinkedlist_find()`, so `remove` normally succeeds; Problem 2 is the lower-reachability path, but the free/remove ordering is unsafe.)

#### Steps to reproduce the issue

Problem 1 (the reachable path):

1. Register an input callback: `IoTHubClientCore_LL_SetInputMessageCallbackEx(handle, "input1", cb1, &ctx1, sizeof(ctx1))`.
2. Update the same input with a new context: `IoTHubClientCore_LL_SetInputMessageCallbackEx(handle, "input1", cb2, &ctx2, sizeof(ctx2))`.
3. Force the `malloc(userContextCallbackExLength)` for the new context to fail.
4. The call returns `IOTHUB_CLIENT_ERROR`, but the `event_callbacks` list still contains the (now freed) `"input1"` entry.
5. Deliver an `"input1"` message, or destroy the client.

#### Expected results

A failed callback update should leave the existing, still-listed callback object valid (or otherwise must remove the node before freeing the object). The list must never reference a freed `IOTHUB_EVENT_CALLBACK`.

#### Actual results

On the context-allocation failure during an update, `delete_event()` frees the object while it is still in `event_callbacks`. The dangling node then causes a use-after-free when a matching message is dispatched (`event_callback->callbackAsyncEx(...)`) and a double free when the client is destroyed (`delete_event_callback` runs `delete_event()` again). The unsubscribe path has the same hazard if `singlylinkedlist_remove()` fails.

#### Versions

Source-level issue in `iothub_client/src/iothub_client_core_ll.c` (`create_event_handler_callback`, `remove_event_unsubscribe_if_needed`).

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime / fault-injection reproducer is required.
