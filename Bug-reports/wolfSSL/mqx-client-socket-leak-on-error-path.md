# Socket leak in MQX client example when address parsing or connect fails

#### Description

I found a possible socket leak in the MQX TLS client example. After `socket()` succeeds, the address-parsing and `connect()` error paths `goto end`, which skips the `socket_cleanup:` label that closes the socket.

File: `IDE/MQX/client-tls.c`

```c
if ((sockfd = socket(AF_INET, SOCK_STREAM, 0)) == -1) {
    ...
    goto end;                 /* socket() failed: nothing to close, ok */
}
...
if (inet_pton(AF_INET, argv[1], &servAddr.sin_addr, sizeof(servAddr.sin_addr)) != 1) {
    ...
    goto end;                 /* sockfd already open -> leaked */
}

if ((ret = connect(sockfd, (struct sockaddr*) &servAddr, sizeof(servAddr)))
    == -1) {
    ...
    goto end;                 /* sockfd already open -> leaked */
}
```

The cleanup labels are:

```c
socket_cleanup:
    close(sockfd);            /* Close the connection to the server */
end:
    return ret;
```

After `socket()` succeeds, the `inet_pton()` failure and the `connect()` failure both `goto end`, jumping past `socket_cleanup: close(sockfd)`, so the open socket is leaked. These paths should `goto socket_cleanup` instead.

A fix is to send the post-`socket()` error paths to `socket_cleanup`, and to make the cleanup robust against the `socket()`-failure case:

```c
int sockfd = -1;
...
socket_cleanup:
    if (sockfd != -1)
        close(sockfd);
end:
    return ret;
```

so that the original `socket()`-failure `goto` can also target `socket_cleanup` safely.

#### Steps to reproduce the issue

1. Run the MQX client example with an invalid IPv4 address argument to make `inet_pton()` fail, or
2. Run it against an unreachable endpoint to make `connect()` fail.

In both cases the function reaches `goto end` after `socket()` has already created the socket.

#### Expected results

When address parsing or `connect()` fails after the socket is created, the socket should be closed before returning.

#### Actual results

The error paths `goto end`, skipping `socket_cleanup: close(sockfd)`, so the socket is leaked. Repeating the failing path (e.g. in a task or test loop) accumulates open descriptors.

#### Versions

Source-level issue in `IDE/MQX/client-tls.c`.

I have not tied this to a specific build or platform. Please let me know if a concrete version string or a runtime reproducer is required.
