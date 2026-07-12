# File descriptor leak in `HAL_AT_Uart_Init()` error paths

I found a possible file descriptor leak in the Ubuntu HAL UART initialization
code. If the UART device is opened successfully but a later termios setup step
fails, the function returns without closing the descriptor.

File: `wrappers/os/ubuntu/HAL_UART_linux.c`

Function: `HAL_AT_Uart_Init`

Relevant code:

```c
if ((at_uart_fd = open(AT_UART_LINUX_DEV,
                       O_RDWR | O_NOCTTY | O_NDELAY)) == -1) {
    printf("open at uart failed\r\n");
    return -1;
}

fd = at_uart_fd;
/* set the serial port parameters */
fcntl(fd, F_SETFL, 0);
if (0 != tcgetattr(fd, &t_opt)) {
    return -1;
}

if (0 != cfsetispeed(&t_opt, baud)) {
    return -1;
}

if (0 != cfsetospeed(&t_opt, baud)) {
    return -1;
}

...

if (0 != tcsetattr(fd, TCSANOW, &t_opt)) {
    return -1;
}
```

After `open()` succeeds, these failure paths return directly:

- `tcgetattr(fd, &t_opt)` failure
- `cfsetispeed(&t_opt, baud)` failure
- `cfsetospeed(&t_opt, baud)` failure
- `tcsetattr(fd, TCSANOW, &t_opt)` failure

None of those paths closes `at_uart_fd`, and the global descriptor is not reset
to `-1`. `HAL_AT_Uart_Deinit()` closes the descriptor on the normal lifecycle,
but it is not reached when initialization itself fails.

This can leak one UART file descriptor per failed initialization attempt.

Suggested fix: route the post-open failures through a common cleanup path that
closes `at_uart_fd`, resets it to `-1`, and then returns `-1`.
