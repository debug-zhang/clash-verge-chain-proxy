# AGENTS

本仓库是 Clash Verge 链式代理的配置方案与运维工具。以下约束在修改代码或文档时必须遵守。

## 最高约束

- 复杂任务优先用 Python 脚本驱动。`cvctl.py` 就是这条约束的产物：能用脚本做的检查，不要写成需要人工复制粘贴的命令序列。
- 默认简体中文回答；中文与英文/数字之间留一个空格（代码块、命令、路径、标识符除外）。

## 领域约束（改配置前必读）

### `dialer-proxy` 只能由 Script 增强注入

写在 Proxies 增强（`profiles/<uid>.yaml`）里的 `dialer-proxy` 会在配置生成阶段被 Verge 剥离。生成物 `clash-verge.yaml` 里计数为 0，但 `type/server/port/username/password` 全部正常 —— 这是专门过滤，不是解析 bug。

改动任何与链式相关的字段，只能落在 `chain-proxy.js`（Script 增强）。不要为了「双保险」在两处都写，那样每次重新生成谁覆盖谁不确定。

### 路径和端口必须探测，禁止硬编码

已知会变的量：

| 量 | 变化来源 | 读取方式 |
|---|---|---|
| 配置目录 | bundle id 可能变 | `config_dir()` |
| 混合端口 | 用户可改，见过 7890、7897 | `config.yaml` 的 `mixed-port` |
| 内核 socket | sidecar 在 `$TMPDIR`，service 在 `/var/run/...`，`$TMPDIR` 每机不同 | `config.yaml` 的 `external-controller-unix`，兜底 `$TMPDIR` |
| 出口节点/名称 | 用户自己的服务 | `chain-proxy.js` 的 `CHAIN` |

新加代码一律走 `cvctl.py` 里的 `config_dir() / mixed_port() / controller_socket()`，不要在别处重复实现或写死。

### 改完必须触发配置重新生成

重启内核**不会**重新生成配置，它只是用已有的 `clash-verge.yaml` 重拉进程。必须走：

```
订阅更新 / 手动保存 profile / 退出重开 App
```

日志出现 `[Validate] 验证成功` 才算生效。`cvctl.py regen` 封装了这个流程并等日志。

### 脚本保持 ES5

Verge 用 Boa 引擎跑增强脚本。只用 `var`、函数声明、`indexOf`、普通对象/数组。不要引入 `const` / `let` / 箭头函数 / 模板字符串 / 可选链 —— 兼容性有风险。

### DNS / TUN 相关（改这块前必读）

- **`/configs` 不再返回 `dns` 段**（mihomo 1.19 起）。判断 DNS 是否生效必须实测（查域名看是否返回 fake-ip），不能读接口或读 yaml 就下结论。
- `verge.yaml` 的 `enable_dns_settings: true` 时，`dns_config.yaml` **覆盖** profile 的 `dns` 段。两处写不一样时实际生效的是 `dns_config.yaml`，只改 Merge 模板会看起来「改了没生效」。
- `nameserver` 必须带 `#代理组名`，否则 DoH 直连（国内被墙）。`proxy-server-nameserver` 则**不能**带 `#组名`，否则解析代理节点域名时循环依赖。
- 订阅自带的 `nameserver-policy` 常指向 `https://dns.cloudflare.com/dns-query`：国内直连被 reset、走代理返回 400，会让域名解析彻底失败。必须在自己模板里用同名 key 覆盖。
- **开 TUN 必须有 root**。sidecar 模式下 `PATCH /configs {"tun":{"enable":true}}` 返回 204 但 `tun.enable` 仍是 false —— 请求被接受、内核没权限建 utun。这种「成功但没生效」是最容易误判的坑，判断要看 `ps -o user= -p $(pgrep -f verge-mihomo)`。
- 装特权 helper 必须写成 `.sh` 包装脚本再用 `osascript ... with administrator privileges` 调用。直接把 `VAR=val /path/installer` 塞进 `do shell script` 会报 AppleScript 语法错误；`--gid` 不是合法参数，它只读环境变量 `CLASH_VERGE_SERVICE_GID`。
- 内核 socket 探测**必须探活**（发 `/version` 看有无 JSON）。切换 sidecar→service 后 `$TMPDIR` 下的旧 socket 文件不会被清理，`os.path.exists` 为真但无人监听。
- 本机（个人电脑）**没有内网 DNS 需求**，不要照搬企业环境的 `nameserver-policy` 内网域名与 `30.0.0.0/8` 排除项。

