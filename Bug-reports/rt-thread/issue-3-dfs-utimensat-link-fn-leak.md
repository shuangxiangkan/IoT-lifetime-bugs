# [Bug] dfs: `utimensat()` leaks `link_fn` on early error paths

## RT-Thread Version

`v5.0.2-2673-gac6dc197a0`

## Hardware Type/Architectures

Not hardware-specific. This is a source-level cleanup issue in DFS POSIX compatibility code:

`components/dfs/dfs_v2/src/dfs_posix.c`

## Develop Toolchain

Not toolchain-specific. The issue was found by source inspection of the error paths.

## Describe the bug

In `components/dfs/dfs_v2/src/dfs_posix.c`, `utimensat()` allocates `link_fn` before validating `__path` and before several early-return checks:

```c
char *link_fn = (char *)rt_malloc(DFS_PATH_MAX);
int err;

if (__path == NULL)
{
    return -EFAULT;
}
```

If `__path == NULL`, the function returns immediately and leaks `link_fn`.

There are additional early-return paths before the final `rt_free(link_fn)`, for example:

```c
if (stat(__path, &buffer) < 0)
{
    return -ENOENT;
}
```

and:

```c
d = fd_get(__fd);
if (!d || !d->vnode)
{
    return -EBADF;
}

fullpath = dfs_dentry_full_path(d->dentry);
if (!fullpath)
{
    rt_set_errno(-ENOMEM);
    return -1;
}
```

The function only frees `link_fn` on later paths:

```c
ret = dfs_file_setattr(fullpath, &attr);
rt_free(link_fn);

return ret;
```

### 1. Steps to reproduce the behavior

Any caller that reaches an early error path after `link_fn` is allocated can trigger the leak. For example:

1. Call `utimensat()` with `__path == NULL`.
2. `link_fn` is allocated with `rt_malloc(DFS_PATH_MAX)`.
3. The function returns `-EFAULT`.
4. `link_fn` is not freed.

Other examples include paths where `stat(__path, &buffer)` fails, `fd_get(__fd)` fails, or `dfs_dentry_full_path()` fails.

### 2. Expected behavior

The function should not allocate `link_fn` until after basic parameter validation, and all later error paths should release it before returning.

For example, move the allocation after the `__path == NULL` check and use a common cleanup label for subsequent failures:

```c
if (__path == NULL)
{
    return -EFAULT;
}

link_fn = (char *)rt_malloc(DFS_PATH_MAX);
...
```

or free `link_fn` before every early return after allocation.

### 3. Add screenshot / media if you have them

No screenshot. This is a source-level resource leak in error paths.

## Other additional context

The issue was detected while scanning RT-Thread with a lightweight resource lifetime checker and then manually confirmed against the source.
