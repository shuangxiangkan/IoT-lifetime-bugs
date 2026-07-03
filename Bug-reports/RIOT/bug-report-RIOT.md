# RIOT lifetime bug 候选复查记录

## 1. 扫描信息

- RIOT commit：`4cf7eaf7899a011e3e2ddfa13bf53f9b6da495a1`
- 扫描日期：2026-06-27
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：1,788 个文件、12,244 个函数、15 个候选、0 个解析警告
- 包含测试代码：2,723 个文件、17,257 个函数、21 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/RIOT
python cli.py lifetime ../IoT-repos/RIOT --include-tests
```

本文区分以下结论：

- **高置信度真 bug**：从当前源码能够构造完整的分配、错误路径和缺失清理链。
- **低影响真问题**：确实没有释放，但仅存在于一次性测试或 fuzzing 程序中。
- **误报**：资源已被返回、转移或延迟释放。

这仍然是静态源码复查结果。向 RIOT 提交 issue 或补丁前，建议在对应模块的可运行配置下补充动态测试。

## 2. 高置信度真 bug

### 2.1 `lwip_sock_sendv()` 的 `netbuf` 泄漏

文件：`pkg/lwip/contrib/sock/lwip_sock.c`

关键位置：

- 639 行：`buf = netbuf_new()`
- 641—643 行：`netbuf_alloc()` 失败时正确调用 `netbuf_delete(buf)`
- 680—684 行：网卡不匹配时直接返回
- 725 行：正常公共清理路径调用 `netbuf_delete(buf)`

触发路径：

```text
netbuf_new()
  -> netbuf_alloc() 成功
  -> conn != NULL
  -> remote != NULL
  -> remote->netif 是指定网卡
  -> netconn_getaddr() 成功
  -> 当前连接绑定的 netif 与 remote->netif 不一致
  -> return -EINVAL
```

问题代码：

```c
if ((remote->netif != netif)
        && (netif != SOCK_ADDR_ANY_NETIF)) {
    DEBUG(...);
    return -EINVAL;
}
```

此处绕过了 725 行的 `netbuf_delete(buf)`，所以已经分配的 `buf`
不会被释放。该路径位于正式网络代码中，并且函数可能被重复调用，泄漏可累积。

建议修复：

```c
netbuf_delete(buf);
return -EINVAL;
```

或者把错误路径统一改成跳转到公共清理标签，避免以后新增返回路径时再次遗漏。

建议验证：

1. 创建一个已经绑定本地 netif 的 lwIP socket。
2. 调用发送接口，并为 `remote->netif` 指定另一个网卡。
3. 确认函数返回 `-EINVAL`。
4. 对 `netbuf_new()` 和 `netbuf_delete()` 计数，修复前应少一次 delete，修复后应保持平衡。

结论：**高置信度真阳性，建议优先报告。**

### 2.2 `pthread_create()` 栈分配失败后留下悬空全局指针

文件：`sys/posix/pthread/pthread.c`

关键位置：

- 125 行：`pt = calloc(...)`
- 131 行：`insert(pt)`
- 92—106 行：`insert()` 把 `pt` 写入 `pthread_sched_threads[]`
- 144 行：为线程栈分配内存
- 146—149 行：栈分配失败时释放 `pt`

触发路径：

```text
calloc(pt) 成功
  -> insert(pt) 成功
  -> pthread_sched_threads[pthread_pid - 1] = pt
  -> malloc(stack_size) 失败
  -> free(pt)
  -> return -ENOMEM
