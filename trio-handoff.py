#!/usr/bin/env python3
"""trio-handoff —— 双向同构的 trio 审稿交接包生成器。

协议（CC 与 Codex 共用同一套结构，只是自动抽取来源不同）:

    ## Objective Evidence   [自动抽取·客观可验证]
      - goal / constraints
      - files examined / external evidence (MCP·web·tools)
      - commands + truncated outputs
      - files changed / current diff / current state
      - raw log path
    ## Caller Declaration   [调用方手填·jsonl 抽不到]
      - rejected alternatives + why
      - why this framing / why this approach
      - unresolved questions
      - review focus
      - do-not-repeat unless new evidence

核心约定:
- **只抽可观察轨迹，不抽隐藏思维链**（CC 的 thinking / Codex 的 reasoning 一律排除）：
  含废弃中间想法、不可验证，喂给 reviewer 反而制造新噪音。
- **判断性字段不假装自动生成**：否决理由、framing、风险判断往往只发生在思考里、
  没留可观察痕迹，必须调用方主动声明。这一半才是防"对方重复我已否掉的建议"的关键——
  空着发出去 = 这套交接白做。

两个方向:
    cc-to-codex   : 从 Claude Code JSONL 抽客观证据，交给 Codex review
    codex-to-cc   : 从 Codex rollout transcript 抽客观证据，交给 CC review

用法:
    trio-handoff.py                          # 自动：最近的 CC session → 给 Codex
    trio-handoff.py --direction codex-to-cc  # 最近的 Codex rollout → 给 CC
    trio-handoff.py <path>                   # 显式源文件，方向自动检测
    trio-handoff.py --last-n 3               # 只保留最近 3 个用户回合（两个方向都生效）
    trio-handoff.py --include-subagents      # （CC 方向）连带子 agent 轨迹
    trio-handoff.py --repo ~/Projects/foo    # 指定 repo（默认从改动文件 / workdir / cwd 推断，支持多 repo）
    trio-handoff.py --base origin/main       # diff 对比基线（含已 commit 的改动）
    trio-handoff.py --out /path/x.md         # 指定输出（默认 ~/Desktop/）
    trio-handoff.py --check <bundle.md>      # 发出前自查：Caller Declaration 是否仍是空模板
    trio-handoff.py --allow-empty            # 抽取结果为空时仍生成（默认拒绝生成空壳包）
"""
import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

__version__ = "2.5.0"

# CC session 按 cwd 散落在 ~/.claude/projects/ 的不同子目录（全局聊在 -Users-<you>/，
# repo 内会话在 -Users-<you>-Projects-<repo>/）——只盯一个子目录会系统性漏源。
# TRIO_CC_DIR 兼容旧语义：可指向 projects 根，也可指向具体子目录（两层 glob 都扫）。
CC_PROJECTS = os.environ.get("TRIO_CC_DIR") or os.path.expanduser("~/.claude/projects")
CC_DIR = (  # 根 project 目录（subagents 定位仍按此推）
    lambda b, c: c if os.path.isdir(c) else b
)(
    CC_PROJECTS,
    os.path.join(CC_PROJECTS, os.path.expanduser("~").replace("/", "-")),
)
CODEX_GLOB = os.environ.get("TRIO_CODEX_GLOB") or os.path.expanduser(
    "~/.codex/sessions/*/*/*/rollout-*.jsonl"
)

CC_READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
CC_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
CC_WEB_TOOLS = {"WebFetch", "WebSearch"}
CODEX_EXEC = {"exec_command", "shell"}
CODEX_PATCH = {"apply_patch"}
# shell 里的读取类命令 → 用来从 Codex 命令反推"检查过的文件"
READ_CMDS = {"cat", "sed", "head", "tail", "less", "more", "nl", "bat",
             "rg", "grep", "egrep", "awk", "view", "wc"}

MAX_CMD_OUTPUT = 1000   # 单条命令输出保留上限（超出取 head+tail）
MAX_DIFF = 16000
MAX_ASST_TEXT = 1200
MAX_USER_TEXT = 2000    # 单条用户输入上限：超长多半是粘贴/skill 注入，截断并标注
SELF_MARKER = "trio-handoff"  # 过滤生成器自身命令，避免自污染

# CC 终端 UI 渲染符号：bridge/resume 把可见输出回灌进 user content 时会混进来
UI_PREFIXES = ("⏺", "⎿", "✻", "✢", "·", "✓", "⎯", "│")
# ANSI / 终端控制序列（Codex exec_command 的 PTY 输出里大量出现，如 \x1b[?1049h）
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# HTML 注释（buddy status-line 装饰等）—— 不是 agent 说出口的话
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


# ---------- 通用工具 ----------

def blank():
    return dict(goal=[], examined=[], external=[], commands=[], changed=[], asst=[],
                workdirs=[])


def merge(a, b):
    for k in a:
        a[k].extend(b[k])
    return a


def dedupe(seq):
    seen, res = set(), []
    for x in seq:
        key = x if isinstance(x, str) else repr(x)
        if key not in seen:
            seen.add(key)
            res.append(x)
    return res


