# paho.mqtt.c lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Eclipse Paho MQTT C Client
- commit：`4a939ddb01eea581a32fd6f0adcfee51b91d2601`
- 扫描日期：2026-06-27
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：24 个文件、350 个函数、40 个候选、0 个解析警告
- 包含 tests/samples：59 个文件、1,150 个函数、52 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/paho.mqtt.c
python cli.py lifetime ../IoT-repos/paho.mqtt.c --include-tests
```

本文使用以下分类：

- **高置信度真 bug**：能够从源码构造完整的分配、错误分支和缺失清理路径。
- **低优先级真问题**：主要存在于 sample 或 test 代码，或者只在低内存条件下产生有界泄漏。
- **误报**：资源已经转移给异步队列、SocketBuffer、返回对象或全局清理逻辑。

目前结论来自静态源码复查。正式向 Paho 提交 issue 前，建议使用 allocator
fault injection、ASan/LSan 和错误 socket 模拟补充动态证据。

## 2. 高置信度真 bug

### 2.1 `pstget()` 在 buffer 分配失败时泄漏文件句柄

文件：`src/MQTTPersistenceDefault.c`

关键位置：

- 290 行：`fp = fopen(filename, "rb")`
- 297 行：`buf = malloc(fileLen)`
- 300 行：分配失败后 `goto exit`
- 307 行：正常路径调用 `fclose(fp)`

触发路径：

```text
fopen() 成功
  -> fseek()/ftell() 完成
  -> malloc(fileLen) 失败
  -> goto exit
  -> return rc
```

问题代码：

```c
if ((buf = (char *)malloc(fileLen)) == NULL)
{
    rc = PAHO_MEMORY_ERROR;
    goto exit;
}
```

`fclose(fp)` 位于这段代码之后，因此该错误路径会直接绕过关闭操作。重复读取
持久化消息并遇到内存压力时，文件描述符可能持续耗尽。

建议修复：

```c
exit:
    if (fp)
        fclose(fp);
```

采用统一 cleanup 时，需要删除正常路径上原有的重复 `fclose()`，或者在关闭
后把 `fp` 设置为 `NULL`。

建议验证：

1. 创建一个有效的持久化消息文件。
2. 让 `fopen()` 成功，并用 fault injection 让 297 行的 `malloc()` 失败。
3. 对 `open/fopen` 和 `close/fclose` 计数。
4. 循环调用 `pstget()`，确认修复前 fd 数量增长、修复后保持稳定。

结论：**高置信度真阳性，适合优先报告。**

### 2.2 `WebSocket_connect()` 的 HTTP headers buffer 泄漏

文件：`src/WebSocket.c`

关键位置：

- 450 行：分配 `headers_buf`
- 494 行：分配 WebSocket handshake `buf`
- 497 行：第二次分配失败后 `goto exit`
- 502—503 行：正常路径才释放 `headers_buf`

触发路径：

```text
存在自定义 HTTP headers
  -> malloc(headers_buf_len) 成功
  -> 计算 handshake 长度
  -> malloc(buf_len) 失败
  -> goto exit
```

此时 `headers_buf` 尚未执行 503 行的 `free(headers_buf)`，函数出口也没有
统一清理。

建议在 `exit` 标签统一释放局部 buffer，或者在 497 行跳转前释放：

```c
free(headers_buf);
headers_buf = NULL;
```

建议验证：

1. 设置至少一个 `httpHeaders` 项。
2. 让 450 行分配成功、494 行分配失败。
3. 使用 LSan 或分配计数检查 `headers_buf`。

结论：**高置信度 OOM 路径泄漏。**

### 2.3 `WebSocket_receiveFrame()` 分片接收错误路径泄漏

文件：`src/WebSocket.c`

关键位置：

- 1021 行：局部 `res = NULL`
- 1190 行：为新 frame 分配 `res`
- 1217—1221 行：完成一次数据读取后仍可能因 socket error 退出
- 1248—1249 行：完整接收后才把 `res` 加入 `in_frames`
- 1252—1259 行：错误出口没有释放新建但尚未入队的 `res`

关键所有权区别：

- 如果函数进入时 `in_frames->first` 已存在，`res` 已经归队列所有。
- 如果队列为空，1190 行新建的 `res` 在 1249 行之前仍然只是局部对象。

因此以下路径会丢失局部对象：

```text
in_frames 为空
  -> malloc(res) 成功
  -> 收到一个非 final 分片，或完成当前分片处理
  -> 后续 socket read/flush 返回错误或中断
  -> goto exit
  -> res 尚未加入 in_frames
