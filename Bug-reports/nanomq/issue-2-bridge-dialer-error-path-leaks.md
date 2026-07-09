# Possible bridge dialer leaks on TCP bridge initialization failures

I found possible resource leaks in the bridge TCP initialization paths. Several
functions allocate a `nng_dialer` wrapper with `nng_zalloc()`, but return
directly on later failures without releasing the allocated wrapper. Some paths
also return after a dialer has been created and stored in `node->dialer`, without
closing the dialer.

File: `nanomq/bridge.c`

Functions:

`hybrid_tcp_client`
`bridge_tcp_reload`
`bridge_tcp_client`

Relevant code from `hybrid_tcp_client()`:

```c
static int
hybrid_tcp_client(bridge_param *bridge_arg)
{
    int           rv;
    nng_dialer    *dialer = (nng_dialer *) nng_zalloc(sizeof(*dialer));;

    nng_socket *new = (nng_socket *) nng_alloc(sizeof(nng_socket));
    conf_bridge_node *node = bridge_arg->config;

    if (node->proto_ver == MQTT_PROTOCOL_VERSION_v5) {
        if ((rv = nng_mqttv5_client_open(new)) != 0) {
            nng_free(new, sizeof(nng_socket));
            log_error("Initializing mqttv5 client failed %d", rv);
            return rv;
        }
    } else {
        if ((rv = nng_mqtt_client_open(new)) != 0) {
            nng_free(new, sizeof(nng_socket));
            log_error("Initializing mqtt client failed %d", rv);
            return rv;
        }
    }

    ...

    if ((rv = nng_dialer_create(dialer, *new, node->address))) {
        nng_free(new, sizeof(nng_socket));
        log_error("nng_dialer_create %d", rv);
        return rv;
    }
    node->dialer = dialer;

#ifdef NNG_SUPP_TLS
    if (node->tls.enable) {
        if ((rv = init_dialer_tls(*dialer, node->tls.ca,
             node->tls.cert, node->tls.key, node->tls.key_password,
             node->tls.sni, node->tls.verify_peer)) != 0) {
            nng_free(new, sizeof(nng_socket));
            log_error("init_dialer_tls %d", rv);
            return rv;
        }
    }
#endif

    ...

    if (node->enable) {
        if (0 != (rv = nng_dialer_start(*dialer, NNG_FLAG_ALLOC))) {
            log_error("nng dialer start failed %d", rv);
            return rv;
        }
    }
    return 0;
}
```

The first two `return rv` paths after `nng_mqtt*_client_open()` and
`nng_dialer_create()` free `new`, but not the already allocated `dialer` pointer.

After `nng_dialer_create()` succeeds, `node->dialer` points to the allocated
wrapper and `*dialer` represents an NNG dialer. If `init_dialer_tls()` or
`nng_dialer_start()` fails, the function returns without closing the created
dialer and without freeing the `dialer` wrapper.

The same pattern appears in `bridge_tcp_reload()`:

```c
nng_dialer *dialer = (nng_dialer *) nng_zalloc(sizeof(*dialer));;

if (node->proto_ver == MQTT_PROTOCOL_VERSION_v5) {
    if ((rv = nng_mqttv5_client_open(sock)) != 0) {
        log_error(" nng_mqttv5_client_open failed %d", rv);
        return rv;
    }
}

...

if ((rv = nng_dialer_create(dialer, *sock, node->address))) {
    log_error("nng_dialer_create failed %d", rv);
    return rv;
}
node->dialer = dialer;

...

if ((rv = init_dialer_tls(*dialer, ...)) != 0) {
    log_error("init_dialer_tls failed %d", rv);
    return rv;
}
```

And in `bridge_tcp_client()`:

```c
nng_dialer *dialer = (nng_dialer *) nng_zalloc(sizeof(*dialer));

...

if ((rv = nng_dialer_create(dialer, *sock, node->address))) {
    log_error("nng_dialer_create failed %d", rv);
    return rv;
}
node->dialer = dialer;

...

if ((rv = init_dialer_tls(*dialer, ...)) != 0) {
    log_error("init_dialer_tls failed %d", rv);
    return rv;
}

...

if (node->enable) {
    rv = nng_dialer_start(*dialer, NNG_FLAG_NONBLOCK);
    if (rv != 0) {
        log_error("nng dialer start failed %d", rv);
        return rv;
    }
}
```

I did not find a cleanup path in `bridge.c` that frees `node->dialer` or closes a
partially created dialer after these initialization failures. Callers also do not
consistently handle these return values; for example `bridge_client()` calls
`bridge_tcp_client(...)` without checking its return value.

Suggested fix: use a common error label after allocating `dialer`. On paths
before `nng_dialer_create()` succeeds, free the wrapper with `nng_free(dialer,
sizeof(*dialer))`. On paths after `nng_dialer_create()` succeeds, close the NNG
dialer first, clear `node->dialer` if it was assigned, and then free the wrapper.
