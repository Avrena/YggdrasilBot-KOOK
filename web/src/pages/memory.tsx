import { useMutation, useQuery } from "@tanstack/react-query";
import { endOfDay, startOfDay } from "date-fns";
import { ArchiveIcon, ArchiveRestoreIcon, LockIcon, LockOpenIcon, PencilIcon, PlusIcon } from "lucide-react";
import { type ReactNode, useState } from "react";
import type { DateRange } from "react-day-picker";
import { toast } from "sonner";
import {
  archiveSmallTopicsMutation,
  archiveTopicMutation,
  createGlossaryTermMutation,
  deleteGlossaryTermMutation,
  deleteProfileFactMutation,
  deleteProfileMutation,
  getProfileOptions,
  listDigestsOptions,
  listGlossaryOptions,
  listPipelinesOptions,
  listProfilesOptions,
  listTopicsOptions,
  lockGlossaryTermMutation,
  restoreProfileFactMutation,
  updateGlossaryTermMutation,
} from "@/client/@tanstack/react-query.gen";
import type { GlossaryIn, GlossaryOut } from "@/client/types.gen";
import {
  ChannelSelect,
  ConfirmButton,
  DateRangePicker,
  DetailDialog,
  Empty,
  Field,
  FormDialog,
  GuildSelect,
  Hint,
  Meta,
  NumberInput,
  OptionSelect,
  PageHeader,
  Pager,
  Pre,
  QueryState,
  SectionHeader,
  TagInput,
  useChannelName,
  useDebounced,
  useGuildName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export function MemoryPage() {
  const [guildId, setGuildId] = useState<string | null>(null);
  const guild = <GuildSelect className="w-56" value={guildId} onChange={setGuildId} />;
  return (
    <div>
      <PageHeader title="记忆" description="频道话题时间线，以及服务器内共享的每日摘要、黑话词典、用户画像和流水线水位。" />
      <Tabs defaultValue="topics">
        <TabsList>
          <TabsTrigger value="topics">话题</TabsTrigger>
          <TabsTrigger value="digests">每日摘要</TabsTrigger>
          <TabsTrigger value="glossary">黑话词典</TabsTrigger>
          <TabsTrigger value="profiles">画像</TabsTrigger>
          <TabsTrigger value="pipelines">流水线</TabsTrigger>
        </TabsList>
        <TabsContent value="topics">
          <Topics guildId={guildId} guild={guild} />
        </TabsContent>
        <TabsContent value="digests">
          <Digests guildId={guildId} guild={guild} />
        </TabsContent>
        <TabsContent value="glossary">
          <Glossary guildId={guildId} guild={guild} />
        </TabsContent>
        <TabsContent value="profiles">
          <Profiles guildId={guildId} guild={guild} />
        </TabsContent>
        <TabsContent value="pipelines">
          <Pipelines />
        </TabsContent>
      </Tabs>
    </div>
  );
}

const TOPIC_STATUS = [
  { value: "open", label: "进行中" },
  { value: "closed", label: "已结束" },
  { value: "archived", label: "已归档" },
];

type TopicFilters = {
  status: string | null;
  q: string;
  user_id: string;
  min_messages: number | null;
  max_messages: number | null;
  min_participants: number | null;
  max_participants: number | null;
};

const NO_FILTERS: TopicFilters = {
  status: null,
  q: "",
  user_id: "",
  min_messages: null,
  max_messages: null,
  min_participants: null,
  max_participants: null,
};

type TabProps = { guildId: string | null; guild: ReactNode };

function Topics({ guildId, guild }: TabProps) {
  const channelName = useChannelName();
  const [channelId, setChannelId] = useState<string | null>(null);
  const [range, setRange] = useState<DateRange | undefined>();
  const [filters, setFilters] = useState<TopicFilters>(NO_FILTERS);
  const [offset, setOffset] = useState(0);
  const q = useDebounced(filters.q);
  const userId = useDebounced(filters.user_id);
  const limit = 30;
  const topics = useQuery(
    listTopicsOptions({
      query: {
        status: filters.status as "open" | "closed" | "archived" | null,
        q: q.trim() || null,
        user_id: userId.trim() || null,
        min_messages: filters.min_messages,
        max_messages: filters.max_messages,
        min_participants: filters.min_participants,
        max_participants: filters.max_participants,
        guild_id: guildId,
        channel_id: channelId,
        since: range?.from ? startOfDay(range.from).toISOString() : null,
        until: range?.from ? endOfDay(range.to ?? range.from).toISOString() : null,
        limit,
        offset,
      },
    }),
  );
  const archive = useMutation(archiveTopicMutation());
  const archiveSmall = useMutation({
    ...archiveSmallTopicsMutation(),
    onSuccess: (r) => toast.success(`归档了 ${r.archived} 个话题`),
  });
  const set = (patch: Partial<TopicFilters>) => {
    setFilters({ ...filters, ...patch });
    setOffset(0);
  };
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="搜索话题"
          className="w-64"
          placeholder="搜索标题、摘要、关键词"
          value={filters.q}
          onChange={(e) => set({ q: e.target.value })}
        />
        {guild}
        <ChannelSelect
          className="w-56"
          guildId={guildId}
          value={channelId}
          onChange={(v) => {
            setChannelId(v);
            setOffset(0);
          }}
        />
        <DateRangePicker
          value={range}
          onChange={(v) => {
            setRange(v);
            setOffset(0);
          }}
        />
        <OptionSelect
          aria-label="状态"
          className="w-28"
          value={filters.status}
          onChange={(status) => set({ status })}
          options={TOPIC_STATUS}
          noneLabel="全部状态"
        />
        <Input
          aria-label="参与人 ID"
          className="w-36"
          placeholder="参与人 KOOK ID"
          value={filters.user_id}
          onChange={(e) => set({ user_id: e.target.value })}
        />
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          消息数
          <NumberInput
            aria-label="最少消息数"
            className="w-20"
            placeholder="最少"
            value={filters.min_messages}
            onChange={(v) => set({ min_messages: v })}
          />
          至
          <NumberInput
            aria-label="最多消息数"
            className="w-20"
            placeholder="最多"
            value={filters.max_messages}
            onChange={(v) => set({ max_messages: v })}
          />
        </div>
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          人数
          <NumberInput
            aria-label="最少人数"
            className="w-20"
            placeholder="最少"
            value={filters.min_participants}
            onChange={(v) => set({ min_participants: v })}
          />
          至
          <NumberInput
            aria-label="最多人数"
            className="w-20"
            placeholder="最多"
            value={filters.max_participants}
            onChange={(v) => set({ max_participants: v })}
          />
        </div>
        <Button
          variant="ghost"
          onClick={() => {
            setChannelId(null);
            setRange(undefined);
            set(NO_FILTERS);
          }}
        >
          清空筛选
        </Button>
        <div className="ml-auto">
          <Hint content={channelId === null ? "先选择频道" : null}>
            <span>
              {channelId === null ? (
                <Button variant="outline" disabled>
                  <ArchiveIcon data-icon="inline-start" /> 按阈值归档
                </Button>
              ) : (
                <ConfirmButton
                  variant="outline"
                  size="default"
                  title={`按当前阈值整理频道 ${channelName(channelId)} 的已结束话题？`}
                  description="消息数和参与人数都低于阈值（设置中的 memory.topic_archive_below_*），或整理任务算出的权重低于 memory.topic_archive_below_weight 的已结束话题会被归档，agent 搜不到，这里仍可查看。手动恢复过的话题也会再次归档。"
                  onConfirm={() => archiveSmall.mutate({ body: { channel_id: channelId } })}
                >
                  <ArchiveIcon data-icon="inline-start" /> 按阈值归档
                </ConfirmButton>
              )}
            </span>
          </Hint>
        </div>
      </div>
      <QueryState isLoading={topics.isLoading} error={topics.error} />
      {topics.data?.items.length === 0 && <Empty />}
      <ol className="relative grid gap-4 border-l pl-6">
        {topics.data?.items.map((t) => (
          <li key={t.id} className="relative flex items-start gap-4">
            <span
              className={cn(
                "absolute top-1.5 -left-[1.85rem] size-3 rounded-full border-2 border-background",
                t.status === "archived" ? "bg-muted-foreground/40" : "bg-primary",
              )}
            />
            <div className={cn("min-w-0 flex-1", t.status === "archived" && "text-muted-foreground")}>
              <Meta
                items={[
                  <>
                    <time dateTime={t.start_at}>{fmtTime(t.start_at)}</time> 至{" "}
                    <time dateTime={t.end_at}>{fmtTime(t.end_at)}</time>
                  </>,
                  channelId === null && channelName(t.channel_id),
                  `${t.msg_count} 条消息`,
                  `${t.participants.length} 人参与`,
                  t.importance !== null && (
                    <Hint content={t.importance_reason}>
                      <span className="inline-flex gap-x-3">
                        <span>重要度 {t.importance}</span>
                        <span>权重 {t.weight?.toFixed(1)}</span>
                      </span>
                    </Hint>
                  ),
                ]}
              />
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-medium">
                {t.title}
                {t.status === "open" && <Badge variant="outline">进行中</Badge>}
                {t.status === "archived" && (
                  <>
                    <Badge variant="outline" className="border-dashed text-muted-foreground">
                      <ArchiveIcon /> 已归档
                    </Badge>
                    <span className="text-xs font-normal text-muted-foreground">agent 不可见</span>
                  </>
                )}
                {t.dirty && <Badge variant="destructive">待重算</Badge>}
              </div>
              <p className="text-sm whitespace-pre-wrap">{t.summary}</p>
              {!!t.keywords.length && (
                <p className="mt-1 text-xs text-muted-foreground">关键词：{t.keywords.join("、")}</p>
              )}
            </div>
            {t.status !== "open" && (
              <Button
                size="sm"
                variant={t.status === "archived" ? "default" : "outline"}
                className="shrink-0"
                disabled={archive.isPending}
                onClick={() =>
                  archive.mutate({ path: { topic_id: t.id }, body: { archived: t.status !== "archived" } })
                }
              >
                {t.status === "archived" ? <ArchiveRestoreIcon data-icon="inline-start" /> : <ArchiveIcon data-icon="inline-start" />}
                {t.status === "archived" ? "恢复" : "归档"}
              </Button>
            )}
          </li>
        ))}
      </ol>
      {topics.data && <Pager total={topics.data.total} limit={limit} offset={offset} onChange={setOffset} />}
    </div>
  );
}

