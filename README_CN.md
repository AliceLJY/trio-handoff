# trio-handoff

两个 AI 编码 agent 互相 review 时用的双向交接包。

为 **trio** 协作流程而建(一个人 + 两个 agent——比如 Claude Code 和 Codex),两个 agent 轮流互审。任何一对保留 session 日志的文件系统 agent 都能用。

## 为什么需要

当 agent A 让 agent B review 自己的工作时,A 通常只甩过去一个 diff 加一句话摘要。B 看不到 A 已经试过什么、检查过哪些证据、刻意否掉了哪些方案——于是 B 反复建议 A 早就排除的东西。

Cognition 的 [*Don't Build Multi-Agents*](https://cognition.ai/blog/dont-build-multi-agents) 点破了根因:**共享完整轨迹,而不只是消息。** 一段压缩过的消息载不动发送方的决策上下文。trio-handoff 是这条原则在"审稿交接"场景下的精确、可落地版本。

## 半抽取,半声明

一个交接包分两段:

**Objective Evidence(客观证据)**——从 agent 自己的 session 日志自动抽取:
目标 / 约束 · 检查过的文件 · 命令 + 截断输出 · 改动的文件 · 当前 diff · 当前状态 · 原始日志路径。

**Caller Declaration(调用方声明)**——由调用方手写,因为日志抓不到:
否掉的方案 + 为什么 · 为什么这样定义问题 / 选这条路 · 未决问题 · review 重点 · 没有新证据就别重提的建议。

**手写的那半才是最值钱的。** 否掉的方案和设计理由常常只存在作者脑子里——它们从没变成一个可观察的动作,所以任何脚本都抽不出来,必须主动声明。而这手写的一半,恰恰是阻止 reviewer 重复提你早否掉的建议的关键。

## 隐藏思维链绝不传

Claude 的 `thinking` 和 Codex 的 `reasoning` 按设计排除。隐藏的思维链里含被废弃的中间想法、且不可验证;reviewer 该锚定在可观察的证据上,而不是作者的内心独白。这里说的"完整轨迹"指**可观察的工作轨迹**(读了什么 / 跑了什么 / 改了什么),不是原始思维链。

## 两个方向,一套结构

| 方向 | 源日志 | 读 / 改通过 |
|---|---|---|
| `cc-to-codex` | Claude Code JSONL 会话 | `Read` / `Edit` / `Write` 工具 |
| `codex-to-cc` | Codex rollout transcript | `exec_command`(`cat`/`sed`)+ `apply_patch` |

结构相同,只是抽取器不同——因为两个 agent 观察自己工作的方式天生不同(Claude 有专门的读/改工具;Codex 靠 shell 命令读、靠 `apply_patch` 改)。

## 用法

```bash
./trio-handoff.py                          # 最近的 Claude 会话 -> 给 Codex 的包
./trio-handoff.py --direction codex-to-cc  # 最近的 Codex rollout -> 给 CC 的包
./trio-handoff.py path/to/session.jsonl    # 显式指定源,方向自动检测
./trio-handoff.py --last-n 3               # 只保留最近 3 个用户回合
./trio-handoff.py --include-subagents      # 带上子 agent 轨迹(cc-to-codex)
./trio-handoff.py --repo ~/code/project    # 在哪个 repo 跑 git diff / status
./trio-handoff.py --out /path/bundle.md    # 输出路径(默认 ~/Desktop/)
```

输出是一个 Markdown 交接包(默认在 `~/Desktop/`)。**发出去之前先填好 Caller Declaration。** 把路径交给负责 review 的 agent——它读这个包作为导览,也能下钻到文末的原始日志路径核实任何一条,不必盲信压缩。

交接包开头带一句 review 指令:

> 先读这个包,提取目标 / 证据 / 否掉的方案 / diff,然后再 review。没有新证据,就别重提已经否掉的建议。

## 配置

| 环境变量 | 默认 |
|---|---|
| `TRIO_CC_DIR` | 从 `$HOME` 自动推断(`~/.claude/projects/-Users-<你>`) |
| `TRIO_CODEX_GLOB` | `~/.codex/sessions/*/*/*/rollout-*.jsonl` |

## 依赖

Python 3.8+,仅标准库。

## 许可

MIT © 2026 AliceLJY · 见 [LICENSE](LICENSE)。English readme: [README.md](README.md)。
