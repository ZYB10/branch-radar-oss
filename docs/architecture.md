# 架构说明

Branch Radar 把事实采集、语义总结和外部发布分开，保证 Codex 或飞书暂时不可用时仍然不会丢失 Git 变化。

```text
本地浏览器 ──► loopback Web/API ──► 单任务队列
                                      │
FactSource（当前实现：本地 Git checkout）
       │
       ▼
BranchMonitor：游标 / 事务 / 降级 ──► SQLite 事件/outbox
       │                                    │
       ▼                                    ├──► 控制台 / JSON
规则摘要 ──► 可选 Codex 摘要                └──► lark-cli 文档发布
```

终端命令和 Web 控制台共用 `RadarService`、SQLite 与 `operation.lock`，不会维护两套分支游标。Web 只负责呈现显式 DTO 和提交后台任务；Git 事实、事件状态与飞书 outbox 仍由核心层决定。

## 事实来源（FactSource）

分支怎么被观察，和观察结果怎么被持久化，是两件事。`FactSource` 是这条缝：

- `refresh(watches, fetch=)` 准备本轮观察，返回**按 branch_id 归类的阻塞错误**。被列出的分支会被完全跳过：不观察、不推进游标。拒绝永远比猜测安全，因为这里猜错等于静默改写该分支的历史。
- `resolve(watch)` 返回当前 tip SHA，分支不存在时返回 `None`。
- `describe(watch, transition)` 负责**分类**并采集证据。分类本身是来源相关的：本地 Git 用 `merge-base --is-ancestor` 判断 fast_forward / rewind / force_push，而 webhook 拿到的是 before/after 加一个 `forced` 标记。无法重建的证据必须置 `analysis_complete=False`，不允许编造。
- `attach_patch_excerpt(...)` 只在显式 `--codex` 时调用。

`BranchMonitor` 因此不认识 Git：它只负责游标推进、事务边界、错误落库和模型调用降级，这样任何来源都拿到同一套持久性保证。实现类不得写数据库，也不得推进游标。

来源在 `RadarService.check()` 里选择，那是唯一的组装点。`baseline` 和 `deleted` 不带 diff 也不带提交区间，因此永远不会触发 Codex 调用。

本地 Git 来源会按 repository/remote 合并关注项，默认以 4 个 worker（硬上限 8）并行执行不同对象库的 remote 验证、远端引用检查和引用预解析；同一 Git common directory（包括 linked worktree）会被放进一个顺序 work unit，避免引用锁冲突和临时 pack 清理互相干扰。每个 work unit 先用 `ls-remote` 读取本轮所选分支的远端 OID，再与对应的 `refs/remotes/<remote>/<branch>` 批量比较：全部一致时直接跳过 `git fetch`；只有部分变化时，也只按那些分支的显式目标 refspec 定向 fetch，不会退化为获取整个远端。远端已经删除的所选分支会同步清理 tracking ref，即使其余分支命中无变化快路径也不会留下陈旧引用。

线程池只产生内存中的解析结果或阻塞错误，`BranchMonitor` 仍按原关注列表顺序消费，并在主流程中完成所有 SQLite 事务和游标推进。进度回调也只由调用线程串行发出：准备阶段的 `current` / `total` 表示仓库组，消费阶段表示分支，两者都单调且有明确上界。

仓库发现只读取用户明确配置或本次命令显式传入的根目录，不猜测 `code`、`github` 等机器目录，也不会在配置为空时退化成扫描 HOME / 当前工作目录。Web 控制台通过有界后台任务唤起 macOS / Linux 的系统文件夹选择器；选择动作只返回路径，保存后才会开始发现，无图形环境仍可折叠展开手动路径入口。HOME 内的新配置会保存为 `~/…`，运行时再展开；历史绝对路径继续兼容。确定并去重路径后，再以同样的默认 4-worker（硬上限 8）并行 `inspect`；最终结果固定按名称和路径排序。单个损坏或异常 checkout 只生成该仓库的错误候选，不会丢弃其余扫描结果。

