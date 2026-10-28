# User guide

[Project home](../README.en.md) | [中文指南](guide.zh-CN.md)

Run the commands below from the project root.

## Requirements

- Python **3.10 or later** and a modern browser.
- Linux, macOS, or Windows. The launch scripts prefer the project's `.venv`, then look for system Python. They do not automatically download or install dependencies.
- A working IANA time zone database. Windows usually needs the `tzdata` package; Linux and macOS usually have a system database, or can install `requirements.txt`.
- Local Codex, Claude Code, supported OpenCode, or Z Code records on this computer. With no logs, the dashboard displays an empty state.

## Download and run

Download or clone the entire project, and enter the `codex-pulse` directory after extracting the ZIP. Paths may contain spaces.

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

Installing dependencies requires internet access; starting the dashboard and reading local logs do not. On Linux/macOS, if system Python already has a usable time zone database, you can run `bash start.sh` without creating a virtual environment. On Windows, you can also run `start.cmd` directly with a Python installation that has `tzdata`. Create a new virtual environment on each computer rather than copying one between computers.

**Installing dependencies does not mean the service is running.** Run the launch script whenever you need the dashboard. It opens **http://127.0.0.1:8765** by default; keep its terminal open and press **Ctrl+C** to stop. When launched from an IDE, a temporary Codex execution session, or an automation tool, the service may receive a termination signal when that session ends or is cleaned up. Exit code 143 (128 + SIGTERM) indicates a termination signal; it does not identify its source or establish that log parsing crashed. After restarting the service, it reads retained logs again; there is no dashboard database to migrate. To start without opening a browser automatically:

```sh
python3 server.py
# Windows: py -3 server.py
# If using a .venv, use its Python interpreter instead.
```

### Optional: Linux user service

For longer-running use, you can manually configure the [systemd user service example](systemd/codex-pulse.service). It assumes the project is at `~/Apps/codex-pulse` with `.venv` created and dependencies installed as above; edit `WorkingDirectory` and `ExecStart` to match the actual location. The example path has no spaces; if using a path with spaces, quote it according to systemd unit syntax. The service runs Python directly, does not open a browser, and still listens only on `127.0.0.1`.

```sh
mkdir -p ~/.config/systemd/user
cp docs/systemd/codex-pulse.service ~/.config/systemd/user/
# Edit paths in ~/.config/systemd/user/codex-pulse.service if needed
systemctl --user daemon-reload
systemctl --user start codex-pulse.service
systemctl --user status codex-pulse.service
journalctl --user -u codex-pulse.service -f
# After leaving the log viewer, stop the service if desired
systemctl --user stop codex-pulse.service
```

Optionally run `systemctl --user enable codex-pulse.service` to start it when the user manager starts; undo this with `systemctl --user disable codex-pulse.service`. User services normally depend on the login session and user manager lifecycle, so they are not guaranteed to keep running after logout or before login; additional system configuration depends on the environment. The template's `Restart=on-failure` restarts on failure; a manual `stop` does not trigger a restart, and recovery after every SIGTERM is not guaranteed. The project does not install or enable the service automatically.

## Options and data sources

The dashboard discovers the four agent data directories and Paseo association metadata on the same computer. It does not require a web login, call an official account usage API, or connect to or take over an agent. On another computer it reads that computer's records.

All launch scripts forward options. For example:

```sh
bash start.sh --port 8766 --timezone Asia/Shanghai --codex-home "/path/to/.codex"
```

```powershell
.\start.cmd --port 8766 --timezone Asia/Shanghai --codex-home "C:\path\to\.codex"
```

