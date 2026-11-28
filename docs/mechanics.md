# 机制细节

这份文档解释「为什么必须这么做」，以及如何验证 Verge 的行为在你当前的版本上没变。

## 配置生成流水线

Verge 不是直接把订阅文件丢给内核，中间有一层合成：

```
远程订阅 yaml（自动覆盖）
  → Merge 增强（profile / dns / 其他合并）
  → Script 增强（JS，能改任意字段）      ← dialer-proxy 只能在这里注入
  → Rules 增强（prepend / append / delete）
  → Proxies 增强（prepend / append / delete）  ← dialer-proxy 在这里会被剥离
  → Groups 增强
  → clash-verge.yaml（生成物，内核实际加载）
```

顺序很重要：Proxies 增强在 Script 之后跑，所以脚本注入的 `dialer-proxy` 不会被后续步骤去掉，但写在 Proxies 增强里的会被专门过滤。

## `dialer-proxy` 被剥离的证据

逐字段比对同一份配置的源文件和生成物：

```
源文件 profiles/<proxies-uid>.yaml:
  dialer-proxy ×2   ✅

生成物 clash-verge.yaml（同一时刻）:
  dialer-proxy ×0   ❌

同一份配置里 type / server / port / username / password 全部正常进入生成物，
只有 dialer-proxy 单独消失。
```

`rules` 增强不受影响（14 条 DIRECT 规则每次都在），说明这是 proxies 增强对 `dialer-proxy` 的**专门过滤**，而不是某个通用 bug。

推测原因：Verge 把「链式代理」做成了独立特性（UI 开关 + localStorage 状态），生成配置时由它统一管理 `dialer-proxy`，因此把 yaml 里的同名手写字段视为冗余并清除。

## UI 链式开关的状态存在哪

不是 yaml，是 WebKit localStorage：

```
~/Library/WebKit/io.github.clash-verge-rev.clash-verge-rev/
  WebsiteData/Default/<hash>/<hash>/LocalStorage/localstorage.sqlite3

key = proxy-chain-mode-enabled   # 'true' / 'false'
      proxy-chain-group
      proxy-chain-exit-node
      proxy-chain-items          # JSON
```

查看：

```bash
python3 - <<'EOF'
import sqlite3, glob
for f in glob.glob("/Users/<you>/Library/WebKit/io.github.clash-verge-rev.clash-verge-rev/"
                   "WebsiteData/Default/*/*/LocalStorage/localstorage.sqlite3"):
    c = sqlite3.connect(f)
    for k, v in c.execute("select key,value from ItemTable where key like 'proxy-chain%'"):
        s = v.decode('utf-16-le', 'replace') if isinstance(v, (bytes, bytearray)) else str(v)
        print(k, "=", s[:80])
EOF
```

结论：这个开关不是配置的一部分，是前端缓存。所以本仓库的方案完全绕开它 —— 关掉它，只用脚本。

## 探针法：确认脚本是否真的在执行

升级 Verge 后行为可能变化。用一个不存在的字段做探针，快速验证 Script 增强还在跑：

```js
// 临时在 main() 里加
config['x-cvctl-probe'] = 'ok';
```

然后触发重新生成，检查生成物：

```bash
grep -c "x-cvctl-probe" "$CFG/clash-verge.yaml"
# 1 → 脚本在执行
# 0 → 脚本没跑，检查 profiles.yaml 的 option.script 绑定
```

验证完记得删掉探针。

## 内核控制接口的两种形态

| 形态 | socket 路径 | 特征 |
|---|---|---|
| sidecar（默认） | `$TMPDIR/verge-mihomo.sock` | 内核作为 App 子进程，以当前用户身份运行，随 App 退出 |
| service（装了特权 helper） | `/var/run/clash-verge-service/users/<uid>/verge-mihomo.sock` | 内核以 root 独立常驻 |

`$TMPDIR` 在 macOS 上形如 `/var/folders/65/<random>/T/`，每台机器不同，**不能硬编码**。

### 探测必须探活，不能只看文件在不在

从 sidecar 切到 service 后，`$TMPDIR` 下那个旧 socket 文件**不会被清理**：文件还在，但已经没有进程监听。`config.yaml` 里的 `external-controller-unix` 也可能残留切换前的旧值。

所以 `cvctl.py` 的 `controller_socket()` 不能只做 `os.path.exists`，而要对每个候选路径发一次 `/version` 探活，有 JSON 返回才算数。曾经因为只判断存在，`cvctl.py status` 一直报「内核里没有 FAST-FRONT 组」，实际是连到了一个死 socket 拿空响应。

候选顺序：`config.yaml` 配置值 → `/var/run/clash-verge-service/users/<uid>/` → `/var/run/clash-verge-service/` → `$TMPDIR`。

### 切到 service 模式才可能开 TUN

macOS 上创建 utun 设备需要 root。sidecar 模式下内核以普通用户运行，`PATCH /configs {"tun":{"enable":true}}` 会返回 HTTP 204 但 `tun.enable` 依然是 `false` —— 请求被接受，内核却没权限建接口，这是最容易误判的地方。

装特权 helper：

