import { format, isThisYear, isToday } from "date-fns";
import {
  BookIcon,
  ChevronDownIcon,
  FileIcon,
  FilmIcon,
  ImageOffIcon,
  type LucideIcon,
  MicIcon,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import type { BotReply, ChatMessage, OpsReply, SourceRef } from "@/client";
import { DetailDialog, ExternalLink, Hint } from "@/components/common";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Message, MessageAvatar, MessageContent, MessageGroup, MessageHeader } from "@/components/ui/message";
import { fmtTime, isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

/** KOOK user_id -> display name. */
export type Names = Record<string, string>;
/** A stored KOOK segment (`yggdrasil/kook/segments.py`). */
export type Segment = { type: string; data: Record<string, unknown> };

type Lookup = (kookMessageId: string) => ChatMessage | undefined;

const BARE: Record<string, true> = { card: true, image: true };
const DIVIDER_GAP_MS = 5 * 60_000;

function str(v: unknown): string {
  return v == null ? "" : String(v);
}

function toSegments(raw: unknown): Segment[] {
  if (typeof raw === "string") return raw ? [{ type: "text", data: { text: raw } }] : [];
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isPlainObject)
    .map((s) => ({ type: str(s.type) || "unknown", data: isPlainObject(s.data) ? s.data : {} }));
}

function senderName(names: Names, userId: string, fallback?: string | null): string {
  return names[userId] || fallback || userId;
}

function mentionName(data: Record<string, unknown>, names: Names): string {
  const uid = str(data.user_id);
  return names[uid] || str(data.name).replace(/^@/, "") || uid;
}

function clock(at: string | null | undefined): string | null {
  if (!at) return null;
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? null : format(d, "HH:mm");
}

function webUrl(v: unknown): string | null {
  const s = str(v).trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("//")) return `https:${s}`;
  return null;
}