- `--port`: default `8765`, allowed range `1024–65535`. Use another port such as `8766` if occupied, and adjust the browser address accordingly.
- `--codex-home`: Codex data root containing `sessions` / `archived_sessions`, not an individual log file. Takes precedence over `CODEX_HOME`.
- `--paseo-home`: Paseo data root; takes precedence over `PASEO_HOME`, otherwise `~/.paseo`. Only `agents/**/*.json` session association metadata is read.
- `--claude-home`: Claude Code data root; takes precedence over `CLAUDE_CONFIG_DIR`, otherwise `~/.claude`. Reads `projects/**/*.jsonl`, including subagents. Specify the `.claude` root, not `projects` or a JSONL file.
- `--opencode-home`: OpenCode data root, defaulting to `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode` when `XDG_DATA_HOME` is unset. Reads `opencode.db` there.
- `--zcode-home`: Z Code root, default `~/.zcode`; reads `cli/db/db.sqlite`.
- `--zcode-db`: explicitly specify a Z Code SQLite database, overriding the home default.
- `--opencode-db`: explicitly specify an OpenCode SQLite database, taking precedence over the default file under `--opencode-home`. The other `--*-home` options take directories.
- `--timezone`: IANA time zone, default `Asia/Shanghai`; for example, use `UTC` or `Europe/London`. It determines the boundaries of “today” and daily totals.
- `--open`: open the default browser when running `server.py` directly.

## Paseo and agent support

The dashboard reads each agent's local logs and associates sessions using `provider` and `persistence.sessionId` in Paseo `agents` metadata; an available `title` can supplement the name. Paseo does not provide a second token ledger, so association does not add usage again. The dashboard does not read Paseo configuration, credentials or conversation text. It covers only this computer, not remote SSH logs or other agent tools.

Agent tool (all / Codex / Claude Code / OpenCode / Z Code) and Paseo association (all / associated / unassociated) are independent filters. Association applies to the whole session: if an existing session is resumed or taken over, its history is also classified as associated, which does not prove it was originally created by Paseo. “Unassociated” means no matching local metadata was found, not that Paseo was never used. A conversation containing both associated and unassociated main or child threads is marked partially associated (`mixed` in CSV); filtering still uses each thread's actual association. Today's card, historical and live views, and CSV follow these filters; the live view is independent of the date range.

| Agent | Usage source and accounting | Historical speed and live status |
| --- | --- | --- |
| Codex | `sessions` and `archived_sessions` JSONL; supports existing newer and older usage formats | Calculated when complete turn boundaries exist; recent unfinished log activity permits a live estimate |
| Claude Code | `projects` JSONL; duplicate snapshots are merged by assistant message ID, retaining the last valid usage | Speed requires a complete record from the user turn start to `end_turn`; tool calls are not treated as new human turns |
| OpenCode 1.x | SQLite `session` / `message` / `part`; supports the confirmed mutually exclusive token buckets from 1.14.46 onward | Usage only: a completed step is not a completed user turn, so no turn speed or running status is invented |
| Z Code | SQLite canonical `model_usage` request ledger; GLM is a model name and can be used by different agents | Historical speed requires a complete successful turn with matching request count and output; no live status inferred |
| OpenCode 2.x | SQLite `session_v2` / `session_message` (ordinary assistant responses; compaction requests are not covered) | Historical speed requires a user start and an `idle` boundary with `outcome=succeeded`; unfinished turns are not claimed to be running |

For Claude Code, total input = non-cached input + cache creation input + cache read input. The cached input column contains cache read only; cache creation is included in non-cached input. Output uses the log's `output_tokens`; thinking is shown separately only if `output_tokens_details.thinking_tokens` is recorded. Otherwise it is “not separately recorded,” not zero; mixed aggregates indicate the recorded portion.

Z Code counts only `model_usage`; `turn_usage` supplies boundaries and is never added again. Input includes cache; older records use the upstream heuristic comparing reported totals to decide whether to add cache, which can be uncertain. Reasoning is part of output. Usage is attributed to request completion (upstream buckets by request start), speed to turn completion. Upstream prunes usage older than 30 days when writing, so older history cannot be recovered. Unknown or damaged databases are skipped with a warning while other sources continue. Live status is not inferred; Z Code titles, bodies, configuration and credentials are not read. See the pinned upstream [schema](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/apps/zcode-cli/packages/adapters/src/storage/session-store/schema.baseline.sql) and [usage implementation](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/apps/zcode-cli/packages/adapters/src/storage/session-store/repositories/usage.ts). Validation uses synthetic data only.

