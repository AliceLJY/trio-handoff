#!/usr/bin/env python3
"""trio-handoff —— 双向同构的 trio 审稿交接包生成器。

协议（CC 与 Codex 共用同一套结构，只是自动抽取来源不同）:

    ## Objective Evidence   [自动抽取·客观可验证]
      - goal / constraints
      - files examined / commands + truncated outputs
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
  没留可观察痕迹，必须调用方主动声明。这一半才是防"对方重复我已否掉的建议"的关键。

两个方向:
    cc-to-codex   : 从 Claude Code JSONL 抽客观证据，交给 Codex review
    codex-to-cc   : 从 Codex rollout transcript 抽客观证据，交给 CC review

用法:
    trio-handoff.py                          # 自动：最近的 CC session → 给 Codex
    trio-handoff.py --direction codex-to-cc  # 最近的 Codex rollout → 给 CC
    trio-handoff.py <path>                   # 显式源文件，方向自动检测
    trio-handoff.py --last-n 3               # 只保留最近 3 个用户回合
    trio-handoff.py --include-subagents      # （CC 方向）连带子 agent 轨迹
    trio-handoff.py --repo ~/Projects/foo    # 指定在哪个 repo 跑 git diff/status
    trio-handoff.py --out /path/x.md         # 指定输出（默认 ~/Desktop/）
"""
import argparse
import glob
import json
import os
import subprocess
from datetime import datetime

def _default_cc_dir():
    """CC 把 cwd 编码进 projects 目录名（/Users/x → -Users-x）；动态构造，不硬编码用户名。"""
    base = os.path.expanduser("~/.claude/projects")
    cand = os.path.join(base, os.path.expanduser("~").replace("/", "-"))
    return cand if os.path.isdir(cand) else base


CC_DIR = os.environ.get("TRIO_CC_DIR") or _default_cc_dir()
CODEX_GLOB = os.environ.get("TRIO_CODEX_GLOB") or os.path.expanduser(
    "~/.codex/sessions/*/*/*/rollout-*.jsonl"
)

CC_READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
CC_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
CODEX_EXEC = {"exec_command", "shell"}
CODEX_PATCH = {"apply_patch"}

MAX_CMD_OUTPUT = 800
MAX_DIFF = 16000
MAX_ASST_TEXT = 1200

# CC 终端 UI 渲染符号：bridge/resume 把可见输出回灌进 user content 时会混进来
UI_PREFIXES = ("⏺", "⎿", "✻", "✢", "·", "✓", "⎯", "│")


# ---------- 通用工具 ----------

def blank():
    return dict(goal=[], examined=[], commands=[], changed=[], asst=[])


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


