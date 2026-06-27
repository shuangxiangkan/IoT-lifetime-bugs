/* Demo cases for IoT resource lifetime analysis.
 *
 * Function names encode the expectation: *_leak / *_double / *_loop_leak are
 * positive cases the analyzer should flag; *_ok / *_store / *_make / *_cache
 * are correct patterns it must NOT flag (regression negatives).
 *
 * These are illustrative snippets, not compilable against real headers.
 */

#include <stdlib.h>

struct ctx {
    struct pbuf *buf;
    int fd;
};

static struct pbuf *g_cached;

int send_packet(struct pbuf *p);
int bind_and_listen(int fd);
int process(char *buf);

/* BUG: pbuf leaks on the error path. */
int demo_pbuf_leak(int n) {
    struct pbuf *p = pbuf_alloc(PBUF_RAW, n, PBUF_RAM);
    if (send_packet(p) < 0)
        return -1;
    pbuf_free(p);
    return 0;
}

/* OK: pbuf freed on every path. */
int demo_pbuf_ok(int n) {
    struct pbuf *p = pbuf_alloc(PBUF_RAW, n, PBUF_RAM);
    if (p == NULL)
        return -1;
    pbuf_free(p);
    return 0;
}

/* BUG: socket fd never closed on the success path. */
int demo_socket_leak(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    bind_and_listen(fd);
    return 0;
}

/* OK: socket closed; the fd < 0 failure branch is not a leak. */
int demo_socket_ok(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    bind_and_listen(fd);
    close(fd);
    return 0;
}

/* OK: stored into a struct field -- ownership escapes this function. */
void demo_store_field(struct ctx *c) {
    c->buf = pbuf_alloc(PBUF_RAW, 10, PBUF_RAM);
}

/* OK: returned to the caller. */
struct pbuf *demo_make_pbuf(int n) {
    struct pbuf *p = pbuf_alloc(PBUF_RAW, n, PBUF_RAM);
    return p;
}

/* OK: cached in a global. */
void demo_cache_global(void) {
    g_cached = pbuf_alloc(PBUF_RAW, 10, PBUF_RAM);
}

/* BUG: a pbuf acquired each iteration, never freed in the loop body. */
void demo_loop_leak(int count) {
    int i;
    for (i = 0; i < count; i++) {
        struct pbuf *p = pbuf_alloc(PBUF_RAW, 10, PBUF_RAM);
        send_packet(p);
    }
}

/* OK: acquire and free inside the loop body. */
void demo_loop_ok(int count) {
    int i;
    for (i = 0; i < count; i++) {
        struct pbuf *p = pbuf_alloc(PBUF_RAW, 10, PBUF_RAM);
        send_packet(p);
        pbuf_free(p);
    }
}

/* BUG: the same pbuf is freed twice. */
void demo_double_free(int n) {
    struct pbuf *p = pbuf_alloc(PBUF_RAW, n, PBUF_RAM);
    pbuf_free(p);
    pbuf_free(p);
}

/* BUG: mutex held across an early return. */
int demo_lock_leak(pthread_mutex_t *m, int n) {
    pthread_mutex_lock(m);
    if (n < 0)
        return -1;
    pthread_mutex_unlock(m);
    return 0;
}

/* OK: mutex released on every path. */
int demo_lock_ok(pthread_mutex_t *m, int n) {
    pthread_mutex_lock(m);
    if (n < 0) {
        pthread_mutex_unlock(m);
        return -1;
    }
    pthread_mutex_unlock(m);
    return 0;
}

/* OK: malloc paired with free. */
int demo_malloc_ok(int n) {
    char *buf = malloc(n);
    if (buf == NULL)
        return -1;
    free(buf);
    return 0;
}

/* BUG: heap buffer leaked on the error path. */
int demo_malloc_leak(int n) {
    char *buf = malloc(n);
    if (process(buf) < 0)
        return -1;
    free(buf);
    return 0;
}

/* A project-defined deallocator: it forwards its argument to free(). The
 * wrapper-discovery pass should learn that demo_free_ctx() releases heap. */
void demo_free_ctx(struct ctx *c) {
    free(c);
}

/* OK: cleaned up through the custom wrapper on every path. Without wrapper
 * discovery this would be a false positive (demo_free_ctx looks like an
 * ordinary call that does not release). */
int demo_wrapper_release_ok(int n) {
    struct ctx *c = malloc(sizeof(struct ctx));
    if (c == NULL)
        return -1;
    if (process((char *)c) < 0) {
        demo_free_ctx(c);
        return -1;
    }
    demo_free_ctx(c);
    return 0;
}

/* A reader that takes a FILE* but does NOT free it -- must NOT be mistaken for
 * a release wrapper, so real leaks through such calls are still reported. */
int demo_read_all(FILE *fp) {
    char tmp[8];
    return (int)fread(tmp, 1, 8, fp);
}

/* BUG: file passed to a non-releasing reader, then leaked. demo_read_all must
 * not be treated as closing fp. */
int demo_reader_is_not_release(const char *path) {
    FILE *fp = fopen(path, "rb");
    if (fp == NULL)
        return -1;
    demo_read_all(fp);
    return 0;
}