For the current OpenCode buckets, input, cache read and cache write count toward total input; output + reasoning count toward total output, and the cached input column includes cache read only. Potentially overlapping `tokens.total` values are not directly added. For 1.x, `step-finish` usage is preferred for a response; assistant message usage is a fallback to avoid double counting. Older or unknown versions and unrecognized structures are skipped with a reading warning; other sources can still be counted. `session.version` identifies only the session's creation version and cannot establish the accounting format of all history before or after upgrades or downgrades. History with an uncertain version or token format should not be treated as complete coverage.

For 1.x assistant-message fallback, usage is attributed by `data.time.completed`. For 2.x only ordinary assistant responses are currently counted. Completed or failed compaction requests may contain usage, but lack a trustworthy response completion time in this structure, so they are excluded and the dashboard warns of incomplete coverage. A turn with known but uncounted nonzero usage is excluded from speed, avoiding division of partial output by the full duration. For 2.x response usage, `data.time.streamed` is preferred, followed by `completed`; records without a trustworthy completion time are skipped with a warning instead of using the database row creation time. User start and idle boundaries use their respective message creation times. The 1.x and 2.x time boundaries are not mixed. Log capabilities differ among tools, so cross-tool speed compares only complete samples that actually qualify. Regression tests use synthetic data; no comparison against real OpenCode sessions has been performed.

## Usage and metrics

These metrics come from local logs and cannot establish remaining official plan quota. “Usage records” (`request_count` in the API) means the number of deduplicated nonzero token usage events, not all API requests. Zero-token events are excluded, consistent with existing Codex accounting. CSV exports use the same usage-event filter.

### Features

- Today, the last 7 / 30 / 90 days, all history, or a custom date range.
- Scope: task usage, all records, main conversations only, subagents only, or automatic approvals only. The default task scope includes main conversations and ordinary subagents, excluding automatic approvals.
- Filter by agent tool, Paseo association and model; inspect input, cached input, output, recorded reasoning output and daily trends. The cache input ratio is based on token counts, not request hit counts.
- Main conversation summaries, search and sorting, source contributions, and recent completed-turn details.
- Separate average turn speed and combined throughput during active periods, with their calculation basis.
- Speed trends use the completed turn's local date; days without samples remain empty. The hourly distribution buckets selected-range usage events by the configured local time zone, combining repeated daylight-saving hours.
- Week-over-week compares local Monday midnight through now with the same period last week. Date-range selection does not affect this comparison; other filters apply. A zero previous value or missing speed sample has no percentage change. Results use retained logs and cannot guarantee complete history.
- Live estimates for main conversations and subagents read logs every 10 seconds. Historical statistics refresh every 15 seconds. Both can be paused together; pausing only affects dashboard reads, and automatic reads pause when the page is hidden.
- Export conversations, daily totals or all qualifying completed turns as CSV under the same filters.

### Live speed

- **Current turn average** = recorded output tokens in this turn / seconds since the turn started. Time includes thinking, tool execution and waiting. Before the first usage record it displays “waiting for usage.”
- **Last 60-second recorded rate** = newly recorded output tokens in the recent window / window seconds. If a turn is less than 60 seconds old, its actual elapsed time is used. Zero means no new usage was recorded in the window; it does not mean the model stopped. A batch of output may be recorded only after the next response completes.
- The 10-second refresh reads local logs, not a token-by-token model stream. Usage is written in batches at response completion, so estimates can jump and cannot measure pure model decoding speed.
- The live view ignores the historical date range but uses the scope, model, agent tool and Paseo filters. When a specific model is selected, only its output is counted; the current turn average is not displayed for mixed-model turns.
- A turn with no completion, interruption or other terminal event and log activity within five minutes is shown as recently active. Logs are not a heartbeat and cannot confirm that the model is generating at this moment. After five minutes of inactivity the status is unconfirmed and current speed is hidden; residual turns without updates for over 24 hours are omitted.
- Completed or interrupted turns leave the live list; completed turns can be inspected in historical details. OpenCode and Z Code running status is not inferred.

