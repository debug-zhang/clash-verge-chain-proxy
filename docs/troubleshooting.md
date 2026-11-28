# 排查手册

## 先跑一把梭

```bash
./cvctl.py diagnose
```

它依次跑 doctor（环境）、status（配置状态）、verify（端到端）。下面按输出异常项展开。

## 故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| `PATCH /configs` 返回 204 但 `tun.enable` 仍是 false | sidecar 模式内核非 root，无权限建 utun | 装特权 helper 切 service 模式，重启 App |
| `cvctl.py status` 报「内核里没有 FAST-FRONT」但配置正确 | 探到了一个已失效的残留 socket（切换模式后旧文件不清理） | 按 `/version` 探活选 socket，不要只看文件存在 |
| DNS 出口仍是国内 IP | TUN 未开、`dns.enable` 为 false，或改后没重启应用 | 查 `/configs` 运行时而非 yaml 文件；重启**应用**不是重启内核 |
| `google.com` 等域名解析彻底失败 | 订阅的 `nameserver-policy` 指向 `dns.cloudflare.com`，该地址直连被墙、走代理返回 400 | 在 Merge 模板里覆盖这些 key，改用带 `#组名` 的 `1.1.1.1` |
| 改了 Merge 的 `dns` 段没生效 | `enable_dns_settings: true` 时被 `dns_config.yaml` 覆盖 | 两处同步改 |
| DoH 解析全超时 | `nameserver` 没加 `#代理组名`，DoH 直连被墙 | 加 `#组名` 后缀 |
| AppleScript 装服务报语法错误 | 把 `VAR=val /path/installer` 直接塞进 `do shell script` | 写成 `.sh` 包装脚本再调用 |
| 装服务报 `group id is unavailable` | 安装器需要 `CLASH_VERGE_SERVICE_GID` 环境变量 | 传 `id -g` 的值，注意 `--gid` 不是合法参数 |
| 全部请求 000 / 7s 超时 | 第一跳挂了，或 `dialer-proxy` 丢失 | `status` 查 `FAST-FRONT` 的 now 和生成物 `dialer-proxy` 计数 |
| `203.0.113.10:443 connect error: failed to create session: context deadline exceeded` | 第一跳不可达；第二跳限源 IP 拒绝直连 | 换第一跳；确认 `dialer-proxy` 存在 |
| 改了脚本但无变化 | 未触发配置重新生成 | `./cvctl.py regen`，不要只重启内核 |
| 源文件有 `dialer-proxy`、生成物没有 | 写在了 Proxies 增强里，被剥离 | 改到 Script 增强；确认 `profiles.yaml` 的 `option.script` 已绑定 |
| UI 显示 Timeout 但实际能通 | `alive` 是上次探测的残留值 | 以 `curl` 实测为准 |
| 某站点打不开、其他正常 | 命中 DIRECT 规则，或链式抖动 | 查 Rules 增强；看内核 warning 日志 |
| 重启后链式失效 | `profiles.yaml` 的 `selected.now` 被切回订阅节点 | 固定为出口节点名 |
| 组里出现 `剩余流量：xx GB` 被选中 | 信息节点伪装成了真实节点 | 检查脚本 `INFO_MARKERS` 是否覆盖 |

## 逐层定位

### 第 1 层：内核活着吗

```bash
./cvctl.py doctor
```

看「混合端口监听中」「内核响应」。挂了就是 App 没起或内核崩溃。

### 第 2 层：配置生成对了吗

```bash
./cvctl.py status
```

三个关键指标：

- 生成物 `dialer-proxy` 计数 > 0
- 生成物 `FAST-FRONT` 计数 ≥ 2（定义 + 引用）
- 内核里 `/proxies/<出口节点>` 返回 `dialer-proxy: FAST-FRONT`

任何一项是 0，直接看 `docs/mechanics.md` 的探针法。

### 第 3 层：链路通吗

```bash
./cvctl.py verify
```

看「连接链」段落。期望看到两跳：

```
dash.cloudflare.com    Japan > <入口组名>
```

只有一跳（`Japan` 单独出现）说明 `dialer-proxy` 没生效或第一跳组没建起来。

### 第 4 层：内核在报什么

```bash
SOCK=$(python3 -c "..." )   # 用 cvctl 打印的路径

# 实时跟 10 秒警告日志
curl -s --unix-socket "$SOCK" "http://localhost/logs?level=warning" & sleep 10; kill %1
```

`context deadline exceeded` = 拨号超时（第一跳不可达）。`connection refused` = 目标直接拒绝（第二跳限源 IP）。

## 离线校验配置合法性

```bash
M="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
CFG="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
mkdir -p /tmp/mihomo-test
cp "/Applications/Clash Verge.app/Contents/Resources/resources/"{geoip.dat,geosite.dat,Country.mmdb,ASN.mmdb} /tmp/mihomo-test/
"$M" -t -d /tmp/mihomo-test -f "$CFG/clash-verge.yaml"
# 期望：test is successful
```

**必须先拷 geo 数据文件进 `-d` 目录**，否则内核会尝试联网下载 MMDB 并超时，导致误报失败。

## 从备份恢复

`cvctl.py apply` 与 `cvctl.py regen` 触发前、以及手动改动前都会生成 `<name>.<timestamp>.bak`：

