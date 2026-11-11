# 部署指南

## 1. 前置条件

- 一台有公网 IPv4 的 Linux 服务器
- Docker Engine 与 Docker Compose 插件
- 一个指向服务器公网 IP 的 DNS 记录，例如 `turn.example.com`
- 云厂商安全组和主机防火墙都可放行 TURN 端口
- 服务器时间同步正常（NTP/chrony）；TURN REST 凭证的过期时间依赖系统时钟

默认按 IPv4 部署。当前一个 coturn 进程只配置一个 `listening-ip`；如果必须支持 IPv6，请先扩展配置和防火墙后再启用 AAAA 记录。

### 容量规划

- 20 个账户会运行 20 个 coturn 子进程，每个默认 1 个 relay 线程。
- 建议至少 2 vCPU、2 GiB 内存；实际内存取决于并发 allocation 数。
- 网络带宽是主要成本。默认账户 `--max-mbps 16`，20 个账户理论峰值可达 320 Mbit/s；如果 VPS 只有 100 Mbit/s，应把每个账户的预算降到 4 到 5 Mbit/s，并限制同时使用的账户数。
- TURN 用量按接收和发送字节合计，云厂商通常也对双向流量计费。

## 2. 获取代码

```bash
git clone https://github.com/3086250048/Sconnect.git
cd Sconnect
```

## 3. 配置环境变量

```bash
cp .env.example .env
chmod 600 .env
ADMIN_TOKEN="$(openssl rand -hex 32)"
sed -i "s|^SCONNECT_ADMIN_TOKEN=.*|SCONNECT_ADMIN_TOKEN=${ADMIN_TOKEN}|" .env
sed -i "s|^TURN_PUBLIC_HOST=.*|TURN_PUBLIC_HOST=turn.example.com|" .env
sed -i "s|^TURN_EXTERNAL_IP=.*|TURN_EXTERNAL_IP=203.0.113.10|" .env
```

### 公网 IP 的两种模式

服务器网卡直接持有公网 IP：

```dotenv
TURN_PUBLIC_HOST=turn.example.com
TURN_EXTERNAL_IP=203.0.113.10
TURN_LISTEN_IP=0.0.0.0
```

服务器位于云 NAT 后面：

```dotenv
TURN_PUBLIC_HOST=turn.example.com
TURN_EXTERNAL_IP=203.0.113.10/10.0.0.12
TURN_LISTEN_IP=10.0.0.12
TURN_RELAY_IP=10.0.0.12
```

其中 `TURN_EXTERNAL_IP` 使用 coturn 的 `PUBLIC_IP/PRIVATE_IP` 格式。云厂商的安全组必须把公网 IP 的 TURN 和 relay 端口映射到这台实例。

## 4. 防火墙

只开放数据面端口，不要开放管理端口：

```bash
ufw allow 34780:34799/tcp
ufw allow 34780:34799/udp
ufw allow 50000:51279/udp
ufw allow 54000:54019/tcp
ufw allow 54000:54019/udp
ufw reload
```

如果未启用 TLS，可以不开放 `54000-54019`。云安全组也需要同样的入站规则。默认不要开放 `8080`、`39000-39019`、`39100-39119`。

## 5. 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 sconnect
```

检查：

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
curl -fsS http://127.0.0.1:8080/metrics \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | head
```

`readyz` 返回 200 表示 turnserver 二进制存在且数据库可读。

## 6. 管理 API 访问方式

默认 API 只绑定 `127.0.0.1`。在本地电脑建立 SSH 隧道：

```bash
ssh -N -L 8080:127.0.0.1:8080 root@203.0.113.10
```

然后在本机设置：

```bash
export SCONNECT_API_URL=http://127.0.0.1:8080
export SCONNECT_ADMIN_TOKEN="<服务器 .env 中的令牌>"
```

也可以直接在容器内执行管理脚本，此时会使用容器中的 `SCONNECT_ADMIN_TOKEN`：

```bash
docker compose exec sconnect /opt/sconnect/scripts/turnctl list
```

不要把管理 API 直接暴露到公网。若应用服务器必须通过 HTTPS 调用凭证接口，建议在控制面前增加带 TLS、IP allowlist 或访问网关的反向代理，并只放行必要路径。

## 7. 创建账户

```bash
export SCONNECT_ADMIN_TOKEN="$(grep '^SCONNECT_ADMIN_TOKEN=' .env | cut -d= -f2-)"
./scripts/turnctl create app-a --max-mbps 32 --sessions 16 --quota-gb 20
```

保存返回的 `api_key`。以后签发凭证：

```bash
./scripts/turnctl creds acct_xxxxxxxxxxxx --ttl 600
```

应用后端应调用控制面获得 `iceServers`，不要在客户端硬编码 TURN 密码。短时凭证过期后重新申请。

如果单个应用预计有 10 到 20 个并发客户端，把 `--sessions` 设为 16 到 24；一个客户端连接通常需要一个 allocation。`--max-mbps` 是账户总预算，并会均分给各会话，因此 32 Mbit/s、16 个会话约为每会话 2 Mbit/s。

## 8. 可选 TLS/DTLS

将证书放入 `./certs`：

```text
certs/fullchain.pem
certs/privkey.pem
```

确保容器内的 `nobody` 用户可读私钥。然后在 `.env` 设置：

```dotenv
TURN_TLS_ENABLED=true
TURN_TLS_CERT=/certs/fullchain.pem
TURN_TLS_KEY=/certs/privkey.pem
```

重启：

```bash
docker compose up -d
```

生成的 `turns:` URL 使用 `TURN_TLS_PORT_BASE` 对应的账户端口。证书必须匹配 `TURN_PUBLIC_HOST`，否则很多客户端会拒绝连接。

## 9. 备份与恢复

SQLite、账户配置和日志都在命名卷 `sconnect-data` 中。备份：

```bash
docker run --rm \
  -v sconnect-data:/data:ro \
  -v "$PWD":/backup \
  debian:trixie-slim \
  tar czf /backup/sconnect-data.tar.gz -C /data .
```

恢复前先停止服务；恢复后重新启动，supervisor 会根据数据库恢复账户进程。

## 10. 升级

```bash
git pull
docker compose build --pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8080/readyz
```

升级前先备份 `/data`。不要直接删除命名卷，否则账户、API key 哈希和累计流量都会丢失。

## 11. 常见问题

### 账户 `running=false`

```bash
./scripts/turnctl show acct_xxx
docker compose logs --tail=200 sconnect
```

同时查看 `/data/logs/<account_id>.log`。常见原因是端口被占用、证书不可读、`TURN_EXTERNAL_IP` 格式错误或 turnserver 启动失败。

### 客户端能连 STUN 但不能完成 TURN allocation

优先检查：

1. `50000-51279/udp` 是否在云安全组和主机防火墙都放行。
2. `TURN_PUBLIC_HOST` 是否解析到正确公网 IP。
3. 服务器是否位于 NAT 后面而 `TURN_EXTERNAL_IP` 没写 `PUBLIC/PRIVATE`。
4. 凭证是否已过期，或账户是否被流量额度暂停。
5. 客户端是否在拉取最新凭证，而不是使用旧的固定密码。

### 端口冲突

服务启动时会拒绝重叠的 TURN、TLS、relay、metrics、CLI 端口范围。修改 `.env` 后重新执行：

```bash
docker compose --env-file .env config
```

### 没有 Prometheus 指标

官方 coturn 镜像通常带 Prometheus 支持；如果某次构建没有该功能，控制面会回退到 loopback CLI。可以检查：

```bash
./scripts/turnctl metrics
```
