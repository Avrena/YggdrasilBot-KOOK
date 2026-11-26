import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { listEventsOptions } from "@/client/@tanstack/react-query.gen";
import {
  Empty,
  FilterBar,
  GuildSelect,
  OptionSelect,
  PageHeader,
  Pager,
  Pre,
  QueryState,
  useChannelName,
  useGuildName,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 50;

const KIND_LABELS: Record<string, string> = {
  "kook.self_joined_guild": "bot 加入服务器",
  "kook.self_exited_guild": "bot 退出/被移出服务器",
  "kook.updated_guild": "服务器信息更新",
  "kook.deleted_guild": "服务器被删除",
  "kook.added_channel": "新增频道",
  "kook.updated_channel": "频道更新",
  "kook.deleted_channel": "频道被删除",
  "kook.joined_guild": "成员加入服务器",
  "kook.exited_guild": "成员离开服务器",
  "kook.pinned_message": "消息置顶",
  "kook.unpinned_message": "取消置顶",
};

export function EventsPage() {
  const [kind, setKind] = useState<string | null>(null);
  const [guildId, setGuildId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const guildName = useGuildName();
  const channelName = useChannelName();
  const events = useQuery(
    listEventsOptions({
      query: { kind, guild_id: guildId, limit: LIMIT, offset },
    }),
  );
  const reset =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };

  return (
    <div>
      <PageHeader
        title="事件"
        description="KOOK 网关收到的服务器、频道与成员系统事件记录。保留天数与自动停用策略在设置的 events 一节。"
      />
      <FilterBar>
        <GuildSelect className="w-56" value={guildId} onChange={reset(setGuildId)} />
        <OptionSelect
          className="w-48"
          aria-label="类型"
          value={kind}
          onChange={reset(setKind)}
          noneLabel="全部类型"
          options={Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label }))}
        />
      </FilterBar>
      <QueryState isLoading={events.isLoading} error={events.error} />
      {events.data?.items.length === 0 && <Empty>没有符合条件的事件</Empty>}
      {!!events.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-20">编号</TableHead>
              <TableHead className="w-36">时间</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>服务器</TableHead>
              <TableHead>频道</TableHead>
              <TableHead>用户</TableHead>
              <TableHead>状态</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.data.items.map((e) => (
              <Fragment key={e.id}>
                <TableRow className="cursor-pointer" onClick={() => setOpen(open === e.id ? null : e.id)}>
                  <TableCell className="font-mono text-xs">
                    <Button
                      variant="ghost"
                      size="xs"
                      className="font-mono"
                      aria-expanded={open === e.id}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setOpen(open === e.id ? null : e.id);
                      }}
                    >
                      E{e.id}
                    </Button>
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(e.at)}</TableCell>
                  <TableCell>{KIND_LABELS[e.kind] ?? e.kind}</TableCell>
                  <TableCell className="text-xs">{e.guild_id ? guildName(e.guild_id) : "—"}</TableCell>
                  <TableCell className="text-xs">{e.channel_id ? channelName(e.channel_id) : "—"}</TableCell>
                  <TableCell className="font-mono text-xs">{e.user_id ?? "—"}</TableCell>
                  <TableCell>{e.status ? <Badge variant="outline">{e.status}</Badge> : "—"}</TableCell>
                </TableRow>
                {open === e.id && (
                  <TableRow>
                    <TableCell colSpan={7}>
                      <Pre value={e.data} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      )}
      {events.data && <Pager total={events.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
