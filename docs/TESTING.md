# 测试指南

测试分三层：单元测试、服务器本地 smoke test、外部网络端到端测试。真实 TURN 行为必须在公网服务器上验证，不能只凭 `localhost` 判断防火墙和 NAT 配置。

## 1. 单元测试

```bash
python3 -m compileall -q app tests scripts
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config
```

单元测试覆盖账户生命周期、租户隔离、API key 轮换、额度暂停、用量统计去重、凭证算法和端口范围校验。它们不需要 coturn 或 Docker daemon。

## 2. 启动后的健康检查

在服务器上：

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
docker compose ps
docker compose logs --tail=100 sconnect
```

创建测试账户：

```bash
export SCONNECT_ADMIN_TOKEN="$(grep '^SCONNECT_ADMIN_TOKEN=' .env | cut -d= -f2-)"
./scripts/turnctl create test-udp --max-mbps 16 --sessions 4 --quota-gb 1
```

记录返回的 `account_id`、`api_key` 和 `iceServers`。

## 3. STUN 可达性

在外部网络的一台 Linux 机器上执行。`turnutils_stunclient` 随 coturn 提供：

```bash
turnutils_stunclient -p 34780 turn.example.com
```

没有本地安装时可用 Docker：

```bash
docker run --rm --network host \
  --entrypoint turnutils_stunclient \
  coturn/coturn:4.18.0-r0-debian \
  -p 34780 turn.example.com
```

预期能看到 STUN binding response，其中包含服务器看到的公网映射地址。如果超时，先检查 UDP `34780` 的云安全组和主机防火墙。

## 4. TURN UDP allocation

在服务器上直接使用控制面 API：

```bash
./scripts/test-turn acct_xxx --transport udp
```

在外网机器上，先把凭证保存成 JSON：

```bash
./scripts/turnctl creds acct_xxx --ttl 600 --json > ice.json
scp ice.json user@laptop:/tmp/ice.json
```

然后在外网执行：

```bash
python3 scripts/test-turn --ice-json /tmp/ice.json --transport udp
```

脚本会调用 `turnutils_uclient`，带有 client-to-client 模式和真实数据包。若本机没有 coturn 工具，会自动尝试 Docker。

外网测试建议在 Linux 主机执行；macOS/Windows 的 Docker Desktop 对 `--network host` 支持与 Linux 不同。

## 5. TCP 与 TLS

TCP：

```bash
./scripts/test-turn acct_xxx --transport tcp
```

TLS（需要先在 `.env` 启用 `TURN_TLS_ENABLED=true` 并放行 `54000-54019/tcp`）：

```bash
./scripts/test-turn acct_xxx --transport tls --ca-file /etc/ssl/certs/ca-certificates.crt
```

如果使用自签名证书，测试时可以加 `--ca-file` 指向自签 CA；生产环境应使用受信任证书。

## 6. 凭证过期

申请 60 秒凭证，等待过期后再连接：

```bash
./scripts/turnctl creds acct_xxx --ttl 60 --json > expired.json
sleep 70
./scripts/test-turn --ice-json expired.json --transport udp
```

预期 allocation 失败，coturn 日志中应出现鉴权失败。不要把“账号密码错误”和“用户名时间戳过期”混在一起排查；TURN REST 凭证的过期时间编码在用户名中。

## 7. 流量统计和额度

先确认 `usage` 会增长：

```bash
./scripts/turnctl usage acct_xxx
./scripts/test-turn acct_xxx --transport udp \
  --messages 5000 --length 1200 --interval-ms 1
./scripts/turnctl usage acct_xxx
```

脚本会打印传输前后的用量差值和观测吞吐。控制面默认每 5 秒采集一次，因此测试后等待至少 6 秒再读取用量。

额度测试建议创建独立账户：

```bash
./scripts/turnctl create quota-test --max-mbps 8 --sessions 2 --quota-gb 0.01
./scripts/test-turn <返回的 account_id> --transport udp \
  --messages 10000 --length 1200 --interval-ms 1 --timeout 120
sleep 8
./scripts/turnctl show <返回的 account_id>
```

预期 `suspended_reason` 变成 `traffic_quota_exceeded`，`running=false`，再次申请凭证返回 409。恢复：

```bash
./scripts/turnctl reset-usage <返回的 account_id>
```

## 8. 带宽限制

把账户带宽设成较低值（例如 1 Mbit/s）：

```bash
./scripts/turnctl set acct_xxx --max-mbps 1
./scripts/test-turn acct_xxx --transport udp \
  --messages 5000 --length 1200 --interval-ms 1
./scripts/turnctl usage acct_xxx
```

注意 coturn 对输入和输出分别限速，`--max-mbps 1` 的双向流量合计可能接近 2 Mbit/s 的观测值。单方向的 relay 峰值应接近配置的会话级上限，而不是无限制增长。

### 并发 10 到 20 个客户端

为单个应用准备 16 个并发 allocation：

```bash
./scripts/turnctl create concurrency-test --max-mbps 32 --sessions 16 --quota-gb 5
```

然后启动 10 个真实 TURN 客户端，持续传输一轮数据：

```bash
./scripts/test-turn <返回的 account_id> --transport udp \
  --clients 10 --messages 2000 --length 1200 --interval-ms 5 --timeout 180
```

测试运行时可在另一个终端观察会话数和流量：

```bash
watch -n 1 ./scripts/turnctl usage <返回的 account_id>
```

`active_sessions` 应能接近客户端数量。如果 `--clients` 大于 `--sessions`，多出的 allocation 会被 coturn 拒绝。浏览器通常为每个活动 peer connection 建立一个 TURN allocation，因此并发人数接近会话上限时应留 20% 余量。

## 9. 多账户隔离

创建两个账户 A、B：

```bash
./scripts/turnctl create tenant-a --max-mbps 8 --sessions 2 --quota-gb 1
./scripts/turnctl create tenant-b --max-mbps 8 --sessions 2 --quota-gb 1
```

验证：

- A 的 TURN 端口和 relay 端口段与 B 不重叠。
- A 的 API key 访问 B 的账户详情返回 403。
- 停止或暂停 A 不影响 B 的 `running` 状态。
- A 的用量只出现在 A 的 `usage` 和指标中。

## 10. 指标观察

```bash
./scripts/turnctl metrics
```

重点看：

- `sconnect_account_running`
- `sconnect_account_active_sessions`
- `sconnect_account_usage_bytes`
- `sconnect_account_suspended`

如需接入 Prometheus，抓取控制面 loopback `/metrics`；控制面端口不要直接暴露到公网。

## 11. 失败判定的优先级

1. 先确认进程是否运行：`turnctl show`、`docker compose logs`、账户日志。
2. 再确认 TURN 端口是否可达：外部 STUN 测试。
3. 再确认 relay 端口是否放行：UDP allocation 和外网 TURN 测试。
4. 再确认鉴权：凭证是否过期、账户是否暂停。
5. 最后才调节带宽或额度；不要用放宽防火墙来掩盖凭证问题。
