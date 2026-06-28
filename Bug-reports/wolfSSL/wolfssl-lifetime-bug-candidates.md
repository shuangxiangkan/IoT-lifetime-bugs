# wolfSSL lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：wolfSSL
- commit：`0cecccdf6e0504100c78126a558b6cbbcc486247`
- 扫描日期：2026-06-28
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：162 个文件、2,454 个函数、12 个候选、0 个解析警告
- 包含 tests/examples：301 个文件、4,108 个函数、15 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/wolfssl
python cli.py lifetime ../IoT-repos/wolfssl --include-tests
```

wolfSSL 核心代码大量使用：

```c
XMALLOC(...)
XCALLOC(...)
XREALLOC(...)
XFREE(...)
```

默认 POSIX 资源规格不包含这些宏。使用临时 allocator 别名补充扫描后，得到
78 个候选：

- 9 个 `socket_not_closed`
- 1 个 `file_not_closed`
- 14 个 `double_release`
- 30 个 `use_after_release`
- 13 个 `memory_not_freed`
- 9 个 `acquire_in_loop_without_release`
- 2 个 `owned_overwrite`

这些新增候选大量受到 `#if/#ifdef` 互斥分支、wolfSSL 自定义清理宏和返回
所有权影响，不能直接作为 bug。本文只把能够从源码构造完整可达路径的问题
列为确认问题。

## 2. 结论摘要

目前确认 6 类 lifetime 问题，均位于 `IDE/` 下的平台移植示例：

1. MQX client 早期错误路径 socket 泄漏。
2. MQX server 多条错误路径泄漏 socket、accepted socket、CTX 和 SSL。
3. QNX client 早期 socket 泄漏及 CTX 清理层级错误。
4. Azure Sphere server 错误路径 accepted socket 泄漏。
5. Azure Sphere server 正常 shutdown 路径可能 double free `ssl`。
6. IoT SAFE client certificate 错误路径绕过统一清理。

尚未确认 wolfSSL 核心密码库中存在真实 lifetime bug。

## 3. 已确认问题

### 3.1 MQX client：IP 或 connect 失败时 socket 泄漏

文件：`IDE/MQX/client-tls.c`

关键位置：

- 58 行：创建 `sockfd`
- 72—75 行：IP 地址非法时 `goto end`
- 79—82 行：`connect()` 失败时 `goto end`
- 161—162 行：`socket_cleanup` 才关闭 socket
- 163—164 行：`end` 直接返回

触发路径一：

```text
socket() 成功
  -> inet_pton() 失败
  -> goto end
  -> return ret
```

触发路径二：

```text
socket() 成功
  -> inet_pton() 成功
  -> connect() 失败
  -> goto end
  -> return ret
```

两个路径都绕过：

```c
socket_cleanup:
    close(sockfd);
```

#### 影响

这是命令行平台示例，单次进程的影响有限。但如果 main 逻辑被移植到任务或
重复调用的测试环境中，socket 可以累积泄漏。

#### 建议修复

将 socket 创建后的错误路径改为：

```c
goto socket_cleanup;
```

同时应初始化：

```c
int sockfd = -1;
```

并在 cleanup 中判断 socket 是否有效，避免 `socket()` 本身失败时关闭无效
或未初始化的描述符。

#### 建议验证

1. 使用非法 IPv4 地址触发 `inet_pton()` 失败。
2. 使用不可达端点触发 `connect()` 失败。
3. 比较调用前后的打开 fd 数量。
4. 重复执行失败路径，确认修复后 fd 数量稳定。

结论：**高置信度真阳性。**

### 3.2 MQX server：多条直接返回路径遗漏清理

文件：`IDE/MQX/server-tls.c`

相关资源：

- 63 行：listening socket `sockfd`
- 71 行：`WOLFSSL_CTX *ctx`
- 123 行：accepted socket `connd`
- 130 行：每个连接的 `WOLFSSL *ssl`

正常路径在 182—194 行释放当前连接、CTX 和 listening socket。然而大量
错误分支直接 `return -1`。

#### listening socket/CTX 泄漏路径

