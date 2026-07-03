# File handle leak in `pstget()` when the read buffer allocation fails

#### Description

I found a possible `FILE *` leak in `pstget()` on the error path where `malloc()` for the read buffer fails after `fopen()` has already succeeded.

File: `src/MQTTPersistenceDefault.c`

Function: `pstget`

```c
fp = fopen(filename, "rb");
free(filename);
if (fp != NULL)
{
    fseek(fp, 0, SEEK_END);
    fileLen = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if ((buf = (char *)malloc(fileLen)) == NULL)
    {
        rc = PAHO_MEMORY_ERROR;
        goto exit;
    }
    bytesRead = (int)fread(buf, sizeof(char), fileLen, fp);
    *buffer = buf;
    *buflen = bytesRead;
    if ( bytesRead != fileLen )
        rc = MQTTCLIENT_PERSISTENCE_ERROR;
    fclose(fp);
} else
    rc = MQTTCLIENT_PERSISTENCE_ERROR;

/* the caller must free buf */
exit:
    return rc;
```

The successful path closes the file with `fclose(fp)`. But when `malloc(fileLen)` fails, the code does `goto exit`, and the `exit:` label only does `return rc;` — the `fclose(fp)` is inside the `if (fp != NULL)` block, after the allocation, so it is skipped. The open `FILE *` (and its underlying fd) is leaked on this path.

This is reached when reading a persisted message under memory pressure (the file opens, but the buffer allocation for its contents fails). `pstget()` is called per persisted message, so repeated reads under low memory can exhaust file descriptors.