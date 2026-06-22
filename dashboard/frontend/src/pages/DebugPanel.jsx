import { useState, useEffect } from "react";
import { api } from "../api";
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Badge,
  Label,
  Textarea,
  Select,
  Field,
  EmptyState,
} from "../components/ui";

// Fix-lifecycle status → badge classes. Page-local categorical palette: these
// outcome hues (running=amber, success=emerald) differ from the shared status
// semantics (running=blue, no `success` key), so they stay local.
const STATUS_STYLES = {
  pending: "bg-slate-500/15 text-slate-400 border-slate-500/25",
  running: "bg-amber-500/15 text-amber-400 border-amber-500/25",
  success: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  failed: "bg-rose-500/15 text-rose-400 border-rose-500/25",
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
      <PageHeader
        title="Debug Panel"
        subtitle="Review failures, paste errors, and re-run agents with fix context"
      />

      {fixStatus?.running && (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-status-warning/10 border border-status-warning/20">
          <div className="w-2 h-2 rounded-full bg-status-warning-fg animate-pulse" />
          <span className="text-sm text-status-warning-fg font-medium">
            Fix running: {fixStatus.agent_type} agent on story {fixStatus.story_id}
          </span>
        </div>
      )}

      <Card padded={false} className="overflow-hidden">
        <CardHeader title={`Failed Stories (${failedItems.length})`} />
        {failedItems.length === 0 ? (
          <EmptyState
            title="No failed stories."
            hint="Run an agent pipeline first, or submit a manual fix below."
          />
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
      </Card>

      <ManualFixForm
        storypacks={storypacks}
        selectedPack={selectedPack}
        onPackChange={setSelectedPack}
        onFixSubmitted={() => {
          api.getFixStatus().then(setFixStatus);
          api.listFixes().then(setFixes);
        }}
      />

      <Card padded={false} className="overflow-hidden">
        <CardHeader title={`Fix History (${fixes.length})`} />
        {fixes.length === 0 ? (
          <EmptyState title="No fix attempts yet." />
        ) : (
          <div className="divide-y divide-border-subtle/50">
            {fixes.map((f) => (
              <FixHistoryRow key={f.id} fix={f} />
            ))}
          </div>
        )}
      </Card>
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
          <Badge className="bg-rose-500/15 text-rose-400 border-rose-500/25">{agentType}</Badge>
          <span className="text-sm font-medium text-fg">{item.title}</span>
        </div>
        <span className="text-[11px] text-fg-faint font-mono">{item.storyId}</span>
      </div>

      <Field label="Error / Runtime Logs">
        <Textarea
          value={errorText}
          onChange={(e) => setErrorText(e.target.value)}
          rows={4}
          className="w-full px-3 py-2 text-xs font-mono"
          placeholder="Paste the error traceback, console output, or describe the bug..."
        />
      </Field>

      <Field label="Fix Instructions (optional)">
        <Textarea
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          rows={2}
          className="w-full px-3 py-2 text-xs"
          placeholder="e.g. 'Add email-validator to requirements.txt' or 'Split the concatenated files'"
        />
      </Field>

      <div className="flex items-center gap-3">
        <Button
          size="sm"
          onClick={handleSubmit}
          loading={submitting}
          disabled={submitting || !errorText.trim()}
        >
          {submitting ? "Submitting..." : "Submit Fix"}
        </Button>
        {result && (
          <span className={`text-xs ${result.ok ? "text-status-success-fg" : "text-status-danger-fg"}`}>
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
    <Card padded={false} className="overflow-hidden">
      <div className="px-5 py-3 border-b border-border-subtle">
        <h3 className="text-[13px] font-semibold text-fg-secondary">Manual Fix Request</h3>
        <p className="text-[11px] text-fg-faint mt-0.5">
          Target any story from any storypack with a custom error and instructions
        </p>
      </div>
      <div className="px-5 py-4 space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Storypack">
            <Select
              value={selectedPack || ""}
              onChange={(e) => { onPackChange(e.target.value); setStoryId(""); }}
              className="w-full px-3 py-2 text-xs"
            >
              <option value="">Select pack...</option>
              {storypacks.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id.slice(0, 8)} ({p.stories?.length || 0} stories)
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Story">
            <Select
              value={storyId}
              onChange={(e) => setStoryId(e.target.value)}
              className="w-full px-3 py-2 text-xs"
            >
              <option value="">Select story...</option>
              {stories.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title} ({s.ownership})
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Agent">
            <Select
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              className="w-full px-3 py-2 text-xs"
            >
              <option value="backend">Backend</option>
              <option value="frontend">Frontend</option>
              <option value="testing">Testing</option>
            </Select>
          </Field>
        </div>

        <Field label="Error / Runtime Logs">
          <Textarea
            value={errorText}
            onChange={(e) => setErrorText(e.target.value)}
            rows={4}
            className="w-full px-3 py-2 text-xs font-mono"
            placeholder="Paste the error traceback, console output, or describe the bug..."
          />
        </Field>

        <Field label="Fix Instructions (optional)">
          <Textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={2}
            className="w-full px-3 py-2 text-xs"
            placeholder="e.g. 'Add CORS middleware' or 'CSS not loading, import styles.css in main.jsx'"
          />
        </Field>

        <div className="flex items-center gap-3">
          <Button
            size="sm"
            onClick={handleSubmit}
            loading={submitting}
            disabled={submitting || !selectedPack || !storyId || !errorText.trim()}
          >
            {submitting ? "Submitting..." : "Submit Fix"}
          </Button>
          {result && (
            <span className={`text-xs ${result.ok ? "text-status-success-fg" : "text-status-danger-fg"}`}>
              {result.msg}
            </span>
          )}
        </div>
      </div>
    </Card>
  );
}

function FixHistoryRow({ fix }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="border-b border-border-subtle/50 last:border-b-0">
      <div
        className="px-5 py-2.5 flex items-center gap-3 text-[13px] hover:bg-surface-2/40 cursor-pointer transition-colors duration-150"
        onClick={() => setOpen(!open)}
      >
        <span className="text-[11px] text-fg-faint whitespace-nowrap font-mono tabular-nums">
          {fix.created_at?.slice(0, 16).replace("T", " ") || ""}
        </span>
        <Badge className={STATUS_STYLES[fix.status] || STATUS_STYLES.pending}>
          {fix.status}
        </Badge>
        <span className="text-xs text-fg-muted flex-1 truncate">
          {fix.agent_type} / {fix.story_id?.slice(0, 8)} / attempt #{fix.attempt_number}
        </span>
        <svg
          width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5"
          className={`text-fg-faint transition-transform duration-150 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="5 3 9 7 5 11" />
        </svg>
      </div>
      {open && (
        <div className="px-5 pb-3 space-y-2">
          {fix.error_text && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Error</p>
              <pre className="p-3 bg-surface-0 rounded-lg text-xs text-rose-400/80 font-mono overflow-x-auto max-h-[200px] overflow-y-auto whitespace-pre-wrap">
                {fix.error_text}
              </pre>
            </div>
          )}
          {fix.user_instructions && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Instructions</p>
              <pre className="p-3 bg-surface-0 rounded-lg text-xs text-fg-muted font-mono overflow-x-auto whitespace-pre-wrap">
                {fix.user_instructions}
              </pre>
            </div>
          )}
          {fix.result_message && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Result</p>
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