以下操作失败时直接返回：

- `wolfSSL_CTX_new()`
- certificate 加载
- private key 加载
- `bind()`
- `listen()`
- `accept()`

一旦 63 行的 `socket()` 成功，以上路径均至少泄漏 `sockfd`。CTX 创建成功
后，后续错误还会泄漏 `ctx`。

#### accepted socket/SSL 泄漏路径

连接被 `accept()` 接受后，以下错误直接返回：

- `wolfSSL_new()` 失败：泄漏 `connd`
- `wolfSSL_accept()` 失败：泄漏 `connd` 和 `ssl`
- `wolfSSL_read()` 失败：泄漏 `connd` 和 `ssl`
- `wolfSSL_write()` 失败：泄漏 `connd` 和 `ssl`

直接返回还绕过 server 级的 `wolfSSL_CTX_free()`、`wolfSSL_Cleanup()` 和
`close(sockfd)`。

#### 建议修复

建立两级 cleanup：

```c
connection_cleanup:
    wolfSSL_free(ssl);
    ssl = NULL;
    if (connd != -1) {
        close(connd);
        connd = -1;
    }

server_cleanup:
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    if (sockfd != -1)
        close(sockfd);
```

连接级错误可以选择结束当前连接并继续 accept，也可以跳到 server cleanup
退出程序；无论哪种行为，都必须先清理当前连接。

#### 建议验证

分别注入：

- `wolfSSL_CTX_new()` 失败
- `accept()` 后 `wolfSSL_new()` 失败
- TLS handshake 失败
- read/write 失败

验证 socket、accepted socket、CTX 和 SSL 对象在每条路径都保持分配/释放
平衡。

结论：**高置信度、多路径资源泄漏。**

### 3.3 QNX client：socket 泄漏和 CTX 清理层级错误

文件：`IDE/QNX/example-client/client-tls.c`

#### socket 泄漏

关键位置：

- 105 行：创建 `sockfd`
- 119—123 行：IP 地址非法后 `goto end`
- 126—129 行：connect 失败后 `goto end`
- 262—263 行：`socket_cleanup` 关闭 socket
- 264—265 行：`end` 直接返回

这与 MQX client 是相同模式：socket 创建成功后跳到了错误的 cleanup 层级。

#### CTX 泄漏

142 行创建 `ctx` 后：

- certificate DER→PEM 转换失败：153 行 `goto socket_cleanup`
- private key DER→PEM 转换失败：170 行 `goto socket_cleanup`

这两个路径虽然关闭了 socket，但绕过：

```c
ctx_cleanup:
    wolfSSL_CTX_free(ctx);
```

因此 `ctx` 泄漏。

#### 建议修复

- socket 创建后的地址/connect 失败跳到 `socket_cleanup`。
- CTX 创建后的 certificate/key 转换失败跳到 `ctx_cleanup`。
- 初始化 `ctx`、`ssl`、`sockfd` 和临时 `pem`。
- 更稳妥的做法是单一 cleanup 标签，根据非空/有效状态逆序释放。

#### 建议验证

1. 非法 IP。
2. connect 失败。
3. certificate 转换失败。
4. private-key 转换失败。
5. 检查 fd 和 wolfSSL allocator 的对象计数。

结论：**高置信度真阳性。**

### 3.4 Azure Sphere server：accepted socket 错误路径泄漏

文件：`IDE/VS-AZURE-SPHERE/server/server.c`

共享清理函数位于：

`IDE/VS-AZURE-SPHERE/shared/util.h`

```c
static void util_Cleanup(int sockfd, WOLFSSL_CTX* ctx, WOLFSSL* ssl)
{
    wolfSSL_free(ssl);
    wolfSSL_CTX_free(ctx);
    wolfSSL_Cleanup();
    close(sockfd);
}
```

server 创建两类 socket：

- 83 行：listening `sockfd`
- 141 行：accepted `connd`

accept 成功后，以下错误路径都调用：

```c
util_Cleanup(sockfd, ctx, ssl);
return -1;
```

涉及：

