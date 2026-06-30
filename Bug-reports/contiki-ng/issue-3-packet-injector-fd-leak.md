# packet-injector test tool: file descriptor leak in `read_packet()`

## Summary

In `tests/20-packet-parsing/packet-injector/packet-injector.c`, `read_packet()`
opens a file but never closes the descriptor. Both the success path and the
read-error path return without `close(fd)`, leaking one descriptor per call.

## Affected code

`tests/20-packet-parsing/packet-injector/packet-injector.c`, `read_packet()`:

```c
static int
read_packet(const char *filename, char *buf, int max_len)
{
  int fd;
  int len;

  /* Read packet data from a file. */
  fd = open(filename, O_RDONLY);
  if(fd < 0) {
    LOG_ERR("open: %s\n", strerror(errno));
    return -1;
  }

  len = read(fd, buf, max_len);
  if(len < 0) {
    LOG_ERR("read: %s\n", strerror(errno));
    return -1;          /* fd leaked */
  }

  return len;           /* fd leaked */
}
```

## Impact

Each call leaks the open descriptor on both the success and the read-error path.
This only affects the packet-parsing test tool, which is short-lived, so the
practical impact is low — but it is a clear resource leak and an easy fix.

## Suggested fix

```c
  len = read(fd, buf, max_len);
  close(fd);

  if(len < 0) {
    LOG_ERR("read: %s\n", strerror(errno));
    return -1;
  }

  return len;
```

If `errno` from `read()` must be preserved across `close()`:

```c
  len = read(fd, buf, max_len);
  if(len < 0) {
    int saved_errno = errno;
    LOG_ERR("read: %s\n", strerror(errno));
    close(fd);
    errno = saved_errno;
    return -1;
  }

  close(fd);
  return len;
```

## Priority

Low — test tooling only, short process lifetime.
