import type { Evidence, Result } from "./v2-api";
import { safeUrl } from "./v2-api";
import s from "./v2.module.css";

export default function SourceSummary({ result }: { result?: Result }) {
  if (!result) return null;
  const sources = [
    ...new Map(
      [...(result.search_sources || []), ...result.sources].map((source) => [
        source.id,
        source,
      ]),
    ).values(),
  ];
  if (!sources.length) return null;
  const groups = new Map<string, Evidence[]>();
  for (const source of sources) {
    const key =
      source.document_id || `${source.kind}:${source.url || source.title}`;
    groups.set(key, [...(groups.get(key) || []), source]);
  }
  return (
    <details className={s.turnSources} aria-label="本次回答依据">
      <summary title="查看报告与参考资料原文。">
        查看本次回答依据
        <span className={s.sourceChevron} aria-hidden="true">
          ›
        </span>
      </summary>
      <div className={s.sourceDrawer}>
        {[...groups].map(([key, pieces]) => {
          const source = pieces[0];
          const cited = pieces.some((piece) =>
            result.claims.some((claim) => claim.evidence_id === piece.id),
          );
          return (
            <article key={key} className={s.sourceEntry}>
              <div className={s.row}>
                <strong>
                  {safeUrl(source.url || "") ? (
                    <a
                      href={safeUrl(source.url)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {source.title}
                    </a>
                  ) : (
                    source.title
                  )}
                </strong>
                <span className={s.badge}>
                  {source.kind === "public"
                    ? "公共资料"
                    : source.kind === "calculated"
                      ? "记录对比"
                      : "个人资料"}
                </span>
                {result.kind !== "chat" && (
                  <small className={s.muted}>
                    {cited ? "回答依据" : "相关参考资料"}
                  </small>
                )}
              </div>
              {source.published_at && (
                <p className={s.muted}>发布日期：{source.published_at}</p>
              )}
              {source.collected_at && (
                <p className={s.muted}>
                  查询日期：{source.collected_at}
                  ，实际服务信息以医院最新公布为准。
                </p>
              )}
              {pieces.map((piece) => (
                <details key={piece.id} className={s.sourcePassage}>
                  <summary>{piece.location || "查看原文位置"}</summary>
                  <p className={s.reportText}>{piece.text}</p>
                  {result.claims
                    .filter((claim) => claim.evidence_id === piece.id)
                    .map((claim, i) => (
                      <blockquote key={i}>
                        <strong>回答引用原文</strong>
                        <p>{claim.quote}</p>
                      </blockquote>
                    ))}
                </details>
              ))}
            </article>
          );
        })}
        {!sources.length && (
          <p className={s.muted}>
            本轮没有检索到匹配的资料，不表示已有本地依据支持回答。
          </p>
        )}
      </div>
    </details>
  );
}
