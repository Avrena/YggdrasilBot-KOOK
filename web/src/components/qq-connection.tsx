import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  deleteQqMilkyMutation,
  deleteQqMilkyTokenMutation,
  getQqMilkyOptions,
  putQqMilkyMutation,
} from "@/client/@tanstack/react-query.gen";
import type { MilkyLinkOut } from "@/client/types.gen";
import { ConfirmButton, Field, PendingButton, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fmtTime } from "@/lib/format";

/** The Milky endpoint (Yogurt) the bot dials out to: its address and token, and how the link is doing. */
export function QQConnectionCard() {
  const milky = useQuery({ ...getQqMilkyOptions(), refetchInterval: 5_000 });
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const stored = milky.data?.url ?? "";
  useEffect(() => setUrl(stored), [stored]);
  const save = useMutation({
    ...putQqMilkyMutation(),
    onSuccess: () => {
      setToken("");
      toast.success("已保存，bot 正在重新连接");
    },
  });
  const clearToken = useMutation({
    ...deleteQqMilkyTokenMutation(),
    onSuccess: () => toast.success("已清除 token，bot 正在重新连接"),
  });
  const remove = useMutation({ ...deleteQqMilkyMutation(), onSuccess: () => toast.success("已清除，bot 断开了 Milky") });

  const d = milky.data;
  const dirty = url.trim() !== stored || token.trim() !== "";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim() && dirty) save.mutate({ body: { url: url.trim(), token: token.trim() || null } });
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>QQ 连接</CardTitle>
        <CardDescription>
          bot 主动连到 Milky 协议端（Yogurt）；改地址或 token 会立即重连，清除后断开。SnowLuma（OneBot v11）仍然通过反向
          WebSocket 连进来，两边都连着时用 Milky。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <QueryState isLoading={milky.isLoading} error={milky.error} rows={2} />
        {d && (
          <>
            <LinkStatus link={d.link} />
            <form onSubmit={submit} className="grid gap-4">
              <Field label="地址" hint="Milky 的 HTTP 地址，只写主机和端口，例如 http://yogurt:3000">
                <Input
                  value={url}
                  placeholder="http://127.0.0.1:3000"
                  autoComplete="off"
                  onChange={(e) => setUrl(e.target.value)}
                />
              </Field>
              <Field
                label="Access token"
                hint={d.token_set ? "已设置；留空保持不变" : "未设置；Yogurt 配置了 milky.http.accessToken 时必填"}
              >
                <Input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
              </Field>
              <div className="flex flex-wrap gap-2">
                <PendingButton type="submit" pending={save.isPending} disabled={!url.trim() || !dirty}>
                  保存并连接
                </PendingButton>
                {d.token_set && (
                  <ConfirmButton
                    variant="outline"
                    size="default"
                    title="清除 access token？"
                    description="bot 会不带 token 重新连接；Yogurt 要求 token 时将连不上。"
                    disabled={clearToken.isPending}
                    onConfirm={() => clearToken.mutate({})}
                  >
                    清除 token
                  </ConfirmButton>
                )}
                {d.url && (
                  <ConfirmButton
                    size="default"
                    title="断开 Milky？"
                    description="清除地址和 token，bot 立即断开 Milky 连接。"
                    disabled={remove.isPending}
                    onConfirm={() => remove.mutate({})}
                  >
                    断开并清除
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

function LinkStatus({ link }: { link: MilkyLinkOut }) {
  if (!link.managed) {
    return <p className="text-sm text-muted-foreground">这个控制台进程没有运行 bot（独立模式），看不到连接状态。</p>;
  }
  if (!link.url) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Badge variant="outline">未配置</Badge>
        <span className="text-muted-foreground">没有 Milky 连接</span>
      </div>
    );
  }
  return (
    <div className="grid gap-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {link.connected ? <Badge>已连接</Badge> : <Badge variant="destructive">未连上</Badge>}
        {!link.connected && <span className="text-muted-foreground">{link.error ?? "正在连接…"}</span>}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="text-muted-foreground">地址</dt>
        <dd className="font-mono break-all">{link.url}</dd>
        {link.self_id != null && (
          <>
            <dt className="text-muted-foreground">QQ 号</dt>
            <dd className="font-mono">{link.self_id}</dd>
          </>
        )}
        {link.since && (
          <>
            <dt className="text-muted-foreground">状态变化于</dt>
            <dd>
              <time dateTime={link.since}>{fmtTime(link.since)}</time>
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