### Historical speed

**Average turn speed** = total output tokens of completed qualifying turns / sum of their durations. It is weighted by duration; durations of parallel turns are added separately.

**Combined throughput during active periods** = output tokens of the same completed samples / the union of their time intervals. Overlap is counted once and gaps with no selected turn running are excluded. This is a historical average for the selected samples, not current speed or an all-day average.

For example, two turns run simultaneously for 100 seconds and each produce 3,000 tokens: average turn speed = 6,000 / (100 + 100) = 30 token/s; combined throughput = 6,000 / 100 = 60 token/s. If they run sequentially, both are 30 token/s.

Pure model generation speed requires reliable per-response generation time, which the historical logs do not provide. Both measures include thinking, tool execution and waiting. Output tokens include reasoning output and are not the same as the length of the final visible answer.

### Accounting details

The newer/older token format and automatic-approval details below primarily concern Codex; see the support table above for Claude Code, OpenCode and Z Code differences.

- New Codex logs use per-response `token_usage_record.usage`, deduplicated by `response_id`. Cached tokens are part of input; reasoning tokens are part of output; total = input + output. Reused context is counted as input again in later requests, so cumulative tokens can greatly exceed the visible answer length.
- When a file has newer records, old `token_count` is not added, avoiding duplicate counts and inherited baselines. Legacy-only logs use cumulative differences; missing beginnings, migration or format changes can affect historical attribution. Cumulative totals are not current context size.
- By default usage is grouped in `Asia/Shanghai` (UTC+8), configurable with `--timezone`, and attributed to response completion dates. Conversations are grouped under the root session ID, with subagent and automatic-approval contributions separated.
- Speed includes output samples with a valid completion time and positive duration only. Excluded turns may still be running, interrupted, or missing logs; their count does not establish live status.
- Speed belongs to the turn completion date and retains the entire turn's output and duration, even across midnight. Usage belongs to each response's date, so the speed sample's output numerator may differ from total output in the selected date range; the dashboard shows the sample numerator separately.
- With a specific model selected, turns that also used other models are excluded from that model's speed to avoid dividing partial model output by the entire turn duration. Usage is still filtered per response. Usage recorded inside the date range for a turn completed outside it remains in the totals; speed belongs to its completion date, while missing completion times are counted separately.
- Combined throughput intervals are reconstructed from completion time and log `duration_ms`; they are not continuous model decoding intervals. Parallelism = sum of turn durations / union of intervals. Median time to first token comes from turn-level `time_to_first_token_ms`; it is not pure generation time and should not simply be subtracted from total duration to estimate decoding speed.
- Conversation details display up to the latest 100 completed turns; the turns CSV exports all qualifying samples. Conversation and daily CSVs include the usage and speed calculation basis for that dimension.
- Today's card follows scope, model, agent tool and Paseo filters independently of the historical date range. Codex counts only local retained `sessions` and `archived_sessions`; other tools use the sources above. Cloud or other-computer conversations, deleted logs and unsaved logs are outside coverage. Local token records are not plan quota, billing cost or official account totals.

## Privacy and sharing

The service listens only on `127.0.0.1` and checks Host and Origin. The page has no CDN, remote fonts, analytics scripts or external API. JSON / JSONL records are briefly read to extract metadata, but conversation text, thinking text and tool output are not saved or returned to the browser; SQLite connections are read-only. Credentials, API keys, `auth.json` and agent configuration are not read. See [PRIVACY.md](../PRIVACY.md) for the full privacy notes (currently in Chinese).

