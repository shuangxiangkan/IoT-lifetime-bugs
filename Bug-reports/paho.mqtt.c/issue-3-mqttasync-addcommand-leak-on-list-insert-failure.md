# `MQTTAsync_addCommand()` leaks the command when the list insert fails

#### Description

I found a memory leak in `MQTTAsync_addCommand()`: when the underlying `ListInsert()` / `ListAppend()` fails (its internal `ListElement` allocation fails), the `command` is neither stored in the queue nor freed, so it leaks. Because this function is the single enqueue point for several public async APIs, the leak is systematic.

File: `src/MQTTAsyncUtils.c`

Function: `MQTTAsync_addCommand`

```c
    /* CONNECT / DISCONNECT: insert at head */
    ListElement* result = ListInsert(MQTTAsync_commands, command, command_size, MQTTAsync_commands->first);
    if (result == NULL)
        rc = PAHO_MEMORY_ERROR;          /* command not stored, not freed -> leak */
    ...
    /* other commands: append */
    if (ListAppend(MQTTAsync_commands, command, command_size) == NULL)
    {
        rc = PAHO_MEMORY_ERROR;
        goto exit;                       /* command not stored, not freed -> leak */
    }
```

The key point is what `ListAppend()` / `ListInsert()` do on failure (`src/LinkedList.c`):

```c
ListElement* ListAppend(List* aList, void* content, size_t size)
{
    ListElement* newel = malloc(sizeof(ListElement));
    if (newel)
        ListAppendNoMalloc(aList, content, newel, size);
    return newel;
}
```

When `malloc(sizeof(ListElement))` fails, `ListAppendNoMalloc()` is never called, so `content` (the `command`) is **not** stored and **not** freed; the function just returns `NULL`. `ListInsert()` has the same semantics.

So on the `result == NULL` / `ListAppend(...) == NULL` paths, `MQTTAsync_addCommand()` sets `rc = PAHO_MEMORY_ERROR` and returns without freeing `command`. The callers simply propagate `rc`, so the caller-allocated `command` is leaked. For SUBSCRIBE / UNSUBSCRIBE / PUBLISH commands this also leaks the nested fields the caller attached (topics, payload, properties, destinationName).

Callers that enqueue through this path include `MQTTAsync_connect()`, `MQTTAsync_reconnect()`, `MQTTAsync_subscribeMany()`, `MQTTAsync_unsubscribeMany()`, `MQTTAsync_send()`, `MQTTAsync_disconnect1()`, and internal timeout/reconnect command creation.

A focused fix is to free the command (with its nested fields) when the insert fails, e.g.:

```c
    if (ListAppend(MQTTAsync_commands, command, command_size) == NULL)
    {
        MQTTAsync_freeCommand(command);
        rc = PAHO_MEMORY_ERROR;
        goto exit;
    }
```

and the same for the `ListInsert()` branch. Note: the duplicate-CONNECT/DISCONNECT branch already calls `MQTTAsync_freeCommand(command)` and returns `MQTTASYNC_COMMAND_IGNORED`; the callers should be checked so this new free does not turn into a double free for any caller that already frees on error.

#### Steps to reproduce the issue

1. Inject an allocation failure into the `malloc(sizeof(ListElement))` inside `ListAppend()` / `ListInsert()`.
2. Drive any of the affected public APIs (e.g. `MQTTAsync_subscribeMany()`), so a `command` is allocated and handed to `MQTTAsync_addCommand()`.
3. Observe the call return `PAHO_MEMORY_ERROR`.

#### Expected results

When the list insert fails, `MQTTAsync_addCommand()` should free the `command` (and its nested fields), since ownership was not transferred to the queue.

#### Actual results

`MQTTAsync_addCommand()` returns `PAHO_MEMORY_ERROR` without freeing `command`. The command struct and its nested topics/payload/properties are leaked. LeakSanitizer reports the command allocation and its nested allocations as still reachable/unfreed.

#### Versions

Source-level issue in `src/MQTTAsyncUtils.c` (`MQTTAsync_addCommand`), with the list-ownership semantics in `src/LinkedList.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
