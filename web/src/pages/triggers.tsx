import { useMutation, useQuery } from "@tanstack/react-query";
import { PencilIcon, PlayIcon, PlusIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import {
  createTriggerMutation,
  deleteTriggerMutation,
  getTriggerMetaOptions,
  listTriggersOptions,
  testTriggerMutation,
  updateTriggerMutation,
} from "@/client/@tanstack/react-query.gen";
import type { TriggerIn, TriggerMeta, TriggerOut, TriggerTestIn } from "@/client/types.gen";
import {
  ChannelSelect,
  ConfirmButton,
  Empty,
  Field,
  FormDialog,
  GuildSelect,
  Meta,
  MultiToggle,
  NumberInput,
  OptionSelect,
  PageHeader,
  PendingButton,
  Pre,
  QueryState,
  TagInput,
  useChannelName,
  useGuildName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const MATCH_LABELS: Record<string, string> = {
  keyword_prefix: "关键词前缀",
  keyword_exact: "关键词全文",
  regex: "正则",
  mention: "@bot",
  reply_to_bot: "回复 bot",
  name_mention: "提到名字",
};
const TARGET_LABELS: Record<string, string> = {
  quoted_or_self: "引用的消息，没有则本条",
  self: "本条消息",
  quoted: "引用的消息",
};
const SCOPE_LABELS: Record<string, string> = {
  global: "全局",
  guild: "服务器",
  category: "分组",
  channel: "频道",
  private: "私聊",
};
const PATTERN_TYPES = ["keyword_prefix", "keyword_exact", "regex"];

export function TriggersPage() {
  const triggers = useQuery(listTriggersOptions());
  const meta = useQuery(getTriggerMetaOptions());
  const guildName = useGuildName();
  const channelName = useChannelName();
  const [editing, setEditing] = useState<TriggerOut | "new" | null>(null);
  const remove = useMutation({ ...deleteTriggerMutation(), onSuccess: () => toast.success("已删除") });
  const update = useMutation(updateTriggerMutation());
  const addButton = (
    <Button onClick={() => setEditing("new")} disabled={!meta.data}>
      <PlusIcon data-icon="inline-start" /> 新增规则
    </Button>
  );

  const scopeTarget = (scope: string, scopeId: string | null | undefined) => {
    if (!scopeId) return null;
    if (scope === "guild") return guildName(scopeId);
    if (scope === "category" || scope === "channel") return channelName(scopeId);
    return scopeId;
  };

  return (
    <div>
      <PageHeader
        title="指令与触发规则"
        description="支持「频道 > 分组 > 服务器 > 全局」四级覆盖：同一 handler 优先使用最内层的规则；按优先级取第一条命中。"
        actions={addButton}
      />
      <QueryState isLoading={meta.isLoading} error={meta.error} variant="card" />
      {meta.data && <TryMatch meta={meta.data} />}
      <QueryState isLoading={triggers.isLoading} error={triggers.error} />
      {triggers.data?.length === 0 && (
        <Empty description="没有规则时，消息不会触发任何 handler。" action={addButton}>
          还没有规则
        </Empty>
      )}
      {!!triggers.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>#</TableHead>
              <TableHead>handler</TableHead>
              <TableHead>范围</TableHead>
              <TableHead>匹配</TableHead>
              <TableHead>选项</TableHead>
              <TableHead>优先级</TableHead>
              <TableHead>启用</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {triggers.data.map((t) => (
              <TableRow key={t.id} className={t.enabled ? "" : "opacity-60"}>
                <TableCell>{t.id}</TableCell>
                <TableCell className="font-mono text-xs">{t.handler}</TableCell>
                <TableCell>
                  <div>{SCOPE_LABELS[t.scope ?? "global"] ?? t.scope}</div>
                  {t.scope_id && (
                    <div className="text-xs text-muted-foreground">{scopeTarget(t.scope ?? "global", t.scope_id)}</div>
                  )}
                </TableCell>
                <TableCell>
                  <div>{MATCH_LABELS[t.match_type] ?? t.match_type}</div>
                  {!!t.patterns?.length && (
                    <ul className="flex flex-wrap gap-x-2 gap-y-1">
                      {t.patterns.map((p) => (
                        <li key={p}>
                          <code className="rounded-sm bg-muted px-1 font-mono text-xs">{p}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                  {t.arg_template && (
                    <div className="mt-1 text-xs text-muted-foreground">
                      参数模板 <code className="font-mono">{t.arg_template}</code>
                    </div>
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  <Meta
                    items={[
                      t.require_mention && "需 @",
                      t.cooldown_s ? `冷却 ${t.cooldown_s}s` : null,
                      t.allowed_roles?.length ? `角色 ${t.allowed_roles.join("、")}` : null,
                      TARGET_LABELS[t.target ?? ""] ?? t.target,
                    ]}
                  />
                  {t.note && <div className="mt-1">{t.note}</div>}
                </TableCell>
                <TableCell>{t.priority}</TableCell>
                <TableCell>
                  <Switch
                    aria-label={`启用规则 #${t.id}`}
                    checked={t.enabled}
                    onCheckedChange={(enabled) =>
                      update.mutate({ path: { trigger_id: t.id }, body: { ...toInput(t), enabled } })
                    }
                  />
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setEditing(t)}>
                      <PencilIcon data-icon="inline-start" /> 编辑
                    </Button>
                    <ConfirmButton title={`删除规则 #${t.id}？`} onConfirm={() => remove.mutate({ path: { trigger_id: t.id } })}>
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {editing && meta.data && (
        <TriggerDialog trigger={editing === "new" ? null : editing} meta={meta.data} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}

function toInput(t: TriggerOut): TriggerIn {
  const { id: _id, created_at: _c, updated_at: _u, ...rest } = t;
  return { ...rest, scope: rest.scope as TriggerIn["scope"] };
}

function TriggerDialog({ trigger, meta, onClose }: { trigger: TriggerOut | null; meta: TriggerMeta; onClose: () => void }) {
  const [form, setForm] = useState<TriggerIn>(
    trigger
      ? toInput(trigger)
      : {
          handler: "chat",
          scope: "global",
          scope_id: null,
          match_type: "keyword_prefix",
          patterns: [],
          arg_template: null,
          require_mention: false,
          target: "quoted_or_self",
          priority: 100,
          enabled: true,
          cooldown_s: 0,
          allowed_roles: [],
          note: "",
        },
  );
  const create = useMutation({ ...createTriggerMutation(), onSuccess: onClose });
  const update = useMutation({ ...updateTriggerMutation(), onSuccess: onClose });
  const scoped = form.scope === "guild" || form.scope === "category" || form.scope === "channel";
  const submit = () => {
    const body = { ...form, scope_id: scoped ? form.scope_id : null };
    if (trigger) update.mutate({ path: { trigger_id: trigger.id }, body });
    else create.mutate({ body });
  };
  const usesPatterns = PATTERN_TYPES.includes(form.match_type);
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={trigger ? `编辑规则 #${trigger.id}` : "新增规则"}
      description="支持全局、服务器、分组、频道和私聊；保存后几秒内热更新。"
      onSubmit={submit}
      pending={create.isPending || update.isPending}
      className="sm:max-w-2xl"
    >
      <div className="grid grid-cols-2 gap-4">
        <Field label="handler" hint={meta.handlers.find((h) => h.name === form.handler)?.description}>
          <OptionSelect
            value={form.handler}
            onChange={(v) => v && setForm({ ...form, handler: v })}
            options={meta.handlers.map((h) => ({
              value: h.name,
              label: (
                <span className="grid gap-0.5">
                  <span className="font-mono">{h.name}</span>
                  <span className="text-xs text-muted-foreground in-data-[slot=select-value]:hidden">{h.description}</span>
                </span>
              ),
            }))}
          />
        </Field>
        <Field label="范围">
          <OptionSelect
            value={form.scope ?? "global"}
            onChange={(v) => v && setForm({ ...form, scope: v as TriggerIn["scope"], scope_id: null })}
            options={meta.scopes.map((s) => ({ value: s, label: SCOPE_LABELS[s] ?? s }))}
          />
        </Field>
        {form.scope === "guild" && (
          <Field label="服务器" className="col-span-2">
            <GuildSelect value={form.scope_id ?? null} onChange={(v) => setForm({ ...form, scope_id: v })} allLabel={null} />
          </Field>
        )}
        {(form.scope === "category" || form.scope === "channel") && (
          <Field label={form.scope === "category" ? "分组" : "频道"} className="col-span-2">
            <ChannelSelect
              value={form.scope_id ?? null}
              onChange={(v) => setForm({ ...form, scope_id: v })}
              includeCategories={form.scope === "category"}
              allLabel={null}
            />
          </Field>
        )}
        <Field label="匹配方式">
          <OptionSelect
            value={form.match_type}
            onChange={(v) => v && setForm({ ...form, match_type: v })}
            options={meta.match_types.map((m) => ({ value: m, label: MATCH_LABELS[m] ?? m }))}
          />
        </Field>
        <Field label="目标消息">
          <OptionSelect
            value={form.target ?? "quoted_or_self"}
            onChange={(v) => v && setForm({ ...form, target: v })}
            options={meta.targets.map((t) => ({ value: t, label: TARGET_LABELS[t] ?? t }))}
          />
        </Field>
        {usesPatterns && (
          <Field
            label="patterns"
            className="col-span-2"
            hint={form.match_type === "regex" ? "忽略大小写；可以带捕获组" : "忽略大小写"}
          >
            <TagInput value={form.patterns ?? []} onChange={(patterns) => setForm({ ...form, patterns })} />
          </Field>
        )}
        <Field label="参数模板" className="col-span-2" hint="把命中结果改写成 handler 参数：{0}、{1} 是捕获组，{rest} 是剩余文本">
          <Input
            className="font-mono"
            value={form.arg_template ?? ""}
            onChange={(e) => setForm({ ...form, arg_template: e.target.value || null })}
          />
        </Field>
        <Field label="允许的角色" className="col-span-2" hint="不选表示所有人；owner 隐含 admin，superuser 总是允许">
          <MultiToggle
            options={meta.roles}
            value={form.allowed_roles ?? []}
            onChange={(allowed_roles) => setForm({ ...form, allowed_roles })}
          />
        </Field>
        <Field label="优先级" hint="越小越先匹配">
          <NumberInput value={form.priority} onChange={(v) => setForm({ ...form, priority: v ?? 100 })} />
        </Field>
        <Field label="冷却（秒）">
          <NumberInput value={form.cooldown_s} onChange={(v) => setForm({ ...form, cooldown_s: v ?? 0 })} />
        </Field>
        <Field label="必须 @bot 或提到名字" orientation="horizontal">
          <Switch checked={!!form.require_mention} onCheckedChange={(c) => setForm({ ...form, require_mention: c })} />
        </Field>
        <Field label="启用" orientation="horizontal">
          <Switch checked={form.enabled ?? true} onCheckedChange={(c) => setForm({ ...form, enabled: c })} />
        </Field>
        <Field label="备注" className="col-span-2">
          <Input value={form.note ?? ""} onChange={(e) => setForm({ ...form, note: e.target.value })} />
        </Field>
      </div>
    </FormDialog>
  );
}

function TryMatch({ meta }: { meta: TriggerMeta }) {
  const [input, setInput] = useState<TriggerTestIn>({
    text: "",
    scope: "channel",
    channel_id: null,
    to_me: false,
    replied_to_bot: false,
    role: "member",
  });
  const test = useMutation(testTriggerMutation());
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!test.isPending) test.mutate({ body: input });
  };
  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle>试匹配</CardTitle>
        <CardDescription>与运行时同一份四级索引代码；规则保存后几秒内热更新到这里。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
          <Field label="消息文本" className="min-w-72 flex-1">
            <Input
              value={input.text}
              placeholder="例如：太长不看 总结最近50条"
              onChange={(e) => setInput({ ...input, text: e.target.value })}
            />
          </Field>
          <Field label="场景" className="w-28">
            <OptionSelect
              value={input.scope ?? "channel"}
              onChange={(v) => v && setInput({ ...input, scope: v as TriggerTestIn["scope"] })}
              options={[
                { value: "channel", label: "频道" },
                { value: "private", label: "私聊" },
              ]}
            />
          </Field>
          {input.scope === "channel" && (
            <Field label="频道" className="w-64">
              <ChannelSelect
                value={input.channel_id ?? null}
                onChange={(c) => setInput({ ...input, channel_id: c })}
                allLabel="未指定"
              />
            </Field>
          )}
          <Field label="角色" className="w-32">
            <OptionSelect
              value={input.role ?? "member"}
              onChange={(v) => v && setInput({ ...input, role: v as TriggerTestIn["role"] })}
              options={meta.roles.map((r) => ({ value: r, label: r }))}
            />
          </Field>
          <Field label="@bot" orientation="horizontal" className="h-8 w-auto">
            <Switch checked={!!input.to_me} onCheckedChange={(c) => setInput({ ...input, to_me: c })} />
          </Field>
          <Field label="回复 bot" orientation="horizontal" className="h-8 w-auto">
            <Switch checked={!!input.replied_to_bot} onCheckedChange={(c) => setInput({ ...input, replied_to_bot: c })} />
          </Field>
          <PendingButton type="submit" pending={test.isPending}>
            {!test.isPending && <PlayIcon data-icon="inline-start" />}
            试匹配
          </PendingButton>
        </form>
        {test.data &&
          (test.data.resolved ? (
            <div className="grid gap-2">
              <div className="flex items-center gap-2 text-sm">
                <Badge>命中</Badge>
                <span className="font-mono">{test.data.resolved.handler}</span>
                <span className="text-muted-foreground">规则 #{test.data.resolved.rule_id}</span>
              </div>
              <Pre value={test.data.resolved} />
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Badge variant="outline">未命中</Badge>
              <span>配置版本 v{test.data.snapshot_version}</span>
            </div>
          ))}
      </CardContent>
    </Card>
  );
}
