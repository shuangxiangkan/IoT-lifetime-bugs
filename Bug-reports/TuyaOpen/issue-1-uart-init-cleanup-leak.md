# UART initialization error paths miss lower-layer deinit

I found possible UART resource leaks on initialization failure paths. Once
`tkl_uart_init()` succeeds, later failures in the TAL UART and CLI initialization
paths can return without calling `tkl_uart_deinit()` / `tal_uart_deinit()`.

File: `src/tal_driver/uart/tal_uart.c`

Function: `tal_uart_init`

Relevant code:

```c
ret = tkl_uart_init(port_num, &cfg->base_cfg);
if (ret != OPRT_OK) {
    PR_ERR("tkl_uart_init(port %d) failed: %d", port_num, ret);
    goto ERR_EXIT;
}

ret = tuya_ring_buff_create(cfg->rx_buffer_size, OVERFLOW_STOP_TYPE, &uart_info->rx_ring);
if (ret != OPRT_OK) {
    goto ERR_EXIT;
}

ret = tal_semaphore_create_init(&uart_info->rx_ring_sem, 1, 1);
if (ret != OPRT_OK) {
    goto ERR_EXIT;
}

...

ERR_EXIT:
    uart_free_source(uart_info);
    return ret;
```

The cleanup helper releases only TAL-level objects:

```c
void uart_free_source(TAL_UART_DEV *uart_info)
{
    if (uart_info->rx_block_sem != NULL) {
        tal_semaphore_release(uart_info->rx_block_sem);
    }
    ...
    if (uart_info->rx_ring != NULL) {
        tuya_ring_buff_free(uart_info->rx_ring);
    }
    ...
    tal_free(uart_info);
}
```

It does not call `tkl_uart_deinit(port_num)`. The normal deinit path does call
the lower-layer deinit:

```c
OPERATE_RET tal_uart_deinit(TUYA_UART_NUM_E port_num)
{
    TAL_UART_DEV *uart_info = uart_list_get_one_node(port_num);
    if (uart_info == NULL) {
        return OPRT_INVALID_PARM;
    }

    OPERATE_RET ret = tkl_uart_deinit(port_num);
    if (ret != OPRT_OK) {
        return ret;
    }
    ...
}
```

On the Linux porting template, `tkl_uart_init()` opens a file descriptor or
socket and starts an IRQ thread:

```c
s_uart_dev[port_id].fd = open("/dev/stdin", O_RDWR | O_NOCTTY | O_NDELAY);
...
pthread_create(&s_uart_dev[port_id].tid, &attr, __irq_handler, &s_uart_dev[port_id]);

...

s_uart_dev[port_id].fd = socket(AF_INET, SOCK_DGRAM, 0);
...
pthread_create(&s_uart_dev[port_id].tid, NULL, __udp_irq_handler, &s_uart_dev[port_id]);
```

and `tkl_uart_deinit()` closes the descriptor and cancels the thread:

```c
close(s_uart_dev[port_id].fd);

if (1 == port_id) {
    pthread_cancel(s_uart_dev[port_id].tid);
    pthread_join(s_uart_dev[port_id].tid, 0);
}
```

There is a related caller-side cleanup gap in `tal_cli_init_with_uart()`:

```c
result = tal_uart_init(uart_num, &cfg);
if (OPRT_OK != result) {
    PR_ERR("uart init failed", result);
    goto __exit;
}

...

result = tal_thread_create_and_start(&s_cli_handle->thread, NULL, NULL, cli_task, s_cli_handle, &param);
if (OPRT_OK != result) {
    PR_ERR("tuya cli create thread failed %d", result);
    goto __exit;
}

...

__exit:
    tal_free(s_cli_handle);
    s_cli_handle = NULL;

    return OPRT_COM_ERROR;
```

If `tal_uart_init()` succeeds but creating the CLI thread fails, the function
only frees `s_cli_handle`; it does not call `tal_uart_deinit(uart_num)`.

Suggested fix: track whether the lower-layer UART has been initialized, and call
`tkl_uart_deinit(port_num)` before `uart_free_source()` on post-`tkl_uart_init`
failures. In `tal_cli_init_with_uart()`, call `tal_uart_deinit(uart_num)` if
thread creation fails after UART initialization succeeds.