```

`TCPSOCKET_INTERRUPTED` 路径只调用 `WebSocket_rewindData()`，没有保存或释放
局部 `res`。

建议修复有两种方向：

1. 新建 `res` 后立即登记到 `in_frames`，后续 `realloc()` 同步更新
   `in_frames->first->content`。
2. 增加明确的 `res_owned_by_list` 标志，在错误出口释放尚未入队的对象。

验证时应分别覆盖：

- 第一个分片后返回 `TCPSOCKET_INTERRUPTED`
- 第二个分片读取时返回 `SOCKET_ERROR`
- 完整帧正常入队
- 已有 queued frame 的 `realloc()` 路径

结论：**高置信度错误/中断路径泄漏，但修复时必须避免释放已经属于队列的
`res`。**

### 2.4 `MQTTAsync_addCommand()` 入队失败时不释放 command

文件：`src/MQTTAsyncUtils.c`

关键位置：

- 792 行：`MQTTAsync_addCommand()`
- 833—835 行：CONNECT/DISCONNECT 的 `ListInsert()` 失败
- 840—843 行：其他命令的 `ListAppend()` 失败
- `src/LinkedList.c` 90—95 行：`ListAppend()` 只分配 list element，不复制或
  释放传入的 content
- `src/LinkedList.c` 107—130 行：`ListInsert()` 具有相同语义

当 list element 的 `malloc(sizeof(ListElement))` 失败时：

```text
调用方已分配 command 及其嵌套字段
  -> MQTTAsync_addCommand(command, ...)
  -> ListInsert/ListAppend 返回 NULL
  -> MQTTAsync_addCommand 返回 PAHO_MEMORY_ERROR
  -> 调用方直接返回 rc
```

此时 command 从未进入 `MQTTAsync_commands`，队列不会负责释放；调用方也不
再持有可用于清理的局部路径。

受影响的主要入口包括：

- `MQTTAsync_connect()`
- `MQTTAsync_reconnect()`
- `MQTTAsync_subscribeMany()`
- `MQTTAsync_unsubscribeMany()`
- `MQTTAsync_send()`
- `MQTTAsync_disconnect1()`
- timeout/reconnect 中创建 command 的内部路径

对于 SUBSCRIBE、UNSUBSCRIBE、PUBLISH，泄漏不仅包括 command 本身，还可能
包括 topics、payload、properties 和 destinationName。

建议明确 `MQTTAsync_addCommand()` 的失败所有权契约。较集中的修复方案是：

```c
if (ListAppend(...) == NULL)
{
    MQTTAsync_freeCommand(command);
    rc = PAHO_MEMORY_ERROR;
    goto exit;
}
```

CONNECT 分支的 `ListInsert()` 失败也要使用相同处理。修复前应检查所有调用
方，避免某个调用方已经在失败后自行释放而导致 double free。

建议验证：

1. 为 `ListAppend/ListInsert` 内部的 list-element 分配注入失败。
2. 分别创建 CONNECT、SUBSCRIBE、UNSUBSCRIBE、PUBLISH、DISCONNECT 命令。
3. 检查返回值为 `PAHO_MEMORY_ERROR`。
4. 用 LSan 验证 command 和全部嵌套字段均被释放。
5. 验证 duplicate CONNECT 分支原有的 `MQTTAsync_freeCommand()` 不受影响。

结论：**高置信度、影响多个公共 API 的系统性 OOM 路径泄漏。**

### 2.5 `MQTTAsync_unsubscribeMany()` 局部分配失败清理不完整

文件：`src/MQTTAsync.c`

关键位置：

- 1229 行：分配 `unsub`
- 1247 行：复制 MQTT properties
- 1250 行：分配 topics 数组
- 1253 行：topics 分配失败后直接 `goto exit`

触发路径：

```text
malloc(unsub) 成功
  -> MQTTProperties_copy() 可能创建 properties.array
  -> malloc(topics array) 失败
  -> goto exit
```

这条路径没有释放：

- `unsub`
- `unsub->command.properties`

此外，1256 行逐项调用 `MQTTStrdup(topic[i])`，没有检查单项复制失败。结果
可能是把含 `NULL` topic 的不完整命令加入队列，而不是安全失败。

建议使用统一的 command 析构函数处理所有失败路径：

```c
MQTTAsync_freeCommand(unsub);
unsub = NULL;
```

还应检查每个 `MQTTStrdup()` 的结果，并清理此前复制成功的 topic。

结论：**高置信度真实泄漏，并伴随不完整命令入队风险。**

### 2.6 `MQTTAsync_subscribeMany()` 的嵌套 properties 泄漏

文件：`src/MQTTAsync.c`

`sub` 本体在多个失败路径上已被释放，但 1087 行通过
`MQTTProperties_copy()` 创建的嵌套内存没有总是释放。

典型路径：

```text
malloc(sub) 成功
  -> MQTTProperties_copy() 成功
  -> optlist/topics/qoss 或单个 topic 分配失败
  -> free(sub)
  -> 未调用 MQTTProperties_free(&sub->command.properties)
