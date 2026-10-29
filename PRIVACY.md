# 隐私说明

## 读取什么

面板只读你指定的本机数据。所有 `--*-home` 参数都是工具的数据根目录；`--opencode-db` 和 `--zcode-db` 指定单个数据库文件。

- **Codex**：`--codex-home`、`CODEX_HOME` 或 `~/.codex` 下的 `sessions/**/*.jsonl`、`archived_sessions/**/*.jsonl` 用量、模型、标识、时间和轮次状态；最新 `state_*.sqlite` 中的标题、模型、代理关系、归档等元数据；`session_index.jsonl` 会话名称元数据；最新 `thread_history_*.sqlite` 的线程 ID、轮次 ID 与终态。
- **Claude Code**：`--claude-home`、`CLAUDE_CONFIG_DIR` 或 `~/.claude` 下的 `projects/**/*.jsonl`，包括子代理文件。提取 session / message / agent 标识、模型、用量、时间与停止原因，并判别用户消息和工具结果，以建立轮次边界。
- **OpenCode**：`--opencode-db` 或 `--opencode-home` 下的 `opencode.db`；默认根目录为 `$XDG_DATA_HOME/opencode` 或 `~/.local/share/opencode`。读取支持的 SQLite 表中会话、父子关系、标题及 allowlist 的用量、模型、时间、角色和完成状态字段；不提取正文、思考文本或工具输出。1.x 与 2.x 的支持能力不同，见 [README.md](README.md)。
- **Z Code**：`--zcode-db` 或 `--zcode-home`（默认 `~/.zcode`）下的 `cli/db/db.sqlite`。仅读取 `session` 的 ID / 父 ID、`model_usage` 的标识、模型、用量、状态与完成时间、`turn_usage` 的轮次边界及核对字段；不读取标题、配置、凭据或正文。实时列表另外读取 `cli/log/zcode-日期.jsonl`（显式数据库仅在标准 `cli/db` 布局下读取相邻 `cli/log`，非标准路径不读取日志） 的事件类型、时间、会话与轮次 ID 及模型 ID，不保留消息、工具内容或其他上下文；最近活动不是进程心跳。上游写入时会清理超过 30 天的用量。
- **Paseo 关联**：`--paseo-home`、`PASEO_HOME` 或 `~/.paseo` 下的 `agents/**/*.json`，只使用 `provider`、`persistence.provider`、`persistence.sessionId` 和可选 `title` 元数据。按代理工具和 session ID 关联整段会话，不建立第二份用量账本。恢复或接管已有会话不代表它最初由 Paseo 创建；没有匹配元数据也不能证明从未通过 Paseo 使用。

JSON / JSONL 解析会短暂读取完整记录以提取上述字段，但不会保存或向浏览器返回对话正文、思考文本与工具输出。Paseo 仅读取 agents 元数据文件，不读取其对话正文。SQLite 连接使用只读模式；启动和刷新不会改写任何代理日志或数据库。

## 不读取什么

不读取任何工具的 `auth.json`、credentials、API Key、登录凭据、Paseo 配置或其他工具配置文件。Claude 根目录可以由 `CLAUDE_CONFIG_DIR` 指定，但读取范围仍只有 `projects` 日志；不会解析配置来寻找凭据。

不连接、控制或接管运行中的会话，不调用官方账号用量接口，不连接远程 SSH 主机或同步其他电脑日志。Paseo 元数据只用于本机会话关联，不用于运行代理命令。

## 数据在哪里

处理在本机进行，解析缓存仅位于内存。服务仅监听 `127.0.0.1` 并验证 Host / Origin；没有遥测、远程上报、外部资源或外部 API，运行与读取日志不需要联网。安装 Python 依赖时可能联网。请勿修改为局域网/公网监听或将服务通过代理暴露出去，除非自行完成相应访问控制评估。

本机页面和 API 会收到对话标题、线程/轮次 ID、模型、代理工具、Paseo 关联标记、统计值与数据根目录路径。这些内容也可能在屏幕截图、终端启动信息或浏览器中可见。CSV 导出包含标题、ID、模型、工具、关联状态、日期和统计信息，需按个人数据保管。浏览器本地存储仅用于保存筛选及显示偏好。

## 共享源码前

源码包仅含程序、文档及合成测试，不附带真实日志、数据库、账号文件、导出 CSV 或个人截图；测试中的名称和 ID 都是构造的例子。不要将开发时为验收生成的运行目录或截图放入共享包。

上传 GitHub 或发送给别人前，请检查暂存区/压缩包，排除 `.codex/`、`.paseo/`、`.claude/`、OpenCode 原始数据目录、`.zcode/`、`auth.json`、credentials、`.env`、`*.jsonl`、数据库及其 WAL/SHM、CSV、日志、截图、缓存、虚拟环境和临时运行目录。`.gitignore` 只能减少误提交，无法清除已被 Git 跟踪或已写入历史的文件。不要为反馈问题上传完整个人日志；先将标题、ID、路径、正文和工具结果脱敏。

在另一台电脑运行时，程序默认读取那台电脑自己的代理日志；它不会自动同步原电脑的数据。GitHub Pages 无法运行本机后端或读取电脑上的日志。
