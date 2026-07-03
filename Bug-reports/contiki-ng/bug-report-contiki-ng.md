# Contiki-NG lifetime bug 候选复查记录

## 1. 扫描信息

- 项目：Contiki-NG
- commit：`f5c991371f6cc693783f74b204e079407f1c5f45`
- 扫描日期：2026-06-30
- 扫描工具：`IoT-lifetime-bugs`
- 默认扫描：600 个文件、4,550 个函数、2 个候选、0 个解析警告
- 包含 tests/examples：730 个文件、5,005 个函数、3 个候选、0 个解析警告

复现命令：

```bash
cd IoT-lifetime-bugs

python cli.py lifetime ../IoT-repos/contiki-ng
python cli.py lifetime ../IoT-repos/contiki-ng --include-tests
```

Contiki-NG 核心较少使用普通堆，而是大量使用：

```c
memb_alloc()
memb_free()
queuebuf_new_from_packetbuf()
queuebuf_free()
```

默认 POSIX 规格不覆盖这些 API。临时补充固定池和 queuebuf 资源语义后，正式
源码产生 12 个候选：

- 8 个 `memory_not_freed`
- 1 个 `use_after_release`
- 1 个 `acquire_in_loop_without_release`
- 1 个 `socket_not_closed`
- 1 个 `fd_not_closed`

人工复查确认：

1. Antelope `index_create()` 存在释放后使用和两个悬空引用。
2. Antelope `index_load()` 存在固定池对象泄漏。
3. packet-injector 测试工具存在 fd 泄漏。

## 2. Contiki `memb` 所有权背景

`memb_alloc()` 从固定大小的静态池中分配对象：

```c
void *memb_alloc(struct memb *m);
```

释放时必须同时提供所属 pool：

```c
int memb_free(struct memb *m, void *ptr);
```

与普通堆泄漏相比，固定池泄漏通常更快造成可观测故障：

- pool 容量在编译时固定。
- 泄漏一个 slot 后，该 slot 永远不能再次使用。
- 重复错误请求可以耗尽整个 pool。
- 后续合法操作会稳定地返回 allocation error。

Antelope index 使用：

```c
MEMB(index_memb, index_t, DB_MAX_INDEXES);
LIST(indices);
```

一个成功创建或加载的 `index_t` 同时被以下对象引用：

```text
attribute_t::index
global indices list
index_memb pool
```

释放 index 时必须保持三个状态同步。

## 3. 高置信度问题一：`index_create()` 释放后使用及悬空引用

### 3.1 位置

文件：

`os/storage/antelope/index.c`

函数：

```c
index_create()
```

关键位置：

- 111 行：`memb_alloc(&index_memb)`
- 125 行：调用 index backend 的 `create`
- 131 行：`attr->index = index`
- 132 行：`list_push(indices, index)`
- 134—135 行：持久化 index descriptor
- 136—137 行：持久化失败时 destroy/free
- 138—139 行：释放后访问 `index->descriptor_file`

### 3.2 正常所有权路径

```text
memb_alloc(index)
  -> api->create(index)
  -> attr->index = index
  -> list_push(indices, index)
  -> storage_put_index(index)
  -> success
```

成功后，index 由 attribute 和全局 index list 持有，后续通过
`index_release()` 清理：

```c
index->attr->index = NULL;
list_remove(indices, index);
memb_free(&index_memb, index);
```

### 3.3 错误路径

当前代码：

```c
attr->index = index;
list_push(indices, index);

if(index->descriptor_file[0] != '\0' &&
   DB_ERROR(storage_put_index(index))) {
  api->destroy(index);
  memb_free(&index_memb, index);
  PRINTF("DB: Failed to store index data in file \"%s\"\n",
         index->descriptor_file);
  return DB_INDEX_ERROR;
}
```

当 `storage_put_index()` 失败时出现三个独立问题。

### 3.4 UAF：释放后打印 `descriptor_file`

执行：

```c
memb_free(&index_memb, index);
```

后紧接着读取：

```c
index->descriptor_file
```

虽然 `memb_free()` 不一定立即覆盖静态 pool 内容，但从对象生命周期上，
该 slot 已经释放，可能被下一次 `memb_alloc()` 重新分配和修改。

