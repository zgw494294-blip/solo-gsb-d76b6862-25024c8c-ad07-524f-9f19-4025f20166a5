# 离线双端消息协议分析 API

基于 Python + FastAPI，对 A、B 两端的 send/receive 状态机做离线可达性分析，包括死锁检测、完成保证、完成计划与最小停用方案。四个分析接口完全无状态；演练会话通过容器内本地 SQLite 文件持久化，不依赖任何外部服务。

## 协议模型

- A、B 各有 1～8 个状态，一个初态和一个非空终态集。
- 每个转移包含：
  - `id`：全局唯一转移 ID；
  - `from`、`to`：端点，只能是 `A` 或 `B`，且不能相同；
  - `action`：`send` 或 `receive`；
  - `message`：消息类型；
  - `source`、`target`：执行该转移的端点状态。
- `send` 转移由 `from` 端点执行，向 `from -> to` 的一格队列写入消息。
- `receive` 转移由 `to` 端点执行，要求反方向队首消息等于 `message`，消费后队列变空。
- 每个方向只有一格队列：
  - 发送方向队列非空时不能发送；
  - 接收消息与队首不匹配时不能接收。
- 所有可执行转移可任意交错。
- 两端均处于终态且两个队列都为空时为“完成”，该配置不再扩展。
- 除完成配置外，其他配置没有任何可执行转移时即为死锁。
- 全局转移数不超过 30，不同消息种类不超过 8。

分析器使用 BFS 遍历全部可达配置。若存在死锁，第一条最短路径会被选中；同一长度下按转移 ID 序列的字典序选择。

## 单条命令启动

```bash
docker compose up --build -d
```

该命令会构建镜像并在后台启动服务。服务监听 **8000 端口**。

查看日志：

```bash
docker compose logs -f
```

- 健康检查：`GET http://localhost:8000/healthz`
- Swagger 文档：`http://localhost:8000/docs`
- OpenAPI：`http://localhost:8000/openapi.json`

如使用旧版 Docker Compose，可运行：

```bash
docker-compose up --build
```

## 调用示例

### 存在死锁

```bash
curl -X POST http://localhost:8000/v1/deadlocks \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "B": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "transitions": [
      {
        "id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "source": 0,
        "target": 1
      },
      {
        "id": "t2",
        "from": "B",
        "to": "A",
        "action": "send",
        "message": "x",
        "source": 0,
        "target": 1
      },
      {
        "id": "t3",
        "from": "B",
        "to": "A",
        "action": "receive",
        "message": "x",
        "source": 0,
        "target": 1
      },
      {
        "id": "t4",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "m",
        "source": 0,
        "target": 1
      }
    ]
  }'
```

示例响应：

```json
{
  "deadlock": true,
  "visited_states": 5,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "deadlock_state": {
    "state": { "A": 1, "B": 1 },
    "queues": { "A_to_B": "m", "B_to_A": "x" }
  },
  "path": [
    {
      "index": 1,
      "transition_id": "t1",
      "from": "A",
      "to": "B",
      "action": "send",
      "message": "m",
      "state": { "A": 1, "B": 0 },
      "queues": { "A_to_B": "m", "B_to_A": null }
    },
    {
      "index": 2,
      "transition_id": "t2",
      "from": "B",
      "to": "A",
      "action": "send",
      "message": "x",
      "state": { "A": 1, "B": 1 },
      "queues": { "A_to_B": "m", "B_to_A": "x" }
    }
  ]
}
```

`visited_states` 表示访问过的不同配置数；配置包含两端状态和两个队列内容。

### 无死锁时的响应

```json
{
  "deadlock": false,
  "visited_states": 1,
  "initial": {
    "state": { "A": "done", "B": "done" },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "deadlock_state": null,
  "path": null
}
```

## 完成保证：`POST /v1/completion-guarantees`

接收与死锁分析**完全相同的协议请求**（不增加任何字段），对从初态出发、按一格队列规则产生的所有可执行转移交错做判定：

- 只有**每次运行都结束于完成配置**（两端均处终态且两个队列都为空，完成配置不再扩展）才算保证完成；
- 运行可能停在完成配置之外、且没有任何可执行转移的配置时，报告 **deadlock**；
- 否则存在可达配置位于有向环上、从而可以无限重复时，报告 **cycle**；即使该循环存在通往完成的出口，也必须报告；
- 死锁与循环并存时，优先报告死锁。

响应字段：

- `result`：`"guaranteed"`、`"deadlock"`、`"cycle"` 之一；
- `visited_states`：访问过的不同配置数（含完成配置）；
- `initial`：初态视图；
- `witness`：保证完成时为 `null`；否则为见证对象：
  - 死锁见证：`kind` 为 `"deadlock"`，`deadlock_state` 为死锁配置，`path` 是从初态到该配置的最短路径（同长度按转移 ID 序列字典序）；
  - 循环见证：`kind` 为 `"cycle"`，`entry_state` 为循环入口配置，`prefix` 是从初态到入口的路径（入口即初态时为 `[]`），`cycle` 是从入口出发并回到入口的非空循环。

`prefix` 与 `cycle` 中的每一步都逐步列出 `transition_id`、`from`、`to`、`action`、`message` 以及执行后的两端状态与队列；两部分各自从 `index: 1` 重新编号。

### 可执行调用示例：存在循环（且循环有通往完成的出口）

```bash
curl -X POST http://localhost:8000/v1/completion-guarantees \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "B": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "transitions": [
      {
        "id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "source": 0,
        "target": 0
      },
      {
        "id": "t2",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "m",
        "source": 0,
        "target": 0
      },
      {
        "id": "t3",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "f",
        "source": 0,
        "target": 1
      },
      {
        "id": "t4",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "f",
        "source": 0,
        "target": 1
      }
    ]
  }'
```

该协议在初态可反复执行 `t1, t2` 回到自身（无限运行），也可执行 `t3, t4` 到达完成配置；由于存在无限运行，结果为 `cycle`：

