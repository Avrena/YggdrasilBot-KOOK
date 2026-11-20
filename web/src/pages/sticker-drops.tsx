import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { StickerDropOut } from "@/client";
import { listStickerDropsOptions } from "@/client/@tanstack/react-query.gen";
import { Empty, FilterBar, GroupSelect, Hint, OptionSelect, Pager, QueryState, useGroupName } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtTime } from "@/lib/format";

const LIMIT = 50;

type Outcome = StickerDropOut["outcome"];

const OUTCOMES: Record<Outcome, { label: string; hint: string; variant: "default" | "secondary" | "outline" | "destructive" }> = {
  sent: { label: "已发出", hint: "适合度够、骰子也过了，表情交给 bot 发到群里", variant: "default" },
  dice: { label: "骰子没过", hint: "适合度够，但最后按 chance 掷骰子没发", variant: "secondary" },
  unfit: { label: "不适合", hint: "模型判断这时发表情不合适（低于 min_fit）", variant: "outline" },
  refused: { label: "无效回答", hint: "模型拒答，或挑了不在候选里的表情", variant: "destructive" },
};

const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);

/** Every call to the sticker-drop decision model, newest first: sent or not, and why. */
export function StickerDrops() {
  const [groupId, setGroupId] = useState<number | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [offset, setOffset] = useState(0);
  const groupName = useGroupName();
  const list = useQuery(listStickerDropsOptions({ query: { group_id: groupId, outcome, limit: LIMIT, offset } }));
  return (
    <div>
      <FilterBar>
        <GroupSelect
          className="w-56"
          value={groupId}
          onChange={(v) => {
            setGroupId(v);
            setOffset(0);
          }}
        />
        <OptionSelect
          className="w-36"
          aria-label="结果"
          value={outcome}
          onChange={(v) => {
            setOutcome(v as Outcome | null);
            setOffset(0);
          }}
          noneLabel="全部结果"
          options={Object.entries(OUTCOMES).map(([value, o]) => ({ value, label: o.label }))}
        />
      </FilterBar>
      <QueryState isLoading={list.isLoading} error={list.error} />
      {list.data?.items.length === 0 && <Empty>还没有表情包判断记录</Empty>}
      {!!list.data?.items.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>时间</TableHead>
              <TableHead>群</TableHead>
              <TableHead>结果</TableHead>
              <TableHead>适合度</TableHead>
              <TableHead>模型挑的表情</TableHead>
              <TableHead>挑选置信度</TableHead>
              <TableHead>候选</TableHead>
              <TableHead>tokens</TableHead>
              <TableHead>花费</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.data.items.map((d) => {
              const o = OUTCOMES[d.outcome];
              return (
                <TableRow key={d.id}>
                  <TableCell className="text-xs whitespace-nowrap">{fmtTime(d.at)}</TableCell>
                  <TableCell className="max-w-36 truncate text-xs">{groupName(d.group_id)}</TableCell>
                  <TableCell>
                    <Hint content={o.hint}>
                      <Badge variant={o.variant}>{o.label}</Badge>
                    </Hint>
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{pct(d.fit)}</TableCell>
                  <TableCell className="max-w-sm">
                    {d.sticker_id == null ? (
                      <span className="text-xs text-muted-foreground">—</span>
                    ) : (
                      <div className="flex items-center gap-2">
                        <div className="flex size-12 shrink-0 items-center justify-center rounded-md bg-muted/40">
                          {d.sticker_has_image && (
                            <img
                              src={`/api/stickers/${d.sticker_id}/image`}
                              alt={d.sticker_description || `表情包 #${d.sticker_id}`}
                              loading="lazy"
                              className="max-h-12 max-w-12 object-contain"
                            />
                          )}
                        </div>
                        <Hint content={d.sticker_description || null}>
                          <span className="line-clamp-2 text-xs">{d.sticker_description || `#${d.sticker_id}`}</span>
                        </Hint>
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{pct(d.confidence)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{d.candidate_count}</TableCell>
                  <TableCell className="text-xs tabular-nums">{d.input_tokens}</TableCell>
                  <TableCell className="text-xs tabular-nums">{d.cost == null ? "—" : `$${d.cost.toFixed(5)}`}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
      {list.data && <Pager total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </div>
  );
}
