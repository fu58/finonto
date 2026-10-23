"""内置领域包：计算机网络（寻址—转发—链路—服务—安全—运维）。

以 RFC / IEEE 802 与等保要求为口径，把设备、地址、策略与故障事件纳入同一本体，
适合做 CMDB、网络运维知识图谱与配置合规校验的概念底座。
"""

from __future__ import annotations

from .base import DomainPack, SampleDoc

SPACE_NAME = "计算机网络与运维本体域"

OBJECT_TYPES: list[dict] = [
    {
        "label": "网络节点",
        "label_en": "NetworkNode",
        "kind": "class",
        "abstract": True,
        "business_domain": "物理与链路",
        "definition": "在网络中可寻址、可转发或可终结数据报文的端点与中间设备统称。",
        "tags": ["抽象类", "节点"],
        "properties": [
            {"label": "节点标识", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True},
            {"label": "节点名称", "datatype": "string", "required": True},
            {"label": "管理地址", "datatype": "identifier"},
        ],
    },
    {
        "label": "主机",
        "label_en": "Host",
        "parent_label": "网络节点",
        "kind": "entity",
        "business_domain": "物理与链路",
        "aliases": ["服务器", "终端"],
        "definition": "运行操作系统并向用户提供计算或应用服务的网络节点，是业务系统的承载体。",
        "properties": [
            {"label": "主机名", "datatype": "string", "required": True, "unique": True, "key_identifier": True},
            {"label": "操作系统", "datatype": "string"},
            {"label": "所属业务系统", "datatype": "string"},
            {"label": "上线日期", "datatype": "date"},
            {"label": "主机状态", "datatype": "enum", "required": True},
        ],
        "enumerations": [
            {"label": "主机状态", "members": ["在线", "离线", "维护中", "隔离", "已下线"]}
        ],
    },
    {
        "label": "网络设备",
        "label_en": "NetworkDevice",
        "parent_label": "网络节点",
        "kind": "entity",
        "business_domain": "物理与链路",
        "aliases": ["路由器", "交换机", "防火墙"],
        "definition": "承担报文转发、交换、过滤或地址转换等网络功能的专用设备。",
        "annotations": {
            "data_classification": "N2",
            "lifecycle": ["上架", "配置", "在网", "降级", "割接", "退役"],
            "standard_refs": ["GB/T 22239 网络安全等级保护基本要求"],
        },
        "properties": [
            {"label": "设备名称", "datatype": "string", "required": True, "unique": True, "key_identifier": True},
            {"label": "设备类型", "datatype": "enum", "required": True},
            {"label": "厂商型号", "datatype": "string"},
            {"label": "软件版本", "datatype": "string", "required": True},
            {"label": "序列号", "datatype": "identifier", "unique": True},
            {"label": "机房位置", "datatype": "string"},
        ],
        "enumerations": [
            {"label": "设备类型", "members": ["路由器", "交换机", "防火墙", "负载均衡", "无线控制器", "专线终端", "代理服务器"]}
        ],
        "constraints": [
            {"name": "在网设备版本须受支持", "kind": "regulatory", "expression": "设备状态=在网 => 软件版本不在厂商终止支持清单", "severity": "violation"},
        ],
    },
    {
        "label": "网络接口",
        "label_en": "NetworkInterface",
        "kind": "entity",
        "business_domain": "物理与链路",
        "aliases": ["端口", "网卡"],
        "definition": "主机或设备上承载链路收发的逻辑端口，具有速率、双工与管理/运行状态。",
        "properties": [
            {"label": "接口标识", "datatype": "identifier", "required": True, "unique": True},
            {"label": "接口名称", "datatype": "string", "required": True},
            {"label": "标称速率", "datatype": "decimal", "unit": "Mbps", "required": True},
            {"label": "管理状态", "datatype": "enum", "required": True},
            {"label": "运行状态", "datatype": "enum", "required": True},
            {"label": "最大传输单元", "datatype": "integer", "unit": "byte"},
        ],
        "enumerations": [
            {"label": "管理状态", "members": ["启用", "关闭"]},
            {"label": "运行状态", "members": ["up", "down", "testing", "协商中"]},
        ],
        "constraints": [
            {"name": "运行状态不得高于管理状态", "kind": "consistency", "expression": "管理状态=关闭 => 运行状态 = down", "severity": "violation"},
        ],
    },
    {
        "label": "网络链路",
        "label_en": "NetworkLink",
        "kind": "entity",
        "business_domain": "物理与链路",
        "aliases": ["电路", "专线"],
        "definition": "两个接口之间的物理或逻辑连通，具有带宽、介质与运营商等属性。",
        "properties": [
            {"label": "链路编号", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True},
            {"label": "链路类型", "datatype": "enum", "required": True},
            {"label": "契约带宽", "datatype": "decimal", "unit": "Mbps"},
            {"label": "运营商", "datatype": "string"},
            {"label": "冗余方式", "datatype": "enum"},
        ],
        "enumerations": [
            {"label": "链路类型", "members": ["光纤", "双绞线", "微波", "互联网专线", "MPLS 专线", "Overlay 隧道"]},
            {"label": "冗余方式", "members": ["无冗余", "主备", "双活", "链路聚合"]},
        ],
    },
    {
        "label": "网络分段",
        "label_en": "NetworkSegment",
        "kind": "class",
        "abstract": True,
        "business_domain": "IP 与路由",
        "definition": "按广播域、地址平面或安全域划分的网络范围，是策略与故障影响面的边界单元。",
        "properties": [
            {"label": "分段名称", "datatype": "string", "required": True, "unique": True},
            {"label": "安全域", "datatype": "enum", "required": True},
            {"label": "用途", "datatype": "enum"},
        ],
        "enumerations": [
            {"label": "安全域", "members": ["互联网区", "DMZ", "核心业务区", "办公区", "运维管理区", "测试区", "灾备区"]}
        ],
    },
    {
        "label": "子网",
        "label_en": "Subnet",
        "parent_label": "网络分段",
        "kind": "entity",
        "business_domain": "IP 与路由",
        "aliases": ["网段", "CIDR 块"],
        "definition": "由网络前缀与掩码界定的连续地址空间，承载一组主机的地址分配与网关归属。",
        "properties": [
            {"label": "子网前缀", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True, "pattern": "^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$"},
            {"label": "网关地址", "datatype": "identifier", "required": True},
            {"label": "可用地址数", "datatype": "integer"},
            {"label": "已分配地址数", "datatype": "integer"},
            {"label": "VLAN 编号", "datatype": "integer"},
        ],
        "constraints": [
            {"name": "地址使用率告警", "kind": "range", "expression": "已分配地址数 / 可用地址数 <= 85%", "severity": "warning"},
            {"name": "网关须在子网内", "kind": "consistency", "expression": "网关地址 ∈ 子网前缀", "severity": "violation"},
        ],
    },
    {
        "label": "IP 地址",
        "label_en": "IPAddress",
        "kind": "entity",
        "business_domain": "IP 与路由",
        "aliases": ["地址", "IPv4 地址", "IPv6 地址"],
        "definition": "在网络层唯一标识接口或服务的地址，具有协议版本、分配方式与租期。",
        "properties": [
            {"label": "地址值", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True, "pattern": "^([0-9]{1,3}\\.){3}[0-9]{1,3}$"},
            {"label": "协议版本", "datatype": "enum", "required": True},
            {"label": "分配方式", "datatype": "enum", "required": True},
            {"label": "租期", "datatype": "duration"},
            {"label": "是否公网地址", "datatype": "boolean", "required": True},
        ],
        "enumerations": [
            {"label": "协议版本", "members": ["IPv4", "IPv6"]},
            {"label": "分配方式", "members": ["静态绑定", "DHCP 动态", "自动配置", "弹性地址"]},
        ],
        "constraints": [
            {"name": "地址唯一性", "kind": "unique", "expression": "同一广播域内地址不得重复", "severity": "violation"},
            {"name": "公网地址须登记用途", "kind": "regulatory", "expression": "是否公网地址=真 => 用途与开放端口 必填", "severity": "violation", "reg_ref": "GB/T 22239 网络安全等级保护基本要求"},
        ],
    },
    {
        "label": "MAC 地址",
        "label_en": "MacAddress",
        "kind": "entity",
        "business_domain": "物理与链路",
        "definition": "烧录在网卡上的二层硬件地址，用于同一广播域内的帧寻址与地址绑定。",
        "properties": [
            {"label": "地址值", "datatype": "identifier", "required": True, "unique": True, "pattern": "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"},
            {"label": "厂商前缀", "datatype": "string"},
        ],
    },
    {
        "label": "路由",
        "label_en": "Route",
        "kind": "entity",
        "business_domain": "IP 与路由",
        "aliases": ["路由条目", "前缀"],
        "definition": "设备依据目的前缀转发报文的规则，包含下一跳、出接口、优先级与来源。",
        "properties": [
            {"label": "目的前缀", "datatype": "identifier", "required": True},
            {"label": "下一跳", "datatype": "identifier", "required": True},
            {"label": "出接口", "datatype": "string"},
            {"label": "管理距离", "datatype": "integer"},
            {"label": "路由来源", "datatype": "enum", "required": True},
        ],
        "enumerations": [
            {"label": "路由来源", "members": ["直连", "静态", "OSPF", "BGP", "IS-IS", "RIP", "默认路由"]}
        ],
        "constraints": [
            {"name": "默认路由唯一", "kind": "cardinality", "expression": "每台设备默认路由 <= 2 条", "severity": "warning"},
        ],
    },
    {
        "label": "路由协议",
        "label_en": "RoutingProtocol",
        "kind": "attribute_domain",
        "business_domain": "IP 与路由",
        "definition": "网络设备之间交换可达性信息并计算路由的协议规范，如 OSPF、BGP、IS-IS。",
        "properties": [
            {"label": "协议名称", "datatype": "string", "required": True, "unique": True},
            {"label": "协议类型", "datatype": "enum", "required": True},
            {"label": "度量值依据", "datatype": "string"},
            {"label": "规范出处", "datatype": "string"},
        ],
        "enumerations": [
            {"label": "协议类型", "members": ["链路状态", "距离矢量", "路径矢量"]}
        ],
    },
    {
        "label": "VLAN",
        "label_en": "VirtualLocalAreaNetwork",
        "kind": "entity",
        "business_domain": "交换与转发",
        "aliases": ["虚拟局域网", "广播域"],
        "definition": "在二层逻辑上隔离出的广播域，用 VLAN 标识把端口划分到不同的分段。",
        "properties": [
            {"label": "VLAN 编号", "datatype": "integer", "required": True, "unique": True, "key_identifier": True},
            {"label": "VLAN 名称", "datatype": "string", "required": True},
            {"label": "成员端口数", "datatype": "integer"},
            {"label": "是否 Trunk", "datatype": "boolean", "required": True},
        ],
        "constraints": [
            {"name": "VLAN 编号范围", "kind": "range", "expression": "1 <= VLAN 编号 <= 4094", "severity": "violation", "reg_ref": "IEEE 802.1Q"},
        ],
    },
    {
        "label": "域名",
        "label_en": "DomainName",
        "kind": "entity",
        "business_domain": "域名与地址",
        "definition": "用于标识服务或组织的分层可读名称，是 DNS 解析与证书绑定的主体。",
        "properties": [
            {"label": "域名", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True, "pattern": "^[a-zA-Z0-9._-]+$"},
            {"label": "注册日期", "datatype": "date"},
            {"label": "到期日期", "datatype": "date", "required": True},
            {"label": "备案状态", "datatype": "enum"},
        ],
        "enumerations": [
            {"label": "备案状态", "members": ["已备案", "备案中", "无需备案", "已注销"]}
        ],
        "constraints": [
            {"name": "域名到期提醒", "kind": "range", "expression": "到期日期 - 当前日期 >= 30 天", "severity": "warning"},
        ],
    },
    {
        "label": "DNS 记录",
        "label_en": "DNSRecord",
        "kind": "document",
        "business_domain": "域名与地址",
        "definition": "把域名映射到地址、邮件服务器或其他域名的解析条目，具有类型、TTL 与优先级。",
        "properties": [
            {"label": "记录值", "datatype": "string", "required": True},
            {"label": "记录类型", "datatype": "enum", "required": True},
            {"label": "TTL", "datatype": "duration", "required": True},
            {"label": "优先级", "datatype": "integer"},
        ],
        "enumerations": [
            {"label": "记录类型", "members": ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "PTR", "SRV"]}
        ],
    },
    {
        "label": "服务端口",
        "label_en": "ServicePort",
        "kind": "entity",
        "business_domain": "应用服务",
        "aliases": ["监听端口"],
        "definition": "主机上由某一进程监听、对外提供协议服务的传输层端点。",
        "properties": [
            {"label": "端口号", "datatype": "integer", "required": True, "key_identifier": True},
            {"label": "传输协议", "datatype": "enum", "required": True},
            {"label": "应用协议", "datatype": "string", "required": True},
            {"label": "暴露范围", "datatype": "enum", "required": True},
            {"label": "所属进程", "datatype": "string"},
        ],
        "enumerations": [
            {"label": "传输协议", "members": ["TCP", "UDP", "SCTP"]},
            {"label": "暴露范围", "members": ["仅本机", "内网", "跨安全域", "互联网"]},
        ],
        "constraints": [
            {"name": "端口号范围", "kind": "range", "expression": "1 <= 端口号 <= 65535", "severity": "violation"},
            {"name": "高危端口须有访问控制策略", "kind": "codependency", "expression": "暴露范围=互联网 => 访问控制策略 必填", "severity": "violation", "reg_ref": "GB/T 22239 网络安全等级保护基本要求"},
        ],
    },
    {
        "label": "访问控制策略",
        "label_en": "AccessControlPolicy",
        "kind": "regulation",
        "business_domain": "安全与访问",
        "aliases": ["ACL", "防火墙策略", "安全规则"],
        "definition": "按源、目的、端口与动作对流量实施允许或拒绝的规则集，是安全域边界的执行载体。",
        "properties": [
            {"label": "策略编号", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True},
            {"label": "动作", "datatype": "enum", "required": True},
            {"label": "源地址范围", "datatype": "string", "required": True},
            {"label": "目的地址范围", "datatype": "string", "required": True},
            {"label": "生效顺序", "datatype": "integer", "required": True},
            {"label": "有效期", "datatype": "date"},
        ],
        "enumerations": [
            {"label": "动作", "members": ["允许", "拒绝", "记录并允许", "限速"]}
        ],
        "constraints": [
            {"name": "禁止全网段放行", "kind": "regulatory", "expression": "动作=允许 => 源地址范围 != 0.0.0.0/0", "severity": "violation", "reg_ref": "GB/T 22239 网络安全等级保护基本要求"},
        ],
    },
    {
        "label": "VPN 隧道",
        "label_en": "VpnTunnel",
        "kind": "entity",
        "business_domain": "安全与访问",
        "aliases": ["加密通道", "IPsec 隧道"],
        "definition": "在不可信网络上建立的加密逻辑通道，用于跨站点或远程接入的安全互联。",
        "properties": [
            {"label": "隧道编号", "datatype": "identifier", "required": True, "unique": True},
            {"label": "隧道类型", "datatype": "enum", "required": True},
            {"label": "加密算法", "datatype": "string", "required": True},
            {"label": "对端地址", "datatype": "identifier", "required": True},
            {"label": "建立时间", "datatype": "datetime"},
        ],
        "enumerations": [
            {"label": "隧道类型", "members": ["IPsec site-to-site", "L2TP/IPsec", "SSL VPN", "WireGuard"]},
        ],
        "constraints": [
            {"name": "禁用弱加密", "kind": "regulatory", "expression": "加密算法 ∉ {DES, 3DES, MD5, NULL}", "severity": "violation"},
        ],
    },
    {
        "label": "网络服务",
        "label_en": "NetworkService",
        "kind": "process",
        "business_domain": "应用服务",
        "aliases": ["基础服务"],
        "definition": "为网络或业务运行提供支撑协议服务的过程，如 DNS、DHCP、NTP、HTTP 与对象存储网关。",
        "properties": [
            {"label": "服务名称", "datatype": "string", "required": True},
            {"label": "服务类型", "datatype": "enum", "required": True},
            {"label": "服务端口", "datatype": "integer", "required": True},
            {"label": "可用性状态", "datatype": "enum", "required": True},
        ],
        "enumerations": [
            {"label": "服务类型", "members": ["DNS", "DHCP", "NTP", "HTTP/HTTPS", "SMTP", "LDAP", "RADIUS", "Syslog"]},
            {"label": "可用性状态", "members": ["正常", "降级", "不可用", "维护中"]},
        ],
        "constraints": [
            {"name": "时间同步服务必须可用", "kind": "regulatory", "expression": "服务类型=NTP => 可用性状态 != 不可用", "severity": "warning"},
        ],
    },
    {
        "label": "TLS 证书",
        "label_en": "TlsCertificate",
        "kind": "document",
        "business_domain": "安全与访问",
        "aliases": ["数字证书", "SSL 证书"],
        "definition": "由证书颁发机构签发、绑定域名与公钥并规定有效期的电子凭证，用于服务身份认证与传输加密。",
        "properties": [
            {"label": "证书序列号", "datatype": "identifier", "required": True, "unique": True},
            {"label": "颁发机构", "datatype": "string", "required": True},
            {"label": "签名算法", "datatype": "string", "required": True},
            {"label": "生效日期", "datatype": "date", "required": True},
            {"label": "到期日期", "datatype": "date", "required": True},
        ],
        "constraints": [
            {"name": "证书剩余有效期", "kind": "range", "expression": "到期日期 - 当前日期 >= 30 天", "severity": "warning"},
        ],
    },
    {
        "label": "网络故障事件",
        "label_en": "NetworkIncident",
        "kind": "event",
        "business_domain": "运维与监控",
        "aliases": ["网络事故", "告警事件"],
        "definition": "网络可用性、性能或安全偏离预期并影响业务的事件，需记录影响范围、持续时长与根因。",
        "properties": [
            {"label": "事件编号", "datatype": "identifier", "required": True, "unique": True, "key_identifier": True},
            {"label": "发生时间", "datatype": "datetime", "required": True},
            {"label": "恢复时间", "datatype": "datetime"},
            {"label": "影响时长", "datatype": "duration"},
            {"label": "故障类别", "datatype": "enum", "required": True},
            {"label": "严重程度", "datatype": "enum", "required": True},
        ],
        "enumerations": [
            {"label": "故障类别", "members": ["链路中断", "环路/广播风暴", "拥塞丢包", "配置错误", "设备宕机", "证书过期", "DNS 解析失败", "攻击行为"]},
            {"label": "严重程度", "members": ["P1 灾难", "P2 严重", "P3 一般", "P4 轻微"]},
        ],
        "constraints": [
            {"name": "P1/P2 须有根因分析", "kind": "codependency", "expression": "严重程度 ∈ {P1, P2} => 根因 必填且须在 24 小时内闭环", "severity": "violation"},
        ],
    },
    {
        "label": "网络性能指标",
        "label_en": "NetworkPerformanceMetric",
        "kind": "metric",
        "business_domain": "运维与监控",
        "aliases": ["QoS 指标"],
        "definition": "度量网络传输质量的量化指标，包括带宽利用率、时延、抖动、丢包率与可用率。",
        "properties": [
            {"label": "指标名称", "datatype": "string", "required": True},
            {"label": "指标值", "datatype": "decimal", "required": True},
            {"label": "单位", "datatype": "enum", "required": True},
            {"label": "采样周期", "datatype": "duration", "required": True},
            {"label": "阈值", "datatype": "decimal"},
        ],
        "enumerations": [
            {"label": "指标名称", "members": ["带宽利用率", "往返时延", "抖动", "丢包率", "可用率", "并发连接数"]},
            {"label": "单位", "members": ["Mbps", "ms", "%", "个"]},
        ],
        "constraints": [
            {"name": "利用率阈值", "kind": "range", "expression": "核心链路带宽利用率 <= 70%", "severity": "warning", "reg_ref": "ITU-T Y.1541 网络性能参数"},
        ],
    },
    {
        "label": "网络日志",
        "label_en": "NetworkLog",
        "kind": "document",
        "business_domain": "运维与监控",
        "aliases": ["syslog", "流日志"],
        "definition": "设备或服务产生的时序记录，含时间戳、级别、来源与事件描述，是故障定位与审计的依据。",
        "properties": [
            {"label": "日志时间", "datatype": "datetime", "required": True},
            {"label": "日志级别", "datatype": "enum", "required": True},
            {"label": "来源地址", "datatype": "identifier", "required": True},
            {"label": "日志内容", "datatype": "text", "required": True},
            {"label": "保存期限", "datatype": "duration"},
        ],
        "enumerations": [
            {"label": "日志级别", "members": ["紧急", "错误", "警告", "注意", "信息", "调试"]}
        ],
        "constraints": [
            {"name": "日志留存不少于 6 个月", "kind": "regulatory", "expression": "保存期限 >= 180 天", "severity": "violation", "reg_ref": "GB/T 22239 网络安全等级保护基本要求"},
        ],
    },
    {
        "label": "网络拓扑",
        "label_en": "NetworkTopology",
        "kind": "document",
        "business_domain": "物理与链路",
        "aliases": ["组网图", "架构示意图"],
        "definition": "描述节点、链路与安全域之间连接关系的受控文档，是变更评审与故障影响分析的基准。",
        "properties": [
            {"label": "拓扑名称", "datatype": "string", "required": True, "unique": True},
            {"label": "版本号", "datatype": "string", "required": True},
            {"label": "更新日期", "datatype": "date", "required": True},
            {"label": "覆盖范围", "datatype": "string"},
        ],
        "constraints": [
            {"name": "拓扑须与配置一致", "kind": "consistency", "expression": "拓扑中的链路与站点关系应与在网设备配置一致", "severity": "warning"},
        ],
    },
]

