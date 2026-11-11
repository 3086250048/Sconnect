# Sconnect

Sconnect 是一个面向中小规模应用互联的 TURN 中转控制面。默认按约 20 个账户、每个账户 4 个并发 allocation 设计，提供按账户隔离的带宽、会话数、流量额度、账户生命周期和用量统计。

## 核心设计

每个 TURN 账户对应一个独立的 coturn 进程，拥有：

- 独立 TURN UDP/TCP 端口
- 独立 relay 端口段
- 独立 TURN REST 认证密钥
- 独立 Prometheus 指标端口和 loopback CLI 端口
- 独立的 `max-bps`、`bps-capacity`、`user-quota`、`total-quota`

这比所有账户共享一个 coturn realm 更适合“精细控制”：coturn 自带的数据库认证可以管理账户，但不能可靠地按用户统计字节数并执行流量额度。独立进程可以把每个账户的带宽、会话数和配额变成真正的硬边界。

控制面是 Python 标准库服务，负责：

- 创建、启用、禁用、删除账户
- 签发短时 TURN REST 凭证
- 轮换账户 API key
- 通过 coturn Prometheus 指标采集用量；Prometheus 不可用时回退到 loopback CLI
- 达到流量额度后停止该账户的 coturn 进程
- 重置用量并恢复账户

## 默认端口

| 用途 | 端口范围 | 是否公网 |
| --- | --- | --- |
| TURN UDP/TCP | `34780-34799` | 是 |
| TURN relay UDP | `50000-51279` | 是 |
| TURN TLS/DTLS（可选） | `54000-54019` | 是 |
| 控制面 API | `127.0.0.1:8080` | 否 |
| coturn CLI | `127.0.0.1:39000-39019` | 否 |
| coturn Prometheus | `127.0.0.1:39100-39119` | 否 |

端口布局是 `20 个账户 × 64 个 relay 端口`。如果修改 `SCONNECT_MAX_ACCOUNTS` 或 `TURN_RELAY_PORTS_PER_ACCOUNT`，必须同步调整防火墙和云安全组，并保证各端口段不重叠；服务启动时会做重叠检查。

已创建账户的端口记录保存在 SQLite 中。上线后修改 TURN、relay、metrics 或 CLI 的端口基址不会自动迁移旧账户；应提前规划端口，或在停机备份后重建账户。启用 TLS 时，已有账户的 TLS 端口会自动按 slot 补齐。

## 快速开始

```bash
git clone https://github.com/3086250048/Sconnect.git
cd Sconnect
cp .env.example .env
```

生成管理令牌并填写服务器公网信息：

```bash
openssl rand -hex 32
```

至少修改 `.env` 中的：

```dotenv
SCONNECT_ADMIN_TOKEN=<上一步生成的随机值>
TURN_PUBLIC_HOST=turn.example.com
TURN_EXTERNAL_IP=<服务器公网 IP>
```

启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f sconnect
```

检查健康状态：

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
```

创建第一个账户并签发 ICE 配置：

```bash
export SCONNECT_ADMIN_TOKEN="$(grep '^SCONNECT_ADMIN_TOKEN=' .env | cut -d= -f2-)"
./scripts/turnctl create demo --max-mbps 32 --sessions 16 --quota-gb 20
```

`create` 输出的 `api_key` 只显示一次，必须立即保存。后续可使用 `turnctl creds <account_id>` 获取短期 ICE 配置。

`--sessions` 是账户同时可用的 TURN allocation 上限。一个浏览器或客户端连接通常占用一个 allocation；十几人同时使用同一应用时建议设为 16 到 24。`--max-mbps` 是账户总带宽预算，服务会把它均分到各会话；上面的示例约为每会话 2 Mbit/s。

## 账户管理

