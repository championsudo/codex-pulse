# Codex Pulse

[中文](README.md) | [English](README.en.md)

A local usage and speed dashboard for **Codex, Claude Code, OpenCode and Z Code**, including GLM and other models recorded in their logs, with Paseo session association. Read-only local data; no API key required.

- **Usage**: daily tokens, local-hour distribution, this week versus the same period last week, and model and conversation contributions.
- **Speed**: daily historical completed-turn trends and live estimates for recently active Codex / Claude Code turns.
- **Filters and exports**: filter by date, model, agent and Paseo association; export conversations, daily totals or turns as CSV.
- **Local operation**: Python backend and offline frontend, no telemetry or external API calls at runtime.

## Preview

Screenshots use synthetic data to illustrate features, not real usage or performance benchmarks. The interface is currently in Chinese.

![Usage overview](docs/images/overview.jpg)

<details>
<summary>More screenshots: live speed, usage trends and conversation details</summary>

![Live speed](docs/images/live-speed.jpg)
![Usage trends](docs/images/usage-trends.jpg)
![Conversation details](docs/images/conversation-details.jpg)

</details>

## Quick start

Requires **Python 3.10+** and a modern browser on Linux, macOS or Windows. Download or clone the project, then enter its directory.

Linux / macOS:

```sh
cd "/path/to/codex-pulse"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash start.sh
```

Windows PowerShell:

```powershell
cd "C:\path\to\codex-pulse"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.cmd
```

Opens **http://127.0.0.1:8765** by default. **Installing dependencies does not keep the service running: keep its terminal open, press Ctrl+C to stop, and restart after closing the terminal or rebooting.** An IDE, automation tool or temporary execution session may also terminate the service when that session ends.

Install dependencies on initial setup or when they change; for everyday use, just run the launch script. Create a separate virtual environment on each computer. Node.js is only used for optional development checks, not normal operation.

If the port is occupied, use `bash start.sh --port 8766` (Windows: `.\start.cmd --port 8766`). See the [user guide](docs/guide.en.md) for all options, Ubuntu `ensurepip` troubleshooting and an optional systemd user service.

## Can it monitor API usage?

| Usage path | Current support |
| --- | --- |
| API calls through the four supported agents, including GLM and other models | Counted when local logs contain supported token records |
| Direct calls from Python, JavaScript, curl or another application | Not collected automatically; requires a separate integration for call records |
| Official account balance, billing or remaining plan quota | Not queried; local token counts are not an official bill |

The dashboard does not intercept API requests. Missing, unsaved or other-computer records are outside coverage. The usage-record count is not the count of all API requests.

## Coverage

- Discovers local data by default; override locations with options such as `--codex-home`, `--claude-home`, `--opencode-db` and `--zcode-db`. No records produces an empty dashboard.
- Speed includes thinking, tools and waiting, not pure model decoding. Live estimates read local logs every 10 seconds; history refreshes every 15 seconds. Both can be paused.
- OpenCode and Z Code live status is not inferred; historical speed depends on the stored format. Z Code prunes usage records older than 30 days. OpenCode / Z Code adapters use synthetic tests and have not been validated against real sessions.

See the [user guide](docs/guide.en.md) for version support, accounting and speed definitions, troubleshooting and development commands.

## Privacy and license

Listens only on `127.0.0.1` and does not read account credentials or API keys. Titles, IDs, paths and exports can still contain private information. Do not upload personal logs, databases, credentials or exports. See the [privacy notes](PRIVACY.md) (Chinese).

Each computer needs its own local backend; GitHub Pages cannot run it or read local logs. [MIT License](LICENSE).
