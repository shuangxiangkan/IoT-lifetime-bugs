# Mosquitto lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Eclipse Mosquitto
- commit：`0e89f0ef10d40b8fd35d831d96098d362139e529`
- 扫描日期：2026-06-27
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：199 个文件、1,200 个函数、9 个候选、0 个解析警告
- 包含 tests/examples：477 个文件、2,930 个函数、40 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/mosquitto
python cli.py lifetime ../IoT-repos/mosquitto --include-tests
```

人工复查结论：

- 确认 1 个正式代码中的 fd 泄漏。
- 保留 1 个低置信度、低影响的插件退出清理问题。
- 其余候选均能找到显式释放或所有权转移证据。

本文结论来自静态源码复查。提交上游 issue 前，建议使用 fault injection
构造 `fdopen()` 失败，并记录进程打开的文件描述符数量。

## 2. 高置信度真 bug

### 2.1 `dynsec_init()` 在 `fdopen()` 失败时泄漏文件描述符

文件：`apps/mosquitto_ctrl/dynsec.c`

关键位置：

- 727 行：生成待写入的 JSON 字符串
- 733 行：通过 `open()` 创建配置文件
- 739 行：通过 `fdopen()` 把 fd 转换为 `FILE *`
- 741—744 行：成功时通过 `fclose(fptr)` 关闭 stream 和底层 fd
- 745—748 行：`fdopen()` 失败时直接返回

相关代码：

```c
int fd = open(filename, O_CREAT | O_EXCL | O_WRONLY, 0640);
if(fd < 0){
    free(json_str);
    fprintf(stderr, ...);
    return -1;
}
fptr = fdopen(fd, "wb");
```

后续错误处理：

```c
if(fptr){
    fprintf(fptr, "%s", json_str);
    free(json_str);
    fclose(fptr);
}else{
    free(json_str);
    fprintf(stderr, ...);
    return -1;
}
```

#### 触发路径

```text
init_create() 成功
  -> cJSON_PrintUnformatted() 返回 JSON
  -> open(filename, ...) 成功，获得 fd
  -> fdopen(fd, "wb") 失败
  -> fptr == NULL
  -> free(json_str)
  -> return -1
```

POSIX `fdopen()` 失败不会替调用方关闭传入的 fd。此时 `fptr` 为 `NULL`，
因此也不能通过 `fclose(fptr)` 清理；必须显式调用 `close(fd)`。

#### 影响

- 每次命中该错误路径都会泄漏一个文件描述符。
- `open()` 使用 `O_CREAT | O_EXCL`，因此失败路径还可能留下一个已经创建但
  内容为空的配置文件。
- `mosquitto_ctrl dynsec init` 通常是短生命周期命令，单次运行的实际影响
  有限，但资源所有权错误是明确的。
- 如果相关逻辑被嵌入长生命周期进程或反复调用，fd 可以累积耗尽。

#### 建议修复

最小修复：

```c
fptr = fdopen(fd, "wb");
if(fptr == NULL){
    close(fd);
}
```

更完整的实现可以直接在错误分支中处理：

```c
fptr = fdopen(fd, "wb");
if(fptr == NULL){
    int saved_errno = errno;
    close(fd);
    free(json_str);
    fprintf(stderr,
            "dynsec init: Unable to open '%s' for writing (%s).\n",
            filename, strerror(saved_errno));
    return -1;
}
```

保存 `errno` 可以避免 `close()` 改写原始的 `fdopen()` 错误原因。

还可考虑在 `fdopen()` 失败时删除刚创建的空文件，但这属于行为设计选择，
不是修复 fd 泄漏的必要条件。

#### 建议验证

1. 使用链接包装、mock 或 fault injection 让 `open()` 成功、`fdopen()` 失败。
2. 调用 `dynsec_init()`。
3. 确认返回错误。
4. 在 Linux 上比较调用前后的 `/proc/self/fd` 数量。
5. 重复执行失败路径，修复前 fd 数量应持续增加，修复后应保持稳定。
6. 检查成功路径仍由 `fclose(fptr)` 关闭底层 fd，避免新增 double close。

可使用的伪测试：

```c
int before = count_open_fds();

mock_open_success();
mock_fdopen_failure();
assert(dynsec_init(...) != 0);

int after = count_open_fds();
assert(after == before);
```

#### 结论

**高置信度真阳性，建议作为独立 issue 或小型补丁提交。**

## 3. 低置信度复查项

### 3.1 client-lifetime-stats 插件退出时可能残留哈希表节点

文件：
`plugins/examples/client-lifetime-stats/mosquitto_client_lifetime_stats.c`

正常所有权流程：

```text
callback_connect()
  -> malloc(client)
  -> strdup(client->id)
  -> HASH_ADD_KEYPTR(local_lifetimes, client)

callback_disconnect()
  -> HASH_FIND(client)
  -> HASH_DELETE(client)
  -> free(client->id)
  -> free(client)
