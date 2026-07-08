# [Bug] lwip-dhcpd raw server leaks copied pbuf when request is larger than reply buffer

## RT-Thread Version

`v5.0.2-2673-gac6dc197a0`

## Hardware Type/Architectures

Not hardware-specific. This is a source-level cleanup issue in the lwIP DHCP server component:

`components/net/lwip-dhcpd/dhcp_server_raw.c`

## Develop Toolchain

Not toolchain-specific. The issue was found by source inspection of the error path.

## Describe the bug

In `components/net/lwip-dhcpd/dhcp_server_raw.c`, `dhcp_server_recv()` allocates a new pbuf `q` to copy and build a DHCP response. If `q` is successfully allocated but is still smaller than the received pbuf `p`, the error path frees only `p` and returns. The newly allocated `q` is not freed.

Affected code:

```c
q = pbuf_alloc(PBUF_TRANSPORT, 1500, PBUF_RAM);
if (q == NULL)
{
    LWIP_DEBUGF(DHCP_DEBUG | LWIP_DBG_TRACE | LWIP_DBG_LEVEL_WARNING,
                ("pbuf_alloc dhcp_msg failed!\n"));
    pbuf_free(p);
    return;
}
if (q->tot_len < p->tot_len)
{
    LWIP_DEBUGF(DHCP_DEBUG | LWIP_DBG_TRACE | LWIP_DBG_LEVEL_WARNING,
                ("pbuf_alloc dhcp_msg too small %d:%d\n", q->tot_len, p->tot_len));
    pbuf_free(p);
    return;
}
```

The normal paths later jump to `free_pbuf_and_return`, which frees `q`:

```c
free_pbuf_and_return:
    pbuf_free(q);
```

However, the `q->tot_len < p->tot_len` branch returns before reaching that cleanup label.

### 1. Steps to reproduce the behavior

1. Enable/use the raw lwIP DHCP server implementation.
2. Call `dhcp_server_recv()` with a received pbuf `p` whose `tot_len` is larger than the newly allocated response pbuf `q` created by `pbuf_alloc(PBUF_TRANSPORT, 1500, PBUF_RAM)`.
3. The function enters the `q->tot_len < p->tot_len` branch.
4. It frees `p` and returns, but `q` remains allocated.

### 2. Expected behavior

Both pbufs owned by the function should be released on the error path. The function should free `q` before returning, for example:

```c
if (q->tot_len < p->tot_len)
{
    LWIP_DEBUGF(DHCP_DEBUG | LWIP_DBG_TRACE | LWIP_DBG_LEVEL_WARNING,
                ("pbuf_alloc dhcp_msg too small %d:%d\n", q->tot_len, p->tot_len));
    pbuf_free(q);
    pbuf_free(p);
    return;
}
```

Alternatively, this branch could jump to a cleanup label that frees both resources.

### 3. Add screenshot / media if you have them

No screenshot. This is a source-level resource leak in an error path.

## Other additional context

The issue was detected while scanning RT-Thread with a lightweight resource lifetime checker and then manually confirmed against the source.

This is a network-triggered path: repeated oversized or otherwise unexpected DHCP packets could repeatedly exercise the branch and leak pbuf memory.