粘贴 GitHub 地址会先按规范化 remote 身份匹配上述发现结果；只有用户明确确认已配置扫描根和目标目录后，才会进入 clone 后台任务。clone 仅接受重新生成的 GitHub HTTPS/SSH 地址，不接受 URL 内凭据、本地路径、`file://` 或 transport helper；它复用本机 credential helper / SSH agent，但关闭交互式凭据输入、tag 与递归 submodule。下载先写入扫描根下的随机 staging，完成 remote 身份校验后再以 no-replace 语义原子改名；取消、超时或验证失败只清理该任务创建的 staging，清理不完整会显式报错，已有目标目录永远不会被覆盖。clone 本身不写关注状态，成功后仍由用户在现有分支选择器中确认并建立静默基线。

## 后台服务

`branch-radar service install` 生成用户级定时单元：macOS 使用 LaunchAgent 的 `StartInterval`，Linux 使用 systemd user timer。默认间隔为 900 秒，产品设置只提供 300 / 900 / 1800 / 3600 秒四档。

- 单元固定安装时的解释器（`python -m branch_radar`）、当时的 `PATH` 和 `BRANCH_RADAR_HOME`。后台进程环境是空的，不继承 PATH 就找不到 `git` / `codex` / `lark-cli`；不继承 `BRANCH_RADAR_HOME` 就会用错状态目录。
- 只做用户级 agent。macOS 上 LaunchDaemon 拿不到用户 Keychain，`lark-cli` 会直接失败。
- 每次触发只执行 `branch-radar daemon --once --json`：一轮有界检查、保留策略与可选通知完成后进程退出。两次触发之间没有常驻 Python daemon，也没有监听端口；系统调度器负责下一轮唤醒，并避免同一任务重叠。
- daemon 会记录最近尝试、成功、失败和连续失败次数。`doctor` 与 Web 只展示经过裁剪的健康状态，不暴露 traceback。
- 跨进程 `operation.lock` 保证定时任务和手动 CLI 不会同时检查；Git、Codex、飞书与通知均有各自的执行超时，单轮不会无限驻留。
- 系统通知默认关闭。启用后只在本轮产生一个或多个真实 `changed` 结果时发送一条合并通知；baseline、无变化和单纯轮询不会提醒。勿扰时段按本机时间判断并支持跨午夜，通知失败只影响展示旁路，不改变事件、游标或检查健康状态。
- `service install --mode web` 是独立的常驻 Web 单元，不使用上述定时唤醒模型；daemon 与 Web 使用不同的服务标识，可以同时存在。

## 本地 Web 控制台

- Web 服务是常驻进程，固定监听 `127.0.0.1`，默认端口 `8765`，不支持局域网访问。它与定时检查单元彼此独立：关闭 Web 不会取消已经安装的定时检查，定时检查空闲时也不会占用 Web 端口。
- 每次启动生成随机会话令牌，通过自动打开链接的 URL fragment 交给 `sessionStorage`，不会进入 HTTP 请求、服务日志或公开首页；所有 API 读取要求令牌，所有写入还要求同源 Origin 与 JSON Content-Type。
- 拒绝非 loopback Host、CORS、iframe 嵌入和内联脚本；响应使用 CSP、`no-store`、`nosniff`。
- 一个进程内最多运行一个扫描、检查、发布或飞书配置任务，并继续使用跨进程 `operation.lock` 与 CLI 协调。
- clone 与目录选择同样是有界、可恢复的后台 job；clone 还支持显式取消，停止整个 Git 进程组后再清理本次 staging。
- 检查与发布采用后台 job，页面刷新后可以从活动 job 恢复；分支部分失败和飞书结果未知不会显示为成功。
- Web 文案通过浏览器端消息目录在简体中文和英文之间即时切换；显式偏好只保存在 `localStorage`，并同步更新 `html.lang`、日期格式、占位符与无障碍名称，不进入 SQLite。
- 浏览器 DTO 不包含作者邮箱、未经脱敏的远程 URL、diff、凭证或状态目录路径。不可信 Git 文本只以 `textContent` 写入 DOM。
- 创建飞书文档使用持久化幂等记录；响应不确定时进入 `reconcile_required`，不会盲目重试并重复创建。

## 数据原则

