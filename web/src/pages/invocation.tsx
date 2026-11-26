import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeftIcon } from "lucide-react";
import { getInvocationOptions, getInvocationTranscriptOptions } from "@/client/@tanstack/react-query.gen";
import type { ModerationOut, VerdictOut } from "@/client/types.gen";
import { ExternalLink, Meta, PageHeader, QueryState, SectionHeader, useChannelName } from "@/components/common";
import { ReplyView } from "@/components/kook";
import { Code, ContextUsage, Facts, Transcript, fmtMs, fmtTokens } from "@/components/transcript";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { statusVariant } from "@/pages/invocations";

const STAGES: Record<string, string> = { input: "输入审核", output: "输出审核", ack: "快速响应审核" };

function Verdict({ v }: { v: VerdictOut | ModerationOut }) {
  const flagged = (v.action || "pass") !== "pass";
  return (
    <div className={cn("grid gap-1 rounded-lg border p-2 text-xs", flagged && "border-destructive/40 bg-destructive/5")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-medium">{STAGES[v.stage] ?? v.stage}</span>
        <Badge variant={flagged ? "destructive" : "secondary"}>{v.verdict}</Badge>
        {v.action && <Badge variant={flagged ? "destructive" : "outline"}>{v.action}</Badge>}
      </div>
      <Meta
        items={[
          <span className="tabular-nums">分数 {v.score.toFixed(2)}</span>,
          !!v.categories?.length && `类别：${v.categories.join("、")}`,
          v.model && <span className="font-mono">{v.model}</span>,
          v.latency_ms != null && <span className="tabular-nums">{v.latency_ms} ms</span>,
        ]}
      />
    </div>
  );
}

export function InvocationPage() {
  const { id } = useParams({ from: "/invocations/$id" });
  const detail = useQuery({
    ...getInvocationOptions({ path: { invocation_id: id } }),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 3000 : false),
  });
  const running = detail.data?.status === "running";
  const transcript = useQuery({
    ...getInvocationTranscriptOptions({ path: { invocation_id: id } }),
    refetchInterval: (q) => (q.state.data?.live || running ? 3000 : false),
  });
  const channelName = useChannelName();
  const d = detail.data;
  const t = transcript.data;
  const verdicts = [d?.input_verdict, d?.output_verdict].filter((v): v is VerdictOut => !!v);
  // Stored moderation rows repeat the input / output verdicts; list only the others.
  const moderation = (d?.moderation ?? []).filter(
    (m) => !verdicts.some((v) => v.stage === m.stage && v.verdict === m.verdict && v.score === m.score),
  );
  const tools = Object.entries(d?.tool_calls ?? {});
  return (
    <div>
      <Button variant="link" size="sm" className="mb-2 px-0" asChild>
        <Link to="/invocations">
          <ArrowLeftIcon data-icon="inline-start" />
          调用记录
        </Link>
      </Button>
      <PageHeader
        title="调用详情"
        description={<span className="font-mono">{id}</span>}
        actions={d?.trace_url && <ExternalLink href={d.trace_url}>在 Grafana 打开</ExternalLink>}
      />
      <QueryState isLoading={detail.isLoading} error={detail.error} variant="card" />
      {d && (
        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <aside aria-label="调用信息" className="xl:sticky xl:top-6 xl:col-start-2 xl:row-start-1">
            <ScrollArea viewportClassName="xl:max-h-[calc(100svh-3rem)]">
              <div className="grid gap-4 rounded-xl border p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={statusVariant(d.status)}>{d.status}</Badge>
                  <span className="font-mono text-sm">{d.handler}</span>
                </div>
                <Facts
                  items={[
                    ["频道", d.channel_id ? channelName(d.channel_id) : "私聊"],
                    [
                      "用户",
                      <>
                        {d.user_name || "—"} <span className="font-mono text-xs text-muted-foreground">{d.user_id}</span>
                      </>,
                    ],
                    ["时间", fmtTime(d.created_at)],
                    ["耗时", d.duration_ms != null ? fmtMs(d.duration_ms) : "—"],
                    [
                      "快速响应",
                      d.ack_kind ? (
                        <Meta
                          className="text-sm"
                          items={[
                            <span className="text-foreground">{d.ack_kind}</span>,
                            d.ack_latency_ms != null && <span className="tabular-nums">{d.ack_latency_ms} ms</span>,
                          ]}
                        />
                      ) : (
                        "—"
                      ),
                    ],
                    [
                      "模型",
                      d.models.length ? (
                        <div className="grid gap-0.5 font-mono text-xs">
                          {d.models.map((m) => (
                            <span key={m}>{m}</span>
                          ))}
                        </div>
                      ) : (
                        "—"
                      ),
                    ],
                    [
                      "tokens",
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <ContextUsage
                          model={d.models[0]}
                          input={d.tokens_in}
                          cached={d.tokens_cached}
                          output={d.tokens_out}
                          transcript={t}
                        />
                        <Meta
                          className="tabular-nums"
                          items={[
                            `输入 ${fmtTokens(d.tokens_in)}`,
                            !!d.tokens_cached && `缓存 ${fmtTokens(d.tokens_cached)}`,
                            `输出 ${fmtTokens(d.tokens_out)}`,
                          ]}
                        />
                      </div>,
                    ],
                    [
                      "工具",
                      tools.length ? (
                        <ul className="grid gap-0.5 text-xs">
                          {tools.map(([name, n]) => (
                            <li key={name} className="flex flex-wrap gap-x-2">
                              <span className="font-mono">{name}</span>
                              <span className="text-muted-foreground tabular-nums">{n} 次</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        "—"
                      ),
                    ],
                    ["trace", d.trace_url ? <ExternalLink href={d.trace_url}>在 Grafana 打开</ExternalLink> : "—"],
                  ]}
                />
                {d.error && <Code title="错误" value={d.error} variant="destructive" className="max-h-60" />}
                {(verdicts.length > 0 || moderation.length > 0) && (
                  <>
                    <Separator />
                    <div className="grid gap-2">
                      <SectionHeader level={3} title="审核" />
                      {verdicts.map((v) => (
                        <Verdict key={v.stage} v={v} />
                      ))}
                      {moderation.map((m) => (
                        <Verdict key={m.id} v={m} />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </ScrollArea>
          </aside>
          <section aria-label="对话" className="grid min-w-0 gap-5 xl:col-start-1 xl:row-start-1">
            <QueryState isLoading={transcript.isLoading} error={transcript.error} variant="card" />
            {t && <Transcript transcript={t} startedAt={d.created_at} />}
            {t && !t.available && d.reply && <ReplyView reply={d.reply} defaultOpen />}
          </section>
        </div>
      )}
    </div>
  );
}
