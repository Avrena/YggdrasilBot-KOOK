import math
from types import SimpleNamespace

from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.delivery.forward import build_nodes, split_text, strip_markdown
from yggdrasil.moderation.guard import decide_action, parse_verdict, score_from_logprobs
from yggdrasil.qq.model import Node
from yggdrasil.settings import InteractionSettings, ModerationCategoryRule, ModerationSettings


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
    # llama.cpp assistant prefill made the guard continue the reply instead of judging it.
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
    assert decide_action("Unsafe", 0.6, ["Violent"], policy) == "pass"  # default threshold 0.9
    assert decide_action("Safe", 1.0, ["Jailbreak"], policy) == "pass"


def test_strip_markdown_keeps_text_and_link_targets():
    md = "## 标题\n**粗体** 和 *斜体*，见 [文档](https://x.dev/a)\n* 项目\n```py\nprint(1)\n```"
    out = strip_markdown(md)
    assert "#" not in out
    assert "**" not in out
    assert "```" not in out
    assert "文档 (https://x.dev/a)" in out
    assert "- 项目" in out
    assert "print(1)" in out


def test_strip_markdown_keeps_dunders_and_code_verbatim():
    md = (
        "用 __init__ 和 __call__，`*args` 和 `**kwargs`\n"
        "```py\ndef __init__(self, *a, **k):\n    return a * 2 * 3\n```\n**结论**好"
    )
    assert strip_markdown(md) == (
        "用 __init__ 和 __call__，*args 和 **kwargs\ndef __init__(self, *a, **k):\n    return a * 2 * 3\n\n结论好"
    )


def test_split_text_respects_limit_and_keeps_content():
    text = "\n\n".join(["段落" * 30] * 5)
    parts = split_text(text, 130)
    assert all(len(p) <= 130 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def _kinds(nodes: list[Node]) -> list[str]:
    """Each node as its text, "sources", "meta", or "图:<caption>" for a picture."""
    out = []
    for n in nodes:
        first = n.segments[0]["data"].get("text", "")
        if n.segments[-1]["type"] == "image":
            out.append(f"图:{first.strip()}")
        else:
            out.append("sources" if first.startswith("来源") else "meta" if first == "meta" else first)
    return out


def test_each_sections_pictures_follow_it_then_attached_sources_metadata():
    reply = BotReply(
        mode="forward",
        title="t",
        sections=[Section(content="一", images=["img#3", "img#1"]), Section(content="二", images=["img#9"])],
        sources=[SourceRef(label="src", url="https://e.x")],
    )
    images = {"img#1": (b"\x89PNG1", "流程"), "img#3": (b"\x89PNG3", "对比"), "img#9": (b"\x89PNG9", "截图")}
    nodes = build_nodes(
        reply, "meta", InteractionSettings(), self_id=1, nickname="b", images=images, attached=[(b"\x89PNG", "任务")]
    )
    assert _kinds(nodes) == ["一", "图:对比", "图:流程", "二", "图:截图", "图:任务", "sources", "meta"]


def test_pictures_that_failed_to_resolve_are_left_out():
    reply = BotReply(mode="forward", sections=[Section(content="一", images=["img#1", "img#2"])])
    nodes = build_nodes(reply, None, InteractionSettings(), self_id=1, nickname="b", images={"img#2": (b"\x89P", "")})
    assert [n.segments[-1]["type"] for n in nodes] == ["text", "image"]


def test_text_over_the_node_cap_merges_and_the_pictures_it_passes_follow():
    sections = [Section(content=f"s{i}", images=[f"img#{i}"] if i in (1, 3) else []) for i in range(6)]
    reply = BotReply(mode="forward", sections=sections, sources=[SourceRef(label="src")])
    cfg = InteractionSettings(forward_max_nodes=7, max_sections=10)
    images = {"img#1": (b"\x89P", "a"), "img#3": (b"\x89P", "b")}
    nodes = build_nodes(reply, "meta", cfg, self_id=1, nickname="b", images=images)
    assert len(nodes) == 7
    assert _kinds(nodes) == ["s0", "s1", "图:a", "s2\n\ns3\n\ns4\n\ns5", "图:b", "sources", "meta"]


def test_pictures_leave_room_for_text():
    reply = BotReply(mode="forward", sections=[Section(content="一", images=["img#1", "img#2", "img#3"])])
    images = {k: (b"\x89P", k) for k in ("img#1", "img#2", "img#3")}
    nodes = build_nodes(reply, "meta", InteractionSettings(forward_max_nodes=3), self_id=1, nickname="b", images=images)
    assert _kinds(nodes) == ["一", "图:img#1", "meta"]


def test_forward_nodes_are_capped_by_merging_the_tail():
    reply = BotReply(mode="forward", sections=[Section(content=f"s{i}") for i in range(20)])
    cfg = InteractionSettings(forward_max_nodes=5, max_sections=10)
    nodes = build_nodes(reply, "meta", cfg, self_id=1, nickname="b")
    assert len(nodes) == 5
    assert "s19" in nodes[3].segments[0]["data"]["text"]


def test_sections_over_the_cap_are_folded_into_the_last_one():
    reply = BotReply(mode="forward", sections=[Section(content=t * 2) for t in "abcd"])
    nodes = build_nodes(reply, None, InteractionSettings(max_sections=2), self_id=1, nickname="b")
    texts = [n.segments[0]["data"]["text"] for n in nodes]
    assert texts == ["aa", "bb\n\ncc\n\ndd"]