- SQLite 是状态源；飞书文档不是数据库。
- 分支观察游标和发布状态分离。
- 新事件落库与分支游标推进位于同一事务。
- 每轮检查都持久为 `check_run` 及分支结果快照；事件、游标与批次关联在同一事务中写入，进程中断也不会产生“无批次事件”。
- 未确认的可操作批次驱动 Web 事件的 `is_new`；确认后消失。`prune` 会保护这些批次及关联事件，只移除已确认或无变化的过期批次。
- 发布失败保留在 outbox 中，可单独重试。
- 时间统一以 UTC ISO 8601 存储，展示时再转本地时区。
- 停止关注只把 branch 置为 disabled，并递增观察序号阻断旧任务落库；事件、OID 与游标继续保留，重新关注时复用。
- SQLite `user_version` 是 schema 守门：支持该机制的旧程序遇到更高版本会拒绝打开。`prune --before` 只清理终态历史，任何仍有 `pending` / `retry` / `unknown` / `inflight` 投递的事件都受到保护。

## 事件分类

| 条件 | 类型 |
|---|---|
| 首次看到分支 | `baseline` |
| 旧 SHA 等于新 SHA | 无事件 |
| 旧 SHA 是新 SHA 的祖先 | `fast_forward` |
| 新 SHA 是旧 SHA 的祖先 | `rewind` |
| 两者互不为祖先 | `force_push` |
| 原来存在、现在不存在 | `deleted` |
| 删除后重新出现 | `recreated` |

提交列表最多展开 250 条，但事件会额外保存真实总数和截断标记。旧提交对象已被 Git GC 时，事件会标记 `analysis_complete=false`，规则摘要和飞书内容都会明确提示分析不完整。

## Codex 边界

Git 层先产生确定性 change bundle，包括 SHA、作者、时间、父节点、文件列表和行数统计。只有显式启用 `--codex` 时，当前进程才会按字节上限读取、过滤并附加一段 diff；这段内容不会进入 SQLite。Codex 只生成标题、概览、分类、风险和验证建议。作者、时间、SHA、游标、事件类型与投递状态永远以 Git/SQLite 为准。

`codex exec --sandbox read-only` 限制写入，不构成分析目录之外的文件读取隔离。适配器会忽略用户配置/规则、裁剪环境变量并明确禁止工具调用，但 prompt 不能替代 OS sandbox。因此 Codex 摘要必须由用户逐次显式启用，只适用于可信仓库；默认规则摘要不启动 agent。

一轮产生至少两条可见事件时，显式启用的 Codex 还可以生成跨事件批次综述。输入只包含已脱敏的规则摘要、分类、高危信号和统计，不包含 diff、文件路径、邮箱或远程 URL。规则综述先行落库，AI 增强失败只保留警告，不会反转已完成的检查。

## 发布目标（PublicationTarget）

事件送到哪里，和送达过程怎么保证不丢，是两件事。`PublicationTarget` 是这条缝，与 `FactSource` 对称：

- `contains(marker)` 回答"目标里是否已经有这条事件"。这是中断后可安全续传的依据：写入前查一次跳过已完成的工作，写入后再查一次证明确实落地。**无法回答必须抛 `PublicationError`，不允许当作"不存在"**——把不可读当成不存在，下一次就会追加出重复。
- `append(delivery, marker)` 追加一条事件，并带上 `marker` 供 `contains` 检索。返回的警告只是提示，本身不决定成败。

`Publisher` 因此不认识任何具体目标：租约、写入与回查的顺序、以及 `succeeded` / `error` / `unknown` 的判定都在它这里，任何目标都拿到同一套持久性保证。目标类不得写数据库，也不得决定投递状态。

目标在 `RadarService._publisher()` 里选择，那是唯一的组装点。

现有实现：

- `LarkDocumentTarget` —— 通过用户自己的 `lark-cli` 追加到飞书文档，凭据始终留在 `lark-cli` 和钥匙串里。
- `MarkdownFileTarget` —— 追加到本机 Markdown 文件。它不需要账号、网络或第三方 CLI，因此也是这条缝是否真的成立的检验：它只实现追加和回查，其余全部继承自 `Publisher`。

每条发布段落带有稳定标记 `BRANCH-RADAR:<event-key>`，`deliveries` 表按 `(event_id, destination_type, destination_key)` 唯一，所以同一条事件可以并行送往多个目标而互不干扰。

## 后续演进

当前版本优先复用本地 checkout 的对象库，这也决定了它今天的上限：只能看见本机 clone 过的仓库，且只在这台机器运行时工作。

