#!/usr/bin/env python3
"""cvctl — Clash Verge 链式代理运维工具（零第三方依赖）

自动探测配置目录、混合端口与内核控制接口，因此换机器不用改脚本。

用法:
    ./cvctl.py doctor             # 环境体检：目录/端口/socket/内核版本
    ./cvctl.py status             # 链式状态：生成物注入计数 + 内核 dialer-proxy
    ./cvctl.py verify             # 端到端：代理连通 + 出口 IP + 连接链
    ./cvctl.py regen              # 触发配置重新生成（重启 App，非重启内核）
    ./cvctl.py paths              # 打印本机路径与当前订阅绑定的增强文件
    ./cvctl.py apply              # 把 chain-proxy.js 写进已绑定的 Script 增强
    ./cvctl.py dns                # 检查 DNS 防泄露与 TUN 是否真的生效
    ./cvctl.py restore <file>     # 从备份恢复
    ./cvctl.py diagnose           # doctor + status + verify
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

APP_NAME = "Clash Verge"
BUNDLE_ID = "io.github.clash-verge-rev.clash-verge-rev"
APP_PATH = "/Applications/Clash Verge.app"
MIHOMO = os.path.join(APP_PATH, "Contents/MacOS/verge-mihomo")
RESOURCES = os.path.join(APP_PATH, "Contents/Resources/resources")


# ---------------------------------------------------------------- 基础工具


def config_dir():
    """Verge 配置目录"""
    override = os.environ.get("CVCTL_CONFIG_DIR")
    if override:
        return override
    return os.path.expanduser(
        "~/Library/Application Support/" + BUNDLE_ID
    )


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def top_level_scalar(text, key):
    """从 yaml 文本里取顶层标量（正则，够用）"""
    m = re.search(r"^%s:\s*(.+?)\s*$" % re.escape(key), text, re.M)
    return m.group(1).strip().strip("'\"") if m else None


def mixed_port():
    text = read_text(os.path.join(config_dir(), "config.yaml"))
    val = top_level_scalar(text, "mixed-port")
    return int(val) if val and val.isdigit() else 7890


def controller_socket():
    """内核 Unix socket 路径

    两种运行模式位置不同，且 config.yaml 里的 external-controller-unix
    在内核以 Service 模式重启后可能还残留 sidecar 时期的 TMPDIR 旧值，
    所以这里按「配置值 → 已知候选路径」依次探测，谁存在用谁。
    """
    text = read_text(os.path.join(config_dir(), "config.yaml"))
    candidates = []
    configured = top_level_scalar(text, "external-controller-unix")
    if configured:
        candidates.append(configured)
    # Service 模式：特权 helper 按 uid 目录放 socket
    candidates.append("/var/run/clash-verge-service/users/%d/verge-mihomo.sock"
                      % os.getuid())
    candidates.append("/var/run/clash-verge-service/verge-mihomo.sock")
    # Sidecar 模式：$TMPDIR 下
    candidates.append(os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                   "verge-mihomo.sock"))
    # 只判断 os.path.exists 会踩坑：切换运行模式后旧 socket 文件会残留，
    # 文件在但没进程监听，连上去只能拿到空响应。用一次 /version 探活。
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        out = subprocess.run(
            ["curl", "-s", "--max-time", "3", "--unix-socket", path,
             "http://localhost/version"],
            capture_output=True, text=True).stdout.strip()
        if out:
            return path
    return None


def controller_http_port():
    text = read_text(os.path.join(config_dir(), "config.yaml"))
    val = top_level_scalar(text, "external-controller")
    if not val:
        return None
    m = re.search(r":(\d+)$", val)
    return int(m.group(1)) if m else None


def tun_enabled():
    """内核是否开着 TUN。开了以后直连也走内核，直连/代理 IP 对比失去意义。"""
    sock = controller_socket()
    data = api_get("/configs", socket_path=sock)
    if not isinstance(data, dict):
        return False
    return bool(data.get("tun", {}).get("enable"))


def api_get(path, socket_path=None, http_port=None, timeout=5):
    """调内核 REST API，优先 Unix socket"""
    if socket_path:
        cmd = ["curl", "-s", "--max-time", str(timeout),
               "--unix-socket", socket_path, "http://localhost" + path]
    elif http_port:
        cmd = ["curl", "-s", "--max-time", str(timeout), "--noproxy", "*",
               "http://127.0.0.1:%d%s" % (http_port, path)]
    else:
        return None
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def api_put(path, payload, socket_path=None, timeout=5):
    cmd = ["curl", "-s", "-X", "PUT", "--max-time", str(timeout),
           "--unix-socket", socket_path, "http://localhost" + path,
           "-H", "Content-Type: application/json", "-d", json.dumps(payload),
           "-w", "%{http_code}"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def curl_proxy(url, proxy, timeout=20, headers=None):
    """返回 (http_code, time_total, body)"""
    cmd = ["curl", "-s", "--max-time", str(timeout),
           "-o", "/tmp/.cvctl_body", "-w", "%{http_code} %{time_total}"]
    if proxy:
        cmd += ["--proxy", proxy]
    else:
        cmd += ["--noproxy", "*"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    parts = out.split()
    code = parts[0] if parts else "000"
    elapsed = parts[1] if len(parts) > 1 else "?"
    body = read_text("/tmp/.cvctl_body")[:200]
    return code, elapsed, body


def ok(msg):
    print("  \033[32m✓\033[0m " + msg)


def bad(msg):
    print("  \033[31m✗\033[0m " + msg)


def warn(msg):
    print("  \033[33m!\033[0m " + msg)


def head(title):
    print("\n" + title)
    print("-" * len(title))


# ---------------------------------------------------------------- 各子命令


def cmd_doctor(_args):
    cfg = config_dir()
    head("环境")
    if os.path.isdir(cfg):
        ok("配置目录 %s" % cfg)
    else:
        bad("配置目录不存在: %s" % cfg)
        return 1

    if os.path.exists(APP_PATH):
        ver = subprocess.run(
            ["defaults", "read", os.path.join(APP_PATH, "Contents/Info.plist"),
             "CFBundleShortVersionString"],
            capture_output=True, text=True).stdout.strip()
        ok("Clash Verge %s" % (ver or "?"))
    else:
        bad("未安装 %s" % APP_PATH)

    if os.path.exists(MIHOMO):
        out = subprocess.run([MIHOMO, "-v"], capture_output=True, text=True).stdout
        ok((out.splitlines() or ["内核 ?"])[0])

    head("端口")
    port = mixed_port()
    with socket.socket() as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            ok("混合端口 %d 监听中" % port)
        else:
            bad("混合端口 %d 未监听" % port)

    head("内核控制接口")
    sock = controller_socket()
    if sock:
        ok("Unix socket %s" % sock)
    else:
        hport = controller_http_port()
        if hport:
            warn("无 socket，尝试 HTTP 127.0.0.1:%d" % hport)
        else:
            bad("找不到控制接口")

    ver = api_get("/version", sock, controller_http_port())
    if ver:
        ok("内核响应: %s" % ver.get("version", "?"))
    else:
        bad("内核无响应")

    head("进程")
    ps = subprocess.run(["pgrep", "-fl", "clash-verge|verge-mihomo"],
                        capture_output=True, text=True).stdout.strip()
    if ps:
        for line in ps.splitlines()[:4]:
            ok(line[:100])
    else:
        bad("Verge 未运行")
    return 0


def cmd_status(_args):
    cfg = config_dir()
    gen = os.path.join(cfg, "clash-verge.yaml")
    text = read_text(gen)

    head("生成物")
    if not text:
        bad("clash-verge.yaml 不存在，配置尚未生成")
        return 1

    n_dialer = text.count("dialer-proxy")
    n_front = text.count("FAST-FRONT")
    if n_dialer == 0:
        bad("dialer-proxy 计数 0 —— 链式未注入")
    else:
        ok("dialer-proxy ×%d" % n_dialer)
    if n_front == 0:
        bad("FAST-FRONT 计数 0 —— 第一跳组缺失")
    else:
        ok("FAST-FRONT ×%d" % n_front)

    # 出口节点实际写的 dialer-proxy
    for m in re.finditer(r"^-\s+name:\s*(\S+)\n(?:.*\n)*?\s+dialer-proxy:\s*(\S+)", text, re.M):
        ok("%s → dialer-proxy %s" % (m.group(1), m.group(2)))

    head("内核视图")
    sock = controller_socket()
    hport = controller_http_port()
    if not sock and not hport:
        warn("无法访问内核控制接口，跳过")
        return 0

    front = api_get("/proxies/FAST-FRONT", sock, hport)
    if front and "message" not in front:
        ok("FAST-FRONT now=%s type=%s members=%d"
           % (front.get("now"), front.get("type"), len(front.get("all") or [])))
    else:
        bad("内核里没有 FAST-FRONT 组")

    # 找出所有挂了 dialer-proxy 的节点
    allp = api_get("/proxies", sock, hport)
    if allp and "proxies" in allp:
        chained = [(n, d.get("dialer-proxy"), d.get("alive"))
                   for n, d in allp["proxies"].items() if d.get("dialer-proxy")]
        if chained:
            for name, dialer, alive in chained:
                ok("%s dialer=%s alive=%s" % (name, dialer, alive))
        else:
            bad("内核里没有任何节点带 dialer-proxy")

    head("入口组")
    for name, d in (allp or {}).get("proxies", {}).items():
        if d.get("type") == "Selector" and d.get("now"):
            now = d.get("now")
            if "/" not in name:
                print("    %s → %s" % (name, now))
    return 0


def cmd_verify(args):
    port = mixed_port()
    proxy = "http://127.0.0.1:%d" % port

    head("代理连通 (%s)" % proxy)
    code, elapsed, _ = curl_proxy("https://cp.cloudflare.com/", proxy)
    if code == "204":
        ok("cp.cloudflare.com 204 %ss" % elapsed)
    else:
        bad("cp.cloudflare.com %s %ss" % (code, elapsed))

    head("出口 IP")
    direct = curl_proxy("https://api.ipify.org", None, timeout=8)[2].strip()
    viaproxy = curl_proxy("https://api.ipify.org", proxy, timeout=20)[2].strip()
    print("    直连: %s" % (direct or "(不通，通常正常)"))
    print("    代理: %s" % (viaproxy or "(失败)"))
    if not viaproxy:
        bad("代理出口 IP 获取失败")
    elif tun_enabled():
        # TUN 开了以后「直连」也被内核接管，两者必然一致，对比没有意义
        ok("出口 %s（TUN 已开启，直连同样被接管，对比不适用）" % viaproxy)
    elif direct and viaproxy == direct:
        warn("出口与直连一致，可能命中 DIRECT 规则")
    elif not direct:
        ok("出口 %s（直连不通，无法对比，以连接链为准）" % viaproxy)
    else:
        ok("出口 %s，与直连 %s 不同，代理生效" % (viaproxy, direct))

    head("连接链")
    sock = controller_socket()
    hport = controller_http_port()
    if sock or hport:
        conns = api_get("/connections", sock, hport) or {}
        seen = set()
        for c in (conns.get("connections") or []):
            host = c.get("metadata", {}).get("host", "")
            chain = " > ".join(c.get("chains") or [])
            if chain and (host, chain) not in seen:
                seen.add((host, chain))
                print("    %-32s %s" % (host[:32], chain))
            if len(seen) >= 8:
                break
        if not seen:
            warn("无活跃连接，先跑一次 curl 再试")
    return 0


def cmd_regen(_args):
    head("重新生成配置")
    print("    原因：改脚本/增强 yaml 后必须触发一次配置生成，重启内核无效")
    subprocess.run(["osascript", "-e", 'quit app "%s"' % APP_NAME],
                   capture_output=True)
    for _ in range(20):
        time.sleep(0.5)
        if not subprocess.run(["pgrep", "-f", "MacOS/clash-verge"],
                              capture_output=True).stdout.strip():
            break
    ok("已退出")
    subprocess.run(["open", "-a", APP_NAME], capture_output=True)
    print("    等待启动与验证...")
    log = os.path.join(config_dir(), "logs", "latest.log")
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        tail = read_text(log)[-4000:]
        if "验证成功" in tail:
            ok("配置生成并验证成功")
            return cmd_status([])
        if "验证失败" in tail:
            bad("配置验证失败，查看 %s" % log)
            return 1
    warn("等待超时，手动检查 %s" % log)
    return 1


def cmd_restore(args):
    if not args:
        bad("用法: cvctl.py restore <备份文件>")
        return 1
    src = args[0]
    if not os.path.exists(src):
        bad("不存在: %s" % src)
        return 1
    name = os.path.basename(src)
    # 备份名形如 xxx.yaml.1795623300.bak，还原成 xxx.yaml
    target = re.sub(r"\.[0-9]{10}\.bak$", "", name)
    dst = os.path.join(config_dir(), "profiles", target)
    shutil.copy2(src, dst)
    ok("%s → %s" % (src, dst))
    print("    记得执行 cvctl.py regen 让改动生效")
    return 0


def profile_items(text):
    """解析 profiles.yaml 的 items，返回 {uid: {type, file, name}}"""
    items = {}
    for m in re.finditer(r"^- uid:\s*(\S+)\s*$((?:\n(?!- uid:).*)*)", text, re.M):
        uid, body = m.group(1), m.group(2)
        def pick(k):
            mm = re.search(r"^\s+%s:\s*(.+?)\s*$" % k, body, re.M)
            return mm.group(1).strip().strip("'\"") if mm else None
        items[uid] = {"type": pick("type"), "file": pick("file"),
                      "name": pick("name")}
    return items


def current_option(text):
    """返回 (current_uid, {option_key: uid})"""
    current = top_level_scalar(text, "current")
    if not current:
        return None, {}
    m = re.search(r"^- uid:\s*%s\s*$((?:\n(?!- uid:).*)*)"
                  % re.escape(current), text, re.M)
    if not m:
        return current, {}
    body = m.group(1)
    # option: 的缩进，其子键缩进更深
    om = re.search(r"^(\s+)option:\s*$", body, re.M)
    opts = {}
    if om:
        base = len(om.group(1))
        for line in body[om.end():].splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base:
                break
            mm = re.match(r"\s+(\w+):\s*(\S+)\s*$", line)
            if mm:
                opts[mm.group(1)] = mm.group(2).strip("'\"")
    return current, opts


def bound_files():
    """当前订阅绑定的增强文件绝对路径"""
    cfg = config_dir()
    text = read_text(os.path.join(cfg, "profiles.yaml"))
    current, opts = current_option(text)
    items = profile_items(text)
    out = {}
    for role in ("script", "proxies", "rules", "merge", "groups"):
        uid = opts.get(role)
        if not uid:
            continue
        info = items.get(uid) or {}
        fname = info.get("file") or (uid + (".js" if role == "script" else ".yaml"))
        out[role] = os.path.join(cfg, "profiles", fname)
    return current, out


def cmd_paths(_args):
    cfg = config_dir()
    head("本机路径")
    print("    配置目录   %s" % cfg)
    print("    生成物     %s" % os.path.join(cfg, "clash-verge.yaml"))
    print("    混合端口   %d" % mixed_port())
    print("    内核 socket %s" % (controller_socket() or "(未找到)"))

    current, files = bound_files()
    head("当前订阅 %s 绑定的增强" % (current or "?"))
    if not files:
        bad("没有绑定任何增强，先在 Verge UI 里创建 Script / Proxies / Rules 增强")
        return 1
    for role in ("script", "proxies", "rules", "merge", "groups"):
        if role in files:
            ok("%-8s %s" % (role, files[role]))
    print("\n    Script 增强文件就是要改成 chain-proxy.js 的那一个。")
    return 0


def cmd_apply(args):
    """把 chain-proxy.js 的内容写进当前订阅绑定的 Script 增强文件

    不新建 uid、不改 profiles.yaml —— 只原地替换已经存在的、Verge 认识的脚本文件。
    """
    cfg = config_dir()
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "chain-proxy.js")
    if not os.path.exists(src):
        bad("缺少 chain-proxy.js")
        return 1
    payload = read_text(src)

    force = "--force" in args
    current, files = bound_files()
    if "script" not in files:
        bad("当前订阅没有绑定 Script 增强")
        print("    先在 Verge UI：订阅页 → 当前配置 → 右键「编辑 Script」创建，再重跑本命令")
        return 1

    dst = files["script"]
    old = read_text(dst)
    head("目标 Script 增强")
    print("    %s" % dst)

    if "FAST-FRONT" in old and not force:
        warn("已经含 FAST-FRONT，看起来装过了。跳过（--force 覆盖）")
        return 0

    backup = "%s.%d.bak" % (dst, int(time.time()))
    shutil.copy2(dst, backup)
    ok("备份 %s" % os.path.basename(backup))

    with open(dst, "w", encoding="utf-8") as f:
        f.write(payload)
    ok("已写入 %d 字节" % len(payload))

    _, files = bound_files()
    head("其余需要你手填的两个文件")
    for role, hint in (("proxies", "出口节点 server/port/username/password"),
                       ("rules", "需要强制直连的域名（可留空）")):
        path = files.get(role)
        if path:
            ok("%-8s %s  ← %s" % (role, path, hint))
        else:
            warn("%s 增强未绑定，对应功能不生效" % role)

    print("\n    改完执行: ./cvctl.py regen && ./cvctl.py verify")
    return 0


def cmd_diagnose(args):
    rc = cmd_doctor(args)
    cmd_status(args)
    cmd_verify(args)
    cmd_dns(args)
    return rc


def dns_runtime():
    """内核运行时 dns 段。返回 dict 或 None。

    mihomo 1.19 起 /configs 不再返回 dns 段（实测 keys 里没有 dns，值为 null），
    所以这里读不到不代表 DNS 没生效。判断是否真的接管 DNS 用 dns_active()。
    """
    data = api_get("/configs", socket_path=controller_socket())
    if not isinstance(data, dict):
        return None
    return data.get("dns")


def dns_active():
    """实测 DNS 是否被内核接管：查一个必走代理的域名，看是否返回 fake-ip。

    比读配置可靠 —— 配置写了不等于生效，开关状态、TUN 劫持、覆写优先级
    任何一个环节出问题，表现都是「配置看着对但解析没接管」。
    """
    fake_net = "198.18."
    out = subprocess.run(["dig", "+short", "+time=4", "+tries=1",
                          "www.google.com"],
                         capture_output=True, text=True).stdout.split()
    return any(ip.startswith(fake_net) for ip in out), out


def tun_runtime():
    data = api_get("/configs", socket_path=controller_socket())
    if not isinstance(data, dict):
        return {}
    return data.get("tun") or {}


def read_yaml_dns_block():
    """从 dns_config.yaml 文本里粗略读 dns 段（零依赖，不 import yaml）。

    只取本工具做检查需要的几个键，够用即可，不追求完整 YAML 解析。
    """
    text = read_text(os.path.join(config_dir(), "dns_config.yaml"))
    if not text:
        return {}
    out = {}
    for key in ("nameserver", "proxy-server-nameserver", "nameserver-policy"):
        m = re.search(r"^  %s:\s*$((?:\n(?:    .*|\s*$))*)"
                      % re.escape(key), text, re.M)
        if not m:
            continue
        body = m.group(1)
        if key == "nameserver-policy":
            pairs = re.findall(r"^    '?([^':]+)'?:\s*(.+)$", body, re.M)
            out[key] = dict((k, v) for k, v in pairs)
        else:
            out[key] = re.findall(r"^    -\s*(.+)$", body, re.M)
    return out


def service_running():
    """内核是否以 root 常驻（service 模式）。TUN 能否开启取决于这个。"""
    out = subprocess.run(["pgrep", "-f", "verge-mihomo"],
                         capture_output=True, text=True).stdout.split()
    if not out:
        return None
    user = subprocess.run(["ps", "-o", "user=", "-p", out[0]],
                          capture_output=True, text=True).stdout.strip()
    return user


def cmd_dns(_args):
    """检查 DNS 是否防泄露、TUN 是否真的开了"""
    cfg = config_dir()

    head("内核运行模式")
    user = service_running()
    if user is None:
        bad("内核没在跑")
        return 1
    print("    内核进程用户: %s" % user)
    tun = tun_runtime()
    tun_on = bool(tun.get("enable"))
    if user == "root":
        ok("service 模式（root），具备创建 utun 的权限")
    elif tun_on:
        warn("非 root 但 TUN 却开着，配置可能来自上次 service 运行")
    else:
        warn("sidecar 模式（%s），无法创建 utun —— TUN 会「返回 204 但不生效」" % user)
        print("    要开 TUN，需装特权 helper，见 docs/mechanics.md")

    head("TUN")
    if tun_on:
        ok("enable=true stack=%s mtu=%s device=%s"
           % (tun.get("stack"), tun.get("mtu"), tun.get("device") or "-"))
    else:
        warn("enable=false")
    print("    dns-hijack: %s" % (tun.get("dns-hijack") or "-"))

    head("DNS 接管状态（实测）")
    taken, answers = dns_active()
    if taken:
        ok("解析返回 fake-ip（%s），内核已接管 DNS" % answers[0])
    else:
        bad("解析结果不是 fake-ip 段: %s" % (", ".join(answers) or "无响应"))
        print("    DNS 没被接管，解析仍走系统解析器 → 会泄露")
        return 1

    dns = dns_runtime()
    if not dns:
        print("    (/configs 不返回 dns 段，mihomo 1.19 起如此，改为读配置文件)")
        dns = read_yaml_dns_block()

    ns = dns.get("nameserver") or []
    print("    nameserver:")
    plain = []
    for one in ns:
        marked = "#" in str(one)
        print("      %s %s" % ("✓" if marked else "!", one))
        if not marked:
            plain.append(one)
    if plain:
        warn("%d 条 nameserver 没有 #组名，DoH 会直连出去（国内通常被墙）"
             % len(plain))
    else:
        ok("全部 nameserver 都经代理组发出")

    psn = dns.get("proxy-server-nameserver") or []
    if not psn:
        warn("proxy-server-nameserver 为空，解析代理节点域名可能循环依赖")
    elif any("#" in str(x) for x in psn):
        bad("proxy-server-nameserver 不能带 #组名，否则解析节点域名时要先有代理")
    else:
        ok("proxy-server-nameserver 直连: %d 条" % len(psn))

    policy = dns.get("nameserver-policy") or {}
    bad_policy = [k for k, v in policy.items()
                  if "dns.cloudflare.com" in str(v)]
    if bad_policy:
        bad("%d 条 policy 指向 dns.cloudflare.com（直连被墙/走代理 400）"
            % len(bad_policy))
        for k in bad_policy[:5]:
            print("      %s" % k)
    else:
        ok("nameserver-policy 无 dns.cloudflare.com")

    head("生效性")
    print("    TUN 已接管全部流量，直连与代理出口必然一致，对比无意义。")
    if tun_on:
        ok("DNS 劫持 + 内核 DNS 同时生效，解析不再走系统解析器")
    else:
        warn("没有 TUN 劫持，系统仍可能绕过内核问路由器")
    print("\n    端到端验证: dig +short TXT whoami.ds.akahelp.net @198.18.0.1")
    print("    看 ecs 是否为出口 IP 所在网段")
    return 0


COMMANDS = {
    "doctor": cmd_doctor,
    "status": cmd_status,
    "verify": cmd_verify,
    "regen": cmd_regen,
    "paths": cmd_paths,
    "apply": cmd_apply,
    "dns": cmd_dns,
    "restore": cmd_restore,
    "diagnose": cmd_diagnose,
}


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = COMMANDS.get(argv[1])
    if not cmd:
        print("未知命令: %s\n" % argv[1] + __doc__)
        return 1
    return cmd(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
