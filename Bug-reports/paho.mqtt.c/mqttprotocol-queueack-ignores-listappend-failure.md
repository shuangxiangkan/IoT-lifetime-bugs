# `MQTTProtocol_queueAck()` ignores `ListAppend()` failure, leaking the ack and silently dropping it

#### Description

I found a memory leak and a protocol-correctness issue in `MQTTProtocol_queueAck()`: the return value of `ListAppend()` is ignored. If the list-element allocation inside `ListAppend()` fails, the `ackReq` is neither queued nor freed, and the function still returns success.

File: `src/MQTTProtocolClient.c`

Function: `MQTTProtocol_queueAck`

```c
int MQTTProtocol_queueAck(Clients* client, int ackType, int msgId)
{
    int rc = 0;
    AckRequest* ackReq = NULL;

    FUNC_ENTRY;
    ackReq = malloc(sizeof(AckRequest));
    if (!ackReq)
        rc = PAHO_MEMORY_ERROR;
    else
    {
        ackReq->messageId = msgId;
        ackReq->ackType = ackType;
        ListAppend(client->outboundQueue, ackReq, sizeof(AckRequest));   /* return value ignored */
    }

    FUNC_EXIT_RC(rc);
    return rc;
}
```

`ListAppend()` allocates a `ListElement` and only stores `content` if that allocation succeeds (`src/LinkedList.c`):

```c
ListElement* ListAppend(List* aList, void* content, size_t size)
{
    ListElement* newel = malloc(sizeof(ListElement));
    if (newel)
        ListAppendNoMalloc(aList, content, newel, size);
    return newel;
}
```

So when `malloc(sizeof(ListElement))` fails, `ackReq` is not added to `client->outboundQueue` and is not freed. Since `MQTTProtocol_queueAck()` does not check the return value, it returns `rc == 0` (success). The result is twofold:

1. The `ackReq` allocation is leaked.
2. The acknowledgement is silently dropped (it never enters the outbound queue), even though the caller is told the queue succeeded.

A fix is to check the return value and clean up / report the error:

```c
    else
    {
        ackReq->messageId = msgId;
        ackReq->ackType = ackType;
        if (ListAppend(client->outboundQueue, ackReq, sizeof(AckRequest)) == NULL)
        {
            free(ackReq);
            rc = PAHO_MEMORY_ERROR;
        }
    }
```

#### Steps to reproduce the issue

1. Inject an allocation failure into the `malloc(sizeof(ListElement))` inside `ListAppend()`.
2. Trigger an outbound acknowledgement that goes through `MQTTProtocol_queueAck()`.
3. Observe that the function returns `0` (success) even though the ack was not queued.

#### Expected results

When `ListAppend()` fails, `MQTTProtocol_queueAck()` should free `ackReq` and return an error (`PAHO_MEMORY_ERROR`), so the failure is neither leaked nor silently swallowed.

#### Actual results

`MQTTProtocol_queueAck()` ignores the `ListAppend()` return value: on list-element allocation failure, `ackReq` is leaked and the function still returns success, silently dropping the acknowledgement.

#### Versions

Source-level issue in `src/MQTTProtocolClient.c` (`MQTTProtocol_queueAck`), with the list-ownership semantics in `src/LinkedList.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
