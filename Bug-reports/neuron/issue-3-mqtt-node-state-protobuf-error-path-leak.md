# Error-path leak in MQTT protobuf `handle_nodes_state()`

There is a memory leak in the MQTT protobuf node-state reporting path when
allocating one of the per-node protobuf objects fails.

Version checked: `main-daily`, commit `26d8505b`

File: `plugins/mqtt/mqtt_handle.c`

Function: `handle_nodes_state`

Relevant code:

```c
if (plugin->config.format == MQTT_UPLOAD_FORMAT_PROTOBUF) {
    Model__NodeStateReport nsr = MODEL__NODE_STATE_REPORT__INIT;

    nsr.timestamp = global_timestamp;
    nsr.n_nodes   = utarray_len(states->states);

    Model__NodeState **node_states =
        calloc(nsr.n_nodes, sizeof(Model__NodeState *));
    if (NULL == node_states) {
        plog_error(plugin, "malloc fail");
        goto end;
    }

    utarray_foreach(states->states, neu_nodes_state_t *, state)
    {
        Model__NodeState *ns = calloc(1, sizeof(Model__NodeState));
        if (NULL == ns) {
            plog_error(plugin, "malloc fail");
            goto end;
        }
        model__node_state__init(ns);
        ns->node    = state->node;
        ns->link    = state->state.link;
        ns->running = state->state.running;

        node_states[utarray_eltidx(states->states, state)] = ns;
    }

    nsr.nodes = node_states;
    size      = model__node_state_report__get_packed_size(&nsr);
    json_str  = malloc(size);
    model__node_state_report__pack(&nsr, (uint8_t *) json_str);

    for (size_t i = 0; i < nsr.n_nodes; i++) {
        free(node_states[i]);
    }
    free(node_states);
}
```

The normal path releases all `node_states[i]` entries and the `node_states`
array. But if an allocation inside the loop fails, the code jumps directly to
`end`:

```c
Model__NodeState *ns = calloc(1, sizeof(Model__NodeState));
if (NULL == ns) {
    plog_error(plugin, "malloc fail");
    goto end;
}
```

At that point `node_states` has already been allocated, and some earlier
`node_states[i]` entries may also have been allocated. The `end` label only
frees `states->states`, so the partially-built protobuf array is leaked.

Suggested fix: add a cleanup path for the protobuf branch that frees all
already-created `node_states[i]` entries and the `node_states` array before
leaving on allocation failure. It would also be safer to check `json_str =
malloc(size)` before packing into it.