- `wolfSSL_new()` 失败
- `wolfSSL_accept()` 失败
- `wolfSSL_read()` 失败
- `wolfSSL_write()` 失败

`util_Cleanup()` 关闭的是 listening `sockfd`，没有接收 `connd`，因此 accepted
socket 被泄漏。

#### 建议修复

为当前连接增加独立清理：

```c
if (connd >= 0) {
    close(connd);
    connd = -1;
}
```

不要简单地把 `connd` 替换传入 `util_Cleanup()`，否则 listening socket、
CTX 和全局 wolfSSL 状态又得不到正确处理。推荐采用 server/connection 两级
cleanup。

#### 建议验证

accept 成功后分别让 `wolfSSL_new()`、handshake、read、write 失败，确认
listening socket 和 accepted socket 都按预期关闭。

结论：**高置信度 accepted-socket 泄漏。**

### 3.5 Azure Sphere server：shutdown 路径可能 double free `ssl`

文件：`IDE/VS-AZURE-SPHERE/server/server.c`

每轮连接正常结束时：

```c
wolfSSL_free(ssl);
close(connd);
```

但代码没有执行：

```c
ssl = NULL;
connd = -1;
```

如果本轮收到 `"shutdown"`：

```text
shutdown = 1
  -> 完成 reply
  -> wolfSSL_free(ssl)
  -> close(connd)
  -> 退出 while
  -> util_Cleanup(sockfd, ctx, ssl)
  -> wolfSSL_free(ssl) 再次执行
```

因此 `ssl` 在正常 shutdown 路径被释放两次。`connd` 不会在最终 cleanup 中
再次关闭，因为 `util_Cleanup()` 只处理 `sockfd`。

#### 建议修复

正常连接清理后立即清空状态：

```c
wolfSSL_free(ssl);
ssl = NULL;
close(connd);
connd = -1;
```

还应避免在下一轮 `wolfSSL_new()` 失败时把上一轮已经释放但未清空的 `ssl`
再次传给 `util_Cleanup()`。

#### 建议验证

1. 客户端发送 `"shutdown"`。
2. 正常完成 handshake、read 和 write。
3. 使用 ASan 或自定义 wolfSSL free hook。
4. 修复前应观察到重复释放，修复后每个 SSL 对象仅释放一次。

结论：**高置信度 double free，严重性高于单纯示例 socket 泄漏。**

### 3.6 IoT SAFE client：certificate 失败时绕过统一清理

文件：`IDE/iotsafe-raspberrypi/client-tls13.c`

函数大多数失败路径使用：

```c
goto exit;
```

`exit` 标签会清理：

- `sockfd`
- `ssl`
- `ctx`
- `WC_RNG`
- wolfSSL 全局状态

但 330—336 行加载 client certificate 失败时直接：

```c
return -1;
```

此时通常已经完成：

- socket 创建和 connect
- `wolfSSL_Init()`
- `wc_InitRng()`
- `wolfSSL_CTX_new()`

直接返回会绕过全部清理。

#### 建议修复

```c
ret = -1;
goto exit;
```

#### 建议验证

让 `wolfSSL_CTX_use_certificate_buffer()` 返回失败，检查 socket、CTX、RNG 和
全局清理函数是否执行。

结论：**高置信度错误路径泄漏。**

## 4. 已排除的默认扫描候选

### 4.1 INTIME TLS client

文件：`IDE/INTIME-RTOS/wolfExamples.c`

所有 socket 创建成功后的错误路径都跳到 `exit`，130—131 行关闭
`sockFd`。扫描器没有准确应用公共 cleanup。

结论：**误报。**

### 4.2 `wolfSSL_fopen()`

文件：`IDE/MDK-ARM/MDK-ARM/wolfSSL/wolfssl_MDK_ARM.c`

该函数是 `fopen()` wrapper，返回 `FILE *` 给调用方。stream 所有权随返回值
转移，不能要求 wrapper 自己关闭。

结论：**返回所有权误报。**

### 4.3 `wolfSSL_Malloc()` 的释放后返回

文件：`wolfcrypt/src/memory.c`

