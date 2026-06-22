import { useState, useEffect } from "react";
import { api } from "../api";
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Badge,
  Field,
  Label,
  Select,
  Textarea,
  EmptyState,
} from "../components/ui";

// Page-local categorical palette: enhancement lifecycle has statuses
// (running=amber, rolled_back=sky) that intentionally differ from the shared
// story/run STATUS_STYLES, so it stays local rather than using <StatusBadge>.
const STATUS_STYLES = {
  pending: "bg-slate-500/15 text-slate-400",
  running: "bg-amber-500/15 text-amber-400",
  success: "bg-emerald-500/15 text-emerald-400",
  failed: "bg-rose-500/15 text-rose-400",
  rolled_back: "bg-sky-500/15 text-sky-400",
};

export default function Enhancements() {
  const [agentType, setAgentType] = useState("frontend");
  const [projectId, setProjectId] = useState("default");
  const [projects, setProjects] = useState([]);
  const [description, setDescription] = useState("");
  const [context, setContext] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitResult, setSubmitResult] = useState(null);
  const [enhancements, setEnhancements] = useState([]);
  const [enhanceStatus, setEnhanceStatus] = useState(null);

  const loadEnhancements = () => {
    api.listEnhancements().then(setEnhancements).catch(console.error);
    api.getEnhanceStatus().then(setEnhanceStatus).catch(console.error);
  };

  useEffect(() => {
    loadEnhancements();
    api.listProjects().then((r) => setProjects(r.projects || [])).catch(console.error);
  }, []);

  useEffect(() => {
    if (!enhanceStatus?.running) return;
    const poll = setInterval(() => {
      loadEnhancements();
    }, 3000);
    return () => clearInterval(poll);
  }, [enhanceStatus?.running]);

  const handleSubmit = async () => {
    if (!description.trim()) return;
    setSubmitting(true);
    setSubmitResult(null);
    try {
      const res = await api.submitEnhancement({
        agent_type: agentType,
        description: description.trim(),
        context: context.trim(),
        project_id: projectId,
      });
      setSubmitResult({ ok: true, msg: `Enhancement submitted (${res.enhance_id})` });
      setDescription("");
      setContext("");
      loadEnhancements();
    } catch (err) {
      setSubmitResult({ ok: false, msg: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const running = enhanceStatus?.running;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Enhance"
        subtitle="Add features, update components, or extend functionality on the existing codebase"
      />

      {running && (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
          <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
          <span className="text-sm text-amber-400 font-medium">
            Enhancement running: {enhanceStatus.agent_type} agent — {enhanceStatus.phase}
          </span>
        </div>
      )}

      <Card padded={false} className="overflow-hidden">
        <CardHeader title="New Enhancement Request" />
        <div className="px-5 py-4 space-y-4">
          <p className="text-[11px] text-fg-faint -mt-1">
            Describe what you want to add or change — PM will create a story, then the agent implements it
          </p>
          <Field label="Project workspace" htmlFor="enh-project-select">
            <Select
              id="enh-project-select"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="w-full max-w-md"
            >
              {(projects.length ? projects : [{ id: "default" }]).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id === "default" ? "default (workspace/)" : p.id}
                </option>
              ))}
            </Select>
          </Field>
          <div>
            <Label>Target Agent</Label>
            <div className="flex gap-2">
              {[
                { value: "frontend", label: "Frontend" },
                { value: "backend", label: "Backend" },
                { value: "both", label: "Both" },
              ].map((t) => (
                <Button
                  key={t.value}
                  variant={agentType === t.value ? "primary" : "secondary"}
                  size="sm"
                  onClick={() => setAgentType(t.value)}
                >
                  {t.label}
                </Button>
              ))}
            </div>
            {agentType === "both" && (
              <p className="text-[11px] text-fg-faint mt-1.5">
                PM will create a backend + frontend story pair. Backend runs first, then frontend.
              </p>
            )}
          </div>

          <Field label="Enhancement Description" htmlFor="enh-description">
            <Textarea
              id="enh-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={4}
              className="w-full"
              placeholder='e.g. "Add a delete button to each customer card with confirmation dialog" or "Change the form layout to a two-column grid on desktop"'
            />
          </Field>

          <Field
            label="Additional Context (optional)"
            htmlFor="enh-context"
          >
            <Textarea
              id="enh-context"
              value={context}
              onChange={(e) => setContext(e.target.value)}
              rows={2}
              className="w-full text-xs"
              placeholder="Any error output, API details, or design reference to provide extra context..."
            />
          </Field>

          <div className="flex items-center gap-3">
            <Button
              size="sm"
              onClick={handleSubmit}
              loading={submitting}
              disabled={submitting || !description.trim() || running}
            >
              {submitting ? "Submitting..." : running ? "Enhancement Running..." : "Submit Enhancement"}
            </Button>
            {submitResult && (
              <span className={`text-xs ${submitResult.ok ? "text-emerald-400" : "text-rose-400"}`}>
                {submitResult.msg}
              </span>
            )}
          </div>
        </div>
      </Card>

      <Card padded={false} className="overflow-hidden">
        <CardHeader title={`Enhancement History (${enhancements.length})`} />
        {enhancements.length === 0 ? (
          <EmptyState
            icon={
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="mx-auto">
                <path d="M12 5v14M5 12h14" strokeLinecap="round" />
              </svg>
            }
            title="No enhancements submitted yet. Use the form above to add features or update components."
          />
        ) : (
          <div className="divide-y divide-border-subtle/50">
            {enhancements.map((enh) => (
              <EnhancementRow key={enh.id} enh={enh} onRefresh={loadEnhancements} />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function EnhancementRow({ enh, onRefresh }) {
  const [open, setOpen] = useState(false);
  const [rolling, setRolling] = useState(false);
  const [rollResult, setRollResult] = useState(null);
  let storyItems = [];
  try {
    const parsed = typeof enh.story_json === "string" ? JSON.parse(enh.story_json) : enh.story_json;
    if (Array.isArray(parsed)) {
      storyItems = parsed.filter((s) => s && Object.keys(s).length > 0);
    } else if (parsed && Object.keys(parsed).length > 0) {
      storyItems = [parsed];
    }
  } catch {
    storyItems = [];
  }

  const formatTime = (iso) => {
    if (!iso) return "";
    try {
      let s = iso;
      if (!s.includes("+") && !s.endsWith("Z")) s += "Z";
      const d = new Date(s);
      if (isNaN(d.getTime())) return iso.slice(0, 16).replace("T", " ");
      return d.toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch {
      return iso.slice(0, 16).replace("T", " ");
    }
  };

  return (
    <div className="border-b border-border-subtle/50 last:border-b-0">
      <div
        className="px-5 py-3 flex items-center gap-3 text-[13px] hover:bg-surface-2/40 cursor-pointer transition-colors duration-150"
        onClick={() => setOpen(!open)}
      >
        <span className="text-[11px] text-fg-faint whitespace-nowrap font-mono tabular-nums">
          {formatTime(enh.created_at)}
        </span>
        <Badge className={`border-transparent ${STATUS_STYLES[enh.status] || STATUS_STYLES.pending}`}>
          {enh.status}
        </Badge>
        <Badge
          className={`border-transparent ${
            enh.agent_type === "both"
              ? "bg-violet-500/15 text-violet-400"
              : "bg-blue-500/15 text-blue-400"
          }`}
        >
          {enh.agent_type}
        </Badge>
        <span className="text-xs text-fg-secondary flex-1 truncate">
          {enh.description?.slice(0, 80)}
          {enh.description?.length > 80 ? "..." : ""}
        </span>
        <svg
          width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5"
          className={`text-fg-faint transition-transform duration-150 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="5 3 9 7 5 11" />
        </svg>
      </div>
      {open && (
        <div className="px-5 pb-4 space-y-3">
          <div>
            <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Description</p>
            <pre className="p-3 bg-surface-0 rounded-lg text-xs text-fg-secondary font-mono overflow-x-auto whitespace-pre-wrap">
              {enh.description}
            </pre>
          </div>
          {enh.context && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Context</p>
              <pre className="p-3 bg-surface-0 rounded-lg text-xs text-fg-muted font-mono overflow-x-auto whitespace-pre-wrap">
                {enh.context}
              </pre>
            </div>
          )}
          {storyItems.length > 0 && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">
                PM-Generated {storyItems.length > 1 ? "Stories" : "Story"}
              </p>
              <div className="space-y-2">
                {storyItems.map((sd, idx) => (
                  <div key={idx} className="p-3 bg-surface-0 rounded-lg space-y-1.5">
                    <div className="flex items-center gap-2">
                      <Badge
                        className={`border-transparent ${
                          sd.ownership === "backend" ? "bg-amber-500/15 text-amber-400" : "bg-cyan-500/15 text-cyan-400"
                        }`}
                      >
                        {sd.ownership}
                      </Badge>
                      <p className="text-xs text-fg font-medium">{sd.title}</p>
                    </div>
                    {sd.description && (
                      <p className="text-xs text-fg-muted">{sd.description}</p>
                    )}
                    {sd.acceptance_criteria?.length > 0 && (
                      <div className="mt-1">
                        <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-0.5">Acceptance Criteria</p>
                        <ul className="list-disc list-inside text-xs text-fg-muted space-y-0.5">
                          {sd.acceptance_criteria.map((ac, i) => (
                            <li key={i}>{ac}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
          {enh.result_message && (
            <div>
              <p className="text-[10px] text-fg-faint uppercase tracking-wider font-medium mb-1">Result</p>
              <pre className={`p-3 bg-surface-0 rounded-lg text-xs font-mono overflow-x-auto whitespace-pre-wrap ${
                enh.status === "success" ? "text-emerald-400/80" : "text-rose-400/80"
              }`}>
                {enh.result_message?.slice(0, 2000)}
              </pre>
            </div>
          )}
          {enh.backup_path && enh.status !== "rolled_back" && enh.status !== "running" && enh.status !== "pending" && (
            <div className="flex items-center gap-3 pt-1">
              <Button
                variant="secondary"
                size="sm"
                onClick={async (e) => {
                  e.stopPropagation();
                  if (!confirm("Roll back workspace to the state before this enhancement? This cannot be undone.")) return;
                  setRolling(true);
                  setRollResult(null);
                  try {
                    await api.rollbackEnhancement(enh.id);
                    setRollResult({ ok: true, msg: "Workspace rolled back successfully." });
                    onRefresh?.();
                  } catch (err) {
                    setRollResult({ ok: false, msg: err.message });
                  } finally {
                    setRolling(false);
                  }
                }}
                loading={rolling}
                disabled={rolling}
                className="bg-sky-500/15 text-sky-400 hover:bg-sky-500/25 hover:text-sky-400 border-sky-500/20"
              >
                {rolling ? "Rolling back..." : "Rollback"}
              </Button>
              {rollResult && (
                <span className={`text-[11px] ${rollResult.ok ? "text-emerald-400" : "text-rose-400"}`}>
                  {rollResult.msg}
                </span>
              )}
              {!rollResult && (
                <span className="text-[10px] text-fg-faint">Restore workspace to pre-enhancement state</span>
              )}
            </div>
          )}
          {enh.status === "rolled_back" && (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sky-500/10 border border-sky-500/20">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-sky-400">
                <polyline points="2 8 6 4 6 12" />
                <path d="M6 8h6a3 3 0 010 6H10" />
              </svg>
              <span className="text-[11px] text-sky-400 font-medium">Workspace was rolled back to pre-enhancement state</span>
            </div>
          )}
          <div className="flex items-center gap-2 text-[10px] text-fg-faint font-mono">
            <span>ID: {enh.id}</span>
            {enh.completed_at && <span> | Completed: {formatTime(enh.completed_at)}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