```

这说明只追踪顶层变量 `sub` 不够，需要按 command 的析构协议清理其嵌套
字段。

建议让全部失败路径调用 `MQTTAsync_freeCommand(sub)`，不要分别手写部分
字段的 `free()`。

结论：**高置信度嵌套资源泄漏。**

### 2.7 `Protocol_processPublication()` 队列构造失败泄漏

异步版本文件：`src/MQTTAsyncUtils.c`

关键位置：

- 2635 行：分配 `mm`
- 2641 行：可选分配 payload
- 2659 行：复制 properties
- 2681 行：分配 `qe`
- 2683—2684 行：`qe` 分配失败后直接退出
- 2688 行：忽略 `ListAppend()` 返回值

如果 `qe` 分配失败，之前创建的 `mm`、payload 和 properties 都没有释放。
如果 `ListAppend()` 内部的 list-element 分配失败，`qe` 和其拥有的 message
对象同样不会进入队列，也没有清理。

同步版本位于 `src/MQTTClient.c` 的 `Protocol_processPublication()`：

- 1176 行：分配 `qe`
- 1179 行：分配 `mm`
- 1193 行：分配 payload
- 1216 行：忽略 `ListAppend()` 返回值

同步版本的前置分配失败清理较完整，但依然没有处理最终入队失败。

建议：

- 为 `qEntry` 建立统一析构 helper。
- 检查 `ListAppend()` 返回值。
- 入队失败时释放 topic、message、payload 和 MQTT properties。
- 注意 `publish->topic` 在同步版本 1190 行已经转移给 `qe`，清理时必须按
  转移后的所有权处理。

结论：**高置信度 OOM 路径泄漏，异步版本的问题更严重。**

### 2.8 `MQTTProtocol_queueAck()` 忽略入队失败

文件：`src/MQTTProtocolClient.c`

关键位置：

- 892 行：分配 `ackReq`
- 899 行：调用 `ListAppend()`
- 902—903 行：直接返回成功

问题代码：

```c
ackReq = malloc(sizeof(AckRequest));
...
ListAppend(client->outboundQueue, ackReq, sizeof(AckRequest));
```

`ListAppend()` 为 list element 分配内存。如果该分配失败，`ackReq` 没有进入
队列且不会被释放，同时函数仍返回成功。结果不仅是泄漏，还可能导致应答
消息静默丢失。

建议检查返回值：

```c
if (ListAppend(...) == NULL)
{
    free(ackReq);
    rc = PAHO_MEMORY_ERROR;
}
```

结论：**高置信度泄漏，并有协议行为影响。**

### 2.9 SUBACK/UNSUBACK 部分构造失败泄漏

文件：`src/MQTTPacketOut.c`

涉及函数：

- `MQTTPacket_suback()`：325—352 行
- `MQTTPacket_unsuback()`：446—475 行

两个函数都循环分配 reason-code 节点并加入 list。后续节点分配失败时，代码
释放 properties 和顶层 `pack`，但没有完整释放：

- list 对象
- 已经成功加入 list 的节点内容
- 可能已经分配的当前节点

另外，`ListAppend()` 返回值没有检查。如果 list-element 分配失败，新分配
的 `newint/newrc` 不会进入 list，也不会被释放。

建议使用对应 packet 析构函数或专用 cleanup label，对部分构造对象执行
递归清理，而不是只释放顶层 `pack`。

建议验证：

1. 构造包含多个 reason code 的 SUBACK/UNSUBACK。
2. 在第二个或后续 reason-code 分配时注入失败。
3. 在 `ListAppend()` 的 element 分配处单独注入失败。
4. 用 LSan 检查 list、已加入节点和当前节点。

结论：**高置信度部分构造失败泄漏。**

## 3. samples/tests 中的低优先级问题

### 3.1 `paho_c_pub` 循环覆盖 `buffer`

文件：`src/samples/paho_c_pub.c`

当启用 `opts.stdin_lines` 时，475 行在主循环的每一轮分配新 `buffer`，但
495 行只在整个循环结束后释放最后一次分配：

```text
loop iteration 1: buffer = malloc(...)
loop iteration 2: buffer = malloc(...)  // 覆盖上一地址
...
after loop: free(buffer)                // 只释放最后一个
```

`MQTTAsync_send()` 会把 payload 复制进 command，因此 `mypublish()` 返回后
sample buffer 不需要继续保留。建议每轮发布完成后释放，或者在下一轮分配
前释放旧值。

结论：**真实、可随输入行数累积的 sample 泄漏。**

### 3.2 sample URL 的清理不一致

涉及：

- `src/samples/paho_c_sub.c`
- `src/samples/paho_cs_sub.c`
- 部分 pub sample 的错误退出路径

例如 `paho_c_sub.c` 230 行在没有显式 connection 时分配 `url`，正常出口
只销毁 MQTT client，没有释放 `url`。`paho_c_pub.c` 的正常路径通过
`url_allocated` 正确释放，但多个 `exit(EXIT_FAILURE)` 路径仍绕过清理。

这些都是短生命周期命令行 sample，影响低，但会产生 LSan 噪声。

### 3.3 Python test binding 的 `CallbackEntry` 泄漏

涉及：

- `test/python/mqttasync_module.c`
- `test/python/mqttclient_module.c`

代码先 `malloc(CallbackEntry)`，再调用 `PyArg_ParseTuple()`。解析失败或
callable 检查失败时直接 `return NULL`，没有释放 `e`。设置 callback 的底层
API 返回失败时也需要检查 `e` 是否被释放。

此外，代码没有先检查 `malloc()` 返回值，内存不足时会把 `&e->context`
传给 `PyArg_ParseTuple()`，可能导致 NULL dereference。

建议把参数解析放在局部变量中，解析成功后再分配 `CallbackEntry`，或者为
所有 Python 异常出口增加统一 cleanup。

结论：**测试绑定中的真实错误路径泄漏，并伴随 OOM 崩溃风险。**

## 4. 已排除的主要误报

### 4.1 `TCPSOCKET_INTERRUPTED` 后的发送 buffer

以下发送函数在 `rc != TCPSOCKET_INTERRUPTED` 时自行释放；发生 interrupted
write 时，buffer 会转移给 `SocketBuffer_pendingWrite()`：

- `MQTTPacket_send()`
- `MQTTPacket_sends()`
- `MQTTPacket_send_connect()`
- `MQTTPacket_send_subscribe()`
- `MQTTPacket_send_unsubscribe()`
- `MQTTPacket_send_publish()`
- ACK/DISCONNECT 发送函数

因此“函数退出时仍 active”不能直接认定为泄漏。这里存在条件式所有权转移。

### 4.2 成功加入 list/command queue 的对象

以下对象在成功路径上转移给队列，并由后续消费或终止逻辑释放：

- `MQTTAsync_queuedCommand`
- `pending_write`
- `AckRequest`
- `qEntry`
- Socket pending/connect 节点

扫描器报告成功返回时未释放属于误报。真正的问题是本文列出的
`ListAppend/ListInsert` 失败路径没有完成回滚。

### 4.3 packet parser 返回对象

`MQTTPacket_suback()`、`MQTTPacket_unsuback()` 等 parser 成功时返回 packet
给调用方，并由对应 packet free 逻辑释放。循环中新建的 reason-code 节点
成功加入 packet list 后，也已经完成所有权转移。

真正的问题只发生在部分构造或 list-element 分配失败时。

### 4.4 `Socket_putdatas()` 和 `Socket_new()`

- `Socket_putdatas()` 的 `sockmem` 成功加入 `write_pending` 后由 list 管理。
- `Socket_new()` 的 `pnewSd` 成功加入 `connect_pending` 后由 list 管理。
- 两者在 `ListAppend()` 失败时均显式释放局部对象。

对应扫描报告属于 sink 推断不足，不是真泄漏。

### 4.5 WebSocket 全局 frame buffer

`WebSocket_getRawSocketData()` 中的 `frame_buffer` 是跨调用缓存，由
`WebSocket_terminate()` 统一释放。它不是每次函数退出都应释放的局部资源。

### 4.6 mutex wrapper

`Paho_thread_lock_mutex()` 的职责就是获得锁并在持锁状态下返回，调用方通过
`Paho_thread_unlock_mutex()` 解锁。不能要求 lock wrapper 在自身退出前释放
锁。

## 5. 推荐复查与修复顺序

1. `pstget()` 文件句柄泄漏：路径最短，容易动态验证。
2. `MQTTAsync_addCommand()` 入队失败：影响多个公共 API，适合统一修复。
3. `MQTTAsync_subscribeMany/unsubscribeMany()` 的嵌套对象清理。
4. publication/ACK/SUBACK 队列失败路径。
5. `WebSocket_connect()` headers 泄漏。
6. `WebSocket_receiveFrame()` 分片状态清理：风险较高，修复时需谨慎处理
   队列所有权。
7. samples 和 Python test binding：单独作为低优先级清理补丁。

建议不要把所有问题合并成一个上游 issue。较合理的拆分是：

- persistence fd leak
- async command/list OOM cleanup
- packet/list partial-construction cleanup
- WebSocket error-path cleanup
- samples/tests cleanup
