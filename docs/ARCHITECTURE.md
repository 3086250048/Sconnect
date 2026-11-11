# 架构说明

## 目标

Sconnect 面向约 10 到 20 个账户的 TURN 中转场景。重点不是无限扩展，而是让每个账户都有清晰、可操作的上限：

- 最大带宽
- 并发 allocation 数
- 流量额度
- 生命周期
- 可观测性

## 组件

```text
应用 / 浏览器
      │
      │ TURN UDP/TCP/TLS
      ▼
┌───────────────────────┐
│ coturn (账户 A)       │  34780 / relay 50000-50063
├───────────────────────┤
│ coturn (账户 B)       │  34781 / relay 50064-50127
├───────────────────────┤
│ ...                   │
└───────────────────────┘
      ▲             ▲
      │ metrics     │ CLI (loopback)
      └──────┬──────┘
             │
      Sconnect 控制面
      ├── SQLite 账户与用量
      ├── REST API
      ├── 进程 supervisor
      └── 配额执行器
```

### 控制面

控制面是单进程 Python 服务，使用标准库 HTTPServer、SQLite 和 subprocess。它维护账户表、启动和停止 coturn、采集用量并执行额度。

### 数据面

每个账户拥有一个独立 coturn 进程。该进程只使用本账户的 realm、REST 密钥、监听端口、relay 端口段和指标端口。

## 为什么不是共享 coturn realm

共享 realm 的问题：

- `user-quota` 限制的是单个用户名，不便于按账户做统一策略。
- `max-bps` 是会话级限制，不能直接限制账户总带宽。
- coturn 用户数据库不提供可靠的累计字节额度执行。
- Prometheus 指标带 realm/user 标签，但进一步做账户生命周期和暂停仍然需要控制面。

独立进程的代价是进程数和内存开销更高，但对 20 个账户以内规模是可接受的，并且边界最清楚。

## 认证模型

TURN 使用 coturn REST 认证：

```text
username = "<expires_at>:<account_id>"
password = base64(HMAC-SHA1(account_secret, username))
```

每个账户有独立 `static-auth-secret`。客户端通过控制面签发短期凭证，过期后 coturn 拒绝新的 allocation。

管理 API 使用全局 admin token；账户 API 使用每账户随机 key。数据库只存 key 的 SHA-256 哈希。

## 流量统计

控制面优先从每个 coturn 的 loopback Prometheus 端口读取：

- `turn_total_traffic_rcvb`
- `turn_total_traffic_sentb`

如果 Prometheus 不可用，则通过 loopback CLI 的 `ps` 输出读取活动 allocation 用量。两种来源代表同一份进程流量，不能相加；控制面只选择其中一个来源。

coturn 进程重启后计数器归零。控制面在进程启动时把数据库中历史已计流量作为 base，再叠加当前进程计数，因此重启不会把历史流量清零。

## 配额执行

默认每 5 秒采集一次。总用量达到 `traffic_quota_bytes` 后：

1. 控制面把账户标记为 `traffic_quota_exceeded`。
2. supervisor 停止该账户的 coturn 进程。
3. 后续签发凭证返回 HTTP 409。
4. 管理员执行 `reset-usage` 后清零并恢复。

额度是软额度，不是内核级硬切断；最坏情况下会多消耗一个采集窗口的流量。

## 带宽模型

每个账户的 coturn 配置：

```text
max-bps = max_bps / allocation_quota
bps-capacity = max_bps
user-quota = allocation_quota
total-quota = allocation_quota
```

`max-bps` 是单会话单方向限速，`bps-capacity` 是该 coturn 进程的总带宽容量。coturn 对输入和输出分别计算，因此双向合计可能接近 `max_bps` 的 2 倍。

## 端口分配

账户获得最小的未占用 slot：

```text
turn_port    = TURN_PORT_BASE + slot
tls_port     = TURN_TLS_PORT_BASE + slot
relay_min    = TURN_RELAY_PORT_BASE + slot * ports_per_account
relay_max    = relay_min + ports_per_account - 1
metrics_port = TURN_METRICS_PORT_BASE + slot
cli_port     = TURN_CLI_PORT_BASE + slot
```

服务启动时检查所有范围互不重叠。默认 20 个账户占用：

- TURN：`34780-34799`
- relay：`50000-51279`
- TLS：`54000-54019`

## 故障模型

- coturn 退出：supervisor 每秒检查并重启，重启尝试有 3 秒退避。
- 控制面重启：从 SQLite 恢复账户，supervisor 重新启动已启用的 coturn。
- 指标采集失败：继续使用 CLI 回退；两者都失败时保留上次已知用量。
- SQLite 写入失败：请求返回 500；进程状态仍由 supervisor 维护。

## 适用边界

该架构适合单机、账户数量有限、需要清楚隔离边界的 TURN 中转。若账户数增长到数百个，应改为多实例 coturn + 外部调度、集中指标和独立配额服务，而不是继续增加单机子进程。