def is_noise_user_text(s):
    s2 = s.lstrip()
    return (
        s2.startswith("<system-reminder>")
        or s2.startswith("Caveat:")
        or s2.startswith("<command-")
        or s2.startswith("<local-command")
    )


def clean_user_text(s):
    """剔除 CC 工具调用的 UI 回显行；散文回灌（无符号前缀）无法机械识别，会残留。"""
    keep = [ln for ln in s.splitlines() if not ln.strip().startswith(UI_PREFIXES)]
    return "\n".join(keep).strip()


def cap_user(s):
    """单条用户输入超长截断：skill 注入全文 / 粘贴回来的第三方 review 多半超 2000 字。"""
    if len(s) <= MAX_USER_TEXT:
        return s
    return (s[:MAX_USER_TEXT] +
            f"\n…[超长已截断 {len(s) - MAX_USER_TEXT} 字符；可能含粘贴/skill 注入，核实见原始 log]")


def clean_asst(s):
    """去掉 HTML 注释（buddy 状态行等装饰），它不是 agent 说出口的话。"""
    return HTML_COMMENT_RE.sub("", s).strip()


def clean_output(s, limit=MAX_CMD_OUTPUT):
    """strip ANSI 控制序列，超长取 head+tail（保住 traceback / pass-fail 这类尾部信息）。"""
    s = ANSI_RE.sub("", s).replace("\x1b", "").strip()
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n…[truncated {len(s) - limit} chars]…\n{s[-half:]}"


# 裸文件名（无路径分隔符）靠扩展名识别，否则 `sed -n 1,80p README.md` 不入 files examined，
# 低估 Codex 真实阅读面
_FILE_EXT_RE = re.compile(
    r"\.(py|md|ts|tsx|js|jsx|json|toml|yaml|yml|sh|txt|html|css|rs|go|java|rb|c|h|cpp|sql|cfg|ini|lock)$",
    re.I)


def read_paths_from_cmd(cmd):
    """从 shell 读取类命令反推被读的文件路径（Codex 没有独立 Read 工具）。"""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return []
    tool = os.path.basename(parts[0])
    if tool not in READ_CMDS:
        return []
    return [p for p in parts[1:]
            if not p.startswith("-") and ("/" in p or _FILE_EXT_RE.search(p))]


def load_rows(path):
    """返回 (rows, malformed 行数)。坏行不再纯静默——格式漂移时调用方能看见信号。"""
    rows, bad = [], 0
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return rows, bad


def slice_last_n(rows, n, kind):
    """保留最近 n 个真实用户回合起的 rows；两个方向各按自己的用户消息记号切。"""
    if n <= 0:
        return rows
    idxs = []
    for i, o in enumerate(rows):
        if kind == "cc":
            if o.get("type") == "user":
                c = (o.get("message") or {}).get("content")
                if isinstance(c, str) and not is_noise_user_text(c):
                    idxs.append(i)
        else:  # codex
            p = o.get("payload") or o
            if isinstance(p, dict) and p.get("type") == "user_message":
                m = p.get("message", "")
                if m.strip() and not is_noise_user_text(m):
                    idxs.append(i)
    return rows if len(idxs) <= n else rows[idxs[-n]:]


def parse_json_args(s):
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------- CC JSONL 解析 ----------

def _cc_result_text(rc):
    if isinstance(rc, list):
        return " ".join(x.get("text", "") for x in rc if isinstance(x, dict))
    return str(rc)


