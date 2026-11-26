# 上游同步指南（FYWinds/YggdrasilBot → YggdrasilBot-KOOK）

本项目基于 [FYWinds/YggdrasilBot](https://github.com/FYWinds/YggdrasilBot) 重构为原生 KOOK 机器人。由于协议层、数据库 ID 类型（全量 `Text` 字符串）、四级配置继承（`global → guild → category → channel`）以及前端控制台均已与上游结构分叉，**日常维护以 `main` 作为 KOOK 主分支，通过 `upstream-base` 标签按需挑选上游提交（selective `cherry-pick` / 移植）**，不做整分支 `merge` 或 `rebase`。

---

## 1. 首次本地配置（只需执行一次）

新 clone 仓库后，配置只读的 `upstream` remote（禁止向原仓库 push）：

```bash
git remote add upstream https://github.com/FYWinds/YggdrasilBot.git
git remote set-url --push upstream no_push
git fetch --tags origin
```

> 基准标签 `upstream-base` 记录当前已审阅/同步到的上游水位（初始分叉点为 `593486a`）。

---

## 2. 同步上游更新的标准流程

```bash
# 1. 拉取上游最新提交
git fetch upstream

# 2. 查看自上次水位以来的全部上游新提交
git log upstream-base..upstream/main --oneline

# 3. 查看某个提交涉及的文件，判断是否需要同步
git show --stat <commit-sha>

# 4. 平台无关的提交直接 cherry-pick；涉及 QQ/群概念的提交按需手工移植
git cherry-pick <commit-sha>
```

---

## 3. 按模块决定同步策略

| 目录 / 模块 | 同步难度 | 策略 |
|---|---|---|
| `yggdrasil/tools/web/`、`yggdrasil/tools/search/`、`yggdrasil/tools/docs.py`、`yggdrasil/tools/ssrf.py` | 极低 | 通常可直接 `git cherry-pick` |
| `yggdrasil/llm/`、`browser/`、`render/`、`sandbox/`（容器内 helper） | 极低 | 通常可直接 `git cherry-pick` |
| `yggdrasil/moderation/`、`yggdrasil/transcript/`、`yggdrasil/agent/budget.py` | 低 | `cherry-pick` 后检查 ID 类型（`str`）与 `BotReply(mode="reply")` |
| `yggdrasil/memory/`、`yggdrasil/pipeline/`、`yggdrasil/sandbox/`（管理端） | 中 | 对照 diff 移植；注意 `channel_id`（会话隔离）与 `guild_id`（同服共享记忆）区分 |
| `yggdrasil/admin/`、`web/`、`yggdrasil/ops/` | 中 | 对照移植；若改动 Admin API，须重新生成 `web/openapi.json` 与前端客户端 |
| `yggdrasil/qq/`、`yggdrasil/bot/plugin.py`、`yggdrasil/delivery/forward.py`、`deploy/example/{yogurt,snowluma}.yaml` | 跳过 | QQ / NoneBot2 / OneBot / Milky 专属改动，不合入本项目 |

---

## 4. 同步后的验证与水位更新

移植完成后执行完整校验：

```bash
# 后端格式、Lint、类型检查与单元测试
uv run ruff format --check yggdrasil tests
uv run ruff check yggdrasil tests
uv run basedpyright
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= uv run pytest -q

# 若修改了 Admin API 或前端
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json
cd web && npm run gen && npx tsc -b --noEmit && npm run build && cd ..
```

确认无误后提交更改，并将 `upstream-base` 水位标签前移到本次已审阅的上游提交：

```bash
git tag -fa upstream-base <reviewed-upstream-sha> -m "Synced upstream up to <reviewed-upstream-sha>"
git push origin main
git push origin -f upstream-base
```