```json
{
  "result": "cycle",
  "visited_states": 4,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "witness": {
    "kind": "cycle",
    "entry_state": {
      "state": { "A": 0, "B": 0 },
      "queues": { "A_to_B": null, "B_to_A": null }
    },
    "prefix": [],
    "cycle": [
      {
        "index": 1,
        "transition_id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "state": { "A": 0, "B": 0 },
        "queues": { "A_to_B": "m", "B_to_A": null }
      },
      {
        "index": 2,
        "transition_id": "t2",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "m",
        "state": { "A": 0, "B": 0 },
        "queues": { "A_to_B": null, "B_to_A": null }
      }
    ]
  }
}
```

### 保证完成时的响应

例如只有一次发送并被消费的协议（A 经 `send` 到终态，B 经 `receive` 到终态）：

```json
{
  "result": "guaranteed",
  "visited_states": 3,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "witness": null
}
```

### 死锁时的响应

存在死锁时（即使同时存在循环），`result` 为 `"deadlock"`，`witness.kind` 为 `"deadlock"`，并返回通往该配置的最短路径，例如本文开头死锁协议的响应：

```json
{
  "result": "deadlock",
  "visited_states": 5,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "witness": {
    "kind": "deadlock",
    "deadlock_state": {
      "state": { "A": 1, "B": 1 },
      "queues": { "A_to_B": "m", "B_to_A": "x" }
    },
    "path": [
      {
        "index": 1,
        "transition_id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "state": { "A": 1, "B": 0 },
        "queues": { "A_to_B": "m", "B_to_A": null }
      },
      {
        "index": 2,
        "transition_id": "t2",
        "from": "B",
        "to": "A",
        "action": "send",
        "message": "x",
        "state": { "A": 1, "B": 1 },
        "queues": { "A_to_B": "m", "B_to_A": "x" }
      }
    ]
  }
}
```

## 带里程碑的完成计划：`POST /v1/completion-plans`

在原有协议请求的基础上增加可选字段 `required_transition_ids`（0～6 个**互异**的现有转移 ID，缺省为 `[]`）。服务在与死锁分析完全相同的可达空间中寻找一条路径，使得：

- 每个指定转移在路径中**至少执行一次**；
- 路径终点两端均处于终态且两个队列都为空（完成配置不再扩展）。

路径优先选择转移数最少的；长度相同时按转移 ID 序列的字典序取第一条。响应中的 `visited_combinations` 是访问过的不同「配置 × 已命中指定 ID 集合」组合数（同一配置经由不同已命中集合到达会分别计数）。

### 可执行调用示例

```bash
curl -X POST http://localhost:8000/v1/completion-plans \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "B": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "transitions": [
      {
        "id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "source": 0,
        "target": 1
      },
      {
        "id": "t2",
        "from": "B",
        "to": "A",
        "action": "send",
        "message": "x",
        "source": 0,
        "target": 1
      },
      {
        "id": "t3",
        "from": "B",
        "to": "A",
        "action": "receive",
        "message": "x",
        "source": 0,
        "target": 1
      },
      {
        "id": "t4",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "m",
        "source": 0,
        "target": 1
      }
    ],
    "required_transition_ids": ["t1"]
  }'
```

存在满足条件的路径时（示例中最短路径为 `t1, t4`）：

```json
{
  "completable": true,
  "visited_combinations": 6,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "path": [
    {
      "index": 1,
      "transition_id": "t1",
      "from": "A",
      "to": "B",
      "action": "send",
      "message": "m",
      "state": { "A": 1, "B": 0 },
      "queues": { "A_to_B": "m", "B_to_A": null }
    },
    {
      "index": 2,
      "transition_id": "t4",
      "from": "A",
      "to": "B",
      "action": "receive",
      "message": "m",
      "state": { "A": 1, "B": 1 },
      "queues": { "A_to_B": null, "B_to_A": null }
    }
  ]
}
```

无满足条件的路径时，`path` 为 `null`：

```json
{
  "completable": false,
  "visited_combinations": 4,
  "initial": {
    "state": { "A": 0, "B": 0 },
    "queues": { "A_to_B": null, "B_to_A": null }
  },
  "path": null
}
```

初态本身已满足条件时（例如 `required_transition_ids` 为空且两端已在终态、队列均空），`completable` 为 `true` 且 `path` 为 `[]`，`visited_combinations` 为 `1`。

## 最小停用方案：`POST /v1/deactivation-plans`

在原有协议请求的基础上增加可选字段 `removable_transition_ids`（0～8 个**互异**的现有转移 ID，缺省为 `[]`）。服务尝试停用其中若干转移，使修订后的协议满足：**从初态可达的每个配置都存在一条通往完成配置的路径**（即使不存在死锁，只要存在无法到达完成的可达配置，该方案也不合格）。只允许停用列表内的转移。

对每个候选方案，服务按原有一格队列语义重建可达图（完成配置不再扩展），并检查全部可达配置是否都能到达完成。方案选择规则：

1. 停用转移数最少者优先；
2. 数量相同时，按升序 ID 列表的字典序取第一个。

响应字段：

- `solvable`：是否存在合格的停用方案；
- `deactivated_transition_ids`：停用的转移 ID 升序列表；原协议本身已合格时为 `[]`，无方案时为 `null`；
- `reachable_configurations`：修订后从初态可达的配置数（含完成配置），无方案时为 `null`。

### 可执行调用示例

```bash
curl -X POST http://localhost:8000/v1/deactivation-plans \
  -H 'Content-Type: application/json' \
  -d '{
    "A": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "B": {
      "states": [0, 1],
      "initial": 0,
      "finals": [1]
    },
    "transitions": [
      {
        "id": "t1",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "m",
        "source": 0,
        "target": 1
      },
      {
        "id": "t2",
        "from": "A",
        "to": "B",
        "action": "receive",
        "message": "m",
        "source": 0,
        "target": 1
      },
      {
        "id": "t3",
        "from": "A",
        "to": "B",
        "action": "send",
        "message": "z",
        "source": 0,
        "target": 0
      }
    ],
    "removable_transition_ids": ["t3"]
  }'
```

示例中 `t3` 发出的 `z` 消息无人接收，会让协议进入死锁配置；停用 `t3` 后所有可达配置都能完成：

```json
{
  "solvable": true,
  "deactivated_transition_ids": ["t3"],
  "reachable_configurations": 3
}
```

