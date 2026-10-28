#!/usr/bin/env python3
"""A loopback-only Codex usage dashboard; tzdata may be needed on Windows."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfoNotFoundError

from usage import UsageIndex

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


def csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_csv(snapshot, export_type="sessions"):
    if export_type not in ("sessions", "daily", "turns"):
        raise ValueError("导出类型必须为 sessions、daily 或 turns")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    common_columns = ["开始日期", "结束日期", "时区", "统计范围", "模型筛选", "代理工具筛选", "Paseo关联筛选"]
    filters = snapshot.get("filters", {})
    common_values = [snapshot["range"]["start"], snapshot["range"]["end"], snapshot["timezone"],
                     filters.get("scope", "task"), filters.get("model", "all"), filters.get("provider", "all"), filters.get("origin", "all")]
    if export_type == "turns":
        writer.writerow(["代理工具", "Paseo关联", "轮次ID", "线程ID", "所属对话ID", "所属对话标题", "类别", "模型", "完成时间", "输出Token", "轮次秒数", "轮次输出Token每秒", "首Token延迟毫秒", "推理记录完整性", "推理已记录请求数", "推理未单列请求数"] + common_columns)
        turns = [{**turn, "session_id": session["id"], "session_title": session["title"]}
                 for session in snapshot["sessions"] for turn in session["turns"]]
        turns.sort(key=lambda turn: turn["completed_at"], reverse=True)
        for turn in turns:
            values = [turn.get("provider", "codex"), turn.get("origin", "other"), turn["id"], turn["thread_id"], turn["session_id"], turn["session_title"], turn["category"], turn["model"], turn["completed_at"],
                      turn["output_tokens"], turn["duration_seconds"], turn["output_tps"], turn["ttft_ms"], turn.get("reasoning_coverage", "full"), turn.get("reasoning_known_requests", 0), turn.get("reasoning_unknown_requests", 0)]
            writer.writerow([csv_safe(value) if value is not None else "" for value in values + common_values])
    else:
        prefix = ["代理工具", "Paseo关联", "对话ID", "标题", "模型"] if export_type == "sessions" else ["日期"]
        writer.writerow(prefix + ["总Token", "输入Token", "缓存输入Token", "输出Token", "推理输出Token",
            "平均轮次Token每秒", "合计吞吐Token每秒", "已完成轮次累计秒", "活跃墙钟秒", "平均并行度",
            "有效速度轮次", "观察到轮次", "缺少有效完成时间轮次", "混模型排除轮次", "完成日期在范围外轮次", "首Token等待中位毫秒", "推理记录完整性", "推理已记录请求数", "推理未单列请求数"]
            + (["主线程Token", "子代理Token", "自动审批Token"] if export_type == "sessions" else ["对话数"]) + common_columns)
        for row in snapshot[export_type]:
            values = [row.get("provider", "codex"), row.get("origin", "other"), row["id"], row["title"], row["model"]] if export_type == "sessions" else [row["date"]]
            values += [row["total_tokens"], row["input_tokens"], row["cached_input_tokens"], row["output_tokens"],
                       (row["reasoning_output_tokens"] if row.get("reasoning_coverage", "full") != "none" else None), row["average_turn_tps"], row["combined_tps"], row["active_seconds"],
                       row["active_wall_seconds"], row["parallelism"], row["speed_samples"], row["observed_turn_count"],
                       row["excluded_turn_count"], row["mixed_model_excluded_turn_count"], row["outside_range_turn_count"], row["ttft_median_ms"], row.get("reasoning_coverage", "full"), row.get("reasoning_known_requests", 0), row.get("reasoning_unknown_requests", 0)]
            values += [row["main_tokens"], row["agent_tokens"], row["review_tokens"]] if export_type == "sessions" else [row["session_count"]]
            writer.writerow([csv_safe(value) if value is not None else "" for value in values + common_values])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, index):
        super().__init__(("127.0.0.1", port), Handler)
        self.index = index


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexPulse/4.0"

    def log_message(self, fmt, *args):
        # Do not record query text, titles or request metadata.
        pass

    def send(self, status, content, mime="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def json(self, value, status=200):
        self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            return self.json({"error": "仅允许本机访问"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed_hosts}:
            return self.json({"error": "不允许跨站读取本机数据"}, 403)
        url = urlsplit(self.path)
        try:
            if url.path == "/api/health":
                return self.json({"service": "codex-pulse", "version": 4, "timezone": str(self.server.index.tz)})
            if url.path == "/api/live":
                query = parse_qs(url.query, keep_blank_values=True)
                options = {key: query[key][0] for key in ("scope", "model", "provider", "origin") if key in query}
                return self.json(self.server.index.live_snapshot(**options))
            if url.path in ("/api/stats", "/api/export.csv"):
                query = parse_qs(url.query, keep_blank_values=True)
                options = {key: query[key][0] for key in ("days", "scope", "model", "start", "end", "provider", "origin") if key in query}
                export_type = query.get("type", ["sessions"])[0]
                if url.path == "/api/export.csv" and export_type not in ("sessions", "daily", "turns"):
                    raise ValueError("导出类型必须为 sessions、daily 或 turns")
                if url.path == "/api/export.csv" and export_type == "turns":
                    options["turn_limit"] = None  # CSV includes every matching turn, not the UI's last 100.
                data = self.server.index.snapshot(**options)
                if url.path == "/api/stats":
                    return self.json(data)
                return self.send(200, export_csv(data, export_type), "text/csv", {"Content-Disposition": f'attachment; filename="codex-{export_type}.csv"'})
            if url.path in ASSETS:
                filename, mime = ASSETS[url.path]
                return self.send(200, (ROOT / "static" / filename).read_bytes(), mime)
            if url.path == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
            return self.json({"error": "页面不存在"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            return self.json({"error": str(exc)}, 400)
        except Exception as exc:
            print(f"请求处理失败：{type(exc).__name__}", file=sys.stderr)
            return self.json({"error": "无法读取统计，请检查终端和本机日志目录的访问权限。"}, 500)


def main():
    # Redirected Windows terminals may use a legacy encoding. Preserve it, but
    # escape unsupported characters instead of failing before the server starts.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Codex Pulse — 本机用量统计")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--paseo-home", default=None, help="Paseo home，默认 PASEO_HOME 或 ~/.paseo")
    parser.add_argument("--claude-home", default=None, help="Claude 配置目录，默认 CLAUDE_CONFIG_DIR 或 ~/.claude")
    parser.add_argument("--opencode-home", default=None, help="OpenCode 数据目录，默认 XDG_DATA_HOME/opencode 或 ~/.local/share/opencode")
    parser.add_argument("--opencode-db", default=None, help="显式指定只读 OpenCode SQLite 数据库")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--open", action="store_true", help="同时打开默认浏览器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024–65535 之间")
    try:
        index = UsageIndex(args.codex_home, args.timezone, paseo_home=args.paseo_home, claude_home=args.claude_home, opencode_home=args.opencode_home, opencode_db=args.opencode_db)
        server = Server(args.port, index)
    except ZoneInfoNotFoundError:
        parser.error("时区不可用。请用运行本程序的 Python 执行：python -m pip install tzdata（或 python -m pip install -r requirements.txt），然后重试；同时确认 --timezone 使用有效的 IANA 时区，例如 Asia/Shanghai。Linux/macOS 也可安装系统时区数据库。")
    except OSError as exc:
        print(f"无法启动端口 {args.port}（{exc.strerror}）。可用 --port 8766 更换端口。", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{args.port}"
    print(f"Codex Pulse 已启动：{url}\n数据源：{index.home}\n时区：{index.tz} · 实时监控每 10 秒 / 历史统计每 15 秒刷新 · Ctrl+C 停止", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCodex Pulse 已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
