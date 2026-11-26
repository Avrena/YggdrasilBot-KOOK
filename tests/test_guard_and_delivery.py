"""Moderation guard scoring/decision and KOOK delivery formatting tests."""

import math
from types import SimpleNamespace

from yggdrasil.delivery.render import split_kmarkdown, to_kmarkdown
from yggdrasil.moderation.guard import decide_action, parse_verdict, score_from_logprobs
from yggdrasil.settings import ModerationCategoryRule, ModerationSettings


def tok(token: str, alts: dict[str, float]) -> SimpleNamespace:
    return SimpleNamespace(
        token=token, top_logprobs=[SimpleNamespace(token=t, logprob=math.log(p)) for t, p in alts.items()]
    )


def test_score_uses_token_after_safety_label_not_the_label_itself():
    logprobs = [
        tok("Safety", {"Safety": 1.0}),
        tok(":", {":": 1.0}),
        tok(" Safe", {" Safe": 0.6, " Unsafe": 0.3, " Cont": 0.1}),
    ]
    assert score_from_logprobs(logprobs, "Safe") == round(0.3 + 0.05, 4)


def test_score_falls_back_without_logprobs():
    assert score_from_logprobs(None, "Controversial") == 0.5
    assert score_from_logprobs([], "Unsafe") == 1.0


def test_verdict_missing_when_guard_echoes_the_reply():
    assert parse_verdict("Deno 团队整体加入 Cloudflare。结论：Deno 整个团队加入 Cloudflare，Deno 运行时") is None
    assert parse_verdict("Safety: Controversial\nCategories: Politically Sensitive Topics") == "Controversial"


def test_decide_action_takes_most_severe_rule_over_threshold():
    policy = ModerationSettings(
        default_rule=ModerationCategoryRule(threshold=0.9, action="review"),
        category_rules={
            "Jailbreak": ModerationCategoryRule(threshold=0.5, action="drop"),
            "PII": ModerationCategoryRule(threshold=0.2, action="review"),
        },
    )
    assert decide_action("Unsafe", 0.6, ["PII", "Jailbreak"], policy) == "drop"
    assert decide_action("Unsafe", 0.3, ["PII", "Jailbreak"], policy) == "review"
    assert decide_action("Unsafe", 0.6, ["Violent"], policy) == "pass"
    assert decide_action("Safe", 1.0, ["Jailbreak"], policy) == "pass"


def test_to_kmarkdown_preserves_markdown_formatting_for_kook():
    md = "## 标题\n**粗体** 和 *斜体*，见 [文档](https://x.dev/a)\n* 项目\n```py\nprint(1)\n```"
    out = to_kmarkdown(md)
    assert "**标题**" in out
    assert "**粗体**" in out
    assert "[文档](https://x.dev/a)" in out
    assert "```py" in out
    assert "print(1)" in out


def test_split_kmarkdown_respects_limit_and_keeps_content():
    text = "\n\n".join(["段落" * 60] * 5)
    parts = split_kmarkdown(text, limit=250)
    assert all(len(p) <= 250 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
