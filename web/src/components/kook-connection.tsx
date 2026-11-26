import { useMutation, useQuery } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import {
  deleteKookConnectionMutation,
  getKookConnectionOptions,
  putKookConnectionMutation,
  syncKookStructureMutation,
} from "@/client/@tanstack/react-query.gen";
import { ConfirmButton, Field, PendingButton, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fmtTime } from "@/lib/format";

/** KOOK Bot Token & WebSocket Gateway status card. */
export function KookConnectionCard() {
  const conn = useQuery({ ...getKookConnectionOptions(), refetchInterval: 5_000 });
  const [token, setToken] = useState("");
  const save = useMutation({
    ...putKookConnectionMutation(),
    onSuccess: () => {
      setToken("");
      toast.success("已保存 Bot Token，正在重连 KOOK 网关");
    },
  });
  const clear = useMutation({
    ...deleteKookConnectionMutation(),
    onSuccess: () => toast.success("已清除数据库中的 Token"),
  });
  const sync = useMutation({
    ...syncKookStructureMutation(),
    onSuccess: (r) => toast.success(`已同步 ${r.guilds} 个服务器、${r.categories} 个分组、${r.channels} 个文字频道`),
  });

  const d = conn.data;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (token.trim()) save.mutate({ body: { bot_token: token.trim() } });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>KOOK 机器人连接</CardTitle>
        <CardDescription>
          直连 KOOK 开放平台 WebSocket 网关与 HTTP API v3（无需第三方适配器）。修改 Bot Token 后自动热重连。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <QueryState isLoading={conn.isLoading} error={conn.error} rows={2} />
        {d && (
          <>
            <div className="grid gap-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                {d.connected ? (
                  <Badge>已连接</Badge>
                ) : d.configured ? (
                  <Badge variant="destructive">未连接</Badge>
                ) : (
                  <Badge variant="outline">未配置</Badge>
                )}
                <span className="text-xs text-muted-foreground">
                  Token 来源：
                  {d.source === "db" ? "控制台数据库" : d.source === "env" ? "环境变量 YGG_KOOK_BOT_TOKEN" : "无"}
                  {d.token_preview && <span className="ml-2 font-mono">{d.token_preview}</span>}
                </span>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
                {d.bot_id && (
                  <>
                    <dt className="text-muted-foreground">机器人</dt>
                    <dd>
                      {d.bot_name || "Bot"} <span className="font-mono text-xs text-muted-foreground">({d.bot_id})</span>
                    </dd>
                  </>
                )}
                <dt className="text-muted-foreground">已同步</dt>
                <dd>
                  {d.guild_count} 个服务器 · {d.enabled_channels} 个已启用频道
                </dd>
                {d.connected_at && (
                  <>
                    <dt className="text-muted-foreground">上线时间</dt>
                    <dd>
                      <time dateTime={d.connected_at}>{fmtTime(d.connected_at)}</time>
                    </dd>
                  </>
                )}
                {d.last_heartbeat && (
                  <>
                    <dt className="text-muted-foreground">最近心跳</dt>
                    <dd>
                      <time dateTime={d.last_heartbeat}>{fmtTime(d.last_heartbeat)}</time>
                    </dd>
                  </>
                )}
              </dl>
            </div>
            <form onSubmit={submit} className="grid gap-3">
              <Field
                label="Bot Token"
                hint="在 developer.kookapp.cn 机器人页获取；保存时加密写入数据库，优先级高于环境变量。"
              >
                <Input
                  type="password"
                  autoComplete="off"
                  placeholder={d.configured ? "输入新的 Token 以覆盖" : "粘贴 KOOK Bot Token"}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                <PendingButton type="submit" pending={save.isPending} disabled={!token.trim()}>
                  保存并连接
                </PendingButton>
                <PendingButton
                  type="button"
                  variant="outline"
                  pending={sync.isPending}
                  disabled={!d.connected}
                  onClick={() => sync.mutate({})}
                >
                  {!sync.isPending && <RefreshCwIcon data-icon="inline-start" />}
                  从 KOOK 同步服务器与频道
                </PendingButton>
                {d.source === "db" && (
                  <ConfirmButton
                    size="default"
                    title="清除数据库中的 Bot Token？"
                    description="清除后若未配置环境变量 YGG_KOOK_BOT_TOKEN，机器人将断开连接。"
                    disabled={clear.isPending}
                    onConfirm={() => clear.mutate({})}
                  >
                    清除 Token
                  </ConfirmButton>
                )}
              </div>
            </form>
          </>
        )}
      </CardContent>
    </Card>
  );
}
