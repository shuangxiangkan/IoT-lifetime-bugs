/* Demo cases for IoT protocol-order (typestate) analysis. The protocol is
 * supplied as data (tests/demo_protocol_spec.json), not bundled -- the engine
 * is generic and ships with no library-specific protocols.
 *
 * *_ok functions follow the legal create -> connect -> ... -> disconnect ->
 * destroy order; the others trigger invalid_protocol_transition.
 */

typedef void *MQTTClient;

/* OK: legal lifecycle. */
void demo_proto_ok(void) {
    MQTTClient c;
    MQTTClient_create(&c, "tcp://h:1883", "id", 0, 0);
    MQTTClient_connect(c, 0);
    MQTTClient_publish(c, "t", 0, 0, 0, 0, 0);
    MQTTClient_subscribe(c, "t", 1);
    MQTTClient_disconnect(c, 1000);
    MQTTClient_destroy(&c);
}

/* OK: reconnect after disconnect is legal. */
void demo_proto_reconnect_ok(void) {
    MQTTClient c;
    MQTTClient_create(&c, "tcp://h:1883", "id", 0, 0);
    MQTTClient_connect(c, 0);
    MQTTClient_disconnect(c, 1000);
    MQTTClient_connect(c, 0);
    MQTTClient_destroy(&c);
}

/* BUG: publish before connect (object is still 'created'). */
void demo_proto_publish_before_connect(void) {
    MQTTClient c;
    MQTTClient_create(&c, "tcp://h:1883", "id", 0, 0);
    MQTTClient_publish(c, "t", 0, 0, 0, 0, 0);
    MQTTClient_destroy(&c);
}

/* BUG: use after destroy. */
void demo_proto_use_after_destroy(void) {
    MQTTClient c;
    MQTTClient_create(&c, "tcp://h:1883", "id", 0, 0);
    MQTTClient_connect(c, 0);
    MQTTClient_destroy(&c);
    MQTTClient_publish(c, "t", 0, 0, 0, 0, 0);
}

/* OK: an untracked client (received as a parameter) is never flagged. */
void demo_proto_unknown_client(MQTTClient c) {
    MQTTClient_publish(c, "t", 0, 0, 0, 0, 0);
}
