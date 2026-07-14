import { useState, useEffect, useRef } from "react";
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
  planning: "bg-indigo-500/15 text-indigo-400",
  pending_review: "bg-violet-500/15 text-violet-400",
  running: "bg-amber-500/15 text-amber-400",
  pending_promote: "bg-teal-500/15 text-teal-400",
  success: "bg-emerald-500/15 text-emerald-400",
  failed: "bg-rose-500/15 text-rose-400",
  rejected: "bg-slate-500/15 text-slate-400",
  discarded: "bg-slate-500/15 text-slate-400",
  rolled_back: "bg-sky-500/15 text-sky-400",
};

// Human-friendly status labels.
const STATUS_LABEL = {
  pending_review: "review",
  pending_promote: "diff review",
};

const DIFF_STATUS_STYLE = {
  added: "bg-emerald-500/15 text-emerald-400",
  modified: "bg-amber-500/15 text-amber-400",
  deleted: "bg-rose-500/15 text-rose-400",
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

  // Poll while anything is in-flight: the backend is running (planning/applying)
  // OR a row is still 'planning' locally (so we catch the -> pending_review flip).
  const busy = enhanceStatus?.running || enhancements.some((e) => e.status === "planning");
  useEffect(() => {
    if (!busy) return;
    const poll = setInterval(() => {
      loadEnhancements();
    }, 3000);
    return () => clearInterval(poll);
  }, [busy]);

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
      setSubmitResult({ ok: true, msg: `Plan requested (${res.enhance_id}) — review it below before it runs` });
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
            Describe what you want to add or change — PM drafts a plan you review and approve <span className="text-fg-muted">before</span> any code changes
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
  const needsReview = enh.status === "pending_review" || enh.status === "pending_promote";
  const [open, setOpen] = useState(needsReview);

  // Auto-open the moment a row *transitions* into a review state (e.g. planning
  // -> pending_review while you watch), but don't fight a manual collapse: the
  // effect only fires on an actual status change, not on every re-render.
  const prevStatus = useRef(enh.status);
  useEffect(() => {
    const changed = enh.status !== prevStatus.current;
    if (changed && (enh.status === "pending_review" || enh.status === "pending_promote")) {
      setOpen(true);
    }
    prevStatus.current = enh.status;
  }, [enh.status]);
  const [rolling, setRolling] = useState(false);
  const [rollResult, setRollResult] = useState(null);
  const [acting, setActing] = useState(null); // "approve" | "reject" | null
  const [actResult, setActResult] = useState(null);
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
          {STATUS_LABEL[enh.status] || enh.status}
        </Badge>
        {needsReview && !open && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-violet-400">
            <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
            action needed
          </span>
        )}
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
          {enh.status === "pending_review" && (
            <div className="flex flex-wrap items-center gap-3 pt-1 border-t border-border-subtle/50 mt-1">
              <span className="text-[11px] text-violet-400 font-medium">
                Review this plan — nothing has changed yet.
              </span>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  loading={acting === "approve"}
                  disabled={!!acting}
                  onClick={async (e) => {
                    e.stopPropagation();
                    setActing("approve");
                    setActResult(null);
                    try {
                      await api.approveEnhancement(enh.id);
                      setActResult({ ok: true, msg: "Approved — applying plan…" });
                      onRefresh?.();
                    } catch (err) {
                      setActResult({ ok: false, msg: err.message });
                    } finally {
                      setActing(null);
                    }
                  }}
                >
                  {acting === "approve" ? "Approving…" : "Approve & Run"}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  loading={acting === "reject"}
                  disabled={!!acting}
                  onClick={async (e) => {
                    e.stopPropagation();
                    if (!confirm("Reject this plan? It will not run.")) return;
                    setActing("reject");
                    setActResult(null);
                    try {
                      await api.rejectEnhancement(enh.id);
                      setActResult({ ok: true, msg: "Plan rejected." });
                      onRefresh?.();
                    } catch (err) {
                      setActResult({ ok: false, msg: err.message });
                    } finally {
                      setActing(null);
                    }
                  }}
                  className="bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 hover:text-rose-400 border-rose-500/20"
                >
                  {acting === "reject" ? "Rejecting…" : "Reject"}
                </Button>
              </div>
              {actResult && (
                <span className={`text-[11px] ${actResult.ok ? "text-emerald-400" : "text-rose-400"}`}>
                  {actResult.msg}
                </span>
              )}
            </div>
          )}
          {enh.status === "pending_promote" && (
            <DiffGate enh={enh} onRefresh={onRefresh} />
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
          {enh.backup_path && (enh.status === "success" || enh.status === "failed") && (
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

function DiffGate({ enh, onRefresh }) {
  const [diff, setDiff] = useState(null); // { files, note }
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState({}); // path -> bool
  const [acting, setActing] = useState(null); // "promote" | "discard"
  const [actResult, setActResult] = useState(null);

  useEffect(() => {
    let alive = true;
    api
      .getEnhancementDiff(enh.id)
      .then((d) => alive && setDiff(d))
      .catch((err) => alive && setDiff({ files: [], note: err.message }))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [enh.id]);

  const files = diff?.files || [];

  const doAction = async (kind) => {
    if (kind === "discard" && !confirm("Discard these staged changes? The workspace is untouched.")) return;
    setActing(kind);
    setActResult(null);
    try {
      if (kind === "promote") await api.promoteEnhancement(enh.id);
      else await api.discardEnhancement(enh.id);
      setActResult({ ok: true, msg: kind === "promote" ? "Promoted to workspace." : "Staged changes discarded." });
      onRefresh?.();
    } catch (err) {
      setActResult({ ok: false, msg: err.message });
    } finally {
      setActing(null);
    }
  };

  return (
    <div className="border-t border-border-subtle/50 mt-1 pt-3 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[10px] text-teal-400 uppercase tracking-wider font-medium">
          Staged changes — review before promoting {files.length ? `(${files.length} file${files.length > 1 ? "s" : ""})` : ""}
        </p>
      </div>

      {loading ? (
        <p className="text-xs text-fg-faint">Loading diff…</p>
      ) : files.length === 0 ? (
        <p className="text-xs text-fg-muted">
          {diff?.note || "No file changes detected in the staged copy."}
        </p>
      ) : (
        <div className="space-y-1.5">
          {files.map((f) => (
            <div key={f.path} className="rounded-lg bg-surface-0 overflow-hidden">
              <button
                type="button"
                onClick={() => setExpanded((e) => ({ ...e, [f.path]: !e[f.path] }))}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-2/40 transition-colors"
              >
                <Badge className={`border-transparent ${DIFF_STATUS_STYLE[f.status] || "bg-slate-500/15 text-slate-400"}`}>
                  {f.status}
                </Badge>
                <span className="text-xs font-mono text-fg-secondary flex-1 truncate">{f.path}</span>
                {(f.additions > 0 || f.deletions > 0) && (
                  <span className="text-[10px] font-mono tabular-nums">
                    <span className="text-emerald-400">+{f.additions}</span>{" "}
                    <span className="text-rose-400">-{f.deletions}</span>
                  </span>
                )}
                <svg
                  width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5"
                  className={`text-fg-faint transition-transform ${expanded[f.path] ? "rotate-90" : ""}`}
                >
                  <polyline points="5 3 9 7 5 11" />
                </svg>
              </button>
              {expanded[f.path] && (
                <pre className="px-3 pb-2 text-[11px] font-mono overflow-x-auto whitespace-pre leading-relaxed max-h-96">
                  {(f.diff || "").split("\n").map((ln, i) => (
                    <div
                      key={i}
                      className={
                        ln.startsWith("+") && !ln.startsWith("+++")
                          ? "text-emerald-400/90"
                          : ln.startsWith("-") && !ln.startsWith("---")
                          ? "text-rose-400/90"
                          : ln.startsWith("@@")
                          ? "text-sky-400/80"
                          : "text-fg-muted"
                      }
                    >
                      {ln || " "}
                    </div>
                  ))}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Button
          size="sm"
          loading={acting === "promote"}
          disabled={!!acting}
          onClick={(e) => {
            e.stopPropagation();
            doAction("promote");
          }}
        >
          {acting === "promote" ? "Promoting…" : "Promote to Workspace"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          loading={acting === "discard"}
          disabled={!!acting}
          onClick={(e) => {
            e.stopPropagation();
            doAction("discard");
          }}
          className="bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 hover:text-rose-400 border-rose-500/20"
        >
          {acting === "discard" ? "Discarding…" : "Discard"}
        </Button>
        <span className="text-[10px] text-fg-faint">Nothing has touched your workspace yet.</span>
        {actResult && (
          <span className={`text-[11px] ${actResult.ok ? "text-emerald-400" : "text-rose-400"}`}>
            {actResult.msg}
          </span>
        )}
      </div>
    </div>
  );
}
