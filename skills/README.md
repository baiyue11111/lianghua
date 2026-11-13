# 项目策略知识库

`skills/` 是本项目**策略工程知识的单一事实源**，随 git 提交、可评审、可持续迭代。
以后写策略前先读 `skills/strategy-guardrails/SKILL.md`。

## 目录

| 路径 | 说明 |
|---|---|
| `skills/strategy-guardrails/SKILL.md` | 标准 skill 入口：G1–G7 铁律 + 工作流 |
| `skills/strategy-guardrails/references/` | 案例证据、检查清单、引擎契约、迭代协议 |
| `skills/strategy-guardrails/scripts/` | 静态检查、三档不变性验收、自检 |
| `skills/strategy-guardrails/CHANGELOG.md` | 每条规则来自哪次实验、何时修订 |

## 让 WorkBuddy 自动发现

WorkBuddy 的项目级 skill 目录是 `.workbuddy/skills/`，而 `.workbuddy/` 被 gitignore。
本机已建立软链；clone 仓库后可执行一次：

```bash
mkdir -p .workbuddy/skills
ln -s ../../skills/strategy-guardrails .workbuddy/skills/strategy-guardrails
```

软链只做发现，内容永远以 `skills/strategy-guardrails/` 为准，不要在软链下另写一份。

## 常用命令

```bash
# 写策略前：静态检查净值反馈
.venv/bin/python skills/strategy-guardrails/scripts/check_strategy.py src/plugins/strategies/<name>.py

# 三档验收（只走 lianghua --backtest，串行）
.venv/bin/python skills/strategy-guardrails/scripts/check_scale_invariance.py \
  --case 10w=bt_10w.json --case 100w=bt_100w.json --case 1000w=bt_1000w.json

# 改动 skill 后回归
.venv/bin/python skills/strategy-guardrails/scripts/selftest.py
```

## 迭代

新增实验或踩坑 → 追加 `references/case-rNN.md` → 更新 `SKILL.md` →
在 `CHANGELOG.md` 追加版本记录。规则详见 `references/iteration.md`：
旧证据不删除，结论可推翻但必须标注原证据。
