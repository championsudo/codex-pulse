# Codex Pulse

[中文](README.md) | [English](README.en.md)

本机 **Codex、Claude Code、OpenCode、Z Code** 用量与速度面板，支持 GLM 等日志中记录的模型，以及 Paseo 会话关联。只读本机数据，无需提供 API Key。

- **看用量**：每日 token、当地小时分布、本周与上周同期对比，以及模型与对话贡献。
- **看速度**：完整轮次的历史速度与每日趋势；Codex / Claude Code 最近活跃轮次的实时估算。
- **筛选与导出**：按日期、模型、工具和 Paseo 关联筛选，导出对话、每日或轮次 CSV。
- **本机运行**：Python 后端与离线前端，无遥测；运行时不访问外部 API。

## 界面预览

截图使用合成数据，仅展示功能，不代表真实用量或性能基准。当前界面为中文。

![用量总览](docs/images/overview.jpg)

<details>
<summary>查看更多：实时速度、用量趋势与对话详情</summary>

![实时速度](docs/images/live-speed.jpg)
![用量趋势](docs/images/usage-trends.jpg)
![对话详情](docs/images/conversation-details.jpg)

</details>

## 快速启动

需要 **Python 3.10+** 和现代浏览器，支持 Linux、macOS、Windows。下载或克隆项目后，进入项目文件夹。

Linux / macOS：

```sh
cd "/path/to/codex-pulse"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash start.sh
```

Windows PowerShell：

```powershell
cd "C:\path\to\codex-pulse"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.cmd
```

默认打开 **http://127.0.0.1:8765**。**安装完成不等于服务持续运行：保持启动终端开启，按 Ctrl+C 停止；关闭终端或重启电脑后需重新启动。** IDE、自动化工具或临时执行会话结束时，也可能终止服务。

依赖只需在首次安装或变更时安装；日常使用直接运行启动脚本。每台电脑需重新创建虚拟环境。Node.js 仅用于可选开发检查，运行面板不需要。

端口被占用时可用 `bash start.sh --port 8766`（Windows：`.\start.cmd --port 8766`）。更多参数、Ubuntu `ensurepip` 排障及可选 systemd 用户服务见[使用指南](docs/guide.zh-CN.md)。

## API 用量能统计吗？

| 使用方式 | 当前支持 |
| --- | --- |
| 通过上述四种工具调用 API，包括使用 GLM 等模型 | 本机日志有受支持的 token 记录时，可以统计 |
| 自己用 Python、JavaScript、curl 或其他程序直接调用 API | 暂不自动采集；需另行接入调用记录 |
| 官方账号余额、账单、套餐剩余额度 | 不查询；本机 token 统计不等于官方账单 |

面板不拦截 API 请求。日志缺失、未保存或位于其他电脑的调用不会被统计；“用量记录数”也不等于所有 API 请求数。

## 支持范围

- 默认发现本机数据；也可用 `--codex-home`、`--claude-home`、`--opencode-db`、`--zcode-db` 等参数指定位置。没有记录时显示空面板。
- 速度包含思考、工具执行与等待，并非纯模型解码速度。实时估算读取本机日志，每 10 秒更新；历史统计每 15 秒更新，可暂停。
- OpenCode 不推断实时运行状态；Z Code 实时列表依据近期日志活动，缺少模型或用量时显示未知或不可计算，日志活动并非进程心跳。历史测速能力取决于日志结构。Z Code 上游用量表会清理超过 30 天的记录。OpenCode / Z Code 适配包含合成测试。

详细的版本支持、计数与测速口径、故障排查和开发命令见[使用指南](docs/guide.zh-CN.md)。

## 隐私与许可

服务仅监听 `127.0.0.1`，不读取账号凭据或 API Key。标题、ID、路径和导出文件仍可能包含隐私，请勿上传个人日志、数据库、凭据或导出数据。详见[隐私说明](PRIVACY.md)。

每台电脑需运行本地后端；GitHub Pages 无法运行后端或读取本机日志。[MIT License](LICENSE)。
