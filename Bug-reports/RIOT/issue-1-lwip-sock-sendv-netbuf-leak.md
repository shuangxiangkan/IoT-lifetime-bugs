# `netbuf` leak in `lwip_sock_sendv()` on netif-mismatch error path

#### Description

I found a `netbuf` leak in `lwip_sock_sendv()` when the send is rejected because the connection's bound netif does not match `remote->netif`.

File: `pkg/lwip/contrib/sock/lwip_sock.c`

Function: `lwip_sock_sendv`

`buf` is allocated near the start of the function:

```c
buf = netbuf_new();

if (netbuf_alloc(buf, iolist_size(snips)) == NULL) {
    netbuf_delete(buf);
    return -ENOMEM;
}

for (const iolist_t *snip = snips; snip != NULL; snip = snip->iol_next) {
    if (pbuf_take_at(buf->p, snip->iol_base, snip->iol_len, payload_len) != ERR_OK) {
        netbuf_delete(buf);
        return -ENOMEM;
    }
    payload_len += snip->iol_len;
}
```

After `buf` has been allocated, later early-return error paths should release it with `netbuf_delete()` before returning. However, the netif-mismatch check returns directly without deleting `buf`:

```c
else if (conn != NULL) {
    ...
    if ((remote != NULL)
            && (remote->netif != SOCK_ADDR_ANY_NETIF)
            && (netconn_getaddr(conn, &addr, &port, 1) == 0)) {
        ...
        uint16_t netif = lwip_sock_bind_addr_to_netif(&addr);
        if ((remote->netif != netif)
                && (netif != SOCK_ADDR_ANY_NETIF)) {
            DEBUG("[lwip_sock_sendv] lwip_sock_bind_addr_to_netif() "
                  "returned %u, but expected %u\n",
                  (unsigned)netif, (unsigned)remote->netif);
            return -EINVAL;
        }
    }
    tmp = conn;
}
```

At this point `buf` has already been allocated and filled, but the direct `return -EINVAL` skips the cleanup at the end of the function:

```c
netbuf_delete(buf);
...
return res;
```

Other post-allocation early-return paths, such as `netbuf_alloc` failure, `pbuf_take_at` failure, `_create` failure, and the `-ENOTCONN` path, release `buf` before returning. This netif-mismatch path appears to be a post-allocation early-return path that does not release it.

The issue is reachable when:

* `conn != NULL`
* `remote != NULL`
* `remote->netif != SOCK_ADDR_ANY_NETIF`
* `netconn_getaddr()` succeeds
* `lwip_sock_bind_addr_to_netif(&addr)` returns a concrete netif different from `remote->netif`

A minimal fix would be to release `buf` before returning:

```c
if ((remote->netif != netif)
        && (netif != SOCK_ADDR_ANY_NETIF)) {
    DEBUG("[lwip_sock_sendv] lwip_sock_bind_addr_to_netif() "
          "returned %u, but expected %u\n",
          (unsigned)netif, (unsigned)remote->netif);
    netbuf_delete(buf);
    return -EINVAL;
}
```

This path does not need to delete `tmp`/`conn`, because no temporary netconn has been created in this branch.

#### Steps to reproduce the issue

The leaking path is:

1. Call `lwip_sock_sendv()` with an existing `conn`.
2. Pass a non-`NULL` `remote`.
3. Set `remote->netif` to a specific netif, i.e., not `SOCK_ADDR_ANY_NETIF`.
4. Let `netconn_getaddr(conn, &addr, &port, 1)` succeed.
5. Let `lwip_sock_bind_addr_to_netif(&addr)` return a concrete netif different from `remote->netif`.

In this case, the function returns `-EINVAL` directly from the netif-mismatch branch after `buf` has already been allocated.

#### Expected results

`lwip_sock_sendv()` should release the allocated `netbuf` before returning from the netif-mismatch error path.

#### Actual results

`lwip_sock_sendv()` returns `-EINVAL` directly from the netif-mismatch branch without calling `netbuf_delete(buf)`, so the allocated `netbuf` is leaked.

#### Versions

Source-level issue in `pkg/lwip/contrib/sock/lwip_sock.c`.

I have not tied this to a specific board or runtime setup. Please let me know if a concrete `make print-versions` output or a runtime reproducer is required.
