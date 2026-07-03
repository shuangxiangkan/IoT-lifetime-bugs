# File descriptor leak in `dynsec_init()` when `fdopen()` fails

#### Description

I found a file descriptor leak in `dynsec_init()` on the error path where `fdopen()` fails after `open()` has already succeeded.

File: `apps/mosquitto_ctrl/dynsec.c`

Function: `dynsec_init`

On non-Windows builds the file is created with `open()` and the returned fd is then wrapped in a `FILE *` with `fdopen()`:

```c
#ifdef WIN32
	fptr = mosquitto_fopen(filename, "wb", true);
#else
	int fd = open(filename, O_CREAT | O_EXCL | O_WRONLY, 0640);
	if(fd < 0){
		free(json_str);
		fprintf(stderr, "dynsec init: Unable to open '%s' for writing (%s).\n", filename, strerror(errno));
		return -1;
	}
	fptr = fdopen(fd, "wb");
#endif
	if(fptr){
		fprintf(fptr, "%s", json_str);
		free(json_str);
		fclose(fptr);
	}else{
		free(json_str);
		fprintf(stderr, "dynsec init: Unable to open '%s' for writing.\n", filename);
		return -1;
	}
```

When `fdopen()` succeeds, `fclose(fptr)` closes the stream and the underlying fd, so that path is fine.

When `fdopen()` fails, `fptr` is `NULL` and the `else` branch frees `json_str`, prints an error, and returns `-1` — but it never calls `close(fd)`. Per POSIX, a failed `fdopen()` does not close the fd passed to it, and because `fptr` is `NULL` the fd cannot be reclaimed through `fclose(fptr)` either. The fd from `open()` is therefore leaked on this path.

Because `open()` uses `O_CREAT | O_EXCL`, this path also leaves behind a freshly created but empty config file.

`mosquitto_ctrl dynsec init` is normally a short-lived command, so a single run has limited impact, but the ownership error is clear and the fd accumulates if this logic is ever reached repeatedly or embedded in a long-lived process.

A minimal fix is to close the fd when `fdopen()` fails:

```c
	fptr = fdopen(fd, "wb");
	if(fptr == NULL){
		int saved_errno = errno;
		close(fd);
		free(json_str);
		fprintf(stderr, "dynsec init: Unable to open '%s' for writing (%s).\n",
		        filename, strerror(saved_errno));
		return -1;
	}
```

Saving `errno` before `close()` keeps the original `fdopen()` failure reason. Optionally the empty file created by `open()` could also be removed on this path, but that is a behavioral choice and not required to fix the fd leak.