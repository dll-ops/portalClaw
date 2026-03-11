# Ead：使用 CalDAV + vdirsyncer 同步 iCloud Calendar / Reminders 与本地事项

## 0. 目标

把 iCloud 的 **Calendar（事件）** 与 **Reminders（提醒事项）** 作为同一套 CalDAV 数据源接入本机；用 `vdirsyncer` 同步到本地 vdir 目录，再由 Ead 读取、创建、修改、删除 `.ics` 项，最后通过 `vdirsyncer sync` 回写到 iCloud。

本方案的核心假设：

- **日历事件** 用 `VEVENT`
- **提醒事项** 用 `VTODO`
- 本地落地格式统一为 `filesystem` storage（一个 collection 一个目录，一个 item 一个 `.ics` 文件）
- iCloud 端 collection 名称可能是 UUID/随机串，不能把目录名当成人类可读名称

---

## 1. 范围与边界

### 支持

- 同步 iCloud Calendar 事件（`VEVENT`）
- 同步 iCloud Reminders 任务（`VTODO`）
- 本地新增 / 修改 / 删除后回写 iCloud
- iCloud 侧新增 / 修改 / 删除后拉回本地
- 用 `discover` 重新发现新建的 calendar/list

### 不保证完整 round-trip 的能力

Apple Reminders 有很多高层能力（如 subtasks、attachments、location-based alert、tags、部分智能列表语义等），这些能力不一定能完整映射到通用 `VTODO`。因此 Ead 默认只依赖通用、可互操作字段；不要把 iCloud 私有高级语义当成稳定接口。

**默认允许的稳定字段：**

- `UID`
- `DTSTAMP`
- `SUMMARY`
- `DESCRIPTION`
- `STATUS`
- `DUE`
- `DTSTART`
- `COMPLETED`
- `LOCATION`
- `PRIORITY`
- `RRULE`（如确有需要）

---

## 2. 数据模型

### 2.1 本地目录模型

建议统一根目录：

```text
~/ead/pim/
├── icloud/
│   ├── <collection_a>/
│   │   ├── <item1>.ics
│   │   └── <item2>.ics
│   ├── <collection_b>/
│   └── ...
└── state/
    ├── collection_map.json
    ├── last_sync.log
    └── sync_errors.log
```

说明：

- `~/ead/pim/icloud/`：vdir 根目录；每个子目录对应一个远端 collection
- `collection_map.json`：Ead 自己维护的“目录名 -> displayname / 类型 / 备注”的映射文件
- collection 目录名可能是随机值；Ead 必须通过内容与 metadata 建立人类可读映射

### 2.2 组件判定规则

Ead 读取 `.ics` 后按主组件类型分类：

- 发现 `BEGIN:VEVENT`：认定为 calendar event
- 发现 `BEGIN:VTODO`：认定为 reminder/task
- 一个 collection 如果绝大多数 item 都是 `VEVENT`，标为 `calendar`
- 一个 collection 如果绝大多数 item 都是 `VTODO`，标为 `reminder`
- 混合 collection 不作为默认目标 collection，除非人工确认

### 2.3 本地最小写入模板

#### VEVENT（最小安全模板）

```ics
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Ead//PIM Sync//EN
BEGIN:VEVENT
UID:<stable-unique-id>
DTSTAMP:<utc-timestamp>
DTSTART:<start-datetime-or-date>
SUMMARY:<title>
END:VEVENT
END:VCALENDAR
```

#### VTODO（最小安全模板）

```ics
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Ead//PIM Sync//EN
BEGIN:VTODO
UID:<stable-unique-id>
DTSTAMP:<utc-timestamp>
SUMMARY:<title>
STATUS:NEEDS-ACTION
END:VTODO
END:VCALENDAR
```

备注：

- `VEVENT`：必须有 `UID`、`DTSTAMP`；在普通对象里通常也应有 `DTSTART`
- `VTODO`：必须有 `UID`、`DTSTAMP`
- `DUE` 与 `DURATION` 不能同时出现在同一 `VTODO`
- `DTEND` 与 `DURATION` 不能同时出现在同一 `VEVENT`

---

## 3. 前置条件

### Apple 侧

1. Apple Account 已启用 **双重认证**
2. iCloud 已启用 **Calendars** 与 **Reminders**
3. 已生成 **app-specific password**（应用专用密码）

注意：

- 如果 Apple Account 主密码被修改/重置，现有 app-specific password 会被自动吊销，需要重新生成
- 不要在配置文件里写主密码，只写 app-specific password

---

## 4. 安装与运行时依赖

在树莓派 / Linux 主机：

```bash
sudo apt update
sudo apt install -y python3-pip
pip3 install --user vdirsyncer
```

确认可执行：

```bash
~/.local/bin/vdirsyncer --version
```

如果环境变量未包含 `~/.local/bin`，Ead 在脚本里应使用完整路径调用。

---

## 5. vdirsyncer 配置

配置文件路径：

```text
~/.config/vdirsyncer/config
```

推荐基线配置：

```ini
[general]
status_path = "~/.local/share/vdirsyncer/status/"

[pair icloud_pim]
a = "local_icloud_pim"
b = "icloud_caldav"
collections = ["from a", "from b"]
metadata = ["displayname"]
# conflict_resolution = null

[storage local_icloud_pim]
type = "filesystem"
path = "~/ead/pim/icloud/"
fileext = ".ics"

[storage icloud_caldav]
type = "caldav"
url = "https://caldav.icloud.com/"
username = "<APPLE_ACCOUNT_EMAIL>"
password = "<APP_SPECIFIC_PASSWORD>"
auth = "basic"
```

### 说明

- `filesystem` storage：本地一个 collection 一个目录，一个 item 一个 `.ics` 文件
- `collections = ["from a", "from b"]`：steady-state 模式；允许本地与远端两侧新增 collection 后，重新 `discover` 时都能被纳入
- `metadata = ["displayname"]`：允许同步人类可读名称；Ead 可再配合 `metasync` 使用
- `url` 先用 iCloud CalDAV 根地址；由 `discover` 决定具体 collections

### 冲突策略

默认不要直接设 `"a wins"` 或 `"b wins"`，先保留 `null`，让冲突显式暴露。等 Ead 的冲突处理逻辑成熟后，再决定是否自动偏向本地或远端。

---

## 6. 首次引导（bootstrap）

### 6.1 discover

```bash
~/.local/bin/vdirsyncer discover icloud_pim
```

### 6.2 sync

```bash
~/.local/bin/vdirsyncer sync icloud_pim
```

### 6.3 可选：metasync

```bash
~/.local/bin/vdirsyncer metasync icloud_pim
```

### 6.4 引导后应出现的状态

- `~/ead/pim/icloud/` 下出现多个 collection 子目录
- 每个子目录中有多个 `.ics` 文件
- 某些 collection 名称可能是 UUID / 随机串
- displayname metadata 被同步后，本地 collection 内可能出现 metadata 文件；Ead 可读取并建立 `collection_map.json`

---

## 7. Ead 的目录扫描与映射建立

首次同步后，Ead 立即执行一次 collection 归档：

### 7.1 扫描规则

对 `~/ead/pim/icloud/*/`：

1. 读取目录名（真实 collection name）
2. 读取 metadata/displayname（如果存在）
3. 随机抽样或全量扫描 `.ics`
4. 统计 `VEVENT` / `VTODO` 数量
5. 生成本地映射文件

### 7.2 映射文件建议格式

```json
{
  "<collection_name>": {
    "displayname": "<human_name_or_null>",
    "kind": "calendar|reminder|mixed|unknown",
    "item_count": 42,
    "component_counts": {
      "VEVENT": 40,
      "VTODO": 2
    },
    "preferred_for_write": true
  }
}
```

### 7.3 写入目标决策

Ead 处理新事项时：

- 新日历事件默认写入 `kind=calendar` 且 `preferred_for_write=true` 的 collection
- 新提醒默认写入 `kind=reminder` 且 `preferred_for_write=true` 的 collection
- 若存在多个候选 collection，按 displayname 优先级或本地策略决定
- 若没有合适目标，不自动写入，先记录错误并请求人工指定

---

## 8. Ead 的读 / 写 / 改 / 删流程

### 8.1 读（从 iCloud 拉到本地）

```text
vdirsyncer sync
→ 扫描本地 vdir
→ 更新 collection_map.json
→ 暴露给 Ead 的统一事项视图
```

### 8.2 新建事件 / 提醒

1. 选定目标 collection
2. 生成稳定 `UID`
3. 生成合法 `.ics`（`VCALENDAR + VEVENT/VTODO`）
4. 写到目标 collection 目录
5. 执行 `vdirsyncer sync`
6. 校验新 item 仍存在且未被远端回滚

### 8.3 修改

1. 先 `sync`
2. 读取目标 `.ics`
3. 仅修改允许字段
4. 保持 `UID` 不变
5. 更新 `DTSTAMP`
6. 回写文件
7. `sync`
8. 校验结果

### 8.4 删除

1. 先 `sync`
2. 通过 `UID` / 文件名定位 item
3. 删除本地文件
4. `sync`
5. 若远端仍存在，再次 `sync` 并记录冲突

---

## 9. discover / sync / metasync 的使用策略

### sync

日常高频使用：

```bash
~/.local/bin/vdirsyncer sync icloud_pim
```

用于同步 items 的新增、修改、删除。

### discover

只在下列场景执行：

- iCloud 侧新建了 calendar / reminder list
- 本地新建了新的 collection 目录
- 远端 collection 结构发生变化

```bash
~/.local/bin/vdirsyncer discover icloud_pim
```

**注意：** 新 collection 不会被普通 `sync` 自动纳入；新增 collection 后必须重新 `discover`。

### metasync

只在需要同步 metadata（如 displayname / color）时执行：

```bash
~/.local/bin/vdirsyncer metasync icloud_pim
```

---

## 10. 建议的计划任务

### 10.1 systemd user timer 或 cron

建议每 10~30 分钟一次：

```bash
~/.local/bin/vdirsyncer sync icloud_pim
```

### 10.2 Ead 晨报前

晨报生成前执行：

```text
sync
→ 扫描 reminders / calendar
→ 生成今天视图
→ 推送简报
```

### 10.3 低频 discover

每日一次或每次检测到“collection 不存在”时：

```bash
~/.local/bin/vdirsyncer discover icloud_pim
```

---

## 11. 互操作约束（必须遵守）

1. **不要重写 UID**，UID 必须稳定
2. **不要把 displayname 当 collection name**；iCloud 的 collection name 可能是随机串
3. **不要依赖 Apple Reminders 的高级私有特性** 进行核心逻辑建模
4. **修改前先 sync**，避免基于旧文件写入
5. **新增 collection 后必须 discover**
6. **生成 `.ics` 时必须包含 `VCALENDAR / VERSION / PRODID`**
7. **任务完成状态**：优先使用 `STATUS:COMPLETED` + `COMPLETED:<timestamp>`；未完成用 `STATUS:NEEDS-ACTION`
8. **全日事件**优先用 `VALUE=DATE`
9. **不要同时写互斥字段**：
   - `VEVENT`：`DTEND` 和 `DURATION` 不能同时存在
   - `VTODO`：`DUE` 和 `DURATION` 不能同时存在；若有 `DURATION`，必须有 `DTSTART`
10. **任何失败都先保留本地文件副本**，不要直接覆盖或丢弃

---

## 12. 故障处理

### 12.1 认证失败

症状：`401` / `403` / 登录失败

处理：

- 检查 Apple Account 双重认证是否开启
- 检查是否用了 app-specific password
- 如果主密码修改过，重新生成 app-specific password

### 12.2 新建的提醒列表 / 日历没同步下来

原因：只执行了 `sync`，没执行 `discover`

处理：

```bash
~/.local/bin/vdirsyncer discover icloud_pim
~/.local/bin/vdirsyncer sync icloud_pim
```

### 12.3 displayname 与目录名对不上

这是正常现象。以目录名作为 collection 的稳定标识；displayname 只是元数据。

### 12.4 高级 Apple 特性丢失

这是预期风险。Ead 只保证通用字段；对 subtasks / attachments / location 等高级语义不做 round-trip 承诺。

### 12.5 冲突

如果同一 item 在本地和远端都被修改，默认让 vdirsyncer 显式报错；Ead 应记录并暂停自动覆盖。

---

## 13. Ead 的建议实现接口

### 13.1 抽象层

Ead 不直接把 iCloud 当业务接口，而是只对本地 vdir 操作。

```text
iCloud CalDAV
↕
vdirsyncer
↕
local vdir (.ics files)
↕
Ead parser / writer / scheduler
```

### 13.2 建议模块

- `sync_runner.py`：封装 discover/sync/metasync 调用
- `collection_index.py`：扫描 collection、生成 `collection_map.json`
- `ics_parser.py`：解析 VEVENT / VTODO
- `ics_writer.py`：生成最小合法 `.ics`
- `task_router.py`：选择写入哪个 collection
- `conflict_guard.py`：发现冲突时阻止静默覆盖

### 13.3 统一事项对象（内部）

```json
{
  "uid": "...",
  "kind": "event|task",
  "title": "...",
  "description": "...",
  "status": "needs-action|completed|cancelled|tentative|confirmed",
  "start": "...",
  "due": "...",
  "completed_at": "...",
  "collection": "...",
  "displayname": "...",
  "source": "icloud"
}
```

---

## 14. 最小工作流（Ead 应按此执行）

### 读取今日安排

```text
1. vdirsyncer sync icloud_pim
2. 扫描所有 collection
3. 解析 VEVENT / VTODO
4. 汇总今天相关事项
5. 输出晨报/查询结果
```

### 创建提醒事项

```text
1. vdirsyncer sync icloud_pim
2. 读取 collection_map.json
3. 选择 reminder collection
4. 生成 VTODO .ics
5. 写入本地目录
6. vdirsyncer sync icloud_pim
7. 校验写入成功
```

### 创建日历事件

```text
1. vdirsyncer sync icloud_pim
2. 读取 collection_map.json
3. 选择 calendar collection
4. 生成 VEVENT .ics
5. 写入本地目录
6. vdirsyncer sync icloud_pim
7. 校验写入成功
```

---

## 15. 不要做的事

- 不要直接操作 iCloud Web UI 的私有接口
- 不要把 Apple Reminders 高级字段当成必须可写
- 不要跳过 `sync` 直接覆盖旧文件
- 不要用随机新 UID 覆盖已有事项
- 不要把 discover 当成每分钟执行的高频操作
- 不要把 displayname 变化视作 collection identity 变化

---

## 16. 一句话原则

**Ead 只把 iCloud 当 CalDAV 数据源；只对本地 `.ics` 做标准化读写；把 collection 名称当稳定 ID，把 displayname 当展示层；把 `sync` 当日常操作，把 `discover` 当 collection 结构刷新。**