LINKS: list[tuple[str, str, str, str]] = [
    ("主机", "是一种", "网络节点", "subClassOf"),
    ("网络设备", "是一种", "网络节点", "subClassOf"),
    ("子网", "是一种", "网络分段", "subClassOf"),
    ("网络接口", "属于", "网络设备", "partOf"),
    ("网络接口", "属于", "主机", "partOf"),
    ("网络链路", "连接", "网络接口", "connects"),
    ("IP 地址", "分配给", "网络接口", "assignedTo"),
    ("MAC 地址", "绑定", "网络接口", "burnedInto"),
    ("子网", "包含", "IP 地址", "contains"),
    ("子网", "划入", "网络分段", "belongsTo"),
    ("VLAN", "对应", "网络分段", "mapsTo"),
    ("路由", "引用", "路由协议", "usesProtocol"),
    ("路由", "指向", "网络接口", "nextHopVia"),
    ("网络分段", "呈现于", "网络拓扑", "depictedIn"),
    ("网络拓扑", "记录", "网络链路", "records"),
    ("主机", "位于", "子网", "locatedIn"),
    ("网络设备", "位于", "子网", "locatedIn"),
    ("服务端口", "运行于", "主机", "exposedOn"),
    ("网络服务", "监听", "服务端口", "listensOn"),
    ("网络服务", "依赖", "DNS 记录", "dependsOn"),
    ("DNS 记录", "解析", "域名", "resolvedBy"),
    ("DNS 记录", "指向", "IP 地址", "pointsTo"),
    ("TLS 证书", "绑定", "域名", "bindsTo"),
    ("访问控制策略", "约束", "服务端口", "constrains"),
    ("访问控制策略", "划分", "网络分段", "separates"),
    ("VPN 隧道", "穿越", "网络链路", "traverses"),
    ("网络故障事件", "影响", "网络链路", "affects"),
    ("网络故障事件", "影响", "网络服务", "affects"),
    ("网络故障事件", "记录于", "网络日志", "evidencedBy"),
    ("网络性能指标", "度量", "网络接口", "measures"),
    ("网络性能指标", "触发", "网络故障事件", "triggers"),
    ("网络日志", "产生自", "网络设备", "records"),
]