```

问题代码：

```c
if (stack == NULL) {
    free(pt);
    return -ENOMEM;
}
```

这里释放了 `pt`，但没有执行：

```c
pthread_sched_threads[pthread_pid - 1] = NULL;
```

全局线程表因此仍然指向已经释放的对象。后续 `pthread_join()`、
`pthread_detach()` 或线程 ID 复用逻辑可能读取该悬空指针，形成
use-after-free；该槽位也无法被 `insert()` 再次正常使用。

同一函数的其他失败路径已经进行了清理：

```c
pthread_sched_threads[pthread_pid - 1] = NULL;
```

例如 reaper 创建失败和底层线程创建失败路径。这进一步说明栈分配失败
路径很可能是遗漏清理，而不是有意保留。

建议修复：

```c
if (stack == NULL) {
    free(pt);
    pthread_sched_threads[pthread_pid - 1] = NULL;
    return -ENOMEM;
}
```

还应考虑只在 `pthread_create()` 完全成功后再写入 `*newthread`，避免失败时
调用方获得一个无效但看似有效的线程 ID。

建议验证：

1. fault injection：让 `calloc(pt)` 成功，让随后的栈 `malloc()` 失败。
2. 检查 `pthread_create()` 返回 `-ENOMEM`。
3. 检查对应的 `pthread_sched_threads[]` 槽位为 `NULL`。
4. 再次创建线程，确认该槽位可以复用。
5. 在修复前调用 `pthread_join(*newthread, ...)`，观察是否能够触发 ASan
   或其他内存检查器报告。

结论：**高置信度真实 lifetime bug，但它是在人工复查相关候选时发现的，
不是扫描器直接给出的精确诊断。**

## 3. fuzzing 辅助代码中的真实泄漏

### 3.1 `fuzzing_read_packet()` 没有释放输入缓冲区

文件：`sys/fuzzing/fuzzing.c`

关键位置：

- 60 行：`input = fuzzing_read_bytes(...)`
- 65—67 行：packet 扩容失败后直接返回
- 69 行：把 `input` 内容复制到 `pkt->data`
- 72 行：成功返回

`fuzzing_read_bytes()` 返回堆内存，但 `fuzzing_read_packet()` 在以下两条路径
都没有释放：

```text
input 分配成功 -> gnrc_pktbuf_realloc_data() 失败 -> return -ENOMEM
input 分配成功 -> memcpy() -> return 0
```

复制完成后 `input` 不再被使用，也没有转移给 `pkt`。`gnrc_pktbuf_fuzzptr`
保存的是 `pkt`，不是 `input`，因此不能视为所有权转移。

建议修复：

```c
if (gnrc_pktbuf_realloc_data(pkt, rsiz)) {
    free(input);
    return -ENOMEM;
}

