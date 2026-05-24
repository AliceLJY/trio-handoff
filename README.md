# trio-handoff

Bidirectional handoff bundles for two AI coding agents that review each other's work.

Built for the **trio** workflow (one human + two agents — e.g. Claude Code and Codex), where the agents take turns reviewing each other. Works for any pair of file-system agents that keep a session log.

## Why

When agent A asks agent B to review its work, A usually hands over a diff and a one-line summary. B can't see what A already tried, what evidence A examined, or which approaches A deliberately rejected — so B keeps re-suggesting things A already ruled out.

Cognition's [*Don't Build Multi-Agents*](https://cognition.ai/blog/dont-build-multi-agents) names the root cause: **share full traces, not just messages.** A compressed message can't carry the sender's decision context. trio-handoff is a precise, practical version of that principle for the review handoff.

## Half-extracted, half-declared

A bundle has two sections:

**Objective Evidence** — auto-extracted from the agent's own session log:
goal / constraints · files examined · commands + truncated outputs · files changed · current diff · current state · raw log path.

**Caller Declaration** — written by the calling agent, because the log can't capture it:
rejected alternatives + why · why this framing / approach · unresolved questions · review focus · do-not-repeat unless new evidence.

**The declared half is the valuable half.** Rejected alternatives and design rationale often live only in the author's head — they never become an observable action, so no script can extract them. They must be declared. And that declared half is exactly what stops the reviewer from repeating ruled-out suggestions.

## Hidden reasoning is never shared

Claude's `thinking` and Codex's `reasoning` are excluded by design. A hidden chain of thought contains discarded mid-thoughts and isn't verifiable; a reviewer should anchor on observable evidence, not the author's inner monologue. "Full trace" here means the observable **work** trace (what was read / run / changed) — not the raw chain of thought.

## Two directions, one structure

| direction | source log | reads / edits via |
|---|---|---|
| `cc-to-codex` | Claude Code JSONL session | `Read` / `Edit` / `Write` tools |
| `codex-to-cc` | Codex rollout transcript | `exec_command` (`cat`/`sed`) + `apply_patch` |

Same bundle structure; only the extractor differs — because the two agents observe their own work differently (Claude has dedicated read/edit tools; Codex reads through shell commands and patches through `apply_patch`).

## Usage

```bash
./trio-handoff.py                          # latest Claude session -> bundle for Codex
./trio-handoff.py --direction codex-to-cc  # latest Codex rollout -> bundle for CC
./trio-handoff.py path/to/session.jsonl    # explicit source, direction auto-detected
./trio-handoff.py --last-n 3               # only the last 3 user turns
./trio-handoff.py --include-subagents      # include subagent traces (cc-to-codex)
./trio-handoff.py --repo ~/code/project    # where to run git diff / status
./trio-handoff.py --out /path/bundle.md    # output path (default ~/Desktop/)
```

Output is a Markdown bundle (default in `~/Desktop/`). **Fill in the Caller Declaration before sending.** Hand the path to the reviewing agent — it reads the bundle for guidance and can drill down into the raw log path at the bottom to verify any claim, so it never has to trust the compression blindly.

The bundle opens with a review instruction:

> Read this bundle; extract goal / evidence / rejected-alternatives / diff; then review. Don't repeat suggestions already ruled out unless you can point to new evidence.

## Config

| env | default |
|---|---|
| `TRIO_CC_DIR` | auto from `$HOME` (`~/.claude/projects/-Users-<you>`) |
| `TRIO_CODEX_GLOB` | `~/.codex/sessions/*/*/*/rollout-*.jsonl` |

## Requirements

Python 3.8+, standard library only.

## License

MIT © 2026 AliceLJY · See [LICENSE](LICENSE). Chinese readme: [README_CN.md](README_CN.md).
