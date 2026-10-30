# 维护工具

从项目根目录或双击这里的 BAT 使用。所有工具自动定位上一级项目目录。

## 日常操作

- `doctor.bat`：状态诊断；认证正常不代表 QQ 收发已验证。
- `pause-bridge.bat` / `resume-bridge.bat`：暂停或恢复 QQ 中转。
- `test-bridge.bat`：源码目录或开发包运行完整离线回归及逻辑自检；运行包明确提示仅运行部署自检。
- `memory-control.bat --start` / `--stop` / `--status`：手动开始、停止或查看记忆整理；`memory-control.py` 接受相同参数。使用前先确认其他 WorkBuddy 窗口没有任务，插件再检查本地任务和当前网关状态；命令提交成功不等于整理已经开始或完成。

平时只将消息按群、成员、时间和本地规则分类入库，不自动调用模型整理。QQ 管理员也可使用 `/整理记忆 开始`、`/整理记忆 停止`、`/整理记忆 状态`。一轮只处理启动时积压的消息，结束后不会继续追着新消息整理；有新的前台任务时停止本轮，空闲后需重新开启。停止不会强杀已经提交到 WorkBuddy 的批次，当前批次会保留跟踪并收尾。

控制请求保存在插件目录的 `memory_control.json`，处理中的请求使用 `memory_control.processing.json`；它们均为本机运行数据，不编辑、不放入分发包。数据库与心跳快照也不随包分发。公开网关接口不能原子保证其他所有 WorkBuddy 会话均空闲，因此人工确认仍有必要。

## 故障修复与更新

- `reconnect.bat`：刷新网关凭据并验证插件重载。
- `reload-plugin.bat`：重载自制插件并核对运行指纹。
- `reset-astrbot-password.bat`：只操作核实属于本项目的 AstrBot 实例。
- `fix-bat-encoding.bat`：检查根目录和 scripts 下的 BAT，原件备份到 backups。

## 开发维护

`diagnostic_evidence.py` 是维护工具共用模块。`build-material.py` 只留在维护者源码目录，包含本机脱敏对照，不随任何材料分发。

- 默认运行 `build-material.py`，生成 `dist/QQbot部署材料/` 及同名 ZIP，包含插件、维护入口和必要部署自检。
- 加 `--development`，生成独立的 `dist/QQbot开发材料/` 及同名 ZIP，额外包含完整离线回归测试。
- 两种模式均可加 `--no-zip` 仅构建并校验目录。

所有模式使用同一套脱敏与完整性校验。根目录 README 提供项目说明，完整部署指南保存在 `docs/部署指南.md`。
