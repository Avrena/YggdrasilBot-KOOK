"""Four-level sparse settings documents: global -> guild -> category -> channel."""

import typing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ValidationError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import change, not_found
from yggdrasil.db.models import Channel, Guild, SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.settings import Settings, merge_settings

router = APIRouter(prefix="/settings", tags=["settings"])

SettingsScope = Literal["guild", "category", "channel"]


class SettingsView(BaseModel):
    value: dict[str, Any]  # stored sparse override at this scope
    effective: dict[str, Any]  # merged result including this scope
    defaults: dict[str, Any]  # inherited parent chain (what value overrides)
    chain: list[str] = []  # human-readable inheritance path, e.g. ["全局", "服务器", "分组", "频道"]


def _unknown_keys(doc: dict[str, Any], model: type[BaseModel], prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in doc.items():
        field = model.model_fields.get(key)
        path = f"{prefix}{key}"
        if field is None:
            out.append(path)
            continue
        if not isinstance(value, dict):
            continue
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out += _unknown_keys(value, ann, f"{path}.")
        elif typing.get_origin(ann) is dict:
            item = typing.get_args(ann)[1]
            if isinstance(item, type) and issubclass(item, BaseModel):
                for k, v in value.items():
                    if isinstance(v, dict):
                        out += _unknown_keys(v, item, f"{path}.{k}.")
    return out


def validate_layers(*layers: dict[str, Any] | None, check: dict[str, Any]) -> Settings:
    """Validate ``check`` for unknown keys and validate the merged chain against :class:`Settings`."""
    unknown = _unknown_keys(check, Settings)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", *k.split(".")], "msg": "未知的设置项", "type": "extra_forbidden"} for k in unknown],
        )
    try:
        return merge_settings(*layers)
    except ValidationError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["body", *err["loc"]], "msg": err["msg"], "type": err["type"]} for err in e.errors()],
        ) from e


async def global_doc() -> dict[str, Any]:
    async with get_session() as s:
        doc = await s.get(SettingsDoc, "global")
        return dict(doc.value) if doc else {}


async def _load_parent_layers(
    scope: SettingsScope, scope_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Return ``(parent_layers, own_doc, chain_labels)`` for a guild / category / channel scope."""
    base = await global_doc()
    async with get_session() as s:
        if scope == "guild":
            g = await s.get(Guild, scope_id)
            if g is None:
                raise not_found(f"服务器 {scope_id}")
            return [base], dict(g.settings), ["全局", f"服务器 {g.name or g.guild_id}"]

        ch = await s.get(Channel, scope_id)
        if ch is None:
            raise not_found(f"{'分组' if scope == 'category' else '频道'} {scope_id}")
        if scope == "category" and not ch.is_category:
            raise not_found(f"分组 {scope_id}")
        if scope == "channel" and ch.is_category:
            raise not_found(f"频道 {scope_id}")

        g = await s.get(Guild, ch.guild_id)
        parents: list[dict[str, Any]] = [base]
        labels = ["全局"]
        if g is not None:
            parents.append(dict(g.settings))
            labels.append(f"服务器 {g.name or g.guild_id}")
        if scope == "channel" and ch.parent_id:
            cat = await s.get(Channel, ch.parent_id)
            if cat is not None:
                parents.append(dict(cat.settings))
                labels.append(f"分组 {cat.name or cat.channel_id}")
        labels.append(f"{'分组' if ch.is_category else '频道'} {ch.name or ch.channel_id}")
        return parents, dict(ch.settings), labels


@router.get("/schema")
async def get_settings_schema() -> dict[str, Any]:
    return Settings.model_json_schema()


@router.get("/global")
async def get_global_settings() -> SettingsView:
    value = await global_doc()
    return SettingsView(
        value=value,
        effective=merge_settings(value).model_dump(mode="json"),
        defaults=Settings().model_dump(mode="json"),
        chain=["默认", "全局"],
    )


@router.put("/global")
async def put_global_settings(body: dict[str, Any], actor: Actor) -> SettingsView:
    effective = validate_layers(body, check=body)
    async with change(actor, "settings", "update", "global") as c:
        doc = await c.session.get(SettingsDoc, "global")
        if doc is None:
            c.session.add(SettingsDoc(key="global", value=body))
        else:
            doc.value = body
        c.detail = {"value": body}
    return SettingsView(
        value=body,
        effective=effective.model_dump(mode="json"),
        defaults=Settings().model_dump(mode="json"),
        chain=["默认", "全局"],
    )


@router.get("/scopes/{scope}/{scope_id}")
async def get_scope_settings(scope: SettingsScope, scope_id: str) -> SettingsView:
    parents, value, labels = await _load_parent_layers(scope, scope_id)
    return SettingsView(
        value=value,
        effective=merge_settings(*parents, value).model_dump(mode="json"),
        defaults=merge_settings(*parents).model_dump(mode="json"),
        chain=labels,
    )


@router.put("/scopes/{scope}/{scope_id}")
async def put_scope_settings(scope: SettingsScope, scope_id: str, body: dict[str, Any], actor: Actor) -> SettingsView:
    parents, _, labels = await _load_parent_layers(scope, scope_id)
    effective = validate_layers(*parents, body, check=body)
    async with change(actor, scope, "settings", scope_id) as c:
        if scope == "guild":
            row = await c.session.get(Guild, scope_id)
            if row is None:
                raise not_found(f"服务器 {scope_id}")
            row.settings = body
        else:
            row = await c.session.get(Channel, scope_id)
            if row is None:
                raise not_found(f"{'分组' if scope == 'category' else '频道'} {scope_id}")
            row.settings = body
        c.detail = {"value": body}
    return SettingsView(
        value=body,
        effective=effective.model_dump(mode="json"),
        defaults=merge_settings(*parents).model_dump(mode="json"),
        chain=labels,
    )