def parse_cc(rows, origin="main"):
    out = blank()
    results = {}
    for o in rows:
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = _cc_result_text(b.get("content"))
    for o in rows:
        t = o.get("type")
        c = (o.get("message") or {}).get("content")
        if t == "user":
            texts = [c] if isinstance(c, str) else (
                [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
                if isinstance(c, list) else []
            )
            for tx in texts:
                if tx.strip() and not is_noise_user_text(tx):
                    cleaned = clean_user_text(tx)
                    if cleaned:
                        out["goal"].append(cap_user(cleaned))
        elif t == "assistant" and isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    continue  # 隐藏思维链不传
                if bt == "text":
                    tx = clean_asst(b.get("text", ""))
                    if tx:
                        out["asst"].append(tx[:MAX_ASST_TEXT])
                elif bt == "tool_use":
                    name, inp = b.get("name"), (b.get("input") or {})
                    if name in CC_READ_TOOLS:
                        tgt = (inp.get("file_path") or inp.get("pattern")
                               or inp.get("path") or json.dumps(inp, ensure_ascii=False)[:80])
                        out["examined"].append(f"{name}: {tgt}")
                    elif name in CC_EDIT_TOOLS:
                        out["changed"].append(inp.get("file_path") or inp.get("notebook_path") or "?")
                    elif name == "Bash":
                        cmd = inp.get("command", "")
                        if SELF_MARKER in cmd:
                            continue  # 过滤生成器自身命令
                        out["commands"].append((
                            cmd, inp.get("description", ""),
                            clean_output(results.get(b.get("id"), "")), origin,
                        ))
                    elif name in CC_WEB_TOOLS:
                        q = inp.get("url") or inp.get("query") or json.dumps(inp, ensure_ascii=False)[:80]
                        out["external"].append(f"{name}: {q}")
                    elif isinstance(name, str) and name.startswith("mcp__"):
                        out["external"].append(
                            f"{name}: {json.dumps(inp, ensure_ascii=False)[:80]}")
    return out


def cc_subagents(jsonl_path):
    sid = os.path.basename(jsonl_path).replace(".jsonl", "")
    return sorted(glob.glob(os.path.join(CC_DIR, sid, "subagents", "*.jsonl")))


# ---------- Codex rollout 解析 ----------

def _patch_files(args):
    txt = args.get("input") or args.get("patch") or ""
    files = []
    for ln in txt.splitlines():
        for m in ("*** Update File:", "*** Add File:", "*** Delete File:"):
            if ln.strip().startswith(m):
                files.append(ln.split(m, 1)[1].strip())
    return files


def parse_codex(rows, origin="codex"):
    out = blank()
    results = {}
    for o in rows:
        p = o.get("payload") or o
        if not isinstance(p, dict):
            continue
        if p.get("type") == "function_call_output":
            results[p.get("call_id")] = str(p.get("output", ""))
        elif p.get("type") == "mcp_tool_call_end":
            results[p.get("call_id")] = str(p.get("result", ""))
    for o in rows:
        p = o.get("payload") or o
        if not isinstance(p, dict):
            continue
        it = p.get("type")
        if it == "user_message":
            msg = p.get("message", "")
            if msg.strip() and not is_noise_user_text(msg):
                out["goal"].append(cap_user(msg.strip()))
        elif it == "agent_message":
            msg = clean_asst(p.get("message", ""))
            if msg:
                out["asst"].append(msg[:MAX_ASST_TEXT])
        elif it == "reasoning":
            continue  # 隐藏思维链不传
        elif it == "function_call":
            name = p.get("name", "?")
            args = parse_json_args(p.get("arguments"))
            res = results.get(p.get("call_id"), "")
            if name in CODEX_EXEC:
                cmd = args.get("cmd") or args.get("command") or ""
                if SELF_MARKER in cmd:
                    continue  # 过滤生成器自身命令（防自污染）
                wd = args.get("workdir", "")
                if wd:
                    out["workdirs"].append(wd)  # apply_patch 常是相对路径，repo 定位靠 workdir 兜底
                out["commands"].append(
                    (cmd, f"workdir={wd}" if wd else "", clean_output(res), origin))
                for fp in read_paths_from_cmd(cmd):   # 读取类命令 → 反推检查过的文件
                    out["examined"].append(f"shell: {fp}")
            elif name in CODEX_PATCH:
                out["changed"].extend(_patch_files(args) or ["(apply_patch)"])
            else:
                # duo / RecallNest / write_stdin 等 → 外部工具证据，不混进 files examined
                out["external"].append(
                    f"{name}: {json.dumps(args, ensure_ascii=False)[:80]}")
    return out


# ---------- repo 信息（支持多 repo）----------

def _git_root(start):
    try:
        r = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def collect_repos(changed, repo_arg, workdirs=None):
    """收集所有改动文件涉及的 git repo（跨 repo 改动全都要 diff）。
    推断顺序：--repo > 改动文件绝对路径向上爬 > exec workdir（Codex apply_patch
    常用相对路径，dirname 爬不到 .git，workdir 是它真正的落点）> cwd fallback。
    cwd fallback 在"不在目标 repo 里跑生成器"时会 diff 错 repo——所以 main 里
    必须把最终 repo 列表打出来让调用方确认。"""
    if repo_arg:
        return [os.path.expanduser(repo_arg)]
    repos = []
    for fp in changed:
        d = os.path.dirname(os.path.expanduser(fp))
        while d and d != "/":
            if os.path.isdir(os.path.join(d, ".git")):
                if d not in repos:
                    repos.append(d)
                break
            d = os.path.dirname(d)
    if not repos and workdirs:
        for wd in dedupe(workdirs):
            root = _git_root(os.path.expanduser(wd))
            if root and root not in repos:
                repos.append(root)
    if not repos:
        root = _git_root(os.getcwd())
        if root:
            repos = [root]
    return repos


class GitDiffError(RuntimeError):
    """git diff 无法提供可信结果。"""


def _run_git_diff(repo, args):
    command = ["git", "-C", repo, "diff", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDiffError(f"运行 {shlex.join(command)} 失败: {exc}") from exc
    if result.returncode != 0:
        detail = clean_output(result.stderr or result.stdout, limit=500)
        suffix = f": {detail}" if detail else ""
        raise GitDiffError(
            f"{shlex.join(command)} 失败 (exit {result.returncode}){suffix}"
        )
    return result.stdout


def git_diff(repo, base=None):
    parts = []
    if base:
        parts.append(_run_git_diff(repo, [f"{base}...HEAD"]))
    parts.append(_run_git_diff(repo, ["--staged"]))
    parts.append(_run_git_diff(repo, []))
    diff = "".join(parts).strip()
    return diff[:MAX_DIFF] if diff else ""


def git_status(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--short", "--branch"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except Exception:
        return None


def git_repo_anchor(repo):
    """v1.10：repo 版本锚点。每个 repo 给一行 branch / HEAD / dirty / ahead-behind / remote，
    避免 reviewer 二审时自己 git rev-parse + git status 补查。"""
    try:
        def run(args, timeout=10):
            r = subprocess.run(["git", "-C", repo, *args],
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else ""

        branch = run(["branch", "--show-current"]) or "(detached)"
        head = run(["rev-parse", "--short", "HEAD"]) or "?"
        porcelain = run(["status", "--porcelain"])
        dirty_count = len(porcelain.splitlines()) if porcelain else 0
        remote = run(["config", "remote.origin.url"]) or "(no remote)"

        ahead, behind = "?", "?"
        upstream_out = run(["rev-list", "--left-right", "--count", "@{u}...HEAD"])
        if upstream_out:
            parts = upstream_out.split()
            if len(parts) == 2:
                behind, ahead = parts[0], parts[1]

        return {
            "path": repo,
            "branch": branch,
            "head": head,
            "dirty_count": dirty_count,
            "ahead": ahead,
            "behind": behind,
            "remote": remote,
        }
    except Exception:
        return None


# ---------- 渲染（双向同构）----------

PROMPTS = {
    "cc-to-codex": "Codex，请先读这个交接包。**第一动作**：跑 repo anchors 段的 verify 命令核对现实"
                   "——不符说明 bundle 生成后 repo 已变动，本包 diff/状态不可信，停止 review 并要求重新生成。"
                   "**第二动作**：检查文末 Caller Declaration——若仍是空模板（只有注释占位、"
                   "rejected alternatives 无内容），先打回要求填写再 review，不要基于半份交接开工。"
                   "然后提取：① 目标和约束 ② CC 已检查过的证据 ③ CC 已否掉的方案 ④ 当前 diff。"
                   "**不要重复提出 CC 已明确否掉的建议，除非你能指出新的证据。**"
                   "如需核实可下钻文末原始 log，不必盲信本包的压缩。",
    "codex-to-cc": "CC，请先读这个交接包。**第一动作**：跑 repo anchors 段的 verify 命令核对现实"
                   "——不符说明 bundle 生成后 repo 已变动，本包 diff/状态不可信，停止 review 并要求重新生成。"
                   "**第二动作**：检查文末 Caller Declaration——若仍是空模板（只有注释占位、"
                   "rejected alternatives 无内容），先打回要求填写再 review，不要基于半份交接开工。"
                   "然后提取：① 目标和约束 ② Codex 已检查过的证据 ③ Codex 已否掉的方案 ④ 当前 diff/状态。"
                   "**不要重复提出 Codex 已明确否掉的建议，除非你能指出新的证据。**"
                   "如需核实可下钻文末原始 log，不必盲信本包的压缩。",
}


# ---------- Execution Boundary（固定纪律，借鉴 repo-harness contract-run EXECUTION_BOUNDARY，2026-07-06）----------
# 接收方执行/审阅本交接时的行为边界。固定文本、非手填——每份 bundle 都印，防被派方自作主张扩 scope 镀金。
# 对应 Alice CLAUDE.md「只写已完成 / 不 gold-plate」，把它前置到派活那一刻。源：Ancienttwo/repo-harness contract-run.ts:67-75。
EXECUTION_BOUNDARY = (
    "> 接收方执行或审阅本交接时守这条边界：\n"
    "> - **缺失的需求 = 禁区，不是改进许可。** 只做本包 goal / in-scope / allowed paths 之内的事。\n"
    "> - 不加未被请求的 feature / 迁移 / 兼容层 / telemetry / 顺手重构。\n"
    "> - 发现「顺手能做」的额外工作 → 只记录、交回请求方决定，不自行执行。\n"
    "> - 要靠扩大 scope 才能完成 → 停下，点名缺的决策，不自作主张填空。"
)


# ---------- 脱敏（移植自 constant alembic::redact, MIT；2026-06-08 借鉴审计）----------
# 交接包会发给另一方 + 落 ~/Desktop/，diff / 命令输出 / remote url 都可能夹带 secret。
# 顺序重要：具体 token 形状在前，通用 key=value 在后。
# 这是 best-effort 安全网而非完备 secret scanner；发出前仍须人工检查生成出的 bundle。
# 接受 over-redaction：宁可黑掉合法的 a@b.com / "token: x"，也不把常见凭证带过边界。
_REDACTORS = [
    # 多行私钥必须先于逐行 / 通用规则处理。
    (re.compile(
        r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
        r"-----END (?P=label)-----",
        re.S,
    ), "[redacted-private-key]"),
    # URL credentials must run before email redaction (password@host otherwise looks like an email).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@"),
     r"\g<1>[redacted-user]:[redacted-password]@"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[redacted-email]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[redacted-jwt]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted-aws-key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,50}\b"), "[redacted-google-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[redacted-key]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[redacted-token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "[redacted-token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[redacted-token]"),
    (re.compile(r"\b(?:xai-|jina_|npm_|hf_|glpat-)[A-Za-z0-9_-]{16,}\b"), "[redacted-token]"),
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "[redacted-bot-token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    # JSON / Python / shell quoted assignments, including provider-prefixed env names.
    (re.compile(
        r"(?i)([\"']?[A-Za-z][A-Za-z0-9_-]*"
        r"(?:api[_-]?key|token|secret|password|authorization|bearer)"
        r"[A-Za-z0-9_-]*[\"']?\s*[:=]\s*)([\"'])[^\"'\r\n]*\2"
    ), r"\g<1>\g<2>[redacted]\g<2>"),
    # Unquoted assignments and command-line flags are common in captured commands.
    (re.compile(
        r"(?i)(\b[A-Za-z][A-Za-z0-9_-]*"
        r"(?:api[_-]?key|token|secret|password|authorization|bearer)"
        r"[A-Za-z0-9_-]*\b\s*[:=]\s*)([^\s,;}\]]+)"
    ), r"\g<1>[redacted]"),
    (re.compile(
        r"(?i)((?:--)[A-Za-z0-9_-]*"
        r"(?:api[_-]?key|token|secret|password|authorization|bearer)"
        r"[A-Za-z0-9_-]*\s+)(\S+)"
    ), r"\g<1>[redacted]"),
    (re.compile(r"(?i)(\bauthorization\b\s*:\s*).*"), r"\g<1>[redacted]"),
]


def redact(text):
    """移植自 constant alembic::redact（思路源 inmzhang/transession, MIT）。
    对常见凭证做 best-effort 脱敏；不是完备扫描器，发出前仍须检查 bundle。"""
    for pat, repl in _REDACTORS:
        text = pat.sub(repl, text)
    return text


def render(data, direction, src_path, sub_paths, diffs, statuses):
    src = "CC JSONL" if direction == "cc-to-codex" else "Codex rollout"
    L = [f"# Trio Handoff　[{direction}]"]
    L.append(f"> 生成：{datetime.now():%Y-%m-%d %H:%M:%S}　来源：{src} `{os.path.basename(src_path)}`")
    if sub_paths:
        L.append(f"> 含 {len(sub_paths)} 个子 agent 轨迹")
    L.append("\n## Review 指令")
    L.append(PROMPTS[direction])

    L.append("\n## Execution Boundary　[固定纪律·接收方执行或审阅时守]")
    L.append(EXECUTION_BOUNDARY)
    L.append("\n---\n")

    # ===== Objective Evidence =====
    L.append("## Objective Evidence　[自动抽取·客观可验证]\n")

    L.append("### goal / constraints")
    goal = dedupe(data["goal"])
    if goal:
        for i, m in enumerate(goal, 1):
            L.append(f"**[{i}]** {m}\n")
    else:
        L.append("_（未捕获）_\n")

    examined = dedupe(data["examined"])
    if examined:
        L.append("### files examined")
        L.extend(f"- {e}" for e in examined)
        L.append("")

    external = dedupe(data["external"])
    if external:
        L.append("### external evidence (MCP / web / tools)")
        L.extend(f"- {e}" for e in external)
        L.append("")

    L.append("### commands + truncated outputs")
    if data["commands"]:
        for cmd, note, out, origin in data["commands"]:
            tag = " _(子agent)_" if origin == "sub" else ""
            head = f"# {note}{tag}".rstrip() if note or tag else "#"
            L.append(f"```bash\n{head}\n{cmd}\n```")
            if out.strip():
                L.append(f"<sub>输出：{out.strip()}</sub>\n")
    else:
        L.append("_（无）_\n")

    L.append("### files changed")
    changed = dedupe(data["changed"])
    L.extend(f"- `{c}`" for c in changed) if changed else L.append("_（无）_")
    L.append("")

    L.append("### current diff")
    if not diffs:
        L.append("_（未定位到 git repo；改动可能在非 repo 路径，见 files changed + Caller Declaration）_")
    else:
        for repo, d in diffs:
            L.append(f"**repo: `{repo}`**")
            if d:
                L.append(f"```diff\n{d}\n```")
            elif d == "":
                L.append("_（无 diff，可能已全部 commit；审已提交改动用 --base）_")
            else:
                L.append("_（diff 读取失败）_")
        L.append("")

    live_status = [(r, s) for r, s in statuses if s]
    if live_status:
        L.append("### current state")
        for repo, s in live_status:
            L.append(f"**repo: `{repo}`**\n```\n{s}\n```")
        L.append("")

    # ===== v1.10：repo anchors（每个 repo 的版本锚点）=====
    repo_paths = [r for r, _ in statuses] or [r for r, _ in diffs]
    anchors = [git_repo_anchor(r) for r in repo_paths]
    anchors = [a for a in anchors if a]
    if anchors:
        L.append("### repo anchors　[v1.11·自动抽取]")
        L.append("> 二审 reviewer 需要的版本锚点。**reviewer 第一动作：跑下面每条 verify 核对现实**"
                 "——文档可能过时，命令输出不会。")
        for a in anchors:
            L.append(f"- `{a['path']}` — branch `{a['branch']}` @ `{a['head']}`　"
                     f"dirty: {a['dirty_count']} files　ahead: {a['ahead']}　behind: {a['behind']}")
            L.append(f"  remote: `{a['remote']}`")
            L.append(f"  verify: `git -C {a['path']} log --oneline -1` → 预期 `{a['head']}`；"
                     f"`git -C {a['path']} status --porcelain | wc -l` → 预期 {a['dirty_count']}。"
                     f"不符 → bundle 生成后 repo 已变动，本包 diff/状态不可信，要求重新生成")
        L.append("")

    # ===== v1.10：runtime surfaces checked（让 Caller 标注盲区）=====
    L.append("### runtime surfaces checked　[v1.10·Caller 标注·自动抽不到]")
    L.append("> 二审最常被反问的「调度/暴露面」——以下 surface 是否查过？")
    L.append("> 查过填 ✓ + 一行结论；没查就留 `_未查_`，让 reviewer 知道这是已知盲区而非疏漏。")
    L.append("- cron / launchd / systemd timers: _未查_")
    L.append("- MCP tools registered: _未查_")
    L.append("- API routes / HTTP endpoints: _未查_")
    L.append("- boot / startup hooks / login items: _未查_")
    L.append("- package.json / pyproject.toml scripts: _未查_")
    L.append("")

    L.append("### public statements　[已说出口的，非思维链]")
    if data["asst"]:
        L.extend(f"> {tx}\n" for tx in dedupe(data["asst"]))
    else:
        L.append("_（无）_")
    L.append("\n---\n")

    # ===== Caller Declaration =====
    other = "Codex" if direction == "cc-to-codex" else "CC"
    L.append(f"## Caller Declaration　[调用方手填·{src} 抽不到]")
    L.append(f"> ⚠️ 这几项 {other} 从轨迹里读不到，是它最容易重复无效建议的盲区。"
             f"**空着发出去 = 这套交接白做**（实测结论）。\n")
    L.append("### rejected alternatives + why\n<!-- 试过但放弃的路线 + 放弃的具体原因。最关键的一栏，别留空 -->\n")
    L.append("### why this framing / why this approach\n<!-- 为什么定义成现在这个问题、为什么选这条路 -->\n")
    L.append("### unresolved questions\n<!-- -->\n")
    L.append(f"### review focus\n<!-- 想让 {other} 重点判断什么，别让它用通用 review 模式泛泛扫 -->\n")
    L.append("### do-not-repeat unless new evidence\n<!-- 已经否掉、不要再提的建议 -->\n")
    L.append("### confidence / evidence gap　[v1.10]\n"
             "<!-- 每个核心判断的置信度 + 没全量验证的部分。让 reviewer 知道哪里是强判断哪里是风险区。\n"
             "格式例：\n"
             "- claim X：高 / 已源码验证\n"
             "- claim Y：中 / 只 grep 了一处，未全量\n"
             "- claim Z：低 / 推论未验证，期待 reviewer 补 -->\n")
    L.append("### without-review baseline　[v1.13·降级退路]\n"
             "<!-- reviewer 不可用（僵死/看门狗止损/超时）时，我会按什么判断继续、承担什么风险。\n"
             "有这栏，交接挂掉可引用它出单方结论、状态标 PARTIAL_EXEC，而不是整件事卡死。\n"
             "空着只警告不阻断（对齐「确定性才配硬拦」）。源：OpenSquilla aggregator 单点无兜底教训（2026-07-09） -->\n")
    L.append("### falsifier / cheapest disproof　[v1.12·brief 完整性]\n"
             "<!-- 什么证据或最小实验能最快证明这个方向错了？先验证哪一步最便宜？对抗谄媚和方向跑偏。 -->\n")
    L.append("### exit criteria (machine-checkable)　[v1.12·brief 完整性]\n"
             "<!-- 完成的客观判据，尽量写成可跑命令 / 可核验清单，别用「感觉可以」。\n"
             "格式例：\n"
             "- `pytest tests/ -q` 全绿\n"
             "- `grep -c TODO file` 返回 0 -->\n")
    L.append("---\n")

    L.append("## Drill-down（下钻入口）")
    L.append(f"- 原始 log：`{src_path}`")
    for p in sub_paths:
        L.append(f"- 子 agent：`{p}`")
    return redact("\n".join(L))


# ---------- 发出前自查（--check）----------

def check_bundle(path):
    """检查 bundle 的 Caller Declaration 是否仍是空模板。
    rejected alternatives 为空 → exit 1（实测结论：这栏空着发出去整套交接白做）；
    其他栏空只警告。这是发送方的最后一道 gate，对应 Review 指令里 reviewer 侧的打回。"""
    try:
        text = open(os.path.expanduser(path)).read()
    except OSError as e:
        print(f"✗ 读不到 bundle: {e}")
        return 2
    # 行首锚定 + 取最后一次出现：bundle 的 diff / 命令输出里可能嵌同样的字符串
    # （比如交接的改动恰好是 markdown 或本工具自身源码），声明区永远在尾部
    idx = text.rfind("\n## Caller Declaration")
    if idx < 0:
        print("✗ 找不到 Caller Declaration 段——这不是 trio-handoff bundle？")
        return 2
    body = text[idx + 1:].split("\n## ", 1)[0]
    empty = []
    for chunk in re.split(r"^### ", body, flags=re.M)[1:]:
        title, _, rest = chunk.partition("\n")
        content = HTML_COMMENT_RE.sub("", rest)
        content = re.sub(r"^-+\s*$", "", content, flags=re.M).strip()
        if not content:
            empty.append(title.strip())

    # v1.12：brief 完整性——Objective Evidence 的 goal / constraints 是否捕获到
    # （借鉴 repo-harness contract-run runBriefPreflight：goal 空 = brief 不完整、reviewer 无从判方向）
    # 克制取舍：唯一硬闸仍是 rejected（exit 1）；goal / falsifier / exit criteria 只强提示不阻断
    # ——对齐「确定性才配硬拦、别过度 fail-closed」。goal 自动抽本就不稳，硬拦会误伤正常流程。
    gm = re.search(r"### goal / constraints\n(.*?)(?=\n### |\n## |\Z)", text, re.S)
    goal_missing = (gm is None) or ("未捕获" in gm.group(1)) or (not gm.group(1).strip())

    if not empty and not goal_missing:
        print("✓ Caller Declaration 已填写、goal 已捕获，可以发出")
        return 0
    if goal_missing:
        print("✗ brief：Objective Evidence 的 goal / constraints 未捕获——建议手动补一句目标（reviewer 判方向要用）。")
    if empty:
        print("✗ Caller Declaration 未填字段：")
        for e in empty:
            print(f"  - {e}")
    if any("rejected" in e for e in empty):
        print("→ rejected alternatives 空着 = 这套交接白做（实测结论）。先填再发。")
        return 1
    print("→ 命门 rejected alternatives 已填；goal / falsifier / exit criteria 建议补齐再发。")
    return 0


# ---------- 方向 / 源检测 ----------

def detect_kind(path):
    with open(path) as fh:
        for _ in range(5):
            line = fh.readline()
            if not line:
                break
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "session_meta" or "payload" in o:
                return "codex"
            if o.get("type") in ("user", "assistant", "summary") or "sessionId" in o:
                return "cc"
    return "cc"


def _jsonl_last_ts(path, tail_bytes=8192):
    """读文件尾部若干字节，取最后一条带 timestamp 的行。失败返回 None。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
        for line in reversed(chunk.splitlines()):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("timestamp"):
                return str(o["timestamp"])
    except Exception:
        pass
    return None


def latest(pattern_or_dir, kind="cc"):
    """选"最近活跃"的 session。

    不能只按 mtime：bridge / patch 进程会批量刷新 jsonl 的 mtime，多 bot 并发时
    最新 N 个文件 mtime 完全相同，max(mtime) 退化成抽签。
    做法：mtime 粗筛 top-N（真·最新文件的 mtime 必然也在最前），再按 jsonl 内部
    末条 timestamp 精排。CC 方向额外扫 projects/*/ 全部子目录（session 按 cwd 散落）。"""
    if os.path.isdir(pattern_or_dir):
        fs = glob.glob(os.path.join(pattern_or_dir, "*.jsonl"))
        if kind == "cc":  # repo 内会话落在子目录，必须两层都扫
            fs += glob.glob(os.path.join(pattern_or_dir, "*", "*.jsonl"))
    else:
        fs = glob.glob(pattern_or_dir)
    if not fs:
        return None
    candidates = sorted(fs, key=os.path.getmtime, reverse=True)[:200]
    ranked = []
    for f in candidates:
        ts = _jsonl_last_ts(f)
        if ts:
            ranked.append((ts, f))
    if ranked:
        ranked.sort(reverse=True)  # ISO 时间戳字符串可直接比较
        return ranked[0][1]
    return candidates[0]  # 都解析不出 timestamp 时退回 mtime 序


def first_user_snippet(path, kind, limit=160):
    """源文件首条真实用户输入的摘要——给调用方一眼确认选源没选错。"""
    try:
        with open(path) as fh:
            for i, line in enumerate(fh):
                if i > 400:
                    break
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind == "cc":
                    if o.get("type") == "user":
                        c = (o.get("message") or {}).get("content")
                        if isinstance(c, str) and not is_noise_user_text(c):
                            t = clean_user_text(c)
                            if t:
                                return t[:limit].replace("\n", " ")
                else:
                    p = o.get("payload") or o
                    if isinstance(p, dict) and p.get("type") == "user_message":
                        m = p.get("message", "")
                        if m.strip() and not is_noise_user_text(m):
                            return m.strip()[:limit].replace("\n", " ")
    except Exception:
        pass
    return "（未捕获到用户输入）"


def main():
    ap = argparse.ArgumentParser(description="生成双向 trio 审稿交接包")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("source", nargs="?", help="源 jsonl（默认按方向取最近的）")
    ap.add_argument("--direction", choices=["cc-to-codex", "codex-to-cc"],
                    help="方向（默认从源自动检测，再默认 cc-to-codex）")
    ap.add_argument("--last-n", type=int, default=0, help="只保留最近 N 个用户回合（两方向通用）")
    ap.add_argument("--include-subagents", action="store_true", help="（CC 方向）带子 agent 轨迹")
    ap.add_argument("--repo", help="指定 repo（默认从改动文件 / workdir / cwd 推断，支持多 repo）")
    ap.add_argument("--base", help="diff 对比基线，如 origin/main（含已 commit 的改动）")
    ap.add_argument("--out", help="输出路径（默认 ~/Desktop/）")
    ap.add_argument("--check", metavar="BUNDLE",
                    help="发出前自查：Caller Declaration 是否仍是空模板（rejected 空则 exit 1）")
    ap.add_argument("--allow-empty", action="store_true",
                    help="抽取结果为空时仍生成 bundle（默认拒绝生成空壳包）")
    args = ap.parse_args()

    # 0) --check 模式：只做发出前自查
    if args.check:
        sys.exit(check_bundle(args.check))

    # 1) 定源 + 方向
    if args.source:
        src = os.path.expanduser(args.source)
        if not os.path.exists(src):
            ap.error(f"源不存在: {src}")
        direction = args.direction or (
            "codex-to-cc" if detect_kind(src) == "codex" else "cc-to-codex")
    else:
        direction = args.direction or "cc-to-codex"
        src = (latest(CC_PROJECTS, "cc") if direction == "cc-to-codex"
               else latest(CODEX_GLOB, "codex"))
        if not src:
            ap.error("找不到默认源，请显式传 source 路径")
    kind = "cc" if direction == "cc-to-codex" else "codex"
    print(f"方向: {direction}\n源: {src}")
    # 选源确认：多 bot / 多窗口并发时"最近 session"可能不是你想交接的那个——
    # 给一行首条输入摘要让调用方一眼核对，选错就显式传 source 路径重跑
    print(f"  末条时间: {_jsonl_last_ts(src) or '?'}")
    print(f"  首条输入: {first_user_snippet(src, kind)}")

    # 2) 解析（last-n 两方向通用）
    all_rows, bad = load_rows(src)
    rows = slice_last_n(all_rows, args.last_n, kind)
    if kind == "cc":
        data = parse_cc(rows)
        sub_paths = []
        if args.include_subagents:
            sub_paths = cc_subagents(src)
            for sp in sub_paths:
                sub_rows, sub_bad = load_rows(sp)
                bad += sub_bad
                data = merge(data, parse_cc(slice_last_n(sub_rows, args.last_n, "cc"), "sub"))
    else:
        data = parse_codex(rows)
        sub_paths = []
    if bad:
        print(f"⚠️  源文件含 {bad} 行无法解析的 JSON——若数量异常大，session 格式可能已漂移")

    # 2.5) 空壳门：抽不出任何核心证据时拒绝生成（静默空壳比显式失败更坏）
    if not (dedupe(data["goal"]) or data["commands"] or dedupe(data["changed"])) \
            and not args.allow_empty:
        print("✗ 抽取结果为空（goal / commands / changed 均无）：源可能选错、--last-n 过小、"
              "或 session 格式已漂移。")
        print("  确认就要生成空包 → 加 --allow-empty；选错源 → 显式传 source 路径。")
        sys.exit(2)

    # 3) repo 信息（多 repo + workdir 兜底 + cwd fallback + 可选 base）
    repos = collect_repos(data["changed"], args.repo, data.get("workdirs"))
    try:
        diffs = [(r, git_diff(r, args.base)) for r in repos]
    except GitDiffError as e:
        ap.error(str(e))
    statuses = [(r, git_status(r)) for r in repos]

    # 4) 渲染 + 写出
    md = render(data, direction, src, sub_paths, diffs, statuses)
    tag = "cc2cx" if direction == "cc-to-codex" else "cx2cc"
    out = (os.path.expanduser(args.out) if args.out else
           os.path.expanduser(f"~/Desktop/trio-handoff-{tag}-{datetime.now():%H%M%S}.md"))
    with open(out, "w") as fh:
        fh.write(md)
    has_diff = any(d for _, d in diffs)
    print(f"交接包已生成: {out}")
    print(f"  goal {len(dedupe(data['goal']))} | examined {len(dedupe(data['examined']))} "
          f"| external {len(dedupe(data['external']))} | commands {len(data['commands'])} "
          f"| changed {len(dedupe(data['changed']))} | repos {len(repos)} "
          f"| diff {'有' if has_diff else '无'}")
    if repos:
        for r in repos:
            print(f"  repo: {r}")
    else:
        print("  repo: （未定位到——改动若在某 repo 内请用 --repo 指定后重跑）")
    print("⚠️  发出前务必手填 Caller Declaration（尤其 rejected alternatives）——空着这套交接白做。")
    print(f"   填完自查: trio-handoff.py --check {out}")


if __name__ == "__main__":
    main()