```bash
GID=$(id -g)
cat > /tmp/install_svc.sh <<EOF
#!/bin/sh
cd /
export CLASH_VERGE_SERVICE_GID=$GID
exec "/Applications/Clash Verge.app/Contents/Resources/resources/clash-verge-service-install" --install-service
EOF
chmod +x /tmp/install_svc.sh
osascript -e 'do shell script "/tmp/install_svc.sh" with administrator privileges'
```

包装成脚本是必须的：直接把 `CLASH_VERGE_SERVICE_GID=N /path/installer` 塞进 `do shell script` 会报 AppleScript 语法错误。`--gid` 不是安装器的参数，它只认环境变量。

装完重启 App，日志出现 `Core running mode changed: NotRunning -> Service`，`ps` 里内核的 user 变 `root`，`tun.enable` 才会真的变 `true`。

端口同理：`mixed-port` 也不固定（见过 7890、7897），一律从 `config.yaml` 读。

## 热重载与 selected 持久化

`config.yaml` 里 `profile.store-selected: true` 时，Verge 会把「每个组当前选中谁」写进 `profiles.yaml` 的 `selected` 数组：

```yaml
selected:
  - name: <入口组名>
    now: Japan        # ← 这个值要盯住
```

如果 UI 里把入口组切到某个订阅节点，`now` 就变了，重启后内核按它恢复，链式退化成单跳。所以要把它固定成出口节点名。

反过来，也可以直接用内核 API 切：

```bash
curl -s -X PUT --unix-socket "$SOCK" \
  "http://localhost/proxies/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" '<入口组名>')" \
  -H "Content-Type: application/json" -d '{"name":"Japan"}'
```

但重启后还是以 `profiles.yaml` 的 `selected` 为准，所以持久化要改文件。

## relay 已移除

早期版本用 `relay` 类型的组做链式。mihomo 后来移除了这个类型：

```
The group with relay type was removed, please using dialer-proxy instead
```

现在 `relay` 只作为「组类型」出现在排除列表里，防止它被当拨号源。

## DNS 策略

### 泄露是怎么发生的

不开 TUN 时，域名解析完全交给系统 DNS（路由器或运营商），出口就是国内解析器。代理只搬运 TCP，不管 DNS。结果是「流量出口在东京、解析出口在杭州」，风控直接判定泄露。

更隐蔽的一层：订阅自带的 `dns` 段里 `nameserver` 是裸 IP 或国内 DoH，且**没有 `#代理组名` 后缀**，意味着即使内核接管了 DNS，查询也是明文直连出去的，照样走国内出口。

### 三层要同时成立

| 层 | 关键配置 | 缺了会怎样 |
|---|---|---|
| 内核 DNS | `dns.enable: true` + `nameserver` 带 `#组名` | 解析不经隧道，出口仍是国内 |
| 系统/DNS 劫持 | TUN 的 `dns-hijack: [any:53]` | 系统仍然问路由器，绕过内核 |
| 反泄露旁路 | 全局 `ipv6: false` | AAAA 查询走 IPv6 直连，绕过隧道 |

本机实测：TUN 开启后，即使用 `dig @<国内 DNS IP>` 显式指定国内 DNS，查询也被劫持进内核返回 fake-ip。`whoami.ds.akahelp.net` 的 ECS 回显是出口 IP 所在网段，而不是国内地址。

### `nameserver` 必须走代理组

```yaml
nameserver:
  - 'https://1.1.1.1/dns-query#悠兔'   # ← #后面是代理组名，DoH 经该组发出
  - 'https://8.8.8.8/dns-query#悠兔'
proxy-server-nameserver:               # ← 解析代理节点自身域名用，必须直连
  - https://doh.pub/dns-query
  - https://dns.alidns.com/dns-query
```

不加 `#组名` 时 DoH 直连出去，国内环境基本连不通（实测 `1.1.1.1` 直连 8 秒超时、`dns.cloudflare.com` 立刻被 reset）。加了之后连接列表能看到 `1.1.1.1 -> ['Japan', '悠兔']`。

`proxy-server-nameserver` 不能用带 `#组名` 的，否则解析「第一跳节点域名」时需要先有代理，形成循环依赖。所以它必须是能直连的国内 DoH。

### 订阅自带 policy 会盖掉你的设置

本机订阅里有 15 条 `nameserver-policy` 指向 `https://dns.cloudflare.com/dns-query`（无 `#组名`）。这个地址国内直连失败、走代理返回 400，导致 `google.com` 等域名**解析彻底失败**：

```
all DNS requests failed, first error: requesting https://dns.cloudflare.com:443/dns-query:
read tcp 192.0.2.10:52412->198.51.100.8:443: read: connection reset by peer
```

处理方式是在自己的 Merge 模板里用同名 key 覆盖，全部改到走代理的 `1.1.1.1`。**Merge 的 `dns` 段优先级高于订阅**，这是唯一能持久化的位置（订阅更新不会覆盖 Merge 模板）。

### `dns_config.yaml` 与 Merge 模板必须同步

`verge.yaml` 的 `enable_dns_settings: true` 打开后，Verge 用 `dns_config.yaml` 覆盖 profile 的 `dns` 段。两份文件写不一样时，实际生效的是 `dns_config.yaml`，改 Merge 模板看起来「没生效」。两处保持一致，或者干脆只改一处并关掉覆写开关。