function Digests({ guildId, guild }: TabProps) {
  const guildName = useGuildName();
  const [offset, setOffset] = useState(0);
  const limit = 14;
  const digests = useQuery(listDigestsOptions({ query: { guild_id: guildId, limit, offset } }));
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">{guild}</div>
      <QueryState isLoading={digests.isLoading} error={digests.error} />
      {digests.data?.items.length === 0 && <Empty />}
      {digests.data?.items.map((d) => (
        <Card key={d.id}>
          <CardHeader>
            <CardTitle className="text-base">
              {d.day}
              {guildId === null && <span className="ml-2 text-sm font-normal text-muted-foreground">{guildName(d.guild_id)}</span>}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm whitespace-pre-wrap">{d.summary}</CardContent>
        </Card>
      ))}
      {digests.data && <Pager total={digests.data.total} limit={limit} offset={offset} onChange={setOffset} />}
    </div>
  );
}

function Glossary({ guildId, guild }: TabProps) {
  const guildName = useGuildName();
  const [q, setQ] = useState("");
  const debouncedQ = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const terms = useQuery(
    listGlossaryOptions({
      query: { guild_id: guildId, q: debouncedQ || null, limit, offset },
    }),
  );
  const lock = useMutation(lockGlossaryTermMutation());
  const remove = useMutation({
    ...deleteGlossaryTermMutation(),
    onSuccess: () => toast.success("已删除"),
  });
  const [editing, setEditing] = useState<GlossaryOut | "new" | null>(null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-64"
          placeholder="搜索词条或释义"
          aria-label="搜索词条"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        {guild}
        <Hint content={guildId === null ? "先选择服务器" : null}>
          <span>
            <Button onClick={() => setEditing("new")} disabled={guildId === null}>
              <PlusIcon data-icon="inline-start" /> 新增词条
            </Button>
          </span>
        </Hint>
      </div>
      <QueryState isLoading={terms.isLoading} error={terms.error} />
      {terms.data?.items.length === 0 && <Empty />}
      {!!terms.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>词条</TableHead>
              <TableHead>释义</TableHead>
              <TableHead>命中</TableHead>
              <TableHead>来源</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {terms.data.items.map((t) => (
              <TableRow key={t.id}>
                <TableCell>
                  <div className="font-medium">{t.term}</div>
                  {!!t.aliases.length && <div className="text-xs text-muted-foreground">{t.aliases.join("、")}</div>}
                  {guildId === null && <div className="text-xs text-muted-foreground">{guildName(t.guild_id)}</div>}
                </TableCell>
                <TableCell className="max-w-xl text-sm whitespace-pre-wrap">{t.definition}</TableCell>
                <TableCell>
                  {t.hits}
                  <div className="text-xs text-muted-foreground">置信 {t.confidence.toFixed(2)}</div>
                </TableCell>
                <TableCell>
                  <Badge variant={t.source === "admin" ? "default" : "outline"}>{t.source}</Badge>
                  {t.locked && <LockIcon className="ml-1 inline size-3.5" aria-label="已锁定" />}
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button size="sm" variant="outline" onClick={() => setEditing(t)}>
                      <PencilIcon data-icon="inline-start" /> 编辑
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        lock.mutate({
                          path: { term_id: t.id },
                          body: { locked: !t.locked },
                        })
                      }
                    >
                      {t.locked ? <LockOpenIcon data-icon="inline-start" /> : <LockIcon data-icon="inline-start" />}{" "}
                      {t.locked ? "解锁" : "锁定"}
                    </Button>
                    <ConfirmButton
                      title={`删除「${t.term}」？`}
                      onConfirm={() => remove.mutate({ path: { term_id: t.id } })}
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {terms.data && <Pager total={terms.data.total} limit={limit} offset={offset} onChange={setOffset} />}
      {editing && (
        <GlossaryDialog
          term={editing === "new" ? null : editing}
          guildId={editing === "new" ? (guildId ?? "") : editing.guild_id}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function GlossaryDialog({
  term,
  guildId,
  onClose,
}: {
  term: GlossaryOut | null;
  guildId: string;
  onClose: () => void;
}) {
  const guildName = useGuildName();
  const [form, setForm] = useState<GlossaryIn>({
    guild_id: guildId,
    term: term?.term ?? "",
    aliases: term?.aliases ?? [],
    definition: term?.definition ?? "",
  });
  const create = useMutation({
    ...createGlossaryTermMutation(),
    onSuccess: onClose,
  });
  const update = useMutation({
    ...updateGlossaryTermMutation(),
    onSuccess: onClose,
  });
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={term ? `编辑「${term.term}」` : `新增词条（${guildName(guildId)}）`}
      description="管理员编辑的词条会被锁定，整理流水线不再覆盖。"
      pending={create.isPending || update.isPending}
      submitDisabled={!form.term.trim() || !form.definition.trim()}
      onSubmit={() =>
        term ? update.mutate({ path: { term_id: term.id }, body: form }) : create.mutate({ body: form })
      }
    >
      <Field label="词条">
        <Input required value={form.term} onChange={(e) => setForm({ ...form, term: e.target.value })} />
      </Field>
      <Field label="别名">
        <TagInput value={form.aliases ?? []} onChange={(aliases) => setForm({ ...form, aliases })} />
      </Field>
      <Field label="释义">
        <Textarea
          rows={5}
          required
          value={form.definition}
          onChange={(e) => setForm({ ...form, definition: e.target.value })}
        />
      </Field>
    </FormDialog>
  );
}

function Profiles({ guildId, guild }: TabProps) {
  const guildName = useGuildName();
  const [q, setQ] = useState("");
  const debouncedQ = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const profiles = useQuery(
    listProfilesOptions({
      query: { guild_id: guildId, q: debouncedQ || null, limit, offset },
    }),
  );
  const [selected, setSelected] = useState<{
    guild_id: string;
    user_id: string;
  } | null>(null);
  const remove = useMutation({
    ...deleteProfileMutation(),
    onSuccess: () => {
      toast.success("已删除画像");
      setSelected(null);
    },
  });
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-64"
          placeholder="搜索昵称、用户 ID 或摘要"
          aria-label="搜索画像"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        {guild}
      </div>
      <QueryState isLoading={profiles.isLoading} error={profiles.error} />
      {profiles.data?.items.length === 0 && <Empty />}
      {!!profiles.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>用户</TableHead>
              <TableHead>摘要</TableHead>
              <TableHead>兴趣</TableHead>
              <TableHead>更新</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {profiles.data.items.map((p) => (
              <TableRow key={`${p.guild_id}:${p.user_id}`}>
                <TableCell>
                  <div className="font-medium">{p.display_name || p.user_id}</div>
                  {guildId === null && <div className="text-xs text-muted-foreground">{guildName(p.guild_id)}</div>}
                  {p.opted_out && <Badge variant="outline">已退出记忆</Badge>}
                </TableCell>
                <TableCell className="min-w-80 max-w-xl text-sm whitespace-normal">
                  <div className="line-clamp-3">{p.summary}</div>
                </TableCell>
                <TableCell className="min-w-40 max-w-xs text-xs whitespace-normal">
                  <div className="line-clamp-3">{p.interests.join("、")}</div>
                </TableCell>
                <TableCell className="text-xs">{fmtTime(p.updated_at)}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        setSelected({
                          guild_id: p.guild_id,
                          user_id: p.user_id,
                        })
                      }
                    >
                      查看
                    </Button>
                    <ConfirmButton
                      title={`删除 ${p.display_name || p.user_id} 的画像？`}
                      description="同时删除画像下的全部事实。"
                      onConfirm={() =>
                        remove.mutate({
                          path: { guild_id: p.guild_id, user_id: p.user_id },
                        })
                      }
                    >
                      删除
                    </ConfirmButton>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {profiles.data && <Pager total={profiles.data.total} limit={limit} offset={offset} onChange={setOffset} />}
      {selected && <ProfileDialog {...selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

const ARCHIVE_REASON: Record<string, string> = {
  low: "重要度低",
  duplicate: "与另一条重复",
  cap: "超出每人上限",
  outdated: "被新消息推翻",
};

function ProfileDialog({ guild_id, user_id, onClose }: { guild_id: string; user_id: string; onClose: () => void }) {
  const guildName = useGuildName();
  const profile = useQuery(getProfileOptions({ path: { guild_id, user_id } }));
  const removeFact = useMutation({ ...deleteProfileFactMutation(), onSuccess: () => toast.success("已删除") });
  const restoreFact = useMutation({ ...restoreProfileFactMutation(), onSuccess: () => toast.success("已恢复") });
  const [showArchived, setShowArchived] = useState(false);
  const p = profile.data;
  const archived = p?.facts.filter((f) => f.archived_at !== null).length ?? 0;
  const facts = p?.facts.filter((f) => showArchived || f.archived_at === null) ?? [];
  return (
    <DetailDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={p?.display_name || user_id}
      description={
        <span className="inline-flex flex-wrap gap-x-3">
          <span>{guildName(guild_id)}</span>
          <span>
            KOOK ID <span className="font-mono">{user_id}</span>
          </span>
        </span>
      }
    >
      <QueryState isLoading={profile.isLoading} error={profile.error} />
      {p && (
        <div className="grid gap-3 text-sm">
          <p className="whitespace-pre-wrap">{p.summary || "（无摘要）"}</p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">兴趣</dt>
            <dd>{p.interests.join("、") || "—"}</dd>
            <dt className="text-muted-foreground">特点</dt>
            <dd>{p.traits.join("、") || "—"}</dd>
          </dl>
          <SectionHeader
            level={3}
            title="事实"
            description="按权重排序；get_profile 给 agent 看权重（含时间衰减）最高的 10 条有效事实。归档的事实 agent 和画像更新都看不到，恢复后整理任务不再归档它。"
            actions={
              archived > 0 && (
                <Button size="xs" variant="ghost" onClick={() => setShowArchived(!showArchived)}>
                  {showArchived ? "隐藏已归档" : `显示已归档（${archived}）`}
                </Button>
              )
            }
          />
          {facts.length === 0 && <Empty>{archived > 0 ? "有效事实都已归档" : "没有记录事实"}</Empty>}
          {facts.length > 0 && (
            <ScrollArea viewportClassName="max-h-80">
              <div className="grid gap-1.5">
                {facts.map((f) => (
                  <div
                    key={f.id}
                    className={cn(
                      "flex items-start justify-between gap-2 rounded-md border px-3 py-2",
                      f.archived_at !== null && "border-dashed text-muted-foreground",
                    )}
                  >
                    <div>
                      {f.fact}
                      <Meta
                        className="mt-0.5"
                        items={[
                          <time dateTime={f.created_at}>{fmtTime(f.created_at)}</time>,
                          f.importance !== null && `重要度 ${f.importance}`,
                          f.importance !== null && `权重 ${f.weight?.toFixed(1)}`,
                          f.archived_at !== null && (
                            <span className="inline-flex items-center gap-1.5">
                              <Badge variant="outline" className="border-dashed text-muted-foreground">
                                <ArchiveIcon /> 已归档
                              </Badge>
                              {ARCHIVE_REASON[f.archive_reason ?? ""] ?? "其他原因"}
                            </span>
                          ),
                          f.locked && (
                            <Badge variant="outline">
                              <LockIcon /> 管理员恢复
                            </Badge>
                          ),
                        ]}
                      />
                    </div>
                    <div className="flex shrink-0 gap-1">
                      {f.archived_at !== null && (
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={restoreFact.isPending}
                          onClick={() => restoreFact.mutate({ path: { guild_id, user_id, fact_id: f.id } })}
                        >
                          <ArchiveRestoreIcon data-icon="inline-start" /> 恢复
                        </Button>
                      )}
                      <ConfirmButton
                        size="xs"
                        variant="ghost"
                        title="删除这条事实？"
                        description={f.fact}
                        disabled={removeFact.isPending}
                        onConfirm={() => removeFact.mutate({ path: { guild_id, user_id, fact_id: f.id } })}
                      >
                        删除
                      </ConfirmButton>
                    </div>
                  </div>
                ))}
              </div>
            </ScrollArea>
          )}
        </div>
      )}
    </DetailDialog>
  );
}

const CHANNEL_PIPELINES = new Set(["chunk", "topic"]);

function Pipelines() {
  const guildName = useGuildName();
  const channelName = useChannelName();
  const pipelines = useQuery(listPipelinesOptions());
  return (
    <div className="grid gap-4">
      <QueryState isLoading={pipelines.isLoading} error={pipelines.error} />
      {pipelines.data?.length === 0 && <Empty>流水线还没有运行过</Empty>}
      {!!pipelines.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>流水线</TableHead>
              <TableHead>作用域</TableHead>
              <TableHead>水位（消息 id）</TableHead>
              <TableHead>积压</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>更新</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pipelines.data.map((p) => (
              <TableRow key={`${p.pipeline}:${p.scope_id}`}>
                <TableCell className="font-mono">{p.pipeline}</TableCell>
                <TableCell>
                  <div>{CHANNEL_PIPELINES.has(p.pipeline) ? channelName(p.scope_id) : guildName(p.scope_id)}</div>
                  <div className="font-mono text-xs text-muted-foreground">{p.scope_id}</div>
                </TableCell>
                <TableCell className="font-mono">{p.last_msg_id}</TableCell>
                <TableCell>{p.backlog >= 10000 ? "10000 以上" : p.backlog}</TableCell>
                <TableCell>
                  {Object.keys(p.state).length ? <Pre value={p.state} className="max-h-32" /> : "—"}
                </TableCell>
                <TableCell className="text-xs">{fmtTime(p.updated_at)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
