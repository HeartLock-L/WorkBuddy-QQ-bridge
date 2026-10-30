# WorkBuddy QQ Bridge

把 QQ 群聊接进本机 WorkBuddy 的 AstrBot 插件。

```text
QQ  ←→  NapCat  ←→  AstrBot  ←→  workbuddy_bridge  ←→  WorkBuddy 本机网关
```

WorkBuddy 负责理解和动手，AstrBot 与 `workbuddy_bridge` 负责消息路由、权限与上下文，NapCat 负责 QQ 收发。

> 想养鱼但是又不想花钱？可以试一下用 WorkBuddy 桥接进 QQ。

## 特性

- **群聊问答**：@、引用机器人消息或命中唤醒词后回答；私聊仅管理员可用
- **附件理解**：收到的图片落盘后交给模型打开，支持「先说要看图、随后补图」；GIF 自动抽帧预览
- **生图**：可选接入即梦 CLI，一人一框、冷却与每日上限，支持补发与本地副本清理
- **长期记忆**：按群 / 成员分层保存到 SQLite，管理员手动开启整理，支持 `/记忆`、`/群记忆` 查询与遗忘
- **自动表情**：同群收藏 QQ 自定义表情，按话题召回，支持人工素材库
- **管理员审批**：普通成员的操作请求先待审批，管理员在原群确认后才执行
- **运维工具**：`doctor` 体检、`reconnect` 刷新网关、`reload-plugin` 热重载、暂停 / 恢复、密码重置

## 目录结构

```text
workbuddy_bridge/      AstrBot 插件源码（部署到 AstrBot/data/plugins/）
scripts/               运维、诊断与打包工具
tests/                 离线回归测试与部署自检
docs/                  部署指南、扮演规则
start-astrbot.bat      启动 AstrBot
start-napcat.bat       启动 NapCat
```

部署时把 `workbuddy_bridge/` 整个目录复制到 `AstrBot/data/plugins/`，其余目录与启动脚本按原层级复制到 AstrBot 项目根目录。

## 快速开始

1. 准备 Windows、已登录的 WorkBuddy、机器人 QQ 号，以及 Python 环境
2. 按 [部署指南](docs/部署指南.md) 放置 AstrBot、NapCat 并创建 `.venv`
3. 复制 `workbuddy_bridge/` 到 `AstrBot/data/plugins/`，复制 `start-*.bat`、`scripts/`、`docs/`、`tests/` 到项目根
4. 填写 `workbuddy_bridge/bridge_policy.py` 中的管理员与机器人身份：

   ```python
   MASTER_ID = "0"        # 管理员 QQ，不要填机器人 QQ
   EXPECTED_SELF_ID = ""  # 建议填写机器人 QQ，登录错号时拒绝处理
   ```

5. 安装依赖并配置 QQ 连接（反向 WebSocket 端口 6199），启动 `start-astrbot.bat` 与 `start-napcat.bat`
6. 在 WorkBuddy 会话内运行 `scripts/reconnect.py` 初始化网关凭据

完整步骤、验收清单与故障排查见 **[部署指南](docs/部署指南.md)**。

## 配置

| 位置 | 说明 |
|---|---|
| `workbuddy_bridge/bridge_policy.py` | 管理员身份、机器人账号、群规则与业务默认值 |
| AstrBot 插件面板 | 引用回复、自动表情、生图、记忆等开关与限额 |
| `workbuddy-gateway.env` | 网关凭据，由 `scripts/reconnect.py` 生成，**不要提交或外发** |
| `QQ_BOT_ROOT` / `QQ_BOT_DREAMINA` | 可选环境变量，覆盖项目根与即梦 CLI 路径 |

## 测试

全部用例离线运行：使用模拟服务与临时目录，不连接 QQ、不调用模型、不发起真实生图。

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python tests/bridge_selftest.py
```

- 需要 Python 3.10+（开发环境使用 3.13）与 `Pillow`（GIF 抽帧）
- `tests/bridge_selftest.py` 是部署自检入口，结果以 `ALL PASS` 结尾
- 部署后也可以直接运行 `scripts/test-bridge.bat`，它会按当前目录自动选择检查范围
- 测试会自动适配两种目录：仓库检出（`workbuddy_bridge/`）与部署后的 AstrBot 项目（`AstrBot/data/plugins/workbuddy_bridge/`）

<details>
<summary>模块职责一览</summary>

| 模块 | 职责 |
|---|---|
| `main.py` | QQ 入口、生命周期、权限与问答调度 |
| `bridge_policy.py` | 身份与业务默认值、纯判断、公共路径 |
| `draw_flow.py` / `proactive_chat.py` | 生图调度与恢复；主动闲聊、跟随、关键词取材 |
| `prompts.py` / `reply_text.py` | 模型提示词；回复排版、截断和隐私过滤 |
| `attachments.py` | 附件限额、GIF 抽帧、来源信息、最近图片与等待补图 |
| `memory_store.py` / `memory_service.py` | SQLite 群 / 成员记忆、时间线、证据与修订 |
| `meme_library.py` / `meme_collection.py` | 表情候选与发送前校验；同群收藏、去重和清理 |
| `cache_maintenance.py` | 受管缓存和过期附件清理 |
| `runtime_status.py` | 白名单状态快照与原子写入 |
| `jimeng.py` / `image_jobs.py` | 即梦 CLI；生图进度、恢复与自拍保留 |
| `image_job_commands.py` | 生图任务查询、管理员确认与补发 |
| `request_queue.py` | 问答排队和远端任务占用 |
| `source_files.py` | 热重载、版本检查与语法检查的源码清单 |

新增源码模块时需同步更新 `source_files.py`。

</details>

## 边界与安全

- 问答走 WorkBuddy 的本机任务通道，**不是隔离的沙箱**：群里的每一次问答都是这台机器上一个真实的 agent 任务，能读写文件、执行命令
- 提示词中的只读约束是约定，不是系统级权限控制；接陌生群前请先想清楚
- 面板、反向 WS 与 NapCat WebUI 不要映射到公网
- 记忆数据库、网关凭据、聊天附件等本机运行数据不会进入仓库（见 `.gitignore`）

## 许可

[MIT](LICENSE)