这属于明确的 use-after-release。

### 3.5 `attr->index` 悬空

失败前已经执行：

```c
attr->index = index;
```

错误路径没有恢复：

```c
attr->index = NULL;
```

因此 attribute 继续指向已经返回 pool 的 slot。后续：

- `index_create()` 会错误认为 attribute 已经建立 index。
- query 可能通过 `attr->index` 调用已释放对象中的 `api`。
- index destroy/release 可能再次释放同一 pool slot。
- slot 被复用后，旧 attribute 可能访问另一个 index 对象。

### 3.6 全局 `indices` 链表悬空

失败前已经执行：

```c
list_push(indices, index);
```

错误路径没有执行：

```c
list_remove(indices, index);
```

全局遍历仍会访问已释放 slot。结合 `attr->index`，同一对象留下了两个独立
悬空引用。

### 3.7 影响

直接影响包括：

- UAF
- stale/dangling index handle
- double release
- index pool slot 被错误复用
- 全局链表结构或数据库查询结果损坏

触发条件是 index backend 创建成功，但 descriptor 持久化失败。可能原因：

- 文件系统写入错误
- 存储空间不足
- descriptor 文件损坏
- 平台存储后端返回错误

### 3.8 建议修复

最佳方案是延迟发布对象：

```text
allocate
  -> backend create
  -> persist descriptor
  -> attr->index = index
  -> list_push(indices, index)
```

只有全部可能失败的初始化完成后，才把 index 发布给 attribute 和全局链表。

示意：

```c
if(index->descriptor_file[0] != '\0' &&
   DB_ERROR(storage_put_index(index))) {
  char descriptor_file[DB_MAX_FILENAME_LENGTH];

  strncpy(descriptor_file, index->descriptor_file,
          sizeof(descriptor_file));
  descriptor_file[sizeof(descriptor_file) - 1] = '\0';

  api->destroy(index);
  memb_free(&index_memb, index);

  PRINTF("DB: Failed to store index data in file \"%s\"\n",
         descriptor_file);
  return DB_INDEX_ERROR;
}

attr->index = index;
list_push(indices, index);
```

如果保持现有发布顺序，则失败路径必须先完整回滚：

```c
char descriptor_file[...];

copy descriptor_file before free;
attr->index = NULL;
list_remove(indices, index);
api->destroy(index);
memb_free(&index_memb, index);
log using the copied string;
```

### 3.9 建议验证

使用存储后端 fault injection：

1. 让 `memb_alloc()` 成功。
2. 让 `api->create()` 成功。
3. 让 `storage_put_index()` 返回错误。
4. 验证 `attr->index == NULL`。
5. 验证全局 `indices` 不包含该地址。
6. 验证 `memb_numfree(&index_memb)` 恢复到调用前数量。
7. 再次创建 index，确认 slot 可安全复用。
8. 使用 ASan 或 pool poisoning 检查日志路径不读取已释放对象。

### 3.10 结论

**高置信度真实 lifetime bug，建议优先报告。**

它同时包含 UAF、悬空 attribute 引用和悬空全局链表节点，不只是单一日志
语句问题。

## 4. 高置信度问题二：`index_load()` 失败路径泄漏固定池对象

### 4.1 位置

文件：

`os/storage/antelope/index.c`

函数：

```c
index_load()
```

关键位置：

- 176 行：从 `index_memb` 分配 index
- 182—185 行：descriptor 读取失败时正确释放
- 192—195 行：找不到 backend API 时直接返回
- 200—202 行：backend load 失败时直接返回
- 205—207 行：成功后发布到 list/attribute

### 4.2 正确处理的失败路径

```c
if(DB_ERROR(storage_get_index(index, rel, attr))) {
  memb_free(&index_memb, index);
  return DB_INDEX_ERROR;
}
```

descriptor 读取失败时能够释放 pool slot。

### 4.3 泄漏路径一：未知 index type

```c
api = find_index_api(index->type);
if(api == NULL) {
  PRINTF("DB: No API for index type %d\n", index->type);
  return DB_INDEX_ERROR;
}
```