def load_rows(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


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
                        out["goal"].append(cleaned)
        elif t == "assistant" and isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    continue  # 隐藏思维链不传
                if bt == "text" and b.get("text", "").strip():
                    out["asst"].append(b["text"].strip()[:MAX_ASST_TEXT])
                elif bt == "tool_use":
                    name, inp = b.get("name"), (b.get("input") or {})
                    if name in CC_READ_TOOLS:
                        tgt = (inp.get("file_path") or inp.get("pattern")
                               or inp.get("path") or json.dumps(inp, ensure_ascii=False)[:80])
                        out["examined"].append(f"{name}: {tgt}")
                    elif name in CC_EDIT_TOOLS:
                        out["changed"].append(inp.get("file_path") or inp.get("notebook_path") or "?")
                    elif name == "Bash":
                        out["commands"].append((
                            inp.get("command", ""), inp.get("description", ""),
                            results.get(b.get("id"), "")[:MAX_CMD_OUTPUT], origin,
                        ))
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
                out["goal"].append(msg.strip())
        elif it == "agent_message":
            msg = p.get("message", "")
            if msg.strip():
                out["asst"].append(msg.strip()[:MAX_ASST_TEXT])
        elif it == "reasoning":
            continue  # 隐藏思维链不传
        elif it == "function_call":
            name = p.get("name", "?")
            args = parse_json_args(p.get("arguments"))
            res = results.get(p.get("call_id"), "")
            if name in CODEX_EXEC:
                cmd = args.get("cmd") or args.get("command") or ""
                wd = args.get("workdir", "")
                note = f"workdir={wd}" if wd else ""
                out["commands"].append((cmd, note, res[:MAX_CMD_OUTPUT], origin))
            elif name in CODEX_PATCH:
                out["changed"].extend(_patch_files(args) or ["(apply_patch)"])
            else:
                # duo / RecallNest / 其他 MCP 工具调用 → 算"检查过的证据"
                out["examined"].append(f"{name}: {json.dumps(args, ensure_ascii=False)[:80]}")
    return out


# ---------- repo 信息 ----------

def infer_repo(changed, repo_arg):
    if repo_arg:
        return os.path.expanduser(repo_arg)
    for fp in changed:
        d = os.path.dirname(os.path.expanduser(fp))
        while d and d != "/":
            if os.path.isdir(os.path.join(d, ".git")):
                return d
            d = os.path.dirname(d)
    return None


def git_diff(repo):
    if not repo:
        return None
    try:
        staged = subprocess.run(["git", "-C", repo, "diff", "--staged"],
                                capture_output=True, text=True, timeout=20).stdout
        unstaged = subprocess.run(["git", "-C", repo, "diff"],
                                  capture_output=True, text=True, timeout=20).stdout
        diff = (staged + unstaged).strip()
        return diff[:MAX_DIFF] if diff else ""
    except Exception:
        return None


def git_status(repo):
    if not repo:
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--short", "--branch"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except Exception:
        return None


# ---------- 渲染（双向同构）----------

PROMPTS = {
    "cc-to-codex": "Codex，请先读这个交接包，重点提取：① 目标和约束 ② CC 已检查过的证据 "
                   "③ CC 已否掉的方案 ④ 当前 diff。然后再 review。"
                   "**不要重复提出 CC 已明确否掉的建议，除非你能指出新的证据。**"
                   "如需核实可下钻文末原始 log，不必盲信本包的压缩。",
    "codex-to-cc": "CC，请先读这个交接包，重点提取：① 目标和约束 ② Codex 已检查过的证据 "
                   "③ Codex 已否掉的方案 ④ 当前 diff/状态。然后再 review。"
                   "**不要重复提出 Codex 已明确否掉的建议，除非你能指出新的证据。**"
                   "如需核实可下钻文末原始 log，不必盲信本包的压缩。",
}


def render(data, direction, src_path, sub_paths, repo, diff, status):
    src = "CC JSONL" if direction == "cc-to-codex" else "Codex rollout"
    L = [f"# Trio Handoff　[{direction}]"]
    L.append(f"> 生成：{datetime.now():%Y-%m-%d %H:%M:%S}　来源：{src} `{os.path.basename(src_path)}`")
    if sub_paths:
        L.append(f"> 含 {len(sub_paths)} 个子 agent 轨迹")
    L.append("\n## Review 指令")
    L.append(PROMPTS[direction])
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

    L.append("### commands + truncated outputs")
    if data["commands"]:
        for cmd, note, out, origin in data["commands"]:
            tag = " _(子agent)_" if origin == "sub" else ""
            head = f"# {note}{tag}".rstrip() if note or tag else "#"
            L.append(f"```bash\n{head}\n{cmd}\n```")
            if out.strip():
                L.append(f"<sub>输出(截断)：{out.strip()}</sub>\n")
    else:
        L.append("_（无）_\n")

    L.append("### files changed")
    changed = dedupe(data["changed"])
    L.extend(f"- `{c}`" for c in changed) if changed else L.append("_（无）_")
    L.append("")

    L.append("### current diff")
    if diff is None:
        L.append("_（未定位到 git repo；改动可能在非 repo 路径，见 files changed + Caller Declaration）_")
    elif diff == "":
        L.append(f"_（repo `{repo}` 无未提交 diff，可能已 commit）_")
    else:
        L.append(f"```diff\n{diff}\n```")
    L.append("")

    if status:
        L.append("### current state")
        L.append(f"```\n{status}\n```")
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
    L.append(f"> 这几项 {other} 从轨迹里读不到，也是它最容易重复无效建议的盲区。空着这套交接就白做。\n")
    L.append("### rejected alternatives + why\n<!-- 试过但放弃的路线 + 放弃的具体原因 -->\n")
    L.append("### why this framing / why this approach\n<!-- 为什么定义成现在这个问题、为什么选这条路 -->\n")
    L.append("### unresolved questions\n<!-- -->\n")
    L.append(f"### review focus\n<!-- 想让 {other} 重点判断什么，别让它用通用 review 模式泛泛扫 -->\n")
    L.append("### do-not-repeat unless new evidence\n<!-- 已经否掉、不要再提的建议 -->\n")
    L.append("---\n")

    L.append("## Drill-down（下钻入口）")
    L.append(f"- 原始 log：`{src_path}`")
    for p in sub_paths:
        L.append(f"- 子 agent：`{p}`")
    return "\n".join(L)


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


def latest(pattern_or_dir):
    fs = (glob.glob(os.path.join(pattern_or_dir, "*.jsonl"))
          if os.path.isdir(pattern_or_dir) else glob.glob(pattern_or_dir))
    return max(fs, key=os.path.getmtime) if fs else None


def main():
    ap = argparse.ArgumentParser(description="生成双向 trio 审稿交接包")
    ap.add_argument("source", nargs="?", help="源 jsonl（默认按方向取最近的）")
    ap.add_argument("--direction", choices=["cc-to-codex", "codex-to-cc"],
                    help="方向（默认从源自动检测，再默认 cc-to-codex）")
    ap.add_argument("--last-n", type=int, default=0, help="只保留最近 N 个用户回合")
    ap.add_argument("--include-subagents", action="store_true", help="（CC 方向）带子 agent 轨迹")
    ap.add_argument("--repo", help="在此 repo 跑 git diff/status")
    ap.add_argument("--out", help="输出路径（默认 ~/Desktop/）")
    args = ap.parse_args()

    # 1) 定源 + 方向
    if args.source:
        src = os.path.expanduser(args.source)
        if not os.path.exists(src):
            ap.error(f"源不存在: {src}")
        direction = args.direction or (
            "codex-to-cc" if detect_kind(src) == "codex" else "cc-to-codex")
    else:
        direction = args.direction or "cc-to-codex"
        src = latest(CC_DIR) if direction == "cc-to-codex" else latest(CODEX_GLOB)
        if not src:
            ap.error("找不到默认源，请显式传 source 路径")
    print(f"方向: {direction}\n源: {src}")

    # 2) 解析
    rows = load_rows(src)
    if direction == "cc-to-codex":
        # last-n 截取：按真实用户回合
        if args.last_n > 0:
            starts = [i for i, o in enumerate(rows) if o.get("type") == "user"
                      and isinstance((o.get("message") or {}).get("content"), str)
                      and not is_noise_user_text(o["message"]["content"])]
            if len(starts) > args.last_n:
                rows = rows[starts[-args.last_n]:]
        data = parse_cc(rows)
        sub_paths = []
        if args.include_subagents:
            sub_paths = cc_subagents(src)
            for sp in sub_paths:
                data = merge(data, parse_cc(load_rows(sp), origin="sub"))
    else:
        data = parse_codex(rows)
        sub_paths = []

    # 3) repo 信息
    repo = infer_repo(data["changed"], args.repo)
    diff = git_diff(repo)
    status = git_status(repo)

    # 4) 渲染 + 写出
    md = render(data, direction, src, sub_paths, repo, diff, status)
    tag = "cc2cx" if direction == "cc-to-codex" else "cx2cc"
    out = (os.path.expanduser(args.out) if args.out else
           os.path.expanduser(f"~/Desktop/trio-handoff-{tag}-{datetime.now():%H%M%S}.md"))
    with open(out, "w") as fh:
        fh.write(md)
    print(f"交接包已生成: {out}")
    print(f"  goal {len(data['goal'])} | examined {len(dedupe(data['examined']))} "
          f"| commands {len(data['commands'])} | changed {len(dedupe(data['changed']))} "
          f"| diff {'有' if diff else '无'} | state {'有' if status else '无'}")


if __name__ == "__main__":
    main()
