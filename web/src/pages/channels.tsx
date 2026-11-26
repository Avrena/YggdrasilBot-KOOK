import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronsUpDownIcon,
  DownloadIcon,
  FolderIcon,
  HashIcon,
  PlusIcon,
  RefreshCwIcon,
  ServerIcon,
  SlidersHorizontalIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  backfillChannelMutation,
  deleteChannelMutation,
  deleteGuildMutation,
  getChannelTreeOptions,
  getScopeSettingsOptions,
  getSettingsSchemaOptions,
  listKookGuildRolesOptions,
  putScopeSettingsMutation,
  syncKookStructureMutation,
  upsertChannelMutation,
  upsertGuildMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ChannelOut, GuildOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  Field,
  JsonEditor,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import {
  getAt,
  type JsonSchema,
  leafPaths,
  presentLeaves,
  SchemaInput,
  SECTION_LABELS,
  setAt,
  unsetAt,
} from "@/components/schema-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fmtTime, isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

type SelectedScope = {
  scope: "guild" | "category" | "channel";
  id: string;
  guildId: string;
  label: string;
};

/**
 * Single-page collapsible 3-level KOOK tree (Guild -> Category -> Channel) + shared right-side sparse override editor.
 * Keeps 4-level inheritance (Global -> Guild -> Category -> Channel) compact and non-bloated.
 */
