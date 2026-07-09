# Possible leaks in URI parser `realloc()` error paths

I found possible memory leaks in the REST URI parsing helpers when `realloc()`
fails after some URI components have already been allocated.

Version checked: `0.25.2-14-g7be56db6`

Files:

`nanomq/rest_api.c`
`nanomq_cli/rest_api.c`

Functions:

`uri_parse_tree`
`uri_param_parse`

Relevant code from `nanomq/rest_api.c`:

```c
while (NULL != (ret = strchr(str, '/'))) {
    num++;
    tree **new_root;
    new_root = realloc(root, sizeof(tree *) * num);
    if (new_root == NULL) {
        if (root != NULL) {
            free(root);
        }
        return NULL;
    }
    root      = new_root;
    len       = ret - str + 1;
    tree *sub = nng_zalloc(sizeof(tree));
    sub->node = nng_zalloc(len);
    strncpy(sub->node, str, len - 1);
    sub->end      = false;
    root[num - 1] = sub;
    str           = ret + 1;
}
```

Later in the same function, the final component is appended with another
`realloc()`:

```c
num++;
tree **new_root;
new_root = realloc(root, sizeof(tree *) * num);
if (new_root == NULL) {
    if (root != NULL) {
        free(root);
    }
    return NULL;
}
root          = new_root;
tree *sub     = nng_zalloc(sizeof(tree));
sub->node     = nng_strdup(str);
sub->end      = true;
root[num - 1] = sub;
```

If the second or any later `realloc()` fails, the code frees only the outer
`root` array. The `tree` objects already stored in the array, and their
`sub->node` strings, are not released. The normal cleanup path shows that these
objects are owned by the URI content and should be freed individually:

```c
for (size_t i = 0; i < ct->sub_count; i++) {
    tree *sub = node[i];
    nng_strfree(sub->node);
    nng_free(sub, sizeof(tree));
}
free(node);
```

`uri_param_parse()` has the same pattern for query parameters:

```c
new_kv_str = realloc(kv_str, sizeof(char *) * num);
if (new_kv_str == NULL) {
    if (kv_str != NULL) {
        free(kv_str);
    }
    return NULL;
}
kv_str          = new_kv_str;
kv_str[num - 1] = nng_strdup(str);
```

When this `realloc()` fails after earlier `kv_str[i]` entries have been
allocated, the function frees only the `kv_str` array and leaks the individual
strings.

The same URI parsing logic exists in `nanomq_cli/rest_api.c`, with the same
error-path cleanup issue.

Suggested fix: before freeing the outer array on a `realloc()` failure, iterate
over the already stored entries and release each owned object. For
`uri_parse_tree()`, free each `tree` and `tree->node`. For `uri_param_parse()`,
free each previously allocated `kv_str[i]`.