团队级可见性需要一个服务端观察者，而这正是 `FactSource` 存在的原因。路线调整为 **webhook 先于 mirror**：webhook 先拆掉“必须在某台个人电脑上 clone 并持续开机”的根本限制；bare mirror 随后作为证据补全和对象保留能力，而不是公网接入的前置条件。

下一阶段依次是：

1. GitLab / GitHub Webhook 接收与 `WebhookFactSource`：用 payload 的 `before` / `after` / `forced` 形成确定性观察；payload 无法证明净 diff、提交区间或被丢弃对象时，必须置 `analysis_complete=False`。
2. 托管 bare mirror（`MirrorFactSource`，也可作为 webhook 的证据补全器）：按规范化远程合并对象库，长期保留 force-push 前后的引用，异步补齐 webhook 本身没有携带的提交和 diff 证据。
3. 飞书即时消息和只读 MCP 服务。

### Webhook 接收端的进程边界

公网 webhook **不会**作为当前 Web server 的一个新路由。现有浏览器控制台继续只监听 loopback，继续使用 Host、same-origin、session token 和 JSON 检查，也不增加任何公网绑定选项。接收端是单独的最小进程，只暴露 provider webhook 路径：

```text
GitHub / GitLab
      │  HTTPS + provider signature
      ▼
独立 webhook receiver ──► SQLite webhook_inbox ──► 有界消费队列
  （只验签和落 inbox）       （共享状态库）             │
                                                       ▼
                                            WebhookFactSource
                                                       │
                                                       ▼
                                            BranchMonitor / outbox

本地浏览器 ──► 127.0.0.1 Web/API（保持不变，不接公网）
```

接收端和核心进程可以共享同一个 SQLite 文件，但权限与写入范围必须分离：

- receiver 只允许在短事务中插入经过验证、裁剪和规范化的 `webhook_inbox` 信封，不得写分支游标、事件或发布 outbox；`FactSource` 仍然不得写数据库或推进游标。
- 只有消费端在 `operation.lock` 协调下把 inbox 观察交给 `BranchMonitor`。事件写入和游标推进继续由现有单事务保证；处理成功后再确认 inbox 项，崩溃重试依赖 delivery id 保持幂等。
- inbox 同时限制单请求字节数、待处理条数和总字节数。HTTP 请求只有在 SQLite 提交成功后才返回 2xx；队列已满时返回 429/503 让 provider 重试，不用无界内存队列吸收峰值。
- receiver 不做 clone、fetch、Codex 或飞书调用。较慢的证据重建进入独立、有界的后台工作队列，并共享现有外部操作并发上限。

### 鉴权、重放与最小化存储

- GitHub 使用 `X-Hub-Signature-256`，GitLab 使用其受支持的 secret/signature 机制；均在读取完整受限请求体后做常量时间比较。缺失、错误或对应项目不匹配时直接拒绝。
- provider delivery id 在数据库中设唯一约束，重复投递只返回之前的接收结果，不重复生成事件。能获得可信时间戳的 provider 还要检查允许的时钟偏差；没有可信时间戳时，唯一 delivery id、密钥轮换和有限保留期共同构成重放边界。
- 只保存分类和重建所需的最小字段：provider、repository fingerprint、ref、before、after、forced、delivery id、接收时间及经过裁剪的提交元数据。原始请求头、签名、密钥、凭证 URL、作者邮箱和完整 payload 不进入日志、事件或浏览器 DTO。
- 每个项目使用可轮换的 webhook secret；密钥来自进程环境或系统凭据存储，不写入 SQLite、配置导出或服务日志。轮换窗口可短暂接受 current/previous 两把密钥，并记录使用的非敏感 key id。
- 未知事件类型、删除/重建证据不足、提交列表被 provider 截断或本地没有对象时，观察可以入 inbox，但 `describe()` 必须明确输出 `analysis_complete=False`，绝不从分支名或提交标题推断缺失事实。

这个边界让公网攻击面停在一个无 UI、无发布权限、无 Git 执行能力的窄进程里；当前 loopback 控制台无需弱化任何安全承诺。以上演进不需要把 Git 细节放进 `BranchMonitor`，也不改变事件/outbox 的权威边界。
