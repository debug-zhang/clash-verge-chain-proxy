// Clash Verge 链式代理注入脚本（Script 增强）
//
// 安装位置：<配置目录>/profiles/<script-uid>.js，并在 profiles.yaml 里绑定
//   option:
//     script: <script-uid>
//
// 为什么必须写成 Script 增强：
//   Verge 在生成配置时会主动剥离 proxies 增强里的 dialer-proxy 字段，
//   只有 Script 增强注入的字段能进入最终生成物。详见 docs/mechanics.md。
//
// 需要改的只有 CHAIN：填第二跳（出口）节点的 name。
// CHAIN 里的节点必须在 proxies 增强（prepend）里定义过。

var FRONT_GROUP = 'FAST-FRONT';
var TEST_URL = 'http://cp.cloudflare.com/generate_204';
var INTERVAL = 300;
var TOLERANCE = 50;

// 订阅信息节点（剩余流量/套餐到期等）会伪装成真实节点，必须排除
var INFO_MARKERS = ['剩余', '重置', '到期', '官网', '：'];

// 需要走链式的出口节点。留空则脚本不做任何注入。
var CHAIN = ['Japan'];

function isInfoNode(name) {
  for (var i = 0; i < INFO_MARKERS.length; i++) {
    if (name.indexOf(INFO_MARKERS[i]) >= 0) return true;
  }
  return false;
}

// 组类型不是可拨号的真实节点，不能作为第一跳
function isGroupType(type) {
  return type === 'select' || type === 'selector' ||
         type === 'url-test' || type === 'urltest' ||
         type === 'fallback' || type === 'loadbalance' ||
         type === 'relay';
}

function main(config, profileName) {
  config = config || {};
  var proxies = config.proxies || [];
  var groups = config['proxy-groups'] || [];
  var i;

  if (CHAIN.length === 0) return config;

  // 收集可作为第一跳的普通节点
  var members = [];
  var seen = {};
  for (i = 0; i < proxies.length; i++) {
    var p = proxies[i];
    if (!p || !p.name || !p.type) continue;
    if (isGroupType(p.type)) continue;          // 组自己不能拨号
    if (seen[p.name]) continue;                 // 去重
    if (isInfoNode(p.name)) continue;           // 排除订阅信息伪装节点
    if (CHAIN.indexOf(p.name) >= 0) continue;   // 防止出口节点链自己
    seen[p.name] = true;
    members.push(p.name);
  }

  // 原地重建组：热重载时能拿到订阅更新后的新节点
  var found = false;
  for (i = 0; i < groups.length; i++) {
    if (groups[i] && groups[i].name === FRONT_GROUP) {
      groups[i].proxies = members;
      groups[i].type = 'url-test';
      groups[i].url = TEST_URL;
      groups[i].interval = INTERVAL;
      groups[i].tolerance = TOLERANCE;
      found = true;
      break;
    }
  }
  if (!found && members.length > 0) {
    groups.push({
      name: FRONT_GROUP,
      type: 'url-test',
      proxies: members,
      url: TEST_URL,
      interval: INTERVAL,
      tolerance: TOLERANCE
    });
  }
  config['proxy-groups'] = groups;

  if (members.length === 0) return config;

  // 给出口节点挂上第一跳
  for (i = 0; i < proxies.length; i++) {
    var node = proxies[i];
    if (!node || !node.name) continue;
    if (CHAIN.indexOf(node.name) < 0) continue;
    node['dialer-proxy'] = FRONT_GROUP;
  }

  return config;
}
