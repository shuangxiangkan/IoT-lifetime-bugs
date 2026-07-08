# Possible double free in `ble_hci_emspi_rx_acl()` on host ACL delivery failure

I found a possible double free in the EMSPI HCI transport receive path when an
incoming ACL packet is passed to the host and host-side queuing fails.

File: `nimble/transport/emspi/src/ble_hci_emspi.c`

Function: `ble_hci_emspi_rx_acl`

Relevant code:

```c
om = ble_transport_alloc_acl_from_ll();
assert(om != NULL);

rc = ble_hci_emspi_rx(om->om_data, BLE_HCI_DATA_HDR_SZ);
if (rc != 0) {
    goto err;
}

...

rc = ble_transport_to_hs_acl(om);
if (rc != 0) {
    goto err;
}

return 0;

err:
    os_mbuf_free_chain(om);
    return rc;
```

The error label is correct for failures that occur before ownership is passed
to the host. However, `ble_transport_to_hs_acl()` eventually calls the host
implementation:

File: `nimble/host/src/ble_hs.c`

```c
ble_transport_to_hs_acl_impl(struct os_mbuf *om)
{
    return ble_hs_rx_data(om, NULL);
}
```

`ble_hs_rx_data()` documents and implements that it consumes the mbuf regardless
of the outcome:

```c
/* Called when a data packet is received from the controller.  This function
 * consumes the supplied mbuf, regardless of the outcome.
 */
static int
ble_hs_rx_data(struct os_mbuf *om, void *arg)
{
    ...
    rc = ble_mqueue_put(&ble_hs_rx_q, ble_hs_evq, om);
    if (rc != 0) {
        os_mbuf_free_chain(om);
        return BLE_HS_EOS;
    }

    return 0;
}
```

So when `ble_mqueue_put()` fails, `ble_hs_rx_data()` already frees `om` and
returns an error. `ble_hci_emspi_rx_acl()` then sees the non-zero return value,
jumps to `err`, and frees the same mbuf again.

This can happen under low-memory or queue-allocation failure conditions in the
host receive path.

Suggested fix: after calling `ble_transport_to_hs_acl(om)`, do not free `om` on
failure because ownership has already been transferred. One option is to return
the error directly:

```c
rc = ble_transport_to_hs_acl(om);
if (rc != 0) {
    return rc;
}
```