memcpy(pkt->data, input, rsiz);
free(input);
```

源码注明该函数当前只能调用一次，所以泄漏通常不会无限累积，但仍会干扰
LeakSanitizer 和长生命周期 fuzzing harness。

结论：**真实但影响受限的泄漏。**

### 3.2 `fuzzing_read_bytes()` 直接覆盖 `realloc` 指针

文件：`sys/fuzzing/fuzzing.c`

关键位置：

- 84 行：初始 `realloc(NULL, rsiz)`
- 95 行：循环内扩容
- 101—102 行：`read()` 失败路径
- 106 行：最终缩容

循环扩容和最终缩容使用了以下模式：

```c
if ((buffer = realloc(buffer, new_size)) == NULL) {
    return NULL;
}
```

当 `new_size` 非零且 `realloc()` 失败时，原内存仍然有效，但唯一的指针
已经被 `NULL` 覆盖，因此发生泄漏。95 行和 106 行都存在该问题。

此外，`read()` 返回 `-1` 时，102 行直接返回，也没有释放当前 `buffer`。

建议使用临时变量：

```c
uint8_t *new_buffer = realloc(buffer, new_size);
if (new_buffer == NULL) {
    free(buffer);
    return NULL;
}
buffer = new_buffer;
```

错误读取路径也应先执行：

```c
free(buffer);
return NULL;
```

需要单独检查 `csiz == 0` 时 `realloc(buffer, 0)` 的平台语义。更清晰的实现
是显式处理零长度输入，避免把合法空输入和分配失败混为一谈。

建议验证：

1. 用 allocator fault injection 分别让 95 行和 106 行的 `realloc()` 失败。
2. 用一个会返回 `-1` 的 fd 触发 102 行。
3. 使用 ASan/LSan 或分配计数验证所有失败路径。

结论：**真实泄漏，主要影响 fuzzing 工具代码。**

## 4. 测试程序中的低优先级问题

### 4.1 tiny-asn1 错误路径泄漏

文件：`tests/pkg/tiny-asn1/main.c`

72 行分配 `asn1_objects`。正常路径在 196 行释放，但 80 行之后存在多个
直接 `return 1` 的解析或格式校验失败路径，没有先释放该数组。

例如：

```c
if (der_decode(...) < 0) {
    printf(...);
    return 1;
}
```

建议统一使用 `goto cleanup`，或者在每个错误返回前调用
`free(asn1_objects)`。

这是一次性测试程序，进程退出后操作系统会回收内存，因此不建议作为主要
漏洞报告；可以作为测试代码质量补丁处理。

结论：**技术上是真泄漏，实际影响低。**

### 4.2 URI parser fuzzing 主程序

文件：`fuzzing/uri_parser/main.c`

18 行通过 `fuzzing_read_bytes()` 获取 `input_buf`，解析后直接调用
`exit(EXIT_SUCCESS)`，没有显式释放。

由于进程立即退出，内存会被操作系统回收。这更接近进程生命周期资源，
通常不作为 RIOT 正式库的有效 bug 报告，但会产生 LeakSanitizer 噪声。

结论：**低价值候选，不建议优先报告。**

## 5. 已排除的主要误报

### ESP8266 FreeRTOS wrapper

- `task_create_wrapper()` 返回新建的 task handle，把所有权交给调用方。
- `semphr_take_wrapper()` 和 `mutex_lock_wrapper()` 是加锁包装函数，本来就应
  在持锁状态下返回；解锁由对应 wrapper 和调用者负责。

这些不能按“函数退出前必须释放”的局部规则判断。

### `make_message()`

文件：`cpu/native/syscalls.c`

- `message` 成功时作为返回值交给调用方。
- `temp = realloc(message, size)` 使用临时变量，不会在失败时丢失旧指针。
- `message = temp` 是合法的别名更新，不是 owned overwrite。

对应的四条报告均为误报。

### `pipe_malloc()`

文件：`sys/pipe/pipe_dynamic.c`

分配对象的首字段是 `pipe_t`，函数返回 `&m_pipe->pipe`。释放函数通过
`pipe->free` 回调设置为 `free`，最终由 `pipe_free()` 释放。这属于返回内部
字段指针和析构回调组合的所有权协议。

### `pthread_create()` 的成功路径

成功创建后，`pt` 保存到 `pthread_sched_threads[]`，stack 在线程退出时交给
reaper 释放，`pt` 在 join/detach 阶段释放。因此扫描器报告的“成功返回时
未释放”不是泄漏。

真正的问题是第 2.2 节描述的栈分配失败路径没有从全局表中移除 `pt`。

### malloc 相关测试

- `tests/sys/malloc/main.c` 的链表由 `free_memory(head)` 释放。
- overflow calloc 测试预期 `calloc()` 返回 `NULL`。
- `tests/sys/malloc_monitor/main.c` 使用 `realloc(ptr, 0)` 测试释放语义。
- `mallinfo()` 中局部变量名 `post` 被错误识别成堆资源，不是真实分配。

## 6. 后续检查优先级

建议按以下顺序继续：

1. 为 lwIP `netbuf` 泄漏编写最小测试并确认目标配置下路径可达。
2. 对 `pthread_create()` 做栈分配失败 fault injection。
3. 用 ASan/LSan 验证 `sys/fuzzing/fuzzing.c` 的三条失败/成功路径。
4. 检查 RIOT 当前上游是否已经有对应 issue 或补丁。
5. 最后再决定是否整理成多个独立 issue；lwIP、pthread 和 fuzzing
   分属不同模块，不建议合并为一个上游报告。