在 `WOLFSSL_FORCE_MALLOC_FAIL_TEST` 分支中，代码释放 `res` 后立即：

```c
return NULL;
```

函数末尾的 `return res` 与该分支在控制流上不可同时执行。扫描器把条件编译
和提前返回错误合流，产生 `use_after_release`。

结论：**误报。**

### 4.4 `tcp_socket()` 的 owned overwrite

文件：`wolfssl/test.h`

UDP、SCTP 和 TCP socket 创建位于互斥的 `if/else` 分支：

```c
if (udp)
    socket(...UDP...);
#ifdef WOLFSSL_SCTP
else if (sctp)
    socket(...SCTP...);
#endif
else
    socket(...TCP...);
```

单次调用只创建一个 socket，不存在先创建后覆盖。预处理条件导致 CFG
错误合并。

结论：**误报。**

### 4.5 async server

文件：`examples/async/async_server.c`

统一出口在 625—631 行明确关闭 `mConnd` 和 `mSockfd`。这是全局 socket
状态的延迟清理。

结论：**误报。**

### 4.6 Renesas FreeRTOS test

文件：`IDE/Renesas/e2studio/RA6M4/test/src/test_main.c`

- `xTaskCreate(..., NULL)` 故意不保存 task handle，测试任务按自身生命周期
  结束。
- `xSemaphoreTake(exit_semaph, ...)` 用于等待 worker 完成，不是要求当前函数
  再 `give` 的 mutex 所有权。

结论：**任务/同步协议误报。**

## 5. allocator 补充扫描的核心库候选

加入 `XMALLOC/XFREE` 后，主要候选集中在：

- `wolfcrypt/src/pkcs7.c`
- `wolfcrypt/src/ecc.c`
- `wolfcrypt/src/tfm.c`
- `src/x509.c`
- `src/internal.c`
- `src/sniffer.c`

目前没有把这些候选升级为确认 bug，主要原因如下。

### 互斥预处理分支被合并

wolfSSL 针对不同数学后端、硬件加速、内存模型和 PKCS7 配置包含大量
`#if/#elif/#else`。源码级 CFG 在没有具体 build configuration 时可能把本来
不能共存的：

```text
free -> return
```

和另一配置下的继续使用路径拼接起来，造成大量 UAF/double-free 级联报告。

### 自定义清理宏未建模

例如：

- `WC_FREE_VAR_EX`
- big integer init/free 宏
- packet/object-specific free helper
- 栈/堆可切换的临时对象宏

仅把 `XMALLOC/XFREE` 映射为堆 API，会识别 acquire，却不能完整识别这些
release wrapper。

### 返回或字段所有权

部分对象在成功路径中：

- 作为返回值交给调用方
- 存入 X509/PKCS7 上下文
- 加入链表
- 交给 wolfSSL 对象析构函数

函数退出时仍然 active 不代表泄漏。

### 后续复查建议

如果要继续验证核心候选，应先：

1. 选择一个实际 wolfSSL build configuration。
2. 使用该配置的预处理结果，而不是原始多配置源码。
3. 补充通用的宏展开/allocator-wrapper 识别。
4. 优先复查单配置下仍保留的 UAF/double-free。
5. 使用现有 wolfSSL unit tests 配合 ASan、LSan 或自定义 allocator hooks。

不建议直接根据当前 78 条候选向上游报告。

## 6. 推荐修复和报告顺序

1. Azure Sphere server double free：路径清晰、严重性最高。
2. Azure Sphere accepted socket 泄漏：可与 double free 放在同一平台补丁。
3. MQX server 多错误路径清理：适合统一重构 cleanup。
4. QNX client cleanup 标签修正。
5. MQX client socket cleanup。
6. IoT SAFE client 的 `return` 改为 `goto exit`。
7. 最后再基于具体编译配置复查核心 allocator 候选。

建议按平台拆分上游提交：

- Azure Sphere server cleanup
- MQX client/server cleanup
- QNX client cleanup
- IoT SAFE client cleanup

不要把这些 IDE 示例问题和尚未确认的核心 PKCS7/crypto 候选合并为一个
issue。
