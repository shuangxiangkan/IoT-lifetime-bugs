# module converter utilities: `code_buffer` leaked once per code section in `module_to_binary` / `module_to_c_array`

## Summary

The host-side module converter utilities allocate a `code_buffer` inside a loop
over the ELF code sections but never free it. Each iteration overwrites
`code_buffer` with a new allocation, so every code section in the input ELF
leaks one buffer until the process exits. Neither tool checks the `malloc()`
result for `NULL`.

## Affected code

`common_modules/module_manager/utilities/module_to_binary.c`, `main()`:

```c
    for (i = 0; i < code_section_index; i++)
    {
        ...
        /* Now allocate memory for the code section. */
        code_buffer = malloc(code_section_array[i].code_section_size);

        /* Read in the code area. */
        j = code_section_array[i].code_section_index;
        elf_object_read(section_header[j].elf_section_header_offset,
                        code_buffer, code_section_array[i].code_section_size);
        ...
        /* (loop body ends without free(code_buffer)) */
    }

    fclose(source_file);
    fclose(binary_file);
    return 0;
```

`common_modules/module_manager/utilities/module_to_c_array.c` has the identical
pattern (per-section `malloc` into `code_buffer`, no `free` before the next
iteration or at exit).

## Problem

`code_buffer` is reassigned every iteration without freeing the previous
allocation, so one buffer per code section is leaked. Additionally, neither
location checks whether `malloc()` returned `NULL`; on OOM a null pointer is
passed to `elf_object_read()` (a separate robustness issue).

## Severity

Low. These are host-side conversion utilities, not embedded ThreadX runtime code:

- The OS reclaims the memory when the process exits.
- The tools typically process a single input file.
- Impact grows with the number and size of code sections.
- A pathological or maliciously crafted ELF could noticeably raise peak memory.

This should be tracked at a lower priority than the runtime byte-pool leaks in
the compatibility layers.

## Suggested fix

Free each buffer after use and check the allocation:

```c
    code_buffer = malloc(code_section_array[i].code_section_size);
    if (code_buffer == NULL) {
        /* close files and report failure */
        return 1;
    }

    ...

    free(code_buffer);
    code_buffer = NULL;
```

## Suggested verification

Build an ELF with several code sections and run both converters under ASan or
Valgrind. After the fix there should be no definite leak of `code_buffer`.
