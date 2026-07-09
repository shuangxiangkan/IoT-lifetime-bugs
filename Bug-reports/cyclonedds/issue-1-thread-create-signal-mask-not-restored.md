# The caller's signal mask is not restored when `pthread_create()` fails

In the POSIX thread backend, `ddsrt_thread_create()` blocks signals in the
calling thread before `pthread_create()`, but only restores the previous mask on
the success path.

File: `src/ddsrt/src/threads/posix/threads.c`

Function: `ddsrt_thread_create`

Relevant code:

```c
/* Block signal delivery in our own threads (SIGXCPU is excluded so we have a way of
   dumping stack traces, but that should be improved upon) */
sigfillset (&set);
#ifdef __APPLE__
DDSRT_WARNING_GNUC_OFF(sign-conversion)
#endif
sigdelset (&set, SIGXCPU);
#ifdef __APPLE__
DDSRT_WARNING_GNUC_ON(sign-conversion)
#endif
sigprocmask (SIG_BLOCK, &set, &oset);
#endif /* !defined(__ZEPHYR__) */
if ((create_ret = pthread_create (&thread->v, &pattr, os_startRoutineWrapper, ctx)) != 0)
{
  DDS_ERROR ("os_threadCreate(%s): pthread_create failed with error %d\n", name, create_ret);
  goto err_create;
}
#if !defined(__ZEPHYR__)
sigprocmask (SIG_SETMASK, &oset, NULL);
#endif
pthread_attr_destroy (&pattr);
return DDS_RETCODE_OK;

err_create:
  ddsrt_free (ctx->name);
  ddsrt_free (ctx);
err:
  pthread_attr_destroy (&pattr);
  return DDS_RETCODE_ERROR;
```

`sigprocmask (SIG_BLOCK, &set, &oset)` changes the mask of the *calling* thread
and saves the old one in `oset`. The successful path restores it:

```c
sigprocmask (SIG_SETMASK, &oset, NULL);
```

When `pthread_create()` fails, execution jumps to `err_create`, frees the thread
context, destroys the pthread attributes, and returns without restoring `oset`.
The calling thread is left with the temporary mask, i.e. with every catchable
signal except `SIGXCPU` blocked.

The practical impact is limited: this only triggers when `pthread_create()` fails
(e.g. `EAGAIN` on thread or memory exhaustion), and it only affects the
non-Zephyr POSIX backend. But the calling thread is then left unable to receive
`SIGINT` / `SIGTERM`, which can prevent a clean shutdown at exactly the moment
the process is already under resource pressure.

Suggested fix: restore the saved signal mask on the `pthread_create()` failure
path before returning. Note that the earlier `goto err` paths all run *before*
`sigprocmask()`, so the restore belongs under `err_create:` rather than `err:`,
where `oset` would not yet be initialized:

```c
err_create:
#if !defined(__ZEPHYR__)
  sigprocmask (SIG_SETMASK, &oset, NULL);
#endif
  ddsrt_free (ctx->name);
  ddsrt_free (ctx);
err:
  pthread_attr_destroy (&pattr);
  return DDS_RETCODE_ERROR;
```

As an aside, POSIX leaves the behaviour of `sigprocmask()` unspecified in a
multi-threaded process; `pthread_sigmask()` is the specified equivalent. The two
are interchangeable for the calling thread on Linux/glibc, so this is not the
cause of the issue above, but it may be worth switching while touching these
lines.
