import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon } from "lucide-react";
import { type ReactNode, useState } from "react";
import type { StickerOut } from "@/client";
import {
  getStickerOptions,
  listStickersOptions,
  setStickerEnabledMutation,
} from "@/client/@tanstack/react-query.gen";
import {
  DetailDialog,
  Empty,
  Field,
  FilterBar,
  GuildSelect,
  Meta,
  OptionSelect,
  PageHeader,
  Pager,
  QueryState,
  SectionHeader,
  useDebounced,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 60;

const STATUS_LABELS: Record<StickerOut["status"], string> = {
  fetching: "下载中",
  untagged: "待打标",
  ready: "已打标",
  failed: "失败",
};

const ERROR_LABELS: Record<string, string> = {
  too_large: "文件超过 5MB，没有保存原图",
};

const UNSAFE_LABELS: Record<string, string> = {
  sexual: "色情",
  gore: "血腥暴力",
  extremism: "极端或政治",
  personal_data: "个人隐私",
};

const hasImage = (s: StickerOut) => !!s.mime;

export function StickersPage() {
  const [kind, setKind] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<string | null>(null);
  const [flagged, setFlagged] = useState<string | null>(null);
  const [guildId, setGuildId] = useState<string | null>(null);
  const [order, setOrder] = useState<string | null>("seen");
  const [q, setQ] = useState("");
  const debouncedQ = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const stickers = useQuery(
    listStickersOptions({
      query: {
        kind: kind as StickerOut["kind"] | null,
        status: status as StickerOut["status"] | null,
        enabled: enabled === null ? null : enabled === "true",
        flagged: flagged === null ? null : flagged === "true",
        guild_id: guildId,
        q: debouncedQ || null,
        order: order === "recent" ? "recent" : "seen",
        limit: LIMIT,
        offset,
      },
    }),
  );
  const toggle = useMutation(setStickerEnabledMutation());
  const setStickerEnabled = (id: number, on: boolean) => toggle.mutate({ path: { sticker_id: id }, body: { enabled: on } });
  const filter =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };
  return (
    <div>
      <PageHeader
        title="表情包"
        description="服务器中出现过的表情包：包含 KOOK 服务器自定义表情和单发 GIF/小图表情，由「表情包打标」任务写描述、打标签并做审核标记。下载失败的默认不显示，在状态里选「失败」查看。点卡片看大图和详细信息。"
      />
      <FilterBar>
        <Input
          className="w-64"
          placeholder="搜索描述、文字、标签"
          aria-label="搜索表情包"
          value={q}
          onChange={(e) => filter(setQ)(e.target.value)}
        />
        <GuildSelect className="w-56" value={guildId} onChange={filter(setGuildId)} />
        <OptionSelect
          className="w-32"
          aria-label="类型"
          value={kind}
          onChange={filter(setKind)}
          noneLabel="全部类型"
          options={[
            { value: "image", label: "图片表情" },
            { value: "emoji", label: "服务器表情" },
          ]}
        />
        <OptionSelect
          className="w-40"
          aria-label="状态"
          value={status}
          onChange={filter(setStatus)}
          noneLabel="全部（不含失败）"
          options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))}
        />
        <OptionSelect
          className="w-28"
          aria-label="审核"
          value={flagged}
          onChange={filter(setFlagged)}
          noneLabel="审核：全部"
          options={[
            { value: "true", label: "有风险" },
            { value: "false", label: "无风险" },
          ]}
        />
        <OptionSelect
          className="w-28"
          aria-label="启用"
          value={enabled}
          onChange={filter(setEnabled)}
          noneLabel="启用：全部"
          options={[
            { value: "true", label: "已启用" },
            { value: "false", label: "已禁用" },
          ]}
        />
        <OptionSelect
          className="w-28"
          aria-label="排序"
          value={order}
          onChange={filter(setOrder)}
          options={[
            { value: "seen", label: "最常用" },
            { value: "recent", label: "最近出现" },
          ]}
        />
      </FilterBar>
      <QueryState isLoading={stickers.isLoading} error={stickers.error} />
      {stickers.data?.items.length === 0 && <Empty>没有符合条件的表情包</Empty>}
      {!!stickers.data?.items.length && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-4">
          {stickers.data.items.map((s) => (
            <StickerCard
              key={s.id}
              sticker={s}
              onOpen={() => setSelected(s.id)}
              onToggle={(on) => setStickerEnabled(s.id, on)}
            />
          ))}
        </div>
      )}
      {stickers.data && <Pager total={stickers.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
      {selected !== null && (
        <StickerDialog
          id={selected}
          onClose={() => setSelected(null)}
          onToggle={(on) => setStickerEnabled(selected, on)}
        />
      )}
    </div>
  );
}