```

因此扫描器对 `callback_connect()` 的普通返回路径报
`memory_not_freed` 是误报：对象已经转移给 `local_lifetimes`。

需要额外复查的是 `mosquitto_plugin_cleanup()` 当前为空。如果插件卸载或
broker 退出时，某些 client 尚未触发 disconnect 回调，哈希表中的节点不会
在 cleanup 中显式释放。

当前不能直接认定为正式 bug，原因是：

- 需要确认 broker 的关闭顺序是否保证先为所有 client 触发 disconnect。
- 这是 examples 下的统计插件，不是 broker 核心逻辑。
- 即使命中，通常也发生在进程或插件生命周期结束时。

建议后续检查：

1. 阅读 Mosquitto plugin shutdown 的事件顺序保证。
2. 在仍有连接 client 时卸载插件或终止 broker。
3. 检查 disconnect callback 是否总在 plugin cleanup 前执行。
4. 如果没有保证，在 cleanup 中遍历 `local_lifetimes` 并释放剩余节点。

结论：**保留为低优先级检查项，不建议在缺少动态证据时作为确认 bug
报告。**

## 4. 已排除的正式源码误报

### 4.1 `db_dump.c` 的统计节点

涉及候选：

- `dump__client_chunk_process()` 中的 `cc`
- `dump__base_msg_chunk_process()` 中的 `mcs`

`cc` 通过 `HASH_ADD_KEYPTR()` 加入 `clients_by_id`，`mcs` 通过
`HASH_ADD()` 加入 `msgs_by_id`。程序结束时：

- 425—428 行遍历并释放 `msgs_by_id`
- 430—434 行遍历并释放 `clients_by_id` 及 `cc->id`

这些对象采用跨函数哈希表所有权，不能要求创建函数退出前释放。

结论：**误报。**

### 4.2 `signal_all()` 中的 `/proc` 文件

文件：`apps/mosquitto_signal/signal_unix.c`

每次成功执行：

```c
fptr = fopen(pathbuf, "r");
```

都会在同一轮循环的 71 行执行：

```c
fclose(fptr);
```

外层 `opendir("/proc")` 也在 77 行通过 `closedir(dir)` 关闭。不存在文件或
目录句柄泄漏。

结论：**误报，可能源于循环和条件分支合流。**

### 4.3 `client_config_load()` 的动态配置路径

文件：`client/client_shared.c`

`loc` 根据 `XDG_CONFIG_HOME`、`HOME` 或 Windows user profile 动态构造。
如果使用该路径打开配置文件，408—410 行会在 `fopen()` 后立即释放：

```c
fptr = fopen(loc, "rt");
free(loc);
loc = NULL;
```

配置解析失败路径中的 `free(loc)` 是防御性清理；正常路径不存在泄漏。

结论：**误报。**

### 4.4 `pub_stdin_line_loop()` 的 `realloc`

文件：`client/pub_client.c`

代码使用临时变量：

```c
buf2 = realloc(line_buf, line_buf_len);
if(!buf2){
    return MOSQ_ERR_NOMEM;
}
line_buf = buf2;
```

如果 `realloc()` 失败，原 `line_buf` 仍然有效，没有被覆盖。`line_buf` 是
外部长期 buffer，由上层生命周期统一清理，不要求在每次循环内释放。

结论：**误报。**

### 4.5 `mosquitto_fgets()` 的 `realloc`

文件：`libcommon/file_common.c`

同样使用安全的临时变量：

```c
newbuf = realloc(*buf, *buflen);
if(!newbuf){
    return NULL;
}
*buf = newbuf;
```

函数通过 `char **buf` 更新调用方拥有的 buffer。成功时是返回/出参所有权，
失败时旧 buffer 仍由调用方持有。

结论：**误报。**

### 4.6 dynamic-security kicklist

文件：`plugins/dynamic-security/kicklist.c`

`dynsec_kicklist__add()` 分配的 `kick` 通过 `DL_APPEND()` 转移给
`data->kicklist`。随后：

- `dynsec_kicklist__kick()` 删除并释放全部节点
- `dynsec_kicklist__cleanup()` 也会遍历释放残留节点

结论：**链表所有权转移导致的误报。**

### 4.7 daemon 标准流重定向

文件：`src/mosquitto.c`

`mosquitto__daemonise()` 使用：

```c
freopen("/dev/null", "r", stdin);
freopen("/dev/null", "w", stdout);
freopen("/dev/null", "w", stderr);
```

这些 stream 是进程级标准流，重定向后需要在 daemon 的剩余生命周期持续
有效，不应在函数退出时 `fclose()`。进程退出时由 C runtime/操作系统清理。

结论：**误报。**

## 5. tests 中候选的判断

包含 tests 后新增 31 条，主要来自：

- `test/apps/ctrl/ctrl_shell_*_test.cpp`
- mock `readline()` 返回字符串
- mock 异步 response 的 `pending_payload`

### `pending_payload`

测试 helper 通过 `calloc()` 创建 payload，随后被 lambda 捕获，并通过
`DL_APPEND()` 加入 fixture 的 `pending_payloads`。模拟消息到达时，节点从
链表移除并执行 `free(pp)`。

这是跨 lambda 和测试队列的所有权转移，不是函数级泄漏。

### mock `readline()` 字符串

`strdup()` 创建的字符串作为 mock `readline()` 的返回值交给被测 ctrl-shell
代码。真实 `readline()` API 本来就返回需要调用方释放的 buffer，因此测试
按真实接口模拟所有权。

这些候选不能仅根据测试函数退出时局部变量未释放判为泄漏。

结论：**目前没有从 tests 新增候选中确认独立真 bug。**

## 6. 后续优先级

1. 为 `dynsec_init()` 编写 `fdopen()` 失败 fault-injection 测试。
2. 修复并确认成功路径没有 double close。
3. 检查失败后是否应删除 `O_CREAT | O_EXCL` 创建的空文件。
4. 核实 plugin cleanup 与 disconnect callback 的顺序保证。
5. 如果准备提交上游，fd 泄漏应单独提交，不要与示例插件清理问题合并。
