# `HAL_UDP_create()` may return a closed socket after connect failure

I found a possible stale file descriptor bug in the Ubuntu HAL UDP creation
code. If a UDP socket is created successfully but `connect()` fails, the socket
is closed, but the local `socket_id` variable is not reset before the function
returns.

File: `wrappers/os/ubuntu/HAL_UDP_linux.c`

Function: `HAL_UDP_create`

Relevant code:

```c
long socket_id = -1;

...

for (ainfo = res; ainfo != NULL; ainfo = ainfo->ai_next) {
    if (AF_INET == ainfo->ai_family) {
        ...

        socket_id = socket(ainfo->ai_family, ainfo->ai_socktype, ainfo->ai_protocol);
        if (socket_id < 0) {
            printf("create socket error");
            continue;
        }
        if (0 == connect(socket_id, ainfo->ai_addr, ainfo->ai_addrlen)) {
            break;
        }

        close(socket_id);
    }
}
freeaddrinfo(res);

return socket_id;
```

When `socket()` succeeds but `connect()` fails, the code closes `socket_id` and
continues. If there is no later successful address, `socket_id` still contains
the numeric value of the closed descriptor, so `HAL_UDP_create()` returns it as
if creation had succeeded.

One caller treats only `-1` as failure:

```c
p_network->context = (void *)HAL_UDP_create(p_param->p_host, p_param->port);
if ((void *) - 1 == p_network->context) {
    return COAP_ERROR_NET_INIT_FAILED;
}
```

So a closed descriptor can be stored as the CoAP network context. Later
read/write/close operations may fail unpredictably, or may accidentally operate
on an unrelated descriptor if the OS reuses the same fd number.

Suggested fix: after `close(socket_id)`, set `socket_id = -1` before continuing.
Alternatively, return immediately on connect failure if retrying other addresses
is not intended.