function StickerBadges({ sticker: s }: { sticker: StickerOut }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      <Badge variant={s.status === "failed" ? "destructive" : "secondary"}>{STATUS_LABELS[s.status]}</Badge>
      {s.kind === "emoji" && <Badge variant="outline">服务器表情</Badge>}
      {s.unsafe_category && (
        <Badge variant="destructive">审核：{UNSAFE_LABELS[s.unsafe_category] ?? s.unsafe_category}</Badge>
      )}
      {!!s.frames && s.frames > 1 && <Badge variant="outline">动图</Badge>}
    </div>
  );
}

function Placeholder({ sticker: s }: { sticker: StickerOut }) {
  return (
    <span className="px-2 text-center text-xs text-muted-foreground">
      {s.kind === "emoji" ? `服务器表情「${s.summary || "—"}」` : STATUS_LABELS[s.status]}
    </span>
  );
}

function StickerCard({
  sticker: s,
  onOpen,
  onToggle,
}: {
  sticker: StickerOut;
  onOpen: () => void;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === "Enter" && onOpen()}
      className="flex cursor-pointer flex-col gap-2 rounded-lg border p-3 text-left text-sm transition-colors hover:border-foreground/30"
    >
      <div className="flex h-36 items-center justify-center rounded-md bg-muted/40">
        {hasImage(s) ? (
          <img
            src={`/api/stickers/${s.id}/image`}
            alt={s.description || s.summary}
            loading="lazy"
            className="max-h-36 max-w-full object-contain"
          />
        ) : (
          <Placeholder sticker={s} />
        )}
      </div>
      <StickerBadges sticker={s} />
      {s.description && <p className="line-clamp-4">{s.description}</p>}
      {!!s.emotions.length && <p className="text-xs text-muted-foreground">情绪：{s.emotions.join("、")}</p>}
      {s.usage && <p className="line-clamp-3 text-xs text-muted-foreground">适合：{s.usage}</p>}
      {s.ocr_text && <p className="line-clamp-2 text-xs text-muted-foreground">文字：{s.ocr_text}</p>}
      {s.error && (
        <p className="line-clamp-2 text-xs break-all text-destructive">{ERROR_LABELS[s.error] ?? s.error}</p>
      )}
      <div className="mt-auto flex items-center justify-between gap-2 pt-1">
        <Meta
          items={[
            `出现 ${s.seen_count} 次`,
            `${s.guild_count} 个服务器`,
            <time dateTime={s.last_seen}>{fmtTime(s.last_seen)}</time>,
          ]}
        />
        <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <Switch
            checked={s.enabled}
            onCheckedChange={onToggle}
            aria-label={`启用表情包 #${s.id}${s.summary ? ` ${s.summary}` : ""}`}
          />
        </span>
      </div>
    </div>
  );
}

