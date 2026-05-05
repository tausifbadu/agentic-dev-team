import { useState, useEffect } from "react";
import { api } from "../api";

export default function TestResults() {
  const [results, setResults] = useState([]);

  useEffect(() => {
    const poll = setInterval(() => {
      api.getTestResults().then(setResults).catch(console.error);
    }, 5000);
    api.getTestResults().then(setResults).catch(console.error);
    return () => clearInterval(poll);
  }, []);

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-semibold text-white">Test Results</h2>
        <p className="text-sm text-slate-500 mt-1">Automated test outcomes across all suites</p>
      </div>

      {results.length === 0 ? (
        <div className="bg-surface-1 rounded-xl border border-border px-6 py-16 text-center">
          <div className="w-10 h-10 rounded-full bg-surface-3 mx-auto mb-3 flex items-center justify-center">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-slate-600">
              <polyline points="4 8 6.5 10.5 12 5" />
              <rect x="1.5" y="1.5" width="13" height="13" rx="2" />
            </svg>
          </div>
          <p className="text-sm text-slate-500">
            No test results yet. Tests run automatically after agents complete.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {results.map((r) => (
            <div
              key={r.id}
              className={`bg-surface-1 rounded-xl border p-5 transition-colors duration-150 ${
                r.passed
                  ? "border-emerald-500/20 hover:border-emerald-500/30"
                  : "border-rose-500/20 hover:border-rose-500/30"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div
                    className={`w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold ${
                      r.passed
                        ? "bg-emerald-500/15 text-emerald-400"
                        : "bg-rose-500/15 text-rose-400"
                    }`}
                  >
                    {r.passed ? "P" : "F"}
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-slate-200 capitalize">{r.test_type} tests</p>
                    <p className="text-xs text-slate-500 font-mono mt-0.5">{r.storypack_id}</p>
                  </div>
                </div>
                <span className="text-[11px] text-slate-600 font-mono tabular-nums">
                  {r.created_at?.slice(0, 19).replace("T", " ")}
                </span>
              </div>
              {r.output && (
                <details className="mt-4 group">
                  <summary className="text-xs text-slate-500 cursor-pointer hover:text-slate-300 transition-colors duration-150 select-none">
                    Show output
                  </summary>
                  <pre className="mt-3 p-4 bg-surface-0 rounded-lg border border-border-subtle text-xs text-slate-400 overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap font-mono leading-relaxed">
                    {r.output}
                  </pre>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
