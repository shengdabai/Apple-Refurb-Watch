# 苹果翻新 Mac 监测器

监测 [苹果官网翻新店](https://www.apple.com.cn/shop/refurbished/mac)，当 **Mac Studio / Mac mini** 翻新机型有新产品上架时，立即通知：**飞书消息优先**，飞书失败再回落到 `config.json` 里配置的收件邮箱（如 `you@example.com`）。同时维护一个**汇总页**，列出官网当前全部在架的翻新 Mac；有新货号时推送一条「汇总页有更新」短消息并附汇总页链接。

- 数据源：翻新总页内嵌的 `REFURB_GRID_BOOTSTRAP` JSON（无需登录、无需第三方 API）
- 去重键：`partNumber`（货号），新出现即视为上架
- 定时：launchd 每 **5 分钟**巡检一次
- **同一货号只推送一次**：推送过的货号（以及首次运行时已在架的基线货号）记入永久台账 `notified`，之后下架再上架不再推送

## 快速开始

```bash
cp config.example.json config.json   # 按需改收件人、机型、SMTP；汇总页默认关闭
export APPLE_MONITOR_SMTP_PASS=...     # 用 SMTP 时需要；只用飞书可不设
python3 monitor.py                       # 首次运行建立基线，不发通知
```

## 汇总页

`config.json` 的 `summary` 段（删掉该段或写 `"enabled": false` 即关闭）：

```json
"summary": {"url": "http://<server>:8080/apple-refurb/", "remote": "user@example-host:/var/www/site/apple-refurb/"}
```

- 每轮巡检生成 `site/index.html`（不入仓库），内容：全部在架翻新 Mac；关注机型的新货号置顶「新上架」区，关注机型分区展开，其余机型折叠，每区按价格从低到高。
- 「新」标记 = 在待通知队列里，或 24 小时内首次发现（`catalog` 记录全部机型货号的首次发现时间；第一次建立时的存量不标「新」；下架超过 60 天的条目清掉）。
- 上传：用 rsync 经 ssh（`BatchMode`）传到 `remote`。只在内容变化时上传；内容没变时每 30 分钟重传一次，刷新页面上的「最近核对」时间。上传超过 60 秒整组终止。
- 推送：汇总页**本轮内容已上传成功**才在推送里带链接（加 `?v=时间` 防缓存），每个新货号只占一行；上传失败时退回逐款详情（含官网链接），**不挡推送**，失败原因记在 `state.json` 的 `page_status`。
- 只影响页面的变化（下架、非关注机型上新）只更新页面，**不推送**。
- 云服务器 `sync-site.sh` 的 `rsync --delete` 已排除 `/apple-refurb/`，同步文章站不会删掉汇总页。

## 通知通道

`config.json` 的 `channels` 决定顺序，默认 `["feishu", "smtp"]`：按顺序尝试，**任一成功即视为已通知**（清空待通知队列）；全部失败才进入下文的指数退避，货号继续留在 `pending`。想只用某一路，写成 `["smtp"]` 或 `["feishu"]`。

- **飞书**：调用你本机的飞书发送库（示例库名 `_feishu-lib.sh`，函数 `feishu_send_text`）（与健康日报等脚本同一发送链路；密钥由库经 `~/.lark-channel/secrets-getter` 获取，本项目不存任何飞书密钥）。默认发给库内默认收件人。可在 `config.json` 加可选段覆盖，均不含密钥：
  ```json
  "feishu": {"lib": "…/_feishu-lib.sh", "timeout": 20, "app_id": "cli_xxx", "receive_id": "oc_xxx", "receive_type": "chat_id"}
  ```
- **超时与回落**：飞书库在 2T 上，TCC 拒绝或未挂载时可能卡住。发送放在独立 bash 子进程里（`timeout`+15 秒硬上限，超时整组 kill）；库不存在、source 失败、无 `feishu_send_text`、超时、返回 `code≠0` 都记日志并回落 SMTP，不会让巡检挂住。
- **通道变更清零退避**：`state.json` 记录上次的 `send_channels`；与当前配置不同（含旧 state 没有此字段）时，一次性清零 `send_failures` / `next_send_after`，让新通道立刻送出积压，而不是被旧通道（SMTP）攒下的退避挡住。
- 验证：`python3 monitor.py --test-feishu` 会**真实**发一条测试消息到飞书；自动化测试用假库/假函数，不会真实发送。

## SMTP 兜底：填授权码

邮件用 QQ 邮箱 SMTP 发送（自己发给自己）。需要一个 **SMTP 授权码**（不是登录密码）：

1. 登录 QQ 邮箱网页版 → 设置 → 账号
2. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」
3. 开启「IMAP/SMTP 服务」，按提示发短信，生成 **16 位授权码**
4. 把授权码写入下面这个文件的第一个非注释行：

   ```
   ~/.config/apple-refurb-monitor/smtp_pass
   ```

   即把 `YOUR_QQ_AUTH_CODE_HERE` 替换成你的授权码。

> 想用别的发件邮箱（如 163/Gmail）？改 `config.json` 里的 `smtp.host/port/sender` 即可。

## 验证可用

```bash
cd <project-dir>

# 1. 逻辑+实时抓取自检（不发邮件）
python3 monitor.py --selftest

# 2. 发一条测试消息验证飞书通道（会真实发送）
python3 monitor.py --test-feishu

# 2b. 填好授权码后，发一封测试邮件验证 SMTP 兜底
python3 monitor.py --test-email

# 离线单元测试（假飞书/假 SMTP，不发送）
python3 -m unittest

# 3. 手动巡检一次
python3 monitor.py --once

# 查看当前状态
python3 monitor.py --status
```

## 启动 / 停止定时监测

```bash
# 启动（开机自启 + 每5分钟）
launchctl load -w ~/Library/LaunchAgents/com.tony.apple-refurb-monitor.plist

# 停止
launchctl unload -w ~/Library/LaunchAgents/com.tony.apple-refurb-monitor.plist

# 改完代码/配置后重载
launchctl unload ~/Library/LaunchAgents/com.tony.apple-refurb-monitor.plist
launchctl load   ~/Library/LaunchAgents/com.tony.apple-refurb-monitor.plist
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `monitor.py` | 主脚本：抓取 → 解析 → 对比 → 通知（飞书优先，SMTP 兜底） |
| `config.json` | 通知通道顺序、收件人、监测机型、SMTP 服务器配置（可选 `feishu` 段） |
| `~/.config/apple-refurb-monitor/smtp_pass` | SMTP 授权码（不入仓库） |
| `state.json` | 运行状态（自动生成）：永久已推送台账 `notified`、在架已知货号 `seen`、待通知 `pending`、汇总页 `catalog` / `page_*`、发信退避 |
| `site/index.html` | 本轮生成的汇总页（上传到云服务器，不入仓库） |
| `logs/monitor.log` | 巡检日志（唯一日志出口；超过 1MB 轮转为 `monitor.log.1`…`.5`；首次轮转前已超大的旧日志归档为 `monitor.log.pre-rotation`，不会被自动删除） |
| `logs/launchd.out.log` / `launchd.err.log` | launchd 捕获的 stdout/stderr；脚本在 launchd 下不再往 stdout 重复写日志，只有异常才会出现在 err |
| `com.tony.apple-refurb-monitor.plist` | launchd 定时配置（在 ~/Library/LaunchAgents） |

## 工作机制

- **首次运行**建立基线：把当前在架的监测机型记为「已知」，**不**发提醒（当前 Mac Studio / mini 均无翻新货，基线为空）。
- 之后每次巡检，发现 `partNumber` 不在已推送台账 `notified` 里的监测机型 → 判定为新上架 → 更新汇总页 → 通知（飞书→SMTP）→ 记入 `notified`。
- `notified` 永不删除，所以已推送过的货号下架再上架只记一行日志「已推送过的货号重新在架，按规则不再推送」。旧 `state.json` 没有该字段时自动迁移一次：沿用 `seen`，再从历史日志（含 `.pre-rotation` 与轮转文件）的「已通知 …: [货号]」行补回已下架的货号。
- `--reset-baseline` 只清基线与待通知，保留 `notified` 和 `catalog`，推送过的货号仍不会再推送。
- 抓取失败自动重试 3 次（指数退避）。
- 发现新品但所有通道都失败时，货号进入 `state.json` 的 `pending`（待通知），**不**写入 `notified`、也不会被丢弃；已在 `notified` 里的货号会从 `pending` 移出，不会重复推送。
- 所有通道连续失败按 10 → 20 → 40 … 分钟指数退避，上限 6 小时（`send_failures` / `next_send_after` 记在 `state.json`），退避期内不再尝试任何通道。
- 发信恢复后，积压期间发现的所有货号合并成**一条**摘要（飞书消息或邮件）；期间已下架的货号会标注「本次检测时已下架」。
- 机型识别用结构化字段 `refurbClearModel`（`macstudio`/`macmini`），标题子串兜底；`Studio Display`（显示器）已正确排除。

## 调整监测机型

编辑 `config.json` 的 `watch_models`（如加 `"macbookpro"`）与 `title_patterns`，重载 launchd 即可。