同一请求去掉 `removable_transition_ids`（缺省为 `[]`，即不允许停用任何转移）时无合格方案：

```json
{
  "solvable": false,
  "deactivated_transition_ids": null,
  "reachable_configurations": null
}
```

## 请求校验与 422

以下情况返回 HTTP `422 Unprocessable Entity`：

- 状态集数量不在 1～8；
- 终态集为空、数量超过 8 或包含重复值；
- 初态或终态非法引用；
- 转移的 `source` 或 `target` 不属于执行端点的状态集；
- 转移 ID 重复；
- 转移数量超过 30；
- 不同消息种类超过 8；
- `from`、`to`、`action` 取值非法，或两端相同；
- 请求中出现未定义字段、空 ID/消息等越界输入。

`POST /v1/completion-guarantees` 直接复用上述通用校验（请求体与死锁分析完全一致，不接受额外字段）。

`POST /v1/completion-plans` 在上述规则之外，以下情况同样返回 `422 Unprocessable Entity`：

- `required_transition_ids` 中出现请求未定义的转移 ID；
- `required_transition_ids` 中包含重复 ID；
- `required_transition_ids` 数量超过 6（或包含空 ID）。

`POST /v1/deactivation-plans` 在通用规则之外，以下情况同样返回 `422 Unprocessable Entity`：

- `removable_transition_ids` 中出现请求未定义的转移 ID；
- `removable_transition_ids` 中包含重复 ID；
- `removable_transition_ids` 数量超过 8（或包含空 ID）。

## 双端协议演练会话

除上述四个离线分析接口外，服务还提供**有状态演练会话**：创建会话后可以逐个执行转移、随时查询当前配置与完整执行轨迹，也可以修改协议后从新初态重放已执行步骤，继续演练；还可以从指定轨迹位置另开**分支会话**，两边独立续演；另一会话中已走通的后续轨迹也可以直接**接回**目标会话，免去逐步重演。两次演练还可以按方向**对比消息交换**：只比较各自 `(action, message)` 序列，忽略转移 ID、端点状态与两方向之间的交错。会话还可以被**锁定为只读基准**：锁定后执行、替换协议与作为接回目标均被拒绝，查询、对比、分支与作为接回来源不受影响，解锁后按新版本继续演练。

- 会话（协议、轨迹 ID、版本、锁定标记）持久化在**本地 SQLite 数据库**中，所有进程共享同一文件：服务或容器重建后仍可按原会话 ID 查询并继续执行、替换协议，版本不会回退。
- 服务默认以 **2 个 Uvicorn worker** 进程运行，启动时自动建库建表（无需迁移旧数据）；旧版数据库启动时自动补充锁定列，已有协议、轨迹与版本保持不变，旧会话默认未锁定。
- 每次成功执行转移、替换协议、接回轨迹或设置锁定，会话 `version` 加 1（创建时为 `0`）。
- 修改类请求必须携带 `expected_version`：与当前版本不一致返回 `409`；两个进程以同一 `expected_version` 并发修改同一会话时只有一项成功，其余返回 `409` 及当前版本。
- 版本检查、锁定检查、轨迹重放与写入在同一个 SQLite 事务（`BEGIN IMMEDIATE`）内完成，针对同一份已提交状态；任何失败（含重放失败）都不改动已存数据。成功响应返回本次提交的完整快照（不混入随后提交的数据），查询永远只读到完整的已提交版本。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/sessions` | 用现有协议请求体（与 `/v1/deadlocks` 完全一致）创建会话，返回会话 ID 与版本 0 |
| `GET` | `/v1/sessions/{id}` | 查询会话快照 |
| `POST` | `/v1/sessions/{id}/transitions` | 执行单个转移，请求体 `{"transition_id": ..., "expected_version": ...}` |
| `PUT` | `/v1/sessions/{id}/protocol` | 替换完整协议，请求体 `{"protocol": {...}, "expected_version": ...}` |
| `PUT` | `/v1/sessions/{id}/lock` | 设置锁定标记，请求体 `{"expected_version": ..., "locked": ...}` |
| `POST` | `/v1/sessions/{id}/branches` | 从指定轨迹位置另开分支会话，请求体 `{"expected_version": ..., "prefix_length": ...}`，返回新会话视图（版本 0） |
| `POST` | `/v1/sessions/{id}/adoptions` | 把来源会话的后续轨迹接回本目标会话，请求体 `{"source_session_id": ..., "target_expected_version": ..., "source_expected_version": ...}` |
| `POST` | `/v1/session-comparisons` | 按消息方向对比两次演练（只读、不改数据与版本），请求体 `{"left_session_id": ..., "right_session_id": ..., "left_expected_version": ..., "right_expected_version": ...}` |

响应均为同一会话视图：

- `id`：会话 ID；
- `version`：当前版本（创建为 0，每次成功修改加 1）；
- `locked`：是否已锁定为只读基准（新建会话与旧库会话默认为 `false`）；
- `protocol`：当前完整协议（与创建请求一致）；
- `configuration`：当前两端状态与两个队列；
- `trace`：完整执行轨迹，每步含 `index`（从 1 计）、`transition_id`、`from`、`to`、`action`、`message` 及执行后的状态与队列；
- `enabled_transition_ids`：当前配置下可执行转移的 ID，按 ID 升序；完成配置下为 `[]`。

状态码：

- 成功：创建与分支 `201`，执行/替换/锁定/接回/查询 `200`；
- `404`：未知会话（接回时目标或来源任一未知）；
- `409`：`expected_version` 过期、转移在当前配置下不可执行（含 ID 不存在）、会话已处于完成配置、或会话已锁定（执行/替换/作为接回目标，错误体 `{"error": "session_locked", ...}`）；接回时还包括目标或来源版本过期——响应体在通用 `current_version` 之外以 `session`（`"target"`/`"source"`）与 `session_id` 指出是哪个会话过期；
- `422`：请求体不合法（复用原协议校验，规则同下节）；锁定请求的 `locked` 不是布尔值；替换协议时还包括**重放失败**——从新协议初态按已执行 ID 重放时，任一步不可执行或在轨迹结束前到达完成配置，响应体 `detail.first_failed_step` 给出从 1 计的首个失败步；分支时还包括 `prefix_length` 非法（布尔值、非整数、负数或超过当前轨迹长度）；接回时还包括：目标与来源是同一会话（`same_session`）、两边协议字段值或列表顺序不同（`protocol_mismatch`）、目标轨迹 ID 序列不是来源轨迹的严格前缀（`trace_not_strict_prefix`）。

对比接口为只读操作，状态码：成功 `200`；任一会话 ID 未知返回 `404`；左或右的期望版本过期返回 `409`，响应体在通用 `current_version` 之外以 `side`（`"left"`/`"right"`）与 `session_id` 指出过期侧（左侧优先判定）；请求体不合法返回 `422`。对比不修改任何会话、不消耗版本。

替换协议允许改变已有 ID 的转移定义（端点状态、消息、source/target 等），但重放时每一步都必须在新协议下可执行，且不能在轨迹结束前到达完成配置；重放成功后轨迹与当前配置全部按新协议重算。完成配置只禁止继续执行转移，不禁止替换协议。

### 可执行调用示例（端口 8000）

创建会话：

```bash
SID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "A": { "states": [0, 1], "initial": 0, "finals": [1] },
    "B": { "states": [0, 1], "initial": 0, "finals": [1] },
    "transitions": [
      { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
      { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
    ]
  }' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
echo "$SID"
```

返回 `201`，`version` 为 `0`、`trace` 为 `[]`、`enabled_transition_ids` 为 `["t1"]`。

执行 `t1`（携带当前版本 0）：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}'
```

返回 `200`，`version` 变为 `1`，轨迹含一步，当前队列 `A_to_B` 为 `"m"`，可执行转移变为 `["t4"]`。若重复用 `"expected_version": 0`，返回 `409`。

执行 `t4` 后协议完成（两端终态、队列均空），可执行转移为 `[]`；之后再执行任何转移都返回 `409`：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 1}'
```

查询会话（始终返回同一版本的完整快照，包括协议、配置、轨迹与可执行 ID）：

```bash
curl -s http://localhost:8000/v1/sessions/$SID
```

替换协议：把 `t1` 改为在 A 端自环（0→0），已执行轨迹 `t1, t4` 从新初态重放，当前配置被重算（此时 A=0、B=1，尚未完成，可用新协议继续演练）：

```bash
curl -s -X PUT http://localhost:8000/v1/sessions/$SID/protocol \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 2,
    "protocol": {
      "A": { "states": [0, 1], "initial": 0, "finals": [1] },
      "B": { "states": [0, 1], "initial": 0, "finals": [1] },
      "transitions": [
        { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 0 },
        { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
      ]
    }
  }'