Titles, thread IDs, data paths and exported CSVs can still contain personal information. Do not commit logs, credentials, exports or screenshots. Before sharing source, inspect the Git staging area or archive and exclude `.codex`, `.paseo`, `.claude`, `.zcode`, OpenCode data, databases, virtual environments and runtime data. `.gitignore` helps avoid accidents but cannot remove already tracked files or history.

GitHub stores source code; each computer still needs its own local Python backend. GitHub Pages can host static files but cannot run this backend or read local agent logs. Logs are not automatically synchronized between computers.

This project is licensed under the [MIT License](../LICENSE).

## Troubleshooting

- **The page does not open or loses connection:** check whether the launch terminal has exited or been closed, look for errors, and verify the browser address and port. Run the launch script again and check the service with `curl http://127.0.0.1:8765/api/health` (adjust the address if you changed the port). If the connection is refused, check whether the service started and whether the environment restricts it. The first read of many logs may take several seconds or longer. Restart after closing the terminal or rebooting unless you configured a user service yourself.
- **Port occupied:** use `--port 8766` and visit the corresponding address.
- **No usage or conversations:** verify `--codex-home`, `--claude-home`, `--opencode-home`, `--opencode-db`, `--zcode-home` or `--zcode-db` points to readable local data. For missing Paseo associations, check `--paseo-home`. Cloud, other-computer, deleted or unsaved records are outside coverage. Remove restrictive date, scope, model, agent or Paseo filters.
- **Live list empty or not updating:** enable automatic refresh and select the appropriate scope, all models, all agents and all association states. OpenCode and Z Code live status is not inferred. Automatic reads pause when the page is hidden; completed turns leave the list, and recent log activity does not prove current generation.
- **Time zone unavailable / `ZoneInfoNotFoundError`:** use the same Python interpreter that runs the dashboard to run `python -m pip install tzdata` or `python -m pip install -r requirements.txt`. For a virtual environment use `.venv/bin/python` (Windows: `.venv\Scripts\python.exe`). Check that the IANA zone name is valid. The program does not silently switch to UTC.
- **Python missing or too old:** install Python 3.10+, reopen the terminal, or recreate the project `.venv` using that version.
- **Ubuntu / Debian virtual environment creation reports missing `ensurepip`:** install the venv system package matching the selected Python, such as `python3-venv` for system `python3`. For a specific version such as Python 3.12, check whether your distribution repository provides `python3.12-venv`. Alternatively, select a Python with venv support, for example `python3.12 -m venv .venv`. If creation already failed, first verify that `.venv` is this project's failed environment and can be deleted, then remove it and recreate it. Install dependencies using that environment's Python.
- **Browser did not open:** manually visit the local address printed in the terminal.
- **New agent log format not recognized:** the format may have changed. Describe the issue without sharing private logs; redact titles, IDs, paths and text when reporting it.

## Development and verification

Tests use synthetic data, not personal agent data directories. Node.js is not needed for normal dashboard use; the optional JavaScript syntax check uses Node.js 22 as the project's supported baseline. Node.js 22 or a newer supported version is recommended; see the [Node.js release status](https://nodejs.org/en/about/previous-releases).

```sh
python3 -m unittest discover -s tests -v
# Windows: py -3 -m unittest discover -s tests -v
node --check static/app.js  # Optional: Node.js 22 or a newer supported version
bash -n start.sh           # Linux / macOS
```

`.github/workflows/tests.yml` defines a Linux, macOS and Windows test matrix with Python 3.10 and 3.13, and uses Node.js 22 for the JavaScript syntax check. Windows/macOS results must be verified on those systems or in CI; Linux results do not establish them.

`server.py` contains the local HTTP service, `usage.py` parses and aggregates logs, `providers.py` handles Claude Code / OpenCode / Z Code and Paseo association, `static/` contains the offline frontend, and `tests/` contains synthetic regression tests. There is no build step.