function humanSize(v: unknown): string {
  let n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${i === 0 ? n : +n.toFixed(1)} ${units[i]}`;
}

function duration(v: unknown): string {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "";
  return n < 60 ? `${n}″` : `${Math.floor(n / 60)}′${n % 60}″`;
}

function segmentText({ type, data }: Segment, names: Names): string {
  switch (type) {
    case "text":
      return str(data.text);
    case "kmarkdown":
      return str(data.content);
    case "mention":
      return `@${mentionName(data, names)} `;
    case "mention_all":
      return "@全体成员 ";
    case "mention_here":
      return "@在线成员 ";
    case "mention_role":
      return `@${str(data.name) || str(data.role_id)} `;
    case "reply":
      return "";
    case "emoji":
      return `:${str(data.name) || str(data.id)}:`;
    case "image":
      return data.sticker === true ? `[表情:${str(data.name) || "图"}]` : "[图片]";
    case "audio":
      return "[语音]";
    case "video":
      return "[视频]";
    case "file":
      return `[文件] ${str(data.file_name)}`;
    case "card":
      return str(data.plain_text) || "[卡片消息]";
    default:
      return `[${type}]`;
  }
}

function plainText(segments: unknown, names: Names): string {
  return toSegments(segments)
    .map((s) => segmentText(s, names))
    .join("")
    .replace(/\s+/g, " ")
    .trim();
}

function Chip({ icon: Icon, parts }: { icon?: LucideIcon; parts: string[] }) {
  return (
    <span className="mx-0.5 inline-flex max-w-full items-center gap-1.5 rounded-md border px-1.5 py-0.5 align-middle text-xs text-muted-foreground [&>svg]:size-3 [&>svg]:shrink-0">
      {Icon && <Icon aria-hidden />}
      {parts.filter(Boolean).map((p, i) => (
        <span key={i} className={cn("truncate", i > 0 && "shrink-0 tabular-nums")}>
          {p}
        </span>
      ))}
    </span>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

export function KookAvatar({ userId, name, className }: { userId: string; name?: string; className?: string }) {
  const label = (name || userId).trim();
  return (
    <Avatar className={className}>
      <AvatarFallback>{Array.from(label)[0]?.toUpperCase() ?? "?"}</AvatarFallback>
    </Avatar>
  );
}

function ReplyQuote({ id, names, lookup }: { id: unknown; names: Names; lookup?: Lookup }) {
  const qid = str(id);
  const target = qid ? lookup?.(qid) : undefined;
  const time = clock(target?.sent_at);
  return (
    <span className="mb-1 block rounded-md border-l-2 border-primary/40 bg-background/50 px-2 py-1 text-xs text-muted-foreground">
      {target ? (
        <>
          <span className="font-medium">{senderName(names, target.user_id, target.sender_name)}</span>
          {time && <span className="ml-1.5 tabular-nums">{time}</span>}
          <span className="line-clamp-2 wrap-break-word">{plainText(target.segments, names) || "[空消息]"}</span>
        </>
      ) : (
        "引用回复了一条消息"
      )}
    </span>
  );
}

function ImageSegment({ data, sticker, alt }: { data: Record<string, unknown>; sticker: boolean; alt: string }) {
  const [broken, setBroken] = useState(false);
  const [open, setOpen] = useState(false);
  const src = str(data.src) || str(data.url);
  if (!src || broken) return <Chip icon={ImageOffIcon} parts={[alt]} />;
  if (sticker) {
    return (
      <img
        src={src}
        alt={alt}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        className="inline-block max-h-24 max-w-24 align-bottom"
      />
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="查看原图"
        className="my-1 block cursor-zoom-in overflow-hidden rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <img
          src={src}
          alt={alt}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setBroken(true)}
          className="max-h-60 max-w-60 object-contain"
        />
      </button>
      <DetailDialog open={open} onOpenChange={setOpen} title="图片" className="sm:max-w-4xl">
        <img src={src} alt={alt} referrerPolicy="no-referrer" className="mx-auto max-h-[70svh] max-w-full object-contain" />
        <ExternalLink href={src}>在新标签页打开</ExternalLink>
      </DetailDialog>
    </>
  );
}

function KookCardSegment({ data }: { data: Record<string, unknown> }) {
  const plain = str(data.plain_text).trim();
  return (
    <div className="my-1 w-full max-w-xl rounded-lg border border-l-4 border-l-primary bg-card p-3 text-left text-sm text-card-foreground">
      <div className="mb-1 text-xs font-medium text-muted-foreground">KOOK 卡片消息</div>
      <div className="whitespace-pre-wrap">{plain || "[卡片]"}</div>
    </div>
  );
}

function SegmentView({ segment, names, lookup }: { segment: Segment; names: Names; lookup?: Lookup }) {
  const { type, data } = segment;
  switch (type) {
    case "text":
      return <span className="whitespace-pre-wrap">{str(data.text)}</span>;
    case "kmarkdown":
      return <span className="whitespace-pre-wrap">{str(data.content)}</span>;
    case "mention":
      return (
        <Hint content={str(data.user_id) || null}>
          <span className="mr-0.5 font-medium text-primary">@{mentionName(data, names)}</span>
        </Hint>
      );
    case "mention_all":
      return <span className="mr-0.5 font-medium text-primary">@全体成员</span>;
    case "mention_here":
      return <span className="mr-0.5 font-medium text-primary">@在线成员</span>;
    case "mention_role":
      return <span className="mr-0.5 font-medium text-primary">@{str(data.name) || str(data.role_id)}</span>;
    case "reply":
      return <ReplyQuote id={data.message_id} names={names} lookup={lookup} />;
    case "emoji":
      return str(data.src) ? (
        <ImageSegment data={data} sticker alt={segmentText(segment, names)} />
      ) : (
        <Chip parts={[segmentText(segment, names)]} />
      );
    case "image":
      return <ImageSegment data={data} sticker={data.sticker === true} alt={segmentText(segment, names)} />;
    case "audio":
      return <Chip icon={MicIcon} parts={["语音", duration(data.duration)]} />;
    case "video":
      return <Chip icon={FilmIcon} parts={["视频", duration(data.duration)]} />;
    case "file":
      return <Chip icon={FileIcon} parts={[str(data.file_name) || "未命名文件", humanSize(data.file_size)]} />;
    case "card":
      return <KookCardSegment data={data} />;
    default:
      return <Chip parts={[segmentText(segment, names)]} />;
  }
}

export function KookSegments({ segments, names, lookup }: { segments: Segment[]; names: Names; lookup?: Lookup }) {
  const list = toSegments(segments);
  if (list.length === 0) return <span className="text-muted-foreground">[空消息]</span>;
  return (
    <div className="min-w-0 wrap-break-word">
      {list.map((segment, i) => (
        <SegmentView key={i} segment={segment} names={names} lookup={lookup} />
      ))}
    </div>
  );
}

function ChatRow({
  userId,
  name,
  at,
  self = false,
  recalled = false,
  highlight = false,
  bare = false,
  children,
}: {
  userId: string;
  name: string;
  at?: string | null;
  self?: boolean;
  recalled?: boolean;
  highlight?: boolean;
  bare?: boolean;
  children: ReactNode;
}) {
  const time = clock(at);
  return (
    <Message
      align={self ? "end" : "start"}
      className={cn("rounded-lg p-1", highlight && "bg-primary/5 ring-1 ring-primary/40")}
    >
      <MessageAvatar className="self-start bg-transparent">
        <KookAvatar userId={userId} name={name} />
      </MessageAvatar>
      <MessageContent className="gap-1">
        <MessageHeader className="gap-2 px-0">
          <Hint content={userId || null}>
            <span className="min-w-0 truncate">{name}</span>
          </Hint>
          {time && at && (
            <Hint content={fmtTime(at)}>
              <time dateTime={at} className="font-normal tabular-nums">
                {time}
              </time>
            </Hint>
          )}
          {recalled && (
            <Badge variant="secondary" className="font-normal text-muted-foreground">
              已删除
            </Badge>
          )}
        </MessageHeader>
        <Bubble
          variant={bare ? "ghost" : self ? "tinted" : "secondary"}
          align={self ? "end" : "start"}
          className={cn(recalled && "opacity-60")}
        >
          <BubbleContent>{children}</BubbleContent>
        </Bubble>
      </MessageContent>
    </Message>
  );
}

export function KookMessageView({
  message,
  names,
  lookup,
  highlight = false,
}: {
  message: ChatMessage;
  names: Names;
  lookup?: Lookup;
  highlight?: boolean;
}) {
  const segments = toSegments(message.segments);
  return (
    <ChatRow
      userId={message.user_id}
      name={senderName(names, message.user_id, message.sender_name)}
      at={message.sent_at}
      self={!!message.is_self}
      recalled={!!message.recalled}
      highlight={highlight}
      bare={segments.length === 1 && BARE[segments[0].type] === true}
    >
      <KookSegments segments={segments} names={names} lookup={lookup} />
    </ChatRow>
  );
}

export function KookMessageList({
  messages,
  names,
  lookup,
  highlightIds,
}: {
  messages: ChatMessage[];
  names: Names;
  lookup?: Lookup;
  highlightIds?: number[];
}) {
  const highlighted = new Set(highlightIds);
  const dividers: (string | null)[] = [];
  let last = Number.NaN;
  for (const m of messages) {
    const t = m.sent_at ? new Date(m.sent_at).getTime() : Number.NaN;
    let label: string | null = null;
    if (!Number.isNaN(t)) {
      if (Number.isNaN(last) || t - last > DIVIDER_GAP_MS) {
        const d = new Date(t);
        label = format(d, isToday(d) ? "HH:mm" : isThisYear(d) ? "M月d日 HH:mm" : "yyyy年M月d日 HH:mm");
      }
      last = t;
    }
    dividers.push(label);
  }
  return (
    <MessageGroup className="gap-3">
      {messages.map((m, i) => (
        <div key={`${i}:${m.id ?? ""}`} className="grid gap-3">
          {dividers[i] && <div className="text-center text-xs text-muted-foreground tabular-nums">{dividers[i]}</div>}
          <KookMessageView
            message={m}
            names={names}
            lookup={lookup}
            highlight={m.id != null && highlighted.has(m.id)}
          />
        </div>
      ))}
    </MessageGroup>
  );
}

function Sources({ sources }: { sources: SourceRef[] }) {
  return (
    <Collapsible className="pl-11 text-xs">
      <CollapsibleTrigger className="group flex items-center gap-1 font-medium text-primary">
        引用了 {sources.length} 个来源
        <ChevronDownIcon className="size-4 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 flex w-fit flex-col gap-2">
        {sources.map((s, i) => {
          const url = webUrl(s.url);
          return (
            <div key={i} className="flex items-center gap-2">
              <BookIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              {url ? <ExternalLink href={url}>{s.label}</ExternalLink> : <span>{s.label}</span>}
            </div>
          );
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}

function isBotReply(reply: BotReply | OpsReply): reply is BotReply {
  return "sections" in reply || "sources" in reply || "title" in reply || "silent_reason" in reply || reply.mode === "silent";
}

/** Renders BotReply (adaptive KMarkdown or KOOK CardMessage preview) or OpsReply. */
export function ReplyView({
  reply,
  botName = "bot",
  botId = "",
}: {
  reply: BotReply | OpsReply;
  botName?: string;
  botId?: string;
  defaultOpen?: boolean;
}) {
  if (isBotReply(reply)) {
    if (reply.mode === "silent") {
      const reaction = reply.silent_reaction ? `（贴表情 ${reply.silent_reaction}）` : "";
      return <Note>{reply.silent_reason ? `不回复${reaction}：${reply.silent_reason}` : `不回复${reaction}`}</Note>;
    }
    if (reply.mode === "done") return <Note>已完成操作，没有发消息</Note>;

    const sections = reply.sections ?? [];
    const isCard =
      reply.style === "card" ||
      Boolean(reply.title) ||
      sections.length > 1 ||
      sections.some((s) => Boolean(s.heading)) ||
      (reply.sources?.length ?? 0) > 0;

    return (
      <div className="grid gap-2">
        <ChatRow userId={botId} name={botName} bare={isCard}>
          {isCard ? (
            <div className="w-full max-w-xl rounded-lg border border-l-4 border-l-primary bg-card p-3 text-left text-sm text-card-foreground">
              {reply.title && <div className="mb-2 text-base font-semibold">{reply.title}</div>}
              <div className="grid gap-2.5">
                {sections.map((s, i) => (
                  <div key={i} className={cn(i > 0 && s.heading && "border-t pt-2")}>
                    {s.heading && <div className="mb-1 font-medium">{s.heading}</div>}
                    <div className="whitespace-pre-wrap">{s.content}</div>
                    {!!s.images?.length && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {s.images.map((img) => (
                          <Badge key={img} variant="secondary" className="font-mono text-xs">
                            图片 {img}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="whitespace-pre-wrap">
              {sections
                .flatMap((s) => [s.content, ...(s.images ?? []).map((k) => `[图片 ${k}]`)])
                .filter((x) => x.trim())
                .join("\n") || "[空消息]"}
            </div>
          )}
        </ChatRow>
        {reply.sources && reply.sources.length > 0 && <Sources sources={reply.sources} />}
      </div>
    );
  }
  if (reply.mode === "reply") {
    return (
      <ChatRow userId={botId} name={botName}>
        <span className="whitespace-pre-wrap">{reply.text || "[空消息]"}</span>
      </ChatRow>
    );
  }
  return <Note>已完成操作</Note>;
}
