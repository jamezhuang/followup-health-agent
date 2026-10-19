import type { History } from "./workspace-api";

const dateFormatter = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });

export function conversationHistory(items: History[], query: string, now = new Date()) {
  const today = dateFormatter.format(now);
  const yesterday = dateFormatter.format(new Date(now.getTime() - 86400000));
  const needle = query.trim().toLocaleLowerCase();
  const groups = new Map<string, History[]>();
  [...items]
    .filter(item => !needle || item.title.toLocaleLowerCase().includes(needle))
    .sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0))
    .forEach(item => {
      const date = new Date(item.updated_at);
      const day = Number.isNaN(date.getTime()) ? "" : dateFormatter.format(date);
      const label = day === today ? "今天" : day === yesterday ? "昨天" : "更早";
      const group = groups.get(label) ?? [];
      group.push(item);
      groups.set(label, group);
    });
  return [...groups].map(([label, conversations]) => ({ label, conversations }));
}
