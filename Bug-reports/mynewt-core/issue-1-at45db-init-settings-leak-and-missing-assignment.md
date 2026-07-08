# Memory leak in `at45db_init()`: allocated `settings` is never stored in `dev->settings`

I found a memory leak in `at45db_init()` on the non-default-baudrate path: a
`hal_spi_settings` buffer is allocated but never assigned to `dev->settings` and
never freed. The same path also mutates the shared global default and leaves
`dev->settings` unset for the later `hal_spi_config()` call.

File: `hw/drivers/flash/at45db/src/at45db.c`

Function: `at45db_init`

Relevant code:

```c
    /* only alloc new settings if using non-default */
    if (dev->baudrate == at45db_default_settings.baudrate) {
        dev->settings = &at45db_default_settings;
    } else {
        settings = malloc(sizeof(at45db_default_settings));
        if (!settings) {
            return -1;
        }
        memcpy(settings, &at45db_default_settings, sizeof(at45db_default_settings));
        at45db_default_settings.baudrate = dev->baudrate;
    }

    hal_gpio_init_out(dev->ss_pin, 1);

    rc = hal_spi_init(dev->spi_num, dev->spi_cfg, HAL_SPI_TYPE_MASTER);
    if (rc) {
        return (rc);
    }

    rc = hal_spi_config(dev->spi_num, dev->settings);
```

`dev->settings` is only ever assigned in the default branch
(`dev->settings = &at45db_default_settings`). In the `else` branch, `settings`
is allocated with `malloc()`, filled with `memcpy()`, and then dropped:

- It is never assigned to `dev->settings`, so the only pointer to it is the local
  variable, which goes out of scope — one allocation leaks per non-default init.
- Instead of storing the per-device baudrate into the copy, the code writes it
  into the **global** default: `at45db_default_settings.baudrate = dev->baudrate`.
  This corrupts the shared default for every other device.
- `dev->settings` is left unset on this path, yet is passed to
  `hal_spi_config(dev->spi_num, dev->settings)` below.

The intent was clearly to build a per-device copy and use it:

```c
    } else {
        settings = malloc(sizeof(at45db_default_settings));
        if (!settings) {
            return -1;
        }
        memcpy(settings, &at45db_default_settings, sizeof(at45db_default_settings));
        settings->baudrate = dev->baudrate;
        dev->settings = settings;
    }
```

Suggested fix: assign the allocated copy to `dev->settings`, set the baudrate on
the copy (not on the global default), and free it in the corresponding teardown
path. Note that even with the assignment, the two `return rc` error paths after
`hal_spi_init()` / `hal_spi_config()` would leak the copy unless a cleanup path
frees `dev->settings`.