`index` 已分配，但返回前没有：

```c
memb_free(&index_memb, index);
```

攻击者或损坏的 descriptor 可以提供不支持的 index type。每次加载失败都会
永久占用一个 `index_memb` slot。

### 4.4 泄漏路径二：backend load 失败

```c
if(DB_ERROR(api->load(index))) {
  PRINTF("DB: Index-specific load failed\n");
  return DB_INDEX_ERROR;
}
```

该路径同样没有释放 index。

此外，`api->load(index)` 可能在失败前已经创建 `opaque_data` 或其他 backend
状态。修复时不能只考虑顶层 `memb_free()`，还要确认 backend 的失败契约：

- `load()` 失败时是否保证自行回滚？
- 是否应调用 `api->destroy(index)`？
- 是否存在专门的 partial-load cleanup？

### 4.5 影响

`index_memb` 数量受 `DB_MAX_INDEXES` 限制。重复加载以下 descriptor：

- 未知 index type
- backend 无法加载的数据
- 损坏或不兼容的 index 文件

会耗尽 index pool，导致后续合法 index 创建和恢复持续失败。

在小型 IoT 系统中，即使只泄漏少量 slot，也可能完全禁用 index 功能。

### 4.6 建议修复

未知 API 路径：

```c
if(api == NULL) {
  PRINTF(...);
  memb_free(&index_memb, index);
  return DB_INDEX_ERROR;
}
```

backend load 失败路径：

```c
if(DB_ERROR(api->load(index))) {
  PRINTF(...);
  api->destroy(index); /* 仅当 API 契约允许清理 partial state */
  memb_free(&index_memb, index);
  return DB_INDEX_ERROR;
}
```

更稳妥的方式是增加统一 cleanup 标签：

```c
db_result_t result = DB_INDEX_ERROR;
bool backend_initialized = false;

...

exit:
  if(result != DB_OK) {
    if(backend_initialized)
      api->destroy(index);
    memb_free(&index_memb, index);
  }
  return result;
```

### 4.7 建议验证

测试一：未知 type

1. 构造 descriptor，使 `storage_get_index()` 成功。
2. 设置一个 `find_index_api()` 不支持的 type。
3. 重复调用 `index_load()` 超过 `DB_MAX_INDEXES` 次。
4. 修复后每次都应返回错误，但 pool free count 不变。

测试二：backend load 失败

1. 使用有效 type。
2. mock `api->load()` 返回错误。
3. 检查顶层 pool slot 和 backend partial allocations。
4. 再次成功加载，确认 pool 未耗尽。

### 4.8 结论

**高置信度固定池泄漏。**

## 5. 低优先级问题：packet-injector fd 泄漏

文件：

`tests/20-packet-parsing/packet-injector/packet-injector.c`

函数：

```c
read_packet()
```

关键位置：

- 87 行：`fd = open(filename, O_RDONLY)`
- 93 行：`read(fd, ...)`
- 96 行：read 失败后直接返回
- 99 行：成功后直接返回

当前代码：

```c
fd = open(filename, O_RDONLY);
if(fd < 0) {
  return -1;
}

len = read(fd, buf, max_len);
if(len < 0) {
  return -1;
}

return len;
```

成功和 read-error 两条路径都没有 `close(fd)`。

建议：

```c
len = read(fd, buf, max_len);
close(fd);

if(len < 0) {
  ...
}
return len;
```

如果需要保留 `errno`：

```c
int saved_errno = errno;
close(fd);
errno = saved_errno;
```

该问题只存在于 packet parsing 测试工具，通常运行时间短，优先级低。

## 6. 已排除的主要候选

### 6.1 `connect_to_server()` socket

文件：

`os/services/rpl-border-router/native/slip-dev.c`

每个 connect 失败的 socket 都立即执行：

```c
close(fd);
```

成功连接的 fd 作为返回值交给调用方，并保存到全局 `slipfd`，用于后续
select/read/write。它是长生命周期 socket，不应在 wrapper 返回前关闭。

如果所有地址连接失败，代码调用 `err(EXIT_FAILURE, ...)` 终止进程。其后的
`return -1` 实际不可达。