## 代码约束

### `cvctl.py`

- 零第三方依赖，只用标准库。目标机器可能没有 PyYAML（已验证过会 `ModuleNotFoundError`），所以 YAML 一律用正则从文本读取，不要 `import yaml`。
- 新子命令在 `COMMANDS` 字典注册，并在文件顶部 docstring 的用法列表里补一行。
- 输出用 `ok() / bad() / warn()` 三个函数，保持终端一致。
- 任何写文件的子命令必须先备份，备份名格式 `<name>.<unix_ts>.bak`（`restore` 依赖这个格式反解析）。
- **不要新建 profile uid，也不要自动改写 `profiles.yaml` 的 `option` 绑定。** Verge 的 profile 元数据由 UI 管理，脚本伪造 uid 可能不被识别，覆盖已有绑定会破坏用户已跑通的配置。`apply` 的语义是「往当前已绑定的 Script 增强文件里写内容」，仅此而已。
- 修改用户配置文件前必须 `apply` 式的先判断「是否已装过」，避免重复覆盖（用特征字符串如 `FAST-FRONT` 判断，配 `--force` 逃生口）。

### `chain-proxy.js`

- 三重排除不可去掉：组类型、`CHAIN` 自身（防环）、订阅信息节点（`INFO_MARKERS`）。
- 建组逻辑必须「原地重建」已存在的同名组，而不是新增。热重载时才能拿到订阅更新后的新节点。
- 不要给出口节点写死第一跳节点名，永远指向 `FAST-FRONT` 组。

### 模板文件（`profiles/*.example.yaml`）

- 只放占位符（`REPLACE_ME`、`203.0.113.10` 文档保留地址），绝不提交真实 IP、账号、密码、订阅链接。
- 出口节点模板里**不能**出现 `dialer-proxy`，这是刻意留空。

## 文档约束

- `README.md` 面向第一次接触的人：问题是什么、为什么需要这个仓库、快速开始、如何验证。
- `docs/mechanics.md` 记录「为什么」和证据（逐字段比对、探针法）。任何关于 Verge 行为的断言都要附带可复现的验证方式。
- `docs/troubleshooting.md` 用现象 → 原因 → 处理的表格，案例部分记录真实踩坑，不要编造。
- Verge 升级后如果行为变化，先更新 `docs/mechanics.md` 的证据，再改代码。

## 验证要求

改动后至少跑：

```bash
node --check chain-proxy.js       # 脚本语法
python3 -m py_compile cvctl.py    # 工具语法
./cvctl.py doctor                 # 环境可读
./cvctl.py status                 # 注入生效
./cvctl.py verify                 # 端到端连通
./cvctl.py dns                    # DNS 接管 + TUN 真的开了
```

无法验证时明确说明缺口，不要假装通过。

## 提交约定

- 一个可验证的原子改动一个提交。
- 模板文件只放占位符；真实出口凭据、订阅链接、真实出口 IP 一律不进仓库（`README.md`、`docs/` 里出现过的也要替换成 `203.0.113.x` 文档保留地址）。
- 同样**不要出现真实的业务域名与内网标识**：公司/内网域名、个人或团队的私有 API 域名、真实站点域名（直连规则里的、案例里的），全部换成 `example-internal.com` / `example-cdn.com` / `<site>` 这类占位符。私网 IP 用文档保留段（`192.0.2.x`、`198.51.100.x`）。
- **写约束本身也不要举真实域名当例子** —— 那等于把要保护的内容又提交进去一次。用「某公司域名」「私有 API 域名」这类描述。
- 提交前跑一遍 `git diff --cached` 人眼过一遍域名和 IP。
- 保留并避开无关改动。
- 提交信息用中文，描述「改了什么 + 为什么」。
- 提交前 `git status` 确认没有把 `profiles.yaml`、真实出口凭据、订阅链接带进来。

## 环境相关

- `gh` 用 `~/.local/bin/gh`（本机安装在 `~/.local/bin`，不在 PATH 里需显式调用）。
- 本仓库 git 走 SSH，但默认 ssh-agent 没有加载 GitHub key。推送需显式指定：
  `GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_github_debug_zhang -o IdentitiesOnly=yes" git push`