function StickerDialog({ id, onClose, onToggle }: { id: number; onClose: () => void; onToggle: (enabled: boolean) => void }) {
  const detail = useQuery(getStickerOptions({ path: { sticker_id: id } }));
  const d = detail.data;
  const size =
    d?.size_bytes == null
      ? "—"
      : d.size_bytes >= 1024 * 1024
        ? `${(d.size_bytes / 1024 / 1024).toFixed(1)} MB`
        : `${(d.size_bytes / 1024).toFixed(0)} KB`;
  const facts: [string, ReactNode][] = d
    ? [
        ["类型", d.kind === "emoji" ? "服务器表情" : "图片表情"],
        [d.kind === "emoji" ? "Emoji ID" : "SHA256", <span className="font-mono break-all select-all">{d.emoji_id || d.ref}</span>],
        ["格式", d.mime || "—"],
        ["大小", size],
        [
          "尺寸",
          d.width && d.height ? (
            <span className="inline-flex gap-x-3">
              <span>宽 {d.width}</span>
              <span>高 {d.height}</span>
            </span>
          ) : (
            "—"
          ),
        ],
        ["帧数", d.frames ?? "—"],
        ["情绪", d.emotions.join("、") || "—"],
        ["出现次数", d.seen_count],
        ["出现服务器数", d.guild_count],
        ["首次出现", fmtTime(d.first_seen)],
        ["最近出现", fmtTime(d.last_seen)],
        ["打标时间", fmtTime(d.tagged_at)],
        ["打标尝试", d.tag_attempts],
        ["审核", d.unsafe_category ? (UNSAFE_LABELS[d.unsafe_category] ?? d.unsafe_category) : "无风险"],
      ]
    : [];
  return (
    <DetailDialog
      open
      onOpenChange={(o) => !o && onClose()}
      className="sm:max-w-4xl"
      title={`表情包 #${id}`}
      description={d?.summary || "大图、打标结果和各服务器出现次数"}
    >
      <QueryState isLoading={detail.isLoading} error={detail.error} variant="card" />
      {d && (
        <div className="grid gap-4 text-sm md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="flex flex-col gap-2">
            <div className="flex min-h-48 items-center justify-center rounded-md bg-muted/40 p-2">
              {hasImage(d) ? (
                <img
                  src={`/api/stickers/${d.id}/image`}
                  alt={d.description || d.summary}
                  className="max-h-[60svh] max-w-full object-contain"
                />
              ) : (
                <Placeholder sticker={d} />
              )}
            </div>
            <div className="flex items-center justify-between gap-2">
              {hasImage(d) ? (
                <Button size="sm" variant="outline" asChild>
                  <a href={`/api/stickers/${d.id}/image`} target="_blank" rel="noreferrer noopener">
                    打开原图 <ExternalLinkIcon data-icon="inline-end" />
                  </a>
                </Button>
              ) : (
                <span />
              )}
              <Field orientation="horizontal" label={d.enabled ? "已启用" : "已禁用"} className="w-auto">
                <Switch checked={d.enabled} onCheckedChange={onToggle} />
              </Field>
            </div>
          </div>
          <div className="flex flex-col gap-3">
            <StickerBadges sticker={d} />
            {d.description && <p>{d.description}</p>}
            {d.usage && (
              <p>
                <span className="text-muted-foreground">适合：</span>
                {d.usage}
              </p>
            )}
            {d.ocr_text && (
              <div>
                <span className="text-muted-foreground">图中文字：</span>
                <p className="whitespace-pre-wrap">{d.ocr_text}</p>
              </div>
            )}
            {d.error && <p className="break-all text-destructive">错误：{ERROR_LABELS[d.error] ?? d.error}</p>}
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
              {facts.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </div>
          {d.guilds.length > 0 && (
            <div className="md:col-span-2">
              <SectionHeader level={3} title="各服务器出现次数" />
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>服务器</TableHead>
                    <TableHead className="w-24 text-right">次数</TableHead>
                    <TableHead className="w-48">最近</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.guilds.map((g) => (
                    <TableRow key={g.guild_id}>
                      <TableCell>
                        {g.guild_name || "—"} <span className="font-mono text-xs text-muted-foreground">{g.guild_id}</span>
                      </TableCell>
                      <TableCell className="text-right">{g.count}</TableCell>
                      <TableCell>{fmtTime(g.last_seen)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      )}
    </DetailDialog>
  );
}