结论：**返回所有权误报。**

### 6.2 `serialdump` 串口 fd

文件：

`tools/serial-io/serialdump.c`

串口打开后进入无限 select/read/write 循环。fd 在工具整个进程生命周期内
持续使用，错误路径通过 `exit()` 结束进程。

结论：**进程生命周期资源，不是循环内泄漏。**

### 6.3 CSMA neighbor/packet/queuebuf

文件：

`os/net/mac/csma/csma-output.c`

成功路径：

```text
memb_alloc(neighbor)
  -> list_add(neighbor_list)

memb_alloc(packet)
  -> memb_alloc(metadata)
  -> queuebuf_new_from_packetbuf()
  -> list_add(neighbor.packet_queue)
```

后续发送完成或丢弃路径通过 `queuebuf_free()` 和 `memb_free()` 清理。

所有中途分配失败路径也逐层回滚。创建函数返回时对象仍 active 是队列所有权，
不是泄漏。

### 6.4 6P transaction

文件：

`os/net/mac/tsch/sixtop/sixp.c`

`sixp_trans_alloc()` 创建协议事务对象。成功后对象进入 6P transaction
state machine，通过：

- response/confirmation
- timeout
- `sixp_trans_abort()`

完成释放。函数退出时 transaction 仍存在是协议设计要求。

### 6.5 neighbor-table key

文件：

`os/net/nbr-table.c`

`nbr_table_allocate()` 返回的 key 被加入：

```c
list_add(nbr_table_keys, key);
```

随后由全局 neighbor table 管理。成功路径不是局部泄漏。

### 6.6 relation attributes

文件：

- `os/storage/antelope/aql-exec.c`
- `os/storage/antelope/relation.c`

`relation_attribute_add()` 分配的 attribute 成功后由 relation 持有，并在
relation 删除/释放时统一归还 pool。不能要求调用函数立即释放。

`relation_select()` 循环创建 result relation 的属性，也属于 result relation
所有权，而不是每轮循环必须释放。

## 7. 工具改进建议

### 7.1 支持 allocator 的额外 pool 参数

`memb_free()` 的资源指针位于第 2 个参数：

```c
memb_free(&pool, ptr);
```

工具需要同时保留：

- 资源指针 `ptr`
- provenance：资源来自哪个 pool

否则可能把使用错误 pool 的释放误认为合法释放。

### 7.2 支持容器悬空引用检查

`index_create()` 的核心问题不是单纯 UAF，而是：

```text
publish pointer to field/list
  -> free pointer
  -> field/list still references pointer
```

可增加通用候选：

```text
released_while_still_escaped
dangling_container_entry
```

### 7.3 支持事务式发布检查

对象在所有可能失败的初始化完成前写入全局容器，是常见危险模式：

```c
global_or_field = object;
list_add(object);
if(late_initialization_fails)
    free(object);
```

分析器可检查失败路径是否撤销全部已建立的 provenance/escape 边。

### 7.4 区分状态机所有权和泄漏

CSMA、6P、neighbor table 等 Contiki 模块大量使用固定池对象和 intrusive
list。粗筛应识别：

- `list_add`：成功后对象逃逸
- `list_remove + memb_free`：生命周期结束
- timer/state-machine callback：跨函数释放

## 8. 推荐后续顺序

1. 为 `index_create()` 的 `storage_put_index()` 失败路径编写 fault-injection
   测试。
2. 验证并修复 `attr->index` 与 `indices` 链表回滚。
3. 为 `index_load()` 的 unknown-type 和 backend-load-failure 添加 pool-count
   测试。
4. 确认各 index backend 在 partial load 失败时的清理契约。
5. 修复 packet-injector 的 `close(fd)`。
6. 再扩展工具对 `memb` provenance 和 intrusive-list 生命周期的建模。

建议上游 issue 拆分：

- Antelope `index_create()` UAF/dangling references
- Antelope `index_load()` pool leak
- packet-injector fd cleanup

前两个也可以合并为一个 Antelope index failure-cleanup 补丁，但报告中应分别
描述，因为触发条件和后果不同。