export function ChannelsPage() {
  const tree = useQuery(getChannelTreeOptions());
  const sync = useMutation({
    ...syncKookStructureMutation(),
    onSuccess: (r) => toast.success(`已同步 ${r.guilds} 个服务器、${r.categories} 个分组、${r.channels} 个文字频道`),
  });
  const upsertGuild = useMutation(upsertGuildMutation());
  const removeGuild = useMutation({ ...deleteGuildMutation(), onSuccess: () => toast.success("已删除服务器配置") });
  const upsertChannel = useMutation(upsertChannelMutation());
  const removeChannel = useMutation({ ...deleteChannelMutation(), onSuccess: () => toast.success("已删除频道配置") });
  const backfill = useMutation({ ...backfillChannelMutation(), onSuccess: () => toast.success("已请求回填历史消息") });

  const [selected, setSelected] = useState<SelectedScope | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const toggleCollapse = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  const saveGuild = (g: GuildOut, enabled: boolean) =>
    upsertGuild.mutate({
      path: { guild_id: g.guild_id },
      body: { name: g.name, icon: g.icon, enabled },
    });

  const saveChannel = (ch: ChannelOut, enabled: boolean | null) =>
    upsertChannel.mutate({
      path: { channel_id: ch.channel_id },
      body: {
        guild_id: ch.guild_id,
        parent_id: ch.parent_id,
        name: ch.name,
        topic: ch.topic,
        position: ch.position,
        is_category: ch.is_category,
        enabled,
      },
    });

  const isSelected = (scope: SelectedScope["scope"], id: string) => selected?.scope === scope && selected.id === id;
  const selectNode = (next: SelectedScope) =>
    setSelected((cur) => (cur?.scope === next.scope && cur.id === next.id ? null : next));

  return (
    <div>
      <PageHeader
        title="服务器与频道"
        description="按「全局 → 服务器 → 分组 → 频道」四级稀疏继承控制功能开关、记忆共享、回复风格与触发词。点击右侧「覆盖」仅修改与上级不同的字段。"
        actions={
          <PendingButton pending={sync.isPending} onClick={() => sync.mutate({})}>
            {!sync.isPending && <RefreshCwIcon data-icon="inline-start" />}
            从 KOOK 同步
          </PendingButton>
        }
      />

      <QueryState isLoading={tree.isLoading} error={tree.error} />
      {tree.data?.length === 0 && (
        <Empty
          description="请先在「设置」页配置 KOOK Bot Token 并连接，然后点击右上角「从 KOOK 同步」拉取服务器、分组与频道。"
        >
          还没有同步任何服务器
        </Empty>
      )}

      {!!tree.data?.length && (
        <div className={cn("grid items-start gap-6", selected && "xl:grid-cols-[minmax(0,1fr)_28rem]")}>
          <div className="grid gap-4">
            {tree.data.map(({ guild, categories, ungrouped }) => {
              const gCollapsed = !!collapsed[`g:${guild.guild_id}`];
              const gOverrides = Object.keys(guild.settings ?? {});
              return (
                <Card key={guild.guild_id} className={cn(!guild.enabled && "opacity-70")}>
                  <CardHeader className="py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="icon-xs"
                        variant="ghost"
                        aria-label={gCollapsed ? "展开服务器" : "折叠服务器"}
                        onClick={() => toggleCollapse(`g:${guild.guild_id}`)}
                      >
                        {gCollapsed ? <ChevronRightIcon /> : <ChevronDownIcon />}
                      </Button>
                      <ServerIcon className="size-4 text-muted-foreground" aria-hidden />
                      <CardTitle className="text-base">{guild.name || guild.guild_id}</CardTitle>
                      <span className="font-mono text-xs text-muted-foreground">{guild.guild_id}</span>
                      {gOverrides.length > 0 && (
                        <Badge variant="secondary" className="text-xs font-normal">
                          覆盖 {gOverrides.length} 节
                        </Badge>
                      )}
                      <div className="ml-auto flex items-center gap-2">
                        <Switch
                          aria-label={`启用服务器 ${guild.name || guild.guild_id}`}
                          checked={guild.enabled}
                          onCheckedChange={(en) => saveGuild(guild, en)}
                        />
                        <Button
                          size="xs"
                          variant={isSelected("guild", guild.guild_id) ? "default" : "outline"}
                          onClick={() =>
                            selectNode({
                              scope: "guild",
                              id: guild.guild_id,
                              guildId: guild.guild_id,
                              label: `服务器 ${guild.name || guild.guild_id}`,
                            })
                          }
                        >
                          <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
                        </Button>
                        <ConfirmButton
                          size="xs"
                          variant="ghost"
                          title={`删除服务器 ${guild.name || guild.guild_id} 的配置？`}
                          description="仅删除配置，不删除历史消息与记忆；重新同步可恢复。"
                          onConfirm={() => removeGuild.mutate({ path: { guild_id: guild.guild_id } })}
                        >
                          删除
                        </ConfirmButton>
                      </div>
                    </div>
                  </CardHeader>

                  {!gCollapsed && (
                    <CardContent className="grid gap-3 pt-0">
                      {categories.map(({ category, channels }) => {
                        const cCollapsed = !!collapsed[`c:${category.channel_id}`];
                        const cOverrides = Object.keys(category.settings ?? {});
                        return (
                          <div key={category.channel_id} className="rounded-lg border bg-muted/20">
                            <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                              <Button
                                size="icon-xs"
                                variant="ghost"
                                aria-label={cCollapsed ? "展开分组" : "折叠分组"}
                                onClick={() => toggleCollapse(`c:${category.channel_id}`)}
                              >
                                {cCollapsed ? <ChevronRightIcon /> : <ChevronDownIcon />}
                              </Button>
                              <FolderIcon className="size-4 text-muted-foreground" aria-hidden />
                              <span className="text-sm font-medium">{category.name || category.channel_id}</span>
                              <span className="font-mono text-xs text-muted-foreground">{category.channel_id}</span>
                              <Badge variant="outline" className="text-xs font-normal">
                                {channels.length} 频道
                              </Badge>
                              {cOverrides.length > 0 && (
                                <Badge variant="secondary" className="text-xs font-normal">
                                  覆盖 {cOverrides.length} 节
                                </Badge>
                              )}
                              <div className="ml-auto flex items-center gap-2">
                                <Switch
                                  aria-label={`启用分组 ${category.name || category.channel_id}`}
                                  checked={Boolean(category.enabled)}
                                  onCheckedChange={(en) => saveChannel(category, en)}
                                />
                                <Button
                                  size="xs"
                                  variant={isSelected("category", category.channel_id) ? "default" : "outline"}
                                  onClick={() =>
                                    selectNode({
                                      scope: "category",
                                      id: category.channel_id,
                                      guildId: guild.guild_id,
                                      label: `分组 ${category.name || category.channel_id}`,
                                    })
                                  }
                                >
                                  <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
                                </Button>
                              </div>
                            </div>
                            {!cCollapsed && channels.length > 0 && (
                              <div className="divide-y border-t bg-background/80">
                                {channels.map((ch) => (
                                  <ChannelRow
                                    key={ch.channel_id}
                                    channel={ch}
                                    hasParent
                                    selected={isSelected("channel", ch.channel_id)}
                                    onSelect={() =>
                                      selectNode({
                                        scope: "channel",
                                        id: ch.channel_id,
                                        guildId: guild.guild_id,
                                        label: `频道 #${ch.name || ch.channel_id}`,
                                      })
                                    }
                                    onStateChange={(en) => saveChannel(ch, en)}
                                    onBackfill={() => backfill.mutate({ path: { channel_id: ch.channel_id } })}
                                    backfilling={backfill.isPending && backfill.variables?.path.channel_id === ch.channel_id}
                                    onDelete={() => removeChannel.mutate({ path: { channel_id: ch.channel_id } })}
                                  />
                                ))}
                              </div>
                            )}
                          </div>
                        );
                      })}

                      {ungrouped.length > 0 && (
                        <div className="rounded-lg border">
                          <div className="border-b bg-muted/20 px-3 py-1.5 text-xs font-medium text-muted-foreground">
                            未分组文字频道
                          </div>
                          <div className="divide-y">
                            {ungrouped.map((ch) => (
                              <ChannelRow
                                key={ch.channel_id}
                                channel={ch}
                                hasParent={false}
                                selected={isSelected("channel", ch.channel_id)}
                                onSelect={() =>
                                  selectNode({
                                    scope: "channel",
                                    id: ch.channel_id,
                                    guildId: guild.guild_id,
                                    label: `频道 #${ch.name || ch.channel_id}`,
                                  })
                                }
                                onStateChange={(en) => saveChannel(ch, en)}
                                onBackfill={() => backfill.mutate({ path: { channel_id: ch.channel_id } })}
                                backfilling={backfill.isPending && backfill.variables?.path.channel_id === ch.channel_id}
                                onDelete={() => removeChannel.mutate({ path: { channel_id: ch.channel_id } })}
                              />
                            ))}
                          </div>
                        </div>
                      )}
                    </CardContent>
                  )}
                </Card>
              );
            })}
          </div>

          {selected && (
            <div className="xl:sticky xl:top-6">
              <ScopeOverrideEditor
                key={`${selected.scope}:${selected.id}`}
                selected={selected}
                onClose={() => setSelected(null)}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChannelRow({
  channel: ch,
  hasParent,
  selected,
  onSelect,
  onStateChange,
  onBackfill,
  backfilling,
  onDelete,
}: {
  channel: ChannelOut;
  hasParent: boolean;
  selected: boolean;
  onSelect: () => void;
  onStateChange: (enabled: boolean | null) => void;
  onBackfill: () => void;
  backfilling: boolean;
  onDelete: () => void;
}) {
  const overrides = Object.keys(ch.settings ?? {});
  const modeValue = ch.enabled === null ? "inherit" : ch.enabled ? "on" : "off";

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 px-3 py-2 text-sm transition-colors",
        selected && "bg-primary/5",
        !ch.effective_enabled && "opacity-70",
      )}
    >
      <HashIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium">{ch.name || ch.channel_id}</span>
          <span className="font-mono text-xs text-muted-foreground">{ch.channel_id}</span>
          <Badge variant={ch.effective_enabled ? "default" : "outline"} className="text-[11px] font-normal">
            {ch.effective_enabled ? "生效中" : "已停用"}
          </Badge>
          {overrides.length > 0 && (
            <Badge variant="secondary" className="text-[11px] font-normal">
              覆盖：{overrides.map((k) => SECTION_LABELS[k] ?? k).join("、")}
            </Badge>
          )}
        </div>
        {ch.backfilled_at && (
          <div className="text-xs text-muted-foreground">上次回填：{fmtTime(ch.backfilled_at)}</div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {hasParent ? (
          <OptionSelect
            aria-label={`频道 ${ch.name || ch.channel_id} 开关模式`}
            className="h-7 w-28 text-xs"
            value={modeValue}
            onChange={(v) => onStateChange(v === "inherit" ? null : v === "on")}
            options={[
              { value: "inherit", label: "继承分组" },
              { value: "on", label: "强制启用" },
              { value: "off", label: "强制停用" },
            ]}
          />
        ) : (
          <Switch
            aria-label={`启用频道 ${ch.name || ch.channel_id}`}
            checked={Boolean(ch.enabled)}
            onCheckedChange={(en) => onStateChange(en)}
          />
        )}
        <Button size="xs" variant={selected ? "default" : "outline"} onClick={onSelect}>
          <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
        </Button>
        <PendingButton size="xs" variant="outline" pending={backfilling} onClick={onBackfill}>
          {!backfilling && <DownloadIcon data-icon="inline-start" />}
          回填
        </PendingButton>
        <ConfirmButton
          size="xs"
          variant="ghost"
          title={`删除频道 #${ch.name || ch.channel_id} 的配置？`}
          description="只删除配置（开关和设置覆盖），不删除历史消息和记忆。"
          onConfirm={onDelete}
        >
          删除
        </ConfirmButton>
      </div>
    </div>
  );
}

const pathKey = (path: string[]) => path.join(".");

const QUICK_OVERRIDES: { path: string[]; label: string }[] = [
  { path: ["access", "allow_bot"], label: "是否可用 bot" },
  { path: ["access", "allowed_roles"], label: "角色白名单" },
  { path: ["access", "denied_roles"], label: "角色黑名单" },
  { path: ["access", "role_rules"], label: "按角色细分权限" },
  { path: ["tools", "enabled"], label: "可用工具" },
];

/** Shared sparse-diff override editor for Guild / Category / Channel nodes. */
function ScopeOverrideEditor({ selected, onClose }: { selected: SelectedScope; onClose: () => void }) {
  const schemaQ = useQuery(getSettingsSchemaOptions());
  const settingsQ = useQuery(
    getScopeSettingsOptions({ path: { scope: selected.scope, scope_id: selected.id } }),
  );
  const rolesQ = useQuery({
    ...listKookGuildRolesOptions({ path: { guild_id: selected.guildId } }),
    retry: false,
  });
  const save = useMutation({
    ...putScopeSettingsMutation(),
    onSuccess: () => toast.success(`已保存「${selected.label}」的覆盖设置`),
  });
  const [doc, setDoc] = useState<Record<string, unknown>>({});
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    if (settingsQ.data) setDoc(settingsQ.data.value);
  }, [settingsQ.data]);

  const root = schemaQ.data as JsonSchema | undefined;
  if (!root || !settingsQ.data) {
    return (
      <QueryState
        isLoading={schemaQ.isLoading || settingsQ.isLoading}
        error={schemaQ.error ?? settingsQ.error}
        variant="card"
      />
    );
  }

  const inherited = settingsQ.data.defaults;
  const present = presentLeaves(doc, root, root).filter((l) => l.path.length);
  const presentKeys = new Set(present.map((l) => pathKey(l.path)));
  const available = leafPaths(root, root).filter((l) => !presentKeys.has(pathKey(l.path)));
  const sections = [...new Set(available.map((l) => l.path[0]))];
  const dirty = JSON.stringify(doc) !== JSON.stringify(settingsQ.data.value);

  const ensureOverride = (path: string[]) => {
    if (!presentKeys.has(pathKey(path))) {
      setDoc((cur) => setAt(cur, path, getAt(inherited, path)));
    }
  };

  const toggleRoleList = (field: "allowed_roles" | "denied_roles", roleId: string) => {
    const path = ["access", field];
    const current = getAt(doc, path) ?? getAt(inherited, path);
    const list = Array.isArray(current) ? current.map(String) : [];
    const next = list.includes(roleId) ? list.filter((r) => r !== roleId) : [...list, roleId];
    setDoc((cur) => setAt(cur, path, next));
  };

  const addRoleRule = (roleId: string) => {
    const path = ["access", "role_rules"];
    const current = getAt(doc, path) ?? getAt(inherited, path);
    const map = isPlainObject(current) ? { ...current } : {};
    if (!(roleId in map)) {
      map[roleId] = { allow_bot: null, tools_allow: [], tools_deny: [] };
      setDoc((cur) => setAt(cur, path, map));
    }
  };

  const effAllowed = new Set(
    (Array.isArray(getAt(doc, ["access", "allowed_roles"]) ?? getAt(inherited, ["access", "allowed_roles"]))
      ? ((getAt(doc, ["access", "allowed_roles"]) ?? getAt(inherited, ["access", "allowed_roles"])) as unknown[])
      : []
    ).map(String),
  );
  const effDenied = new Set(
    (Array.isArray(getAt(doc, ["access", "denied_roles"]) ?? getAt(inherited, ["access", "denied_roles"]))
      ? ((getAt(doc, ["access", "denied_roles"]) ?? getAt(inherited, ["access", "denied_roles"])) as unknown[])
      : []
    ).map(String),
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{selected.label}</CardTitle>
            <CardDescription className="mt-1 text-xs">
              继承链：{(settingsQ.data.chain ?? []).join(" → ")}
            </CardDescription>
          </div>
          <Button size="icon-xs" variant="ghost" aria-label="关闭覆盖面板" onClick={onClose}>
            <XIcon />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="form">
          <TabsList>
            <TabsTrigger value="form">表单</TabsTrigger>
            <TabsTrigger value="json">JSON</TabsTrigger>
          </TabsList>
          <TabsContent value="form" className="grid gap-3 pt-2">
            <div className="flex flex-wrap gap-1.5">
              {QUICK_OVERRIDES.filter((q) => !presentKeys.has(pathKey(q.path))).map((q) => (
                <Button key={pathKey(q.path)} size="xs" variant="outline" onClick={() => ensureOverride(q.path)}>
                  <PlusIcon data-icon="inline-start" /> {q.label}
                </Button>
              ))}
            </div>
            {!!rolesQ.data?.length && (
              <div className="rounded-lg border bg-muted/20 p-2.5">
                <div className="mb-1.5 text-xs font-medium text-muted-foreground">
                  本服 KOOK 角色（点按快速加入白名单 / 黑名单 / 细分规则）
                </div>
                <div className="grid max-h-36 gap-1.5 overflow-y-auto pr-1">
                  {rolesQ.data.map((r) => {
                    const inAllow = effAllowed.has(r.role_id);
                    const inDeny = effDenied.has(r.role_id);
                    return (
                      <div key={r.role_id} className="flex flex-wrap items-center gap-1.5 text-xs">
                        <span className="min-w-0 flex-1 truncate font-medium" title={`${r.name} (${r.role_id})`}>
                          {r.name} <span className="font-mono text-[11px] text-muted-foreground">({r.role_id})</span>
                        </span>
                        <Button
                          size="xs"
                          variant={inAllow ? "default" : "outline"}
                          className="h-6 px-1.5 text-[11px]"
                          onClick={() => toggleRoleList("allowed_roles", r.role_id)}
                        >
                          {inAllow ? "已白名单" : "+白名单"}
                        </Button>
                        <Button
                          size="xs"
                          variant={inDeny ? "destructive" : "outline"}
                          className="h-6 px-1.5 text-[11px]"
                          onClick={() => toggleRoleList("denied_roles", r.role_id)}
                        >
                          {inDeny ? "已禁用" : "+禁用"}
                        </Button>
                        <Button
                          size="xs"
                          variant="ghost"
                          className="h-6 px-1.5 text-[11px]"
                          onClick={() => addRoleRule(r.role_id)}
                        >
                          +工具规则
                        </Button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {present.length === 0 && (
              <p className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">
                当前未覆盖任何字段，全部沿用上级配置。
              </p>
            )}
            {present.map(({ path, schema }) => (
              <div key={pathKey(path)} className="flex min-w-0 items-start gap-2 rounded-lg border p-3">
                <Field
                  className="min-w-0 flex-1"
                  label={
                    <span className="inline-flex flex-wrap items-baseline gap-x-2">
                      <span className="text-muted-foreground">{SECTION_LABELS[path[0]] ?? path[0]}</span>
                      <span className="font-mono text-xs">{path.slice(1).join(".") || path[0]}</span>
                    </span>
                  }
                  hint={
                    <>
                      {schema.description && <span className="block">{schema.description}</span>}
                      <span className="block break-all">上级继承值：{JSON.stringify(getAt(inherited, path))}</span>
                    </>
                  }
                >
                  <SchemaInput
                    schema={schema}
                    root={root}
                    value={getAt(doc, path)}
                    onChange={(v) => setDoc(setAt(doc, path, v))}
                  />
                </Field>
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`移除覆盖 ${pathKey(path)}`}
                  onClick={() => setDoc(unsetAt(doc, path))}
                >
                  <Trash2Icon />
                </Button>
              </div>
            ))}
            <Popover open={picking} onOpenChange={setPicking}>
              <PopoverTrigger asChild>
                <Button
                  size="sm"
                  variant="outline"
                  role="combobox"
                  aria-expanded={picking}
                  disabled={!available.length}
                  className="w-fit"
                >
                  <PlusIcon data-icon="inline-start" /> 添加覆盖项
                  <ChevronsUpDownIcon data-icon="inline-end" className="text-muted-foreground" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-96 p-0" align="start">
                <Command>
                  <CommandInput aria-label="搜索设置项" placeholder="搜索功能开关、记忆共享、回复风格、别名…" />
                  <CommandList>
                    <CommandEmpty>没有匹配的设置项</CommandEmpty>
                    {sections.map((section) => (
                      <CommandGroup key={section} heading={SECTION_LABELS[section] ?? section}>
                        {available
                          .filter((l) => l.path[0] === section)
                          .map((l) => (
                            <CommandItem
                              key={pathKey(l.path)}
                              value={pathKey(l.path)}
                              keywords={[SECTION_LABELS[section] ?? section, l.schema.description ?? ""]}
                              onSelect={() => {
                                setDoc(setAt(doc, l.path, getAt(inherited, l.path)));
                                setPicking(false);
                              }}
                            >
                              <span className="font-mono text-xs">{l.path.slice(1).join(".") || section}</span>
                            </CommandItem>
                          ))}
                      </CommandGroup>
                    ))}
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </TabsContent>
          <TabsContent value="json" className="pt-2">
            <Field label="覆盖文档（JSON）">
              <JsonEditor value={doc} rows={12} onChange={(v) => isPlainObject(v) && setDoc(v)} />
            </Field>
          </TabsContent>
        </Tabs>
        <div className="mt-4 flex gap-2">
          <PendingButton
            size="sm"
            pending={save.isPending}
            disabled={!dirty}
            onClick={() =>
              save.mutate({ path: { scope: selected.scope, scope_id: selected.id }, body: doc })
            }
          >
            保存覆盖
          </PendingButton>
          <Button size="sm" variant="outline" disabled={!dirty} onClick={() => setDoc(settingsQ.data.value)}>
            还原
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
