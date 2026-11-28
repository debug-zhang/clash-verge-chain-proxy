# Clash Verge 链式代理配置

在 Clash Verge Rev 里用 `dialer-proxy` 搭起「订阅节点 → 静态出口」两跳链路，并配好 TUN + 防泄露 DNS，把踩过的坑固化成可复用的脚本和诊断工具。

适用：Clash Verge Rev 2.x + verge-mihomo 1.19.x，macOS。
Linux / Windows 的 Verge 同理，只需改路径（脚本自动探测）。

## 它解决什么问题

你有一个固定出口 IP 的代理（住宅 IP、独享 IP、白名单 IP），服务端**只接受来自指定节点的连接**，本机直连会被立刻拒绝。于是需要两跳：

```
应用 → 127.0.0.1:<mixed-port> → 规则匹配 → 入口组 → 出口节点（socks5）→ 目标
                                              ↑
                                    第一跳：订阅节点，负责翻墙出网
```

mihomo 用 `dialer-proxy` 表达这个关系：出口节点自己不直连，而是「由某个节点/组来拨号」。

## 为什么需要这个仓库

两件事在 UI 上做不成，或者做成了会悄悄失效：

1. **Verge 会剥离 `dialer-proxy`。** 写在「Proxies 增强」yaml 里的 `dialer-proxy`，在生成最终配置时被专门过滤掉，只有 `type/server/port/username/password` 能进入生成物。必须改用 **Script 增强**注入。
2. **UI 的「链式代理模式」不可靠。** 它把状态存在 WebKit localStorage，不是 yaml；关闭时触发上面的剥离行为，而且换机器/清缓存就没了。

所以本仓库把链式做成**脚本注入 + 单一来源**，不依赖任何 UI 开关。

## 快速开始

前提：Verge 里已经有一个可用的订阅，并且**已经创建过 Script / Proxies / Rules 三个增强**
（订阅页 → 当前配置 → 右键菜单里创建）。`cvctl.py apply` 只往 Verge 已经认识的文件里写内容，
不伪造 uid、不改 `profiles.yaml` —— 那样容易和 UI 状态打架。

```bash
git clone <this-repo>
cd clash-verge-chain-proxy

# 1. 体检 + 看清本机路径（端口/socket/增强文件位置因机器而异）
./cvctl.py doctor
./cvctl.py paths

# 2. 把链路脚本写进已绑定的 Script 增强（自动备份原文件）
./cvctl.py apply

# 3. 填真实值：path 由上一步打印
$EDITOR "<proxies 增强文件>"   # 出口节点 server/port/username/password
$EDITOR "<script 增强文件>"    # CHAIN = ['Japan']，与节点 name 一致

# 4. 重新生成配置（不是重启内核）
./cvctl.py regen

# 5. 验证
./cvctl.py verify
```

期望输出：

```
代理连通 (http://127.0.0.1:7897)
  ✓ cp.cloudflare.com 204 0.48s

出口 IP
    直连: (不通，通常正常)
    代理: 203.0.113.10
  ✓ 出口 203.0.113.10（直连不通，无法对比，以连接链为准）

连接链
    dash.cloudflare.com    Japan > <入口组名>
```

看到 `Japan > <入口组名>` 或 `订阅节点 > FAST-FRONT` 这样的两跳链，就说明成功了。

## 核心设计

### 第一跳是 url-test 组，不是固定节点

脚本扫描所有可用节点建一个 `FAST-FRONT`（`url-test`，300 秒重测，容差 50ms）。订阅节点增减、某个节点宕机，都不需要人工干预。

早期做法是固定一个节点名，节点挂了整条链就断，误判成配置问题排查很久。改成 `url-test` 后延迟从平均 4.5s 降到 0.86s。

### 三重排除，避免非法配置

建组时排除：组类型（`select/url-test/fallback/loadbalance/relay` 不能当拨号源）、`CHAIN` 里的出口节点自身（防环）、订阅信息节点（`剩余流量/套餐到期/官网` 这些伪装成真实节点的假节点）。

信息节点尤其坑：它们长得像节点，`type` 也是真实的 AnyTLS，被选中会因为连不上而拖慢整个 url-test。

### `dialer-proxy` 只有一个来源

出口节点定义（Proxies 增强）里**故意不写** `dialer-proxy`，只在脚本里注入。两处都写的话，每次重新生成谁覆盖谁不确定。

### 全 ES5 语法

Verge 用 Boa 引擎跑增强脚本，`var` + `indexOf` 最稳。`const/let/箭头函数` 有兼容风险。

## 生效时机（最容易踩）

**重启内核不会重新生成配置。** 它只是用已有的 `clash-verge.yaml` 重新拉起 mihomo 进程。

必须触发一次配置生成：

- 订阅更新（`[Subscription] 订阅更新` → `[Validate] 生成临时配置文件用于验证`）
- 或手动保存 / 切换 profile
- 或退出并重开 App（实测有效）

日志里出现 `[Validate] 验证成功` 才算数。`cvctl.py regen` 做的就是退出重开 + 等日志。

## 文件说明

