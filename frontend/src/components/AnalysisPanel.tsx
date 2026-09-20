import type { ResultAnalysis } from "../types/agent";
import { ResultTable } from "./ResultTable";

export function AnalysisPanel({ analysis }: { analysis: ResultAnalysis }) {
  return (
    <section aria-label="结果分析" className="mt-4 space-y-4 border-t border-ink/10 pt-4 text-sm leading-6">
      <div className="flex flex-wrap items-center gap-2 text-ink/60">
        <span className="font-semibold text-moss">结果分析</span>
        <span>{analysis.status === "partial" ? "部分完成 · 仍有证据缺口" : "分析完成"}</span>
        <span>结论依据：{analysis.summary.evidence_ids.join("、")}</span>
      </div>
      {analysis.findings.length > 0 && (
        <ul className="list-disc space-y-2 pl-5">
          {analysis.findings.map((finding, index) => (
            <li key={index}>
              <span className="whitespace-pre-wrap">{finding.text}</span>
              <span className="ml-2 text-xs text-moss">依据 {finding.evidence_ids.join("、")}</span>
            </li>
          ))}
        </ul>
      )}
      {analysis.limitations.length > 0 && (
        <div className="border-l-2 border-ink/20 bg-ink/[0.03] px-3 py-2">
          <p className="font-medium">还不能确认的部分</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-ink/70">
            {analysis.limitations.map((item, index) => <li key={index}>{item}</li>)}
          </ul>
        </div>
      )}
      {analysis.next_steps.length > 0 && (
        <div>
          <p className="font-medium">建议继续核查</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-ink/70">
            {analysis.next_steps.map((item, index) => <li key={index}>{item}</li>)}
          </ul>
        </div>
      )}
      <details className="border border-ink/10 bg-white/40 px-3 py-2">
        <summary className="cursor-pointer text-moss">查看分析依据（{analysis.evidence.length} 次查询）</summary>
        {analysis.evidence.map((item) => (
          <div key={item.id} className="mt-4 border-t border-ink/10 pt-3">
            <p className="font-medium">{item.id} · {item.question}</p>
            {item.preview_truncated || item.possibly_limited ? (
              <p className="mt-1 text-xs text-ink/60">返回 {item.row_count} 行，以下仅为预览或可能受行数上限影响。</p>
            ) : null}
            <ResultTable data={item.data} />
            {item.sql && (
              <details className="mt-2 text-xs text-ink/60">
                <summary className="cursor-pointer">查看查询 SQL</summary>
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words">{item.sql}</pre>
              </details>
            )}
          </div>
        ))}
      </details>
    </section>
  );
}
