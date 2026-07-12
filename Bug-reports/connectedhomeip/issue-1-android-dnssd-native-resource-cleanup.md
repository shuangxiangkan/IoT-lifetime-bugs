# GitHub issue draft — project-chip/connectedhomeip bug report template

## Title

[BUG] Android DNS-SD bridge: `strdup`/`delete[]` mismatch, missing `ReleaseByteArrayElements`, and error-path leak in `HandleBrowse`

## Reproduction steps

These are native resource cleanup bugs in `src/platform/android/DnssdImpl.cpp`, found by code inspection (static lifetime analysis). They sit on the normal Android discovery path, so no special setup is needed beyond running discovery:

1. Run a Matter Android app that performs DNS-SD discovery (e.g. commissioning or operational discovery), so that `HandleResolve` / `HandleBrowse` in `src/platform/android/DnssdImpl.cpp` are invoked.
2. Resolve any service that carries TXT entries. In `HandleResolve`, each TXT key is allocated with `strdup()`:

```cpp
jstring jniKeyObject = (jstring) env->GetObjectArrayElement(keys, i);
JniUtfString key(env, jniKeyObject);
entries[i].mKey = strdup(key.c_str());
```

   but the cleanup section frees it with `delete[]`:

```cpp
for (size_t i = 0; i < size; i++)
{
    delete[] service.mTextEntries[i].mKey;
    if (service.mTextEntries[i].mData != nullptr)
    {
        delete[] service.mTextEntries[i].mData;
    }
}
delete[] service.mTextEntries;
```

   `strdup()` allocates with the C allocator, so the matching deallocator is `free()`. Using `delete[]` here is an allocator/deallocator mismatch (undefined behavior). Running the resolve path under AddressSanitizer reports it as `alloc-dealloc-mismatch`.

3. On the same path, the TXT data pointer obtained from JNI is never released:

```cpp
jbyte * jnidata = env->GetByteArrayElements(datas, nullptr);
for (size_t j = 0; j < dataSize; j++)
{
    data[j] = static_cast<uint8_t>(jnidata[j]);
}
```

   There is no matching `ReleaseByteArrayElements()`. On ART, which typically returns a copy, this leaks one copy of the TXT data per entry on every resolve; since Matter discovery resolves continuously, this accumulates steadily in a long-running app. On JVMs that pin instead, it keeps the Java byte array pinned longer than intended.

4. In `HandleBrowse`, browse results that fail validation leak the `DnssdService` array:

```cpp
auto size              = env->GetArrayLength(instanceName);
DnssdService * service = new DnssdService[size];
for (decltype(size) i = 0; i < size; i++)
{
    JniUtfString jniInstanceName(env, (jstring) env->GetObjectArrayElement(instanceName, i));
    VerifyOrReturn(strlen(jniInstanceName.c_str()) <= Operational::kInstanceNameMaxLength,
                   dispatch(CHIP_ERROR_INVALID_ARGUMENT));

    CopyString(service[i].mName, jniInstanceName.c_str());
    VerifyOrReturn(extractProtocol(jniServiceType.c_str(), service[i].mType, service[i].mProtocol) == CHIP_NO_ERROR,
                   dispatch(CHIP_ERROR_INVALID_ARGUMENT));
}

dispatch(CHIP_NO_ERROR, service, size);
delete[] service;
```

   If either `VerifyOrReturn` fires after `service` has been allocated, the function returns without `delete[] service`. The instance names come from mDNS advertisements of other devices on the local network, so an over-length instance name or a malformed service type is externally supplied input — each such browse result leaks one array.

## Bug prevalence

The `strdup`/`delete[]` mismatch and the missing `ReleaseByteArrayElements` are hit on every resolve of a service with TXT entries on Android. The `HandleBrowse` leak triggers whenever a browse result fails instance-name or service-type validation.

## GitHub hash of the SDK that was being used

236733d916fcb44665d747e250374c39affe0f9f (also verified present on current `master`)

## Platform

android

## Platform Version(s)

N/A

## Anything else?

Suggested fixes:

- Free `entries[i].mKey` with `free()`, or allocate it with `new[]` consistently.
- Release `jnidata` with `ReleaseByteArrayElements(datas, jnidata, JNI_ABORT)` after copying the data.
- In `HandleBrowse`, use RAII such as `std::unique_ptr<DnssdService[]>`, or route validation failures through a common cleanup path before returning.

A hint that this is unintentional: the comment above the allocation block in `HandleResolve` says "We are only allocating the entries list and the data field of each entry so we free these in the exit section" — it does not mention the keys, yet the exit section frees them, suggesting the key allocation and the cleanup path drifted apart at some point.