| 文件 | 作用 |
|---|---|
| `chain-proxy.js` | 链式注入脚本，装到 `profiles/<uid>.js` 并在 `profiles.yaml` 绑定 |
| `cvctl.py` | 运维工具：doctor / paths / apply / status / verify / regen / diagnose / restore |
| `profiles/exits.example.yaml` | 出口节点模板（Proxies 增强） |
| `profiles/rules.example.yaml` | 直连规则模板（Rules 增强） |
| `profiles/merge.example.yaml` | DNS + TUN 策略模板（Merge 增强） |
| `docs/mechanics.md` | 机制细节：为什么被剥离、生成流程、探针法 |
| `docs/troubleshooting.md` | 故障对照表与实际排查记录 |
| `AGENTS.md` | 给 AI 编码助手的项目约束 |

## 排查

```bash
./cvctl.py diagnose     # doctor + status + verify 一把梭
```

高频故障：

| 现象 | 原因 | 处理 |
|---|---|---|
| 全部请求 000 / 超时 | 第一跳挂了，或 `dialer-proxy` 丢失 | `cvctl.py status` 查两级 |
| `context deadline exceeded` | 第一跳不可达，第二跳限源 IP 拒直连 | 换第一跳；确认 `dialer-proxy` |
| 改了脚本没变化 | 未触发配置重新生成 | `cvctl.py regen` |
| 源文件有 `dialer-proxy`、生成物没有 | UI 链式开关关着导致剥离，或 Script 未绑定 | 检查 `profiles.yaml` 的 `option.script` |
| UI 显示 Timeout 但实际能通 | `alive` 是上次探测残留 | 以 `curl` 实测为准 |
| `PATCH /configs` 返回 204 但 TUN 没开 | sidecar 模式内核非 root，建不了 utun | 装特权 helper 切 service 模式 |
| DNS 出口仍是国内 IP | TUN 未开，或 `nameserver` 缺 `#组名` | 见下方「DNS 与 TUN」 |
| 部分域名解析彻底失败 | 订阅的 `policy` 指向 `dns.cloudflare.com`（直连被墙、走代理 400） | Merge 模板里覆盖这些 key |

## DNS 与 TUN

链式只解决「流量从哪出去」，不解决「域名在哪解析」。两者不一致时，出口在东京、解析出口在杭州，风控直接判定泄露。

### 三层要同时成立

| 层 | 配置 | 缺了会怎样 |
|---|---|---|
| 内核 DNS | `dns.enable: true`，`nameserver` 带 `#代理组名` | 解析不经隧道 |
| DNS 劫持 | TUN 的 `dns-hijack: [any:53]` | 系统仍问路由器，绕过内核 |
| 反旁路 | 全局 `ipv6: false` | AAAA 查询走 IPv6 直连 |

### 最小可用配置

```yaml
dns:
  enable: true
  ipv6: false
  respect-rules: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  default-nameserver: [223.5.5.5, 119.29.29.29]
  nameserver:                       # 必须带 #组名，否则 DoH 直连被墙
    - 'https://1.1.1.1/dns-query#悠兔'
    - 'https://8.8.8.8/dns-query#悠兔'
  proxy-server-nameserver:          # 解析代理节点自身域名用，必须能直连
    - https://doh.pub/dns-query
    - https://dns.alidns.com/dns-query

tun:
  stack: gvisor
  mtu: 1400                         # 1500 会让 TLS 握手卡在分片
  auto-route: true
  dns-hijack: [any:53]
```

策略放在 **Merge 增强**里，不要放 Proxies 增强。Merge 的优先级高于订阅，且订阅更新不会覆盖它 —— 这是唯一能持久化的位置。

### 注意订阅自带的 policy

很多订阅会在 `nameserver-policy` 里塞 `https://dns.cloudflare.com/dns-query`（不带 `#组名`）。这个地址国内直连被 reset、走代理返回 400，会让对应域名**解析彻底失败**。用 Merge 模板里同名 key 覆盖掉它们。

### 开 TUN 需要 root

sidecar 模式下内核以普通用户运行，`PATCH /configs` 会返回 204 但 `tun.enable` 依然是 `false` —— 请求被接受，内核却没权限建 utun 接口。这种「成功但没生效」最容易误判。

需要装特权 helper 切到 service 模式，详见 `docs/mechanics.md`。

### 验证

```bash
# 解析器出口回显，ECS 应是代理 IP 所在网段
dig +short TXT whoami.ds.akahelp.net @198.18.0.1

# DoH 是否走了代理组
curl -s --unix-socket <sock> http://localhost/connections | \
  python3 -c "import json,sys;[print(c['metadata'].get('destinationIP'),'->',c['chains']) for c in json.load(sys.stdin)['connections'] if c['metadata'].get('destinationIP','').startswith(('1.1.1.1','8.8.8.8'))]"
# 1.1.1.1 -> ['Japan', '<入口组名>']
```

## 安全

`profiles.yaml` 和各增强 yaml 里含订阅地址与出口账号密码，**不要提交到 git**。本仓库只放模板，`apply` 不会把你的凭据带出来。

## 许可

MIT