```bash
./scripts/turnctl list
./scripts/turnctl show acct_xxx
./scripts/turnctl creds acct_xxx --ttl 600
./scripts/turnctl usage acct_xxx
./scripts/turnctl set acct_xxx --max-mbps 8 --quota-gb 50
./scripts/turnctl disable acct_xxx
./scripts/turnctl enable acct_xxx
./scripts/turnctl rotate-key acct_xxx
./scripts/turnctl reset-usage acct_xxx
./scripts/turnctl delete acct_xxx
```

`--max-mbps` 是账户总带宽预算，以 Mbit/s 表示。服务内部按 `max_bps / allocation_quota` 计算单会话 `max-bps`。coturn 的输入和输出方向分别限速，因此双向实测总量可能接近该预算的 2 倍。

`--quota-gb` 使用 GiB；`0` 表示不限流量。流量额度是软额度：默认每 5 秒采集一次，因此高带宽账户可能在最后一次采集窗口内小幅超额，然后被立即停止。

## API

管理端使用 `Authorization: Bearer <SCONNECT_ADMIN_TOKEN>`。账户端使用创建时返回的 API key，只能访问自己的资源。

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/healthz` | 无 |
| GET | `/readyz` | 无 |
| GET | `/metrics` | 管理端 |
| GET | `/v1/accounts` | 管理端 |
| POST | `/v1/accounts` | 管理端 |
| GET | `/v1/accounts/{id}` | 管理端或本账户 |
| PATCH | `/v1/accounts/{id}` | 管理端 |
| DELETE | `/v1/accounts/{id}` | 管理端 |
| GET | `/v1/accounts/{id}/usage` | 管理端或本账户 |
| POST | `/v1/accounts/{id}/credentials` | 管理端或本账户 |
| GET | `/v1/accounts/{id}/ice-servers?ttl_seconds=600` | 管理端或本账户 |
| POST | `/v1/accounts/{id}/rotate-api-key` | 管理端 |
| POST | `/v1/accounts/{id}/reset-usage` | 管理端 |

默认 API 只监听 loopback。管理操作通过 SSH 隧道、同机进程或私有网络访问，不要把管理端口直接暴露到公网。

## 应用接入

应用后端使用账户 API key 获取短期 ICE 配置，再把 `iceServers` 下发给浏览器或客户端：

```bash
curl -fsS \
  -H "Authorization: Bearer sc_xxxxxxxx" \
  "http://127.0.0.1:8080/v1/accounts/acct_xxx/ice-servers?ttl_seconds=600"
```

返回结构可以直接作为 WebRTC 的 ICE server 配置：

```json
{
  "iceServers": [
    {
      "urls": ["turn:turn.example.com:34780?transport=udp"],
      "username": "1700000000:acct_xxx",
      "credential": "base64-hmac-password"
    }
  ]
}
```

不要把账户 API key 放进浏览器代码；由应用后端保存并代理凭证签发。若应用服务器不在同一台机器，使用私有网络、WireGuard、SSH 隧道，或在控制面前增加受控 HTTPS 反向代理。

## 安全边界

- TURN 凭证是短时 REST 凭证，默认 1 小时，可配置 60 秒到 24 小时。
- 账户 API key 只存 SHA-256 哈希，轮换后旧 key 立即失效。
- 默认拒绝 relay 到私网、loopback、link-local、CGNAT 和组播地址，降低 SSRF 与内网探测风险。需要内网互联时，显式设置 `TURN_ALLOW_PRIVATE_PEERS=true`，并评估风险。
- 管理 API、CLI 和 Prometheus 指标默认只绑定 loopback。
- 容器使用 host network、只读根文件系统、丢弃全部 capability，并以非 root 用户运行。

## 文档

- [部署指南](docs/DEPLOY.md)
- [架构说明](docs/ARCHITECTURE.md)
- [测试指南](docs/TESTING.md)

## 本地测试

```bash
python3 -m compileall -q app tests scripts
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config
```

单元测试不需要 coturn 或 Docker daemon；真实的 TURN 联通、带宽和额度测试必须在一台可公网访问的服务器上执行。
