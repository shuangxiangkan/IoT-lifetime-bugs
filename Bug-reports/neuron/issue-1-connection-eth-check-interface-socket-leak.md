# Socket leak in `neu_conn_eth_check_interface()` on `ioctl()` failure

I found a socket fd leak in `neu_conn_eth_check_interface()` when the interface
hardware-address lookup fails.

Version checked: `main-daily`, commit `26d8505b`

File: `src/connection/connection_eth.c`

Function: `neu_conn_eth_check_interface`

Relevant code:

```c
int neu_conn_eth_check_interface(const char *interface)
{
    struct ifreq ifr = { 0 };
    int          fd  = socket(PF_PACKET, SOCK_RAW, ETH_P_IPV6);
    int          ret = -1;

    if (fd <= 0) {
        return -1;
    }

    snprintf(ifr.ifr_name, IFNAMSIZ, "%s", interface);
    ret = ioctl(fd, SIOCGIFHWADDR, &ifr);
    if (ret != 0) {
        return -1;
    }

    close(fd);
    return ret;
}
```

The success path closes `fd` before returning:

```c
close(fd);
return ret;
```

But if `ioctl(fd, SIOCGIFHWADDR, &ifr)` fails, the function returns `-1`
without closing the socket. This leaks one socket fd per failed interface check.

Suggested fix: close `fd` before returning from the `ioctl()` error path.

Also, `socket()` can legally return fd `0`, so the check should probably be
`fd < 0` instead of `fd <= 0`.
