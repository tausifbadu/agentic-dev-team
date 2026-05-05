import { useState, useEffect } from "react";
import { api } from "../api";

const STATUS_STYLES = {
  pending: "bg-slate-500/15 text-slate-400",
  running: "bg-amber-500/15 text-amber-400",
  success: "bg-emerald-500/15 text-emerald-400",
  failed: "bg-rose-500/15 text-rose-400",
};

export default function DebugPanel() {
  const [status, setStatus] = useState(null);
  const [logs, setLogs] = useState([]);
  const [fixes, setFixes] = useState([]);
  const [fixStatus, setFixStatus] = useState(null);
  const [storypacks, setStorypacks] = useState([]);
  const [selectedPack, setSelectedPack] = useState(null);

  useEffect(() => {
    api.getAgentStatus().then(setStatus).catch(console.error);
    api.getAgentLogs(500).then(setLogs).catch(console.error);
    api.listStorypacks().then(setStorypacks).catch(console.error);
    api.listFixes().then(setFixes).catch(console.error);
  }, []);

  useEffect(() => {
    if (!fixStatus?.running) return;
    const poll = setInterval(() => {
      api.getFixStatus().then(setFixStatus).catch(console.error);
      api.listFixes().then(setFixes).catch(console.error);
      api.getAgentLogs(500).then(setLogs).catch(console.error);
    }, 2000);
    return () => clearInterval(poll);
  }, [fixStatus?.running]);

  const failedStoryIds = status?.failed_stories || [];
  const errorLogs = logs.filter(
    (l) => l.level === "error" && l.message.startsWith("Failed:")
  );

  const packMap = {};
  storypacks.forEach((p) => {
    (p.stories || []).forEach((s) => {
      packMap[s.id] = { packId: p.id, story: s };
    });
  });

  const failedItems = failedStoryIds.map((sid) => {
    const info = packMap[sid];
    const errLog = errorLogs.find((l) => l.story_id === sid);
    return {
      storyId: sid,
      storypackId: info?.packId || selectedPack || "",
      title: info?.story?.title || sid,
      ownership: info?.story?.ownership || "unknown",
      errorMessage: errLog?.message || "",
      errorDetail: errLog?.detail || "",
    };
  });

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-semibold text-white">Debug Panel</h2>
        <p className="text-sm text-slate-500 mt-1">
          Review failures, paste errors, and re-run agents with fix context
        </p>
      </div>

      {fixStatus?.running && (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
          <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
          <span className="text-sm text-amber-400 font-medium">
            Fix running: {fixStatus.agent_type} agent on story {fixStatus.story_id}
          </span>
        </div>
      )}

      <div className="bg-surface-1 rounded-xl border border-border overflow-hidden">
        <div className="px-5 py-3 border-b border-border-subtle">
          <h3 className="text-[13px] font-semibold text-slate-300">
            Failed Stories ({failedItems.length})
          </h3>
        </div>
        {failedItems.length === 0 ? (
          <div className="px-6 py-12 text-center">
            <p className="text-sm text-slate-600">
              No failed stories. Run an agent pipeline first, or submit a manual fix below.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border-subtle/50">
            {failedItems.map((item) => (
              <FailedStoryCard
                key={item.storyId}
                item={item}
                onFixSubmitted={() => {
                  api.getFixStatus().then(setFixStatus);
                  api.listFixes().then(setFixes);
                }}
              />
            ))}
          </div>
        )}
      </div>

      <ManualFixForm
        storypacks={storypacks}
        selectedPack={selectedPack}
        onPackChange={setSelectedPack}
        onFixSubmitted={() => {
          api.getFixStatus().then(setFixStatus);
          api.listFixes().then(setFixes);
        }}
      />

      <div className="bg-surface-1 rounded-xl border border-border overflow-hidden">
        <div className="px-5 py-3 border-b border-border-subtle">
          <h3 className="text-[13px] font-semibold text-slate-300">
            Fix History ({fixes.length})
          </h3>
        </div>
        {fixes.length === 0 ? (
          <div className="px-6 py-8 text-center">
            <p className="text-sm text-slate-600">No fix attempts yet.</p>
          </div>
        ) : (
          <div className="divide-y divide-border-subtle/50">
            {fixes.map((f) => (
              <FixHistoryRow key={f.id} fix={f} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FailedStoryCard({ item, onFixSubmitted }) {
  const [errorText, setErrorText] = useState(item.errorDetail || item.errorMessage);
  const [instructions, setInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);

  const agentType = item.ownership === "frontend" ? "frontend" : "backend";

  const handleSubmit = async () => {
    if (!errorText.trim()) return;
    setSubmitting(true);
    setResult(null);
    try {
      const res = await api.submitFix({
        storypack_id: item.storypackId,
        story_id: item.storyId,
        agent_type: agentType,
        error_text: errorText,
        user_instructions: instructions,
      });
      setResult({ ok: true, msg: `Fix submitted (${res.fix_id})` });
      onFixSubmitted?.();
    } catch (err) {
      setResult({ ok: false, msg: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="px-5 py-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex px-2 py-0.5 text-[10px] font-semibold rounded bg-rose-500/15 text-rose-400">
            {agentType}
          </span>
          <span className="text-sm font-medium text-slate-200">{item.title}</span>
        </div>
        <span className="text-[11px] text-slate-600 font-mono">{item.storyId}</span>
      </div>

      <div>
        <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
          Error / Runtime Logs
        </label>
        <textarea
          value={errorText}
          onChange={(e) => setErrorText(e.target.value)}
          rows={4}
          className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs font-mono text-slate-300 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-accent/50 resize-y"
          placeholder="Paste the error traceback, console output, or describe the bug..."
        />
      </div>

      <div>
        <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
          Fix Instructions (optional)
        </label>
        <textarea
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          rows={2}
          className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-accent/50 resize-y"
          placeholder="e.g. 'Add email-validator to requirements.txt' or 'Split the concatenated files'"
        />
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleSubmit}
          disabled={submitting || !errorText.trim()}
          className="px-4 py-1.5 bg-accent text-white text-xs font-semibold rounded-lg hover:bg-accent-hover transition-colors duration-150 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {submitting ? "Submitting..." : "Submit Fix"}
        </button>
        {result && (
          <span className={`text-xs ${result.ok ? "text-emerald-400" : "text-rose-400"}`}>
            {result.msg}
          </span>
        )}
      </div>
    </div>
  );
}

function ManualFixForm({ storypacks, selectedPack, onPackChange, onFixSubmitted }) {
  const [storyId, setStoryId] = useState("");
  const [agentType, setAgentType] = useState("backend");
  const [errorText, setErrorText] = useState("");
  const [instructions, setInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);

  const stories = storypacks.find((p) => p.id === selectedPack)?.stories || [];

  const handleSubmit = async () => {
    if (!selectedPack || !storyId || !errorText.trim()) return;
    setSubmitting(true);
    setResult(null);
    try {
      const res = await api.submitFix({
        storypack_id: selectedPack,
        story_id: storyId,
        agent_type: agentType,
        error_text: errorText,
        user_instructions: instructions,
      });
      setResult({ ok: true, msg: `Fix submitted (${res.fix_id})` });
      onFixSubmitted?.();
    } catch (err) {
      setResult({ ok: false, msg: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="bg-surface-1 rounded-xl border border-border overflow-hidden">
      <div className="px-5 py-3 border-b border-border-subtle">
        <h3 className="text-[13px] font-semibold text-slate-300">Manual Fix Request</h3>
        <p className="text-[11px] text-slate-600 mt-0.5">
          Target any story from any storypack with a custom error and instructions
        </p>
      </div>
      <div className="px-5 py-4 space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
              Storypack
            </label>
            <select
              value={selectedPack || ""}
              onChange={(e) => { onPackChange(e.target.value); setStoryId(""); }}
              className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs text-slate-300 focus:outline-none focus:ring-1 focus:ring-accent/50"
            >
              <option value="">Select pack...</option>
              {storypacks.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id.slice(0, 8)} ({p.stories?.length || 0} stories)
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
              Story
            </label>
            <select
              value={storyId}
              onChange={(e) => setStoryId(e.target.value)}
              className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs text-slate-300 focus:outline-none focus:ring-1 focus:ring-accent/50"
            >
              <option value="">Select story...</option>
              {stories.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title} ({s.ownership})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
              Agent
            </label>
            <select
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs text-slate-300 focus:outline-none focus:ring-1 focus:ring-accent/50"
            >
              <option value="backend">Backend</option>
              <option value="frontend">Frontend</option>
              <option value="testing">Testing</option>
            </select>
          </div>
        </div>

        <div>
          <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
            Error / Runtime Logs
          </label>
          <textarea
            value={errorText}
            onChange={(e) => setErrorText(e.target.value)}
            rows={4}
            className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs font-mono text-slate-300 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-accent/50 resize-y"
            placeholder="Paste the error traceback, console output, or describe the bug..."
          />
        </div>

        <div>
          <label className="block text-[11px] text-slate-500 uppercase tracking-wider font-medium mb-1.5">
            Fix Instructions (optional)
          </label>
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={2}
            className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-accent/50 resize-y"
            placeholder="e.g. 'Add CORS middleware' or 'CSS not loading, import styles.css in main.jsx'"
          />
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={handleSubmit}
            disabled={submitting || !selectedPack || !storyId || !errorText.trim()}
            className="px-4 py-1.5 bg-accent text-white text-xs font-semibold rounded-lg hover:bg-accent-hover transition-colors duration-150 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {submitting ? "Submitting..." : "Submit Fix"}
          </button>
          {result && (
            <span className={`text-xs ${result.ok ? "text-emerald-400" : "text-rose-400"}`}>
              {result.msg}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function FixHistoryRow({ fix }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="border-b border-border-subtle/50 last:border-b-0">
      <div
        className="px-5 py-2.5 flex items-center gap-3 text-[13px] hover:bg-surface-2/40 cursor-pointer transition-colors duration-100"
        onClick={() => setOpen(!open)}
      >
        <span className="text-[11px] text-slate-600 whitespace-nowrap font-mono tabular-nums">
          {fix.created_at?.slice(0, 16).replace("T", " ") || ""}
        </span>
        <span className={`inline-flex px-2 py-0.5 text-[10px] font-semibold rounded ${STATUS_STYLES[fix.status] || STATUS_STYLES.pending}`}>
          {fix.status}
        </span>
        <span className="text-xs text-slate-400 flex-1 truncate">
          {fix.agent_type} / {fix.story_id?.slice(0, 8)} / attempt #{fix.attempt_number}
        </span>
        <svg
          width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5"
          className={`text-slate-600 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="5 3 9 7 5 11" />
        </svg>
      </div>
      {open && (
        <div className="px-5 pb-3 space-y-2">
          {fix.error_text && (
            <div>
              <p className="text-[10px] text-slate-600 uppercase tracking-wider font-medium mb-1">Error</p>
              <pre className="p-3 bg-surface-0 rounded-lg text-xs text-rose-400/80 font-mono overflow-x-auto max-h-[200px] overflow-y-auto whitespace-pre-wrap">
                {fix.error_text}
              </pre>
            </div>
          )}
          {fix.user_instructions && (
            <div>
              <p className="text-[10px] text-slate-600 uppercase tracking-wider font-medium mb-1">Instructions</p>
              <pre className="p-3 bg-surface-0 rounded-lg text-xs text-slate-400 font-mono overflow-x-auto whitespace-pre-wrap">
                {fix.user_instructions}
              </pre>
            </div>
          )}
          {fix.result_message && (
            <div>
              <p className="text-[10px] text-slate-600 uppercase tracking-wider font-medium mb-1">Result</p>
              <pre className={`p-3 bg-surface-0 rounded-lg text-xs font-mono overflow-x-auto whitespace-pre-wrap ${
                fix.status === "success" ? "text-emerald-400/80" : "text-rose-400/80"
              }`}>
                {fix.result_message}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