```

成功返回 `200`，`version` 变为 `3`，轨迹仍是两步（按新定义重算状态）。若新协议使第 2 步不可执行（例如把 `t4` 的 `message` 改为 `"ZZ"`），返回 `422`：

```json
{ "detail": { "error": "replay_failed", "first_failed_step": 2, "reason": "transition 't4' is not enabled" } }
```

此时会话保持旧协议与版本 `3` 不变。用过期版本（如 `"expected_version": 2`）替换返回 `409`；查询不存在的会话 ID 返回 `404`。

### 锁定会话为只读基准：`PUT /v1/sessions/{id}/lock`

把选定演练会话锁为只读基准，需要时再解除锁定：

- 请求体 `{"expected_version": ..., "locked": ...}`；`locked` 必须是布尔值，`"true"`、`1`、`0` 等非布尔输入一律返回 `422`。
- 每次成功设置**只改变锁定标记**、`version` 加 `1`，协议、轨迹与当前配置保持不变；响应是含 `locked` 字段的完整会话快照。重复设置同一取值同样只推进版本。
- **锁定期间**：执行转移、替换协议、作为接回目标均返回 `409`（`{"detail": {"error": "session_locked", ...}}`）；查询、对比、作为接回来源与创建分支不受影响，分支出的新会话默认未锁定。
- 锁定检查与写入针对同一份已提交状态（同一个 `BEGIN IMMEDIATE` 事务）：同一 `expected_version` 的锁定设置与执行、替换协议或目标接回并发时**最多一项成功**，其余返回 `409` 及 `current_version`；未知会话返回 `404`；任何失败都不改动数据。
- 解锁（`"locked": false`）同样是版本化修改，之后可用**新版本**继续执行转移或替换协议。
- 锁定标记持久保存在会话库中；旧版数据库在启动时自动补充该列，已有会话的协议、轨迹与版本不变，默认未锁定。

#### 可执行示例：锁定拒写、解锁续演（端口 8000）

新建会话并执行 `t1`（`version` 变为 `1`）：

```bash
SID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "A": { "states": [0, 1], "initial": 0, "finals": [1] },
    "B": { "states": [0, 1], "initial": 0, "finals": [1] },
    "transitions": [
      { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
      { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
    ]
  }' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}'
```

锁定为只读基准（返回 `200`，`version` 变为 `2`，`locked` 为 `true`，轨迹与配置不变）：

```bash
curl -s -X PUT http://localhost:8000/v1/sessions/$SID/lock \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 1, "locked": true}'
```

锁定期间执行转移（或替换协议、作为接回目标）返回 `409`，会话保持原样：

```bash
curl -s -i -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 2}'
# HTTP/1.1 409 Conflict
# {"detail":{"error":"session_locked","session_id":"…"}}
```

`locked` 非布尔值返回 `422`；`expected_version` 过期返回 `409` 及 `current_version`：

```bash
curl -s -i -X PUT http://localhost:8000/v1/sessions/$SID/lock \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 2, "locked": "yes"}'
# HTTP/1.1 422 Unprocessable Entity

curl -s -i -X PUT http://localhost:8000/v1/sessions/$SID/lock \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "locked": false}'
# HTTP/1.1 409 Conflict
# {"detail":{"error":"version_conflict","current_version":2}}
```

解锁（`version` 变为 `3`），随后用新版本继续执行 `t4` 至完成：

```bash
curl -s -X PUT http://localhost:8000/v1/sessions/$SID/lock \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 2, "locked": false}'

curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 3}'
# 200：version 变为 4，locked 为 false，协议到达完成配置
```

### 分支会话：`POST /v1/sessions/{id}/branches`

在源会话的指定轨迹位置另开一条独立分支，源会话保持可用且不被修改：

- 请求体 `{"expected_version": ..., "prefix_length": ...}`；`prefix_length` 是保留的已执行步数，必须为 **0 至当前轨迹长度的整数**（布尔值、非整数、负数或超过轨迹长度均返回 `422`）。
- 新会话使用源会话该版本的**当前协议**（源协议曾被替换时采用替换后的定义，不恢复旧协议）与前 `prefix_length` 步轨迹，获得全新会话 ID，版本从 `0` 开始。
- 新会话的当前配置与可执行转移由前缀轨迹从初态**重算**，不复制源会话末态；`prefix_length` 为 `0` 时即同一协议的空轨迹新会话，为轨迹全长时相当于复制整个会话。
- 版本检查与复制针对**同一份源快照**：与源会话的修改并发时，要么复制该版本的完整快照，要么按过期返回 `409` 及 `current_version`；源会话不存在返回 `404`。
- 任何失败都不会生成分支，源会话始终不变；同一源版本可多次分支，其版本号不被消耗。
- 分支会话是普通会话：沿用执行、替换协议与查询接口，之后两边互不影响，重启后仍可续演。已锁定的源会话仍可分支，新分支默认未锁定。

#### 分支后分别续演示例（端口 8000）

新建会话并执行 `t1`（`version` 变为 `1`，轨迹 `[t1]`）：

```bash
SID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "A": { "states": [0, 1], "initial": 0, "finals": [1] },
    "B": { "states": [0, 1], "initial": 0, "finals": [1] },
    "transitions": [
      { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
      { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
    ]
  }' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}'
```

从第 1 步处分支（保留 1 步已执行轨迹）：

```bash
BID=$(curl -s -X POST http://localhost:8000/v1/sessions/$SID/branches \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 1, "prefix_length": 1}' | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
echo "$BID"
```

返回 `201`：新会话 `version` 为 `0`，轨迹为前 1 步，当前配置与可执行转移由该前缀重算（队列 `A_to_B` 为 `"m"`，可执行为 `["t4"]`）。

分支用自己的版本 `0` 继续执行 `t4` 至完成：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$BID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 0}'
```

源会话不受影响，仍用自己的版本继续（`version` 仍为 `1`，执行后变为 `2`）：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 1}'
```

同一源版本可重复分支（版本号不被消耗），例如再开一条空轨迹分支：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/branches \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 2, "prefix_length": 0}'
```

`prefix_length` 超过当前轨迹长度、为负数或布尔值时返回 `422`；`expected_version` 过期返回 `409` 及当前版本；源会话不存在返回 `404`；失败时不生成分支。

### 轨迹接回：`POST /v1/sessions/{id}/adoptions`

把另一演练会话（来源）中已经走通的**后续轨迹**直接接回本会话（目标），免去把共享步骤逐步重演一遍：

- 请求体 `{"source_session_id": ..., "target_expected_version": ..., "source_expected_version": ...}`：路径 `{id}` 是目标会话；两个期望版本分别针对目标与来源会话的当前版本。
- 目标轨迹 ID 序列必须是来源轨迹 ID 序列的**严格前缀**：共享位置的转移 ID 必须逐一相同，且来源必须至少还剩一步未被目标走过（空目标轨迹是任意非空来源轨迹的前缀；两边轨迹等长——含两边皆空——均拒绝）。
- 两边**当前协议必须完全一致**：任一字段值不同（状态集、初态、终态，或某条转移的端点/动作/消息/source/target）即拒绝；同一协议仅转移（或状态、终态）**列表顺序不同**也拒绝。
- 成功时只把来源轨迹中目标尚未走过的**剩余后缀**追加到目标（共享前缀不重写），目标 `version` 加 `1`，轨迹与当前配置按原规则从协议初态重算，响应返回目标本次提交的**完整快照**；来源会话只读不写、版本不消耗，同一来源版本可被任意多个目标接回。
- 两边的版本读取、协议一致性与严格前缀检查、目标写入全部在同一个 `BEGIN IMMEDIATE` 事务内、基于**同一份已提交快照**完成：与目标或来源任一会话的修改并发时，要么接纳指定两个版本的完整成果，要么按版本过期返回 `409`；同一目标版本下，接回与执行转移、替换协议、锁定设置互斥，恰好一项成功。
- 目标会话已锁定时接回返回 `409`（`session_locked`）；来源会话只读不写，锁定不影响其作为接回来源。
- 任何失败都不改动两个会话。

状态码与错误体：

- 成功：`200`，响应为目标会话视图（同查询接口）；
- `404`：目标或来源会话 ID 未知（响应体 `{"detail": "unknown session"}`）；
- `409`：版本过期，或目标会话已锁定（`session_locked`）。版本过期时 `detail` 指出对应会话及其当前版本：

```json
{ "detail": { "error": "version_conflict", "current_version": 2, "session": "source", "session_id": "8f51ba3f6921412080fab99706d5290a" } }
```

- `422`：
  - 同一会话：`{"detail": {"error": "same_session", "session_id": "..."}}`；
  - 协议不一致（字段值或列表顺序不同）：`{"detail": {"error": "protocol_mismatch"}}`；
  - 非严格前缀：给出两边轨迹长度；若前缀位置出现不同 ID，还给出从 1 计的 `first_divergent_step`：

```json
{ "detail": { "error": "trace_not_strict_prefix", "target_trace_length": 1, "source_trace_length": 2, "first_divergent_step": 1 } }
```

#### 可执行示例：接回已走通的后续轨迹（端口 8000）

下面用两个会话演示：目标只执行了 `t1`；来源（另一演练会话，可能是从目标分支出去的）已把 `t1, t4` 全部走通。一次接回即让目标得到完整轨迹 `t1, t4`，无需再手动执行 `t4`。

```bash
PROTO='{
  "A": { "states": [0, 1], "initial": 0, "finals": [1] },
  "B": { "states": [0, 1], "initial": 0, "finals": [1] },
  "transitions": [
    { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
    { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
  ]
}'

# 目标会话：只走 t1（version 0 -> 1）
TID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' -d "$PROTO" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$TID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}'

# 来源会话：已在另一会话中把 t1, t4 走通（version 0 -> 2）
SID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' -d "$PROTO" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}'
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 1}'

echo "target=$TID source=$SID"
```

接回（目标当前版本 `1`、来源当前版本 `2`）：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$TID/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$SID\", \"target_expected_version\": 1, \"source_expected_version\": 2}"
```

返回 `200`：目标 `version` 变为 `2`，轨迹为完整的 `[t1, t4]`，当前为完成配置、`enabled_transition_ids` 为 `[]`；该快照是本次提交重算的结果：

```json
{
  "id": "…目标…",
  "version": 2,
  "configuration": { "state": { "A": 1, "B": 1 }, "queues": { "A_to_B": null, "B_to_A": null } },
  "trace": [
    { "index": 1, "transition_id": "t1", "from": "A", "to": "B", "action": "send", "message": "m", "state": { "A": 1, "B": 0 }, "queues": { "A_to_B": "m", "B_to_A": null } },
    { "index": 2, "transition_id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "state": { "A": 1, "B": 1 }, "queues": { "A_to_B": null, "B_to_A": null } }
  ],
  "enabled_transition_ids": []
}
```

来源会话保持不变（仍为版本 `2`、轨迹 `[t1, t4]`），其版本号不被消耗：

```bash
curl -s http://localhost:8000/v1/sessions/$SID
```

#### 可执行示例：各类冲突（422 / 409 / 404）

同一目标与来源：

```bash
curl -s -i -X POST http://localhost:8000/v1/sessions/$TID/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$TID\", \"target_expected_version\": 2, \"source_expected_version\": 2}"
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"error":"same_session","session_id":"…"}}
```

目标轨迹不是来源的严格前缀（上面目标已经接回到 `[t1, t4]`；再从一个空轨迹来源接回，目标更长，第 1 个前缀位置就越界）：

```bash
EMPTY=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' -d "$PROTO" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -i -X POST http://localhost:8000/v1/sessions/$TID/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$EMPTY\", \"target_expected_version\": 2, \"source_expected_version\": 0}"
# HTTP/1.1 422
# {"detail":{"error":"trace_not_strict_prefix","target_trace_length":2,"source_trace_length":0,"first_divergent_step":1}}
```

前缀在中间分叉（目标第 1 步与来源第 1 步不是同一转移）可用一个有两条独立路径的协议演示：

```bash
TWO='{
  "A": { "states": [0, 1], "initial": 0, "finals": [1] },
  "B": { "states": [0, 1], "initial": 0, "finals": [1] },
  "transitions": [
    { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
    { "id": "t2", "from": "B", "to": "A", "action": "send",    "message": "n", "source": 0, "target": 1 },
    { "id": "t3", "from": "B", "to": "A", "action": "receive", "message": "n", "source": 0, "target": 1 },
    { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
  ]
}'
DA=$(curl -s -X POST http://localhost:8000/v1/sessions -H 'Content-Type: application/json' -d "$TWO" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
DB=$(curl -s -X POST http://localhost:8000/v1/sessions -H 'Content-Type: application/json' -d "$TWO" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$DA/transitions -H 'Content-Type: application/json' -d '{"transition_id":"t1","expected_version":0}'
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$DB/transitions -H 'Content-Type: application/json' -d '{"transition_id":"t2","expected_version":0}'
curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$DB/transitions -H 'Content-Type: application/json' -d '{"transition_id":"t3","expected_version":1}'
curl -s -i -X POST http://localhost:8000/v1/sessions/$DA/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$DB\", \"target_expected_version\": 1, \"source_expected_version\": 2}"
# HTTP/1.1 422
# {"detail":{"error":"trace_not_strict_prefix","target_trace_length":1,"source_trace_length":2,"first_divergent_step":1}}
```

协议字段值不同（例如消息由 `"m"` 改为 `"ZZ"`），或转移列表仅顺序不同，均返回 `422 {"detail":{"error":"protocol_mismatch"}}`：

```bash
# 仅把 t1/t4 两条转移的顺序对调，内容完全相同
REORDERED=$(PROTO="$PROTO" python3 -c '
import json, os
p = json.loads(os.environ["PROTO"])
p["transitions"] = [p["transitions"][1], p["transitions"][0]]
print(json.dumps(p))')
RID=$(curl -s -X POST http://localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' -d "$REORDERED" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s -i -X POST http://localhost:8000/v1/sessions/$EMPTY/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$RID\", \"target_expected_version\": 0, \"source_expected_version\": 0}"
# HTTP/1.1 422  ->  {"detail":{"error":"protocol_mismatch"}}
```

版本过期（`session` 标明是目标还是来源过期）：

```bash
curl -s -i -X POST http://localhost:8000/v1/sessions/$TID/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$SID\", \"target_expected_version\": 1, \"source_expected_version\": 2}"
# HTTP/1.1 409 Conflict
# {"detail":{"error":"version_conflict","current_version":2,"session":"target","session_id":"…"}}

curl -s -i -X POST http://localhost:8000/v1/sessions/$EMPTY/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$SID\", \"target_expected_version\": 0, \"source_expected_version\": 1}"
# HTTP/1.1 409 Conflict
# {"detail":{"error":"version_conflict","current_version":2,"session":"source","session_id":"…"}}
```

未知会话：

```bash
curl -s -i -X POST http://localhost:8000/v1/sessions/does-not-exist/adoptions \
  -H 'Content-Type: application/json' \
  -d "{\"source_session_id\": \"$SID\", \"target_expected_version\": 0, \"source_expected_version\": 2}"
# HTTP/1.1 404 Not Found  ->  {"detail":"unknown session"}
```

以上任何失败都不改数据：两个会话的版本、协议与轨迹保持原样，可用 `GET /v1/sessions/{id}` 复核。

### 两次演练对比：`POST /v1/session-comparisons`

对比两次演练，**区分消息交换本身的变化与双方向之间的交错次序差异**：

- 请求体给出左、右两个会话 ID 及各自的期望版本：`{"left_session_id": ..., "right_session_id": ..., "left_expected_version": ..., "right_expected_version": ...}`。左右**可以是同一会话 ID**（会话与自身比较必为相同），两边的**协议也可以完全不同**。
- 服务把每个会话的轨迹 ID 按**各自当前协议**重放，把每步投影成该消息方向上的一个 `(action, message)` 事件：
  - 按消息流向分成 **A→B**（`"A_to_B"`）与 **B→A**（`"B_to_A"`）两条序列，每条**保留原轨迹中的先后顺序**（发送与接收该消息的步骤进入同一条）；
  - 比较**只看 `action` 与 `message`**，忽略转移 ID、执行后端点状态/队列，以及两个方向之间的交错次序——因此两次演练即使步骤交错完全不同，只要每个方向上的动作/消息序列一致，就判为相同。
- 每个方向独立返回 `left_event_count`、`right_event_count` 与 `relation`，取值：
  - `"equal"`：两条序列逐项相同（含两边皆空），`first_difference` 为 `null`；
  - `"left_is_strict_prefix"`：左侧是右侧的严格前缀（右侧还继续）；
  - `"right_is_strict_prefix"`：右侧是左侧的严格前缀（左侧还继续）；
  - `"divergent"`：共有位置上出现首个不同的动作/消息。
- `first_difference` 给出**从 1 计的首个差异位置**：`position` 是该方向序列中的位置；`left`、`right` 分别是双方在该位置的事件（`action`、`message`，以及定位用的原轨迹步号 `trace_step` 与 `transition_id`），缺失侧为 `null`；`"equal"` 时整个字段为 `null`。严格前缀的首个差异位置为「较短长度 + 1」，缺失侧为 `null`。
- 响应同时回显所用的 `left_session_id`、`right_session_id`、`left_version`、`right_version`；接口**只读**，不改动任何数据或版本。
- 两个会话行在**同一个读事务**内读取：在 WAL 模式下整个事务只观察同一份已提交快照，因此两边必然来自同一已提交状态。与执行转移、协议替换或接回并发时，请求要么基于指定版本的**完整轨迹**给出比较，要么返回 `409`，`detail` 以 `side`（`"left"`/`"right"`，左侧先判定）、`session_id` 与 `current_version` 指出过期侧；响应绝不会混入随后提交的步骤。任一会话 ID 未知返回 `404`。

#### 可执行示例：交错不同、消息交换相同（端口 8000）

下面的协议每个方向各有一次发送/接收，且发送步骤在状态 `0` 自环，所以「先 A→B 还是先 B→A」都合法。两次演练的轨迹交错不同，但每个方向上的 `(action, message)` 序列完全一致。

```bash
INTER='{
  "A": { "states": [0, 1], "initial": 0, "finals": [1] },
  "B": { "states": [0, 1], "initial": 0, "finals": [1] },
  "transitions": [
    { "id": "sAB", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 0 },
    { "id": "rAB", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 },
    { "id": "sBA", "from": "B", "to": "A", "action": "send",    "message": "n", "source": 0, "target": 0 },
    { "id": "rBA", "from": "B", "to": "A", "action": "receive", "message": "n", "source": 0, "target": 1 }
  ]
}'
mk() { curl -s -X POST http://localhost:8000/v1/sessions -H 'Content-Type: application/json' -d "$1" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'; }
ex() { curl -s -o /dev/null -X POST http://localhost:8000/v1/sessions/$1/transitions \
  -H 'Content-Type: application/json' -d "{\"transition_id\":\"$2\",\"expected_version\":$3}"; }

L=$(mk "$INTER"); R=$(mk "$INTER")
# 左：sAB sBA rAB rBA —— 交错次序 A先
ex $L sAB 0; ex $L sBA 1; ex $L rAB 2; ex $L rBA 3
# 右：sBA sAB rBA rAB —— 交错次序 B先
ex $R sBA 0; ex $R sAB 1; ex $R rBA 2; ex $R rAB 3

curl -s -X POST http://localhost:8000/v1/session-comparisons \
  -H 'Content-Type: application/json' \
  -d "{\"left_session_id\":\"$L\",\"right_session_id\":\"$R\",\"left_expected_version\":4,\"right_expected_version\":4}"
```

两个方向各自的动作/消息序列都是先发送后接收，交错被忽略，结果两个方向都为 `equal`：

```json
{
  "left_session_id": "…L…",
  "right_session_id": "…R…",
  "left_version": 4,
  "right_version": 4,
  "A_to_B": {
    "left_event_count": 2,
    "right_event_count": 2,
    "relation": "equal",
    "first_difference": null
  },
  "B_to_A": {
    "left_event_count": 2,
    "right_event_count": 2,
    "relation": "equal",
    "first_difference": null
  }
}
```

#### 可执行示例：严格前缀与分歧

左会话只执行 `t1`（A→B 发送 `m`，版本 `1`），右会话执行 `t1, t4`（发送后接收，版本 `2`）：

```bash
PROTO='{
  "A": { "states": [0, 1], "initial": 0, "finals": [1] },
  "B": { "states": [0, 1], "initial": 0, "finals": [1] },
  "transitions": [
    { "id": "t1", "from": "A", "to": "B", "action": "send",    "message": "m", "source": 0, "target": 1 },
    { "id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m", "source": 0, "target": 1 }
  ]
}'
L=$(mk "$PROTO"); R=$(mk "$PROTO")
ex $L t1 0
ex $R t1 0; ex $R t4 1

curl -s -X POST http://localhost:8000/v1/session-comparisons \
  -H 'Content-Type: application/json' \
  -d "{\"left_session_id\":\"$L\",\"right_session_id\":\"$R\",\"left_expected_version\":1,\"right_expected_version\":2}"
```

A→B 方向左侧是右侧的严格前缀，首个差异位置 `2` 上左侧缺失（`null`），右侧事件回指原轨迹第 2 步；B→A 方向两边皆空、判为 `equal`：

```json
{
  "left_session_id": "…L…",
  "right_session_id": "…R…",
  "left_version": 1,
  "right_version": 2,
  "A_to_B": {
    "left_event_count": 1,
    "right_event_count": 2,
    "relation": "left_is_strict_prefix",
    "first_difference": {
      "position": 2,
      "left": null,
      "right": { "action": "receive", "message": "m", "trace_step": 2, "transition_id": "t4" }
    }
  },
  "B_to_A": {
    "left_event_count": 0,
    "right_event_count": 0,
    "relation": "equal",
    "first_difference": null
  }
}
```

左右允许使用**不同协议**：例如右侧把消息由 `"m"` 改成 `"ZZ"`（转移 ID 相同也不影响比较），则 A→B 方向在位置 `1` 即分歧，`first_difference.left` / `.right` 给出双方事件及其原轨迹步号与转移 ID。把左右对调请求即可得到 `"right_is_strict_prefix"`；两边 ID 相同（会话与自身比较）时两方向都为 `"equal"`。

版本过期与未知会话：

```bash
# 左侧期望版本过期（当前为 1，却传 0）-> 409，side 指出过期侧
curl -s -i -X POST http://localhost:8000/v1/session-comparisons \
  -H 'Content-Type: application/json' \
  -d "{\"left_session_id\":\"$L\",\"right_session_id\":\"$R\",\"left_expected_version\":0,\"right_expected_version\":2}"
# HTTP/1.1 409 Conflict
# {"detail":{"error":"version_conflict","current_version":1,"side":"left","session_id":"…L…"}}

# 任一会话 ID 未知 -> 404 {"detail":"unknown session"}
curl -s -i -X POST http://localhost:8000/v1/session-comparisons \
  -H 'Content-Type: application/json' \
  -d "{\"left_session_id\":\"does-not-exist\",\"right_session_id\":\"$R\",\"left_expected_version\":0,\"right_expected_version\":2}"
# HTTP/1.1 404 Not Found
```

对比不写任何数据：调用后用 `GET /v1/sessions/{id}` 复核，两个会话的版本与轨迹均不变。并发场景下（2 个 worker），对比与执行/替换/接回同时进行时，每个对比要么返回指定版本的完整结果，要么返回 `409`，不会返回跨版本拼凑的轨迹。

#### 可执行示例：同目标版本的并发只成一项

以 2 个 worker 运行时，对同一目标的同一 `target_expected_version` 并发发起「接回」与「执行转移」（也可换成替换协议），恰好一项返回 `200`，其余返回 `409`：

```bash
# 接上面 EMPTY（空轨迹、version 0）与 SID（轨迹 [t1,t4]、version 2）
for i in $(seq 1 4); do
  curl -s -o /tmp/adopt_$i.out -w '%{http_code}\n' -X POST \
    http://localhost:8000/v1/sessions/$EMPTY/adoptions \
    -H 'Content-Type: application/json' \
    -d "{\"source_session_id\": \"$SID\", \"target_expected_version\": 0, \"source_expected_version\": 2}" &
  curl -s -o /tmp/exec_$i.out -w '%{http_code}\n' -X POST \
    http://localhost:8000/v1/sessions/$EMPTY/transitions \
    -H 'Content-Type: application/json' \
    -d '{"transition_id": "t1", "expected_version": 0}' &
done
wait
# 8 个请求中恰好 1 个 200，其余 7 个 409；目标最终 version 恰为 1
curl -s http://localhost:8000/v1/sessions/$EMPTY \
  | python3 -c 'import sys, json; b = json.load(sys.stdin); print(b["version"], [s["transition_id"] for s in b["trace"]])'
# 1 [t1, t4]   <- 接回获胜；若执行获胜则为 1 [t1]
```

与来源会话自身的修改并发时，每个接回要么读到来源指定版本的完整快照（接纳其全部剩余轨迹），要么以 `session: "source"` 返回 `409`，响应绝不会混入来源随后提交的步骤。

### 存储位置与卷保留规则

- 数据库文件：容器内 **`/data/sessions.db`**（WAL 模式，运行时可能伴随 `-wal` / `-shm` 文件），可用环境变量 `SESSIONS_DB` 覆盖；本地开发缺省为工作目录下的 `./sessions.db`。
- Compose 命名卷 `sessions-data` 挂载到 `/data`；首次挂载时卷会继承镜像中 `/data` 的属主（运行用户 `nobody`），保证运行用户可写。服务启动时自动建库建表；旧版数据库启动时自动补充 `locked` 列（默认未锁定），已有会话的协议、轨迹与版本保持不变。
- `docker compose stop` / `restart` / `down` / `up --build` 都**保留**命名卷，会话数据不丢；`docker compose down -v` 会**删除**命名卷，全部会话随之清除。
- 查看卷：`docker volume ls | grep sessions-data`。

### 跨进程并发修改示例

服务以 2 个 worker 进程运行，共享同一 SQLite 库。两个请求携带**同一 `expected_version`** 并发修改同一会话（接上面 `version` 为 `0` 的新会话）：

```bash
curl -s -o /tmp/r1.json -w '%{http_code}\n' -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}' &
curl -s -o /tmp/r2.json -w '%{http_code}\n' -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t1", "expected_version": 0}' &
wait
cat /tmp/r1.json /tmp/r2.json
```

恰好一项返回 `200`（`version` 变为 `1`），另一项返回 `409` 及当前版本：

```json
{ "detail": { "error": "version_conflict", "current_version": 1 } }
```

### 重启续演示例

重建或重启服务（命名卷保留，会话不丢）：

```bash
docker compose up --build -d        # 或 docker compose restart
```

按**原会话 ID** 查询，版本、协议与轨迹保持重启前的已提交状态：

```bash
curl -s http://localhost:8000/v1/sessions/$SID
```

用查询到的当前版本继续执行（假设当前 `version` 为 `1`）：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 1}'
```

重启前的旧版本号仍然失效，返回 `409` 及当前版本：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/$SID/transitions \
  -H 'Content-Type: application/json' \
  -d '{"transition_id": "t4", "expected_version": 0}'
# -> 409 {"detail": {"error": "version_conflict", "current_version": 2}}
```

## 本地开发（不使用 Docker）

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

本地开发时会话库默认写到当前目录的 `sessions.db`（可用环境变量 `SESSIONS_DB` 指定其他路径），首次请求时自动建库建表。

## 项目结构

```text
.
├── app/
│   ├── __init__.py
│   ├── analyzer.py
│   ├── main.py
│   ├── schemas.py
│   └── sessions.py
├── Dockerfile
├── docker-compose.yml
├── README.md
└── requirements.txt
```