```bash
ls "$CFG"/*.bak "$CFG"/profiles.yaml.*.bak
./cvctl.py restore "$CFG/profiles.yaml.1795623300.bak"
./cvctl.py regen
```

## 实际排查记录（案例）

### 案例 1：迁到 dialer-proxy 只改在生成物上

改了 `clash-verge.yaml`，当下能用，下一次订阅更新就丢了 —— 因为生成物会被整体覆盖。

教训：任何配置都必须落在持久化位置（增强 yaml / 脚本），永远不要手改生成物。

### 案例 2：固定第一跳节点宕机

第一跳固定写死某个订阅节点名。该节点某天挂了，链路全断，报 `context deadline exceeded`，看起来像配置错误。

教训：第一跳用 `url-test` 组自动选，不要写死。

### 案例 3：UI 链式开关关闭导致字段消失

手写 `dialer-proxy` 在 Proxies 增强里，源文件有、生成物没有。排查很久才定位到是 UI 某个开关的副作用。

教训：`dialer-proxy` 只走 Script 增强，且用探针法确认脚本执行。

### 案例 4：本机与另一台机器路径不同

照抄文档里的 `SOCK=/var/run/clash-verge-service/users/501/verge-mihomo.sock`，结果本机是 sidecar 模式，socket 在 `$TMPDIR`。所有命令都 `Resource not found`。

教训：路径和端口都必须探测，不能硬编码。这就是 `cvctl.py` 存在的原因。

### 案例 5：把「域名本身不通」误判成链路故障

某个站点的裸域直连也返回 000（DNS/证书本身有问题），但它的子域名 `docs.<site>` 正常 200。一度以为是代理链断了。

教训：先直连测一次，区分「站点本身问题」和「代理链路问题」。

### 案例 6：`PATCH /configs` 返回 204 但 TUN 没开

```bash
curl -X PATCH --unix-socket $SOCK http://localhost/configs \
  -d '{"tun":{"enable":true,"stack":"gvisor","mtu":1400}}' -w "%{http_code}"
# 204
```

再查 `/configs`，`tun.enable` 仍然是 `false`，只有 `stack` 和 `mtu` 变了。

根因：sidecar 模式内核以当前用户身份运行，macOS 建 utun 设备需要 root。API 接受参数并写回配置，但建接口那一步失败且不报错。

教训：`/configs` 的返回值是配置值，不代表能力可用。要连同 `ps -o user= -p $(pgrep -f verge-mihomo)` 一起看。

### 案例 7：装了服务还报 group id 不可用

```
Error: unable to resolve the invoking user's service group
Caused by: the invoking user's group id is unavailable; use sudo or pkexec
```

先试 `--gid 20`，报 `unknown installer argument: "--gid"`。翻 `strings` 才发现它读的是环境变量 `CLASH_VERGE_SERVICE_GID`，代码里写死为 `cd /; CLASH_VERGE_SERVICE_GID=<gid> <installer> --install-service`。

又试 `osascript -e 'do shell script "cd /; CLASH_VERGE_SERVICE_GID=20 /path/installer --install-service" with administrator privileges'`，报 AppleScript 语法错误 `预期是""，却找到未知的记号`。原因是 `do shell script` 里内嵌的未转义引号。

最终写成 `.sh` 包装脚本：

```sh
#!/bin/sh
cd /
export CLASH_VERGE_SERVICE_GID=20
exec "/Applications/Clash Verge.app/Contents/Resources/resources/clash-verge-service-install" --install-service
```

再 `osascript -e 'do shell script "/tmp/install_svc.sh" with administrator privileges'` 成功。

教训：App 自带的安装器不友好，别硬猜参数，`strings` 看它实际接受什么；AppleScript 拼复杂 shell 命令一律走脚本文件。

### 案例 8：切到 service 后脚本读不到内核

`cvctl.py status` 报「内核里没有 FAST-FRONT 组」，但 `grep` 生成物一切正常，`/proxies` 手工查也有组。

根因：`$TMPDIR/verge-mihomo.sock` 是 sidecar 时期留下的 socket 文件，切到 service 后文件没被清理，`os.path.exists` 为真，但连上去返回空。

教训：socket 探测必须探活（发 `/version` 看有无 JSON），不能只判断文件存在。同理，`Resource not found` 和「空响应」是两种完全不同的失败。

### 案例 9：`socks5_proxy` 写了但不生效

`~/.zshrc` 里原本写：

```sh
export socks5_proxy="socks5://127.0.0.1:7897"
```

看起来配了 socks5 代理，实际 curl / node / python 都不认这个变量名 —— 它不是标准。标准的是 `all_proxy`。

症状是「http/https 走代理正常，但某些只支持 socks 的工具连不上」，而且看环境变量会误以为配置了。

正确写法：

```sh
export http_proxy="http://127.0.0.1:7897"
export https_proxy="$http_proxy"
export all_proxy="socks5://127.0.0.1:7897"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export ALL_PROXY="$all_proxy"
```

教训：代理相关环境变量认 `http_proxy / https_proxy / all_proxy / no_proxy` 四个（大小写各一份），`socks5_proxy` 不是其中之一。