PACK = DomainPack(
    key="network",
    label="计算机网络与运维",
    summary="节点—接口—链路—地址—路由—策略—故障—指标的运维本体，适配 CMDB、监控与配置合规校验。",
    space_name=SPACE_NAME,
    space_iri="https://onto.example.com/ontology/network#",
    space_description="面向企业网络与运维的本体域，覆盖物理链路、IP 与路由、安全域与访问控制、服务与故障性能监控。",
    business_domains=["物理与链路", "IP 与路由", "交换与转发", "域名与地址", "安全与访问", "应用服务", "运维与监控", "通用"],
    object_types=OBJECT_TYPES,
    links=LINKS,
    samples=[
        SampleDoc("企业网络架构与命名规范要点.md", "企业标准", "网络运维中心", "中国大陆", 0.8),
        SampleDoc("网络故障与配置基线台账.md", "数据字典", "网络运维中心", "中国大陆", 0.6),
    ],
    extra_kinds={
        "address_space": "地址/编号空间（受控分配）",
    },
    extra_relation_categories={
        "connectivity": "连通/承载（链路、接口、隧道）",
        "policy": "策略生效（访问控制、绑定关系）",
    },
    facets={
        "data_classification": "网络信息敏感分级（N1/N2/N3）",
        "security_zone": "安全域",
        "standard_refs": "适用 RFC / 标准",
        "lifecycle": "设备与配置生命周期",
        "sls": "服务等级与影响面",
    },
    standards=[
        "RFC 791 (IPv4)",
        "RFC 8200 (IPv6)",
        "RFC 1918 私有地址空间",
        "RFC 1035 (DNS)",
        "RFC 8446 (TLS 1.3)",
        "IEEE 802.1Q (VLAN)",
        "IEEE 802.3 (以太网)",
        "RFC 2544 网络设备基准测试方法",
        "ITU-T Y.1541 网络性能参数",
        "GB/T 22239 网络安全等级保护基本要求",
    ],
    classification={
        "N1": "可公开的网络信息",
        "N2": "内部拓扑与配置数据",
        "N3": "涉及安全域、密钥与访问控制的敏感配置",
    },
    link_categories={
        "partOf": "part_of",
        "connects": "connectivity",
        "assignedTo": "reference",
        "burnedInto": "reference",
        "contains": "part_of",
        "belongsTo": "part_of",
        "mapsTo": "reference",
        "usesProtocol": "reference",
        "nextHopVia": "connectivity",
        "depictedIn": "reference",
        "locatedIn": "part_of",
        "exposedOn": "policy",
        "listensOn": "connectivity",
        "dependsOn": "reference",
        "resolvedBy": "reference",
        "pointsTo": "reference",
        "bindsTo": "policy",
        "separates": "policy",
        "traverses": "connectivity",
        "affects": "causal",
        "evidencedBy": "reference",
        "measures": "derivation",
        "records": "reference",
    },
    term_suffixes=(
        "网络", "路由", "交换机", "防火墙", "链路", "带宽", "时延", "抖动", "丢包", "子网",
        "网关", "隧道", "端口", "协议", "地址", "域名", "证书", "策略", "拓扑", "分段",
        "拥塞", "重传", "握手", "广播", "组播", "镜像", "割接", "告警", "日志", "集群",
    ),
    clause_markers=("应当隔离", "禁止直通", "须经审批", "变更前", "回滚方案"),
    domain_cues={
        "物理与链路": ("光纤", "专线", "链路", "接口", "机房", "网卡", "拓扑"),
        "IP 与路由": ("IP", "子网", "路由", "网关", "DHCP", "前缀"),
        "交换与转发": ("交换机", "VLAN", "Trunk", "广播域", "生成树", "转发"),
        "域名与地址": ("域名", "DNS", "解析", "记录", "地址"),
        "安全与访问": ("防火墙", "访问控制", "策略", "证书", "加密", "VPN", "安全域"),
        "应用服务": ("服务", "端口", "监听", "HTTP", "负载", "NTP"),
        "运维与监控": ("监控", "告警", "指标", "时延", "丢包", "故障", "日志", "巡检"),
    },
    tag_cues={
        "故障": "故障相关",
        "安全": "安全相关",
        "地址": "寻址",
        "链路": "连通性",
        "指标": "计量",
        "配置": "配置项",
        "性能": "性能",
    },
    synonyms={
        "时延": ["延迟", "latency"],
        "丢包": ["packet loss", "丢包率"],
        "带宽": ["契约带宽", "throughput"],
        "交换机": ["switch", "SW"],
        "防火墙": ["FW", "访问控制设备"],
        "子网": ["网段", "subnet"],
        "路由": ["route", "转发表"],
        "链路": ["线路", "circuit"],
        "拓扑": ["网络结构", "topology"],
        "隧道": ["VPN", "加密通道"],
    },
    sample_questions=[
        "跨机房链路的时延阈值是多少？",
        "全网默认路由条目数量有什么限制？",
        "接口带宽利用率连续多久算越限？",
        "跨安全域访问的策略有哪些最小授权要求？",
    ],
    agent_hint=(
        "建模侧重：节点—接口—链路—地址—分段的连通层次，以及策略—服务—端口的访问控制层次；"
        "故障事件要能回溯到受影响的链路与服务，指标须写明单位与采样口径。"
    ),
)
