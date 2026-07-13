import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api";
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Badge,
  Dropdown,
  Input,
  Textarea,
  Select,
  EmptyState,
  Spinner,
} from "../components/ui";

// Layer meta: order + labels for the summary cards and tabs.
const LAYERS = [
  { key: "api", label: "API" },
  { key: "ui_component", label: "UI Components" },
  { key: "ui_flow", label: "UI Flows" },
  { key: "integration", label: "Integration" },
  { key: "directive", label: "Guidance" },
];

const KINDS = [
  "business_flow", "process_flow", "data_flow", "integration_flow",
  "edge_case", "negative_case", "ui", "api", "note",
];
const PRIORITIES = ["high", "medium", "low"];

function statusColor(status) {
  switch (status) {
    case "passed": return "bg-emerald-500/15 text-emerald-400 ring-emerald-500/25";
    case "failed": return "bg-rose-500/15 text-rose-400 ring-rose-500/25";
    case "covered": return "bg-sky-500/15 text-sky-400 ring-sky-500/25";
    case "uncovered": return "bg-slate-500/15 text-slate-400 ring-slate-500/25";
    default: return "bg-slate-500/15 text-slate-400 ring-slate-500/25";
  }
}

function StatusPill({ status }) {
  return (
    <span className={`inline-flex px-2 py-0.5 rounded text-[11px] font-semibold ring-1 ring-inset capitalize ${statusColor(status)}`}>
      {String(status || "—").replace(/_/g, " ")}
    </span>
  );
}

function priorityColor(p) {
  return p === "high" ? "bg-rose-500/15 text-rose-400"
    : p === "low" ? "bg-slate-500/15 text-slate-400"
    : "bg-amber-500/15 text-amber-400";
}

function timeAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function TestCoverage() {
  const [projects, setProjects] = useState(["default"]);
  const [projectId, setProjectId] = useState("default");
  const [coverage, setCoverage] = useState(null);
  const [runs, setRuns] = useState([]);
  const [tab, setTab] = useState("api");
  const [loading, setLoading] = useState(true);
  const [runStatus, setRunStatus] = useState({ running: false });
  const [error, setError] = useState("");
  const pollRef = useRef(null);

  useEffect(() => {
    api.listProjects()
      .then((ps) => setProjects((ps || []).map((p) => p.project_id || p.slug || p).filter(Boolean)))
      .catch(() => setProjects(["default"]));
  }, []);

  const load = useCallback(async () => {
    try {
      const [cov, rs] = await Promise.all([
        api.getTestCoverage(projectId),
        api.listTestRuns(projectId, 30),
      ]);
      setCoverage(cov);
      setRuns(rs || []);
      setError("");
    } catch (e) {
      setError(e.message || "Failed to load coverage");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // Poll the run status; refresh coverage when a run finishes.
  useEffect(() => {
    const tick = async () => {
      try {
        const s = await api.getTestRunStatus();
        setRunStatus((prev) => {
          if (prev.running && !s.running) load(); // just finished
          return s;
        });
      } catch { /* ignore */ }
    };
    tick();
    pollRef.current = setInterval(tick, 2500);
    return () => clearInterval(pollRef.current);
  }, [load]);

  const runTests = async () => {
    try {
      await api.runTests(projectId);
      setRunStatus({ running: true, project_id: projectId, phase: "testing" });
    } catch (e) {
      setError(e.message || "Failed to start test run");
    }
  };

  const totals = coverage?.totals || {};
  const layers = coverage?.layers || {};
  const gaps = coverage?.gaps || [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Test Coverage"
        subtitle="What's been tested for this workspace — by layer, with gaps and run history."
        actions={
          <div className="flex items-center gap-3">
            <Dropdown
              value={projectId}
              onChange={setProjectId}
              options={projects.map((p) => ({ value: p, label: p }))}
              size="sm"
            />
            <Button
              variant="primary"
              size="sm"
              onClick={runTests}
              loading={runStatus.running}
              disabled={runStatus.running}
              icon={<PlayIcon />}
            >
              {runStatus.running ? "Running…" : "Run tests"}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="text-sm text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg px-4 py-2">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-20"><Spinner size="lg" /></div>
      ) : (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {LAYERS.map((l) => {
              const t = totals[l.key] || { covered: 0, total: 0, passed: 0, failed: 0 };
              const pct = t.total ? Math.round((t.covered / t.total) * 100) : 0;
              return (
                <Card key={l.key} padded={false} className="p-4">
                  <div className="text-[11px] uppercase tracking-wider text-fg-faint font-medium">{l.label}</div>
                  <div className="mt-1.5 text-lg font-semibold text-fg">
                    {t.covered}/{t.total}
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-[11px]">
                    <span className="text-emerald-400">{t.passed}✓</span>
                    {t.failed > 0 && <span className="text-rose-400">{t.failed}✗</span>}
                    <span className="text-fg-faint ml-auto">{pct}%</span>
                  </div>
                  <div className="mt-2 h-1 rounded-full bg-surface-3 overflow-hidden">
                    <div className="h-full bg-accent rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
                  </div>
                </Card>
              );
            })}
          </div>

          {/* Gaps panel */}
          {gaps.length > 0 && (
            <Card padded={false} className="border-amber-500/30">
              <CardHeader
                title={`⚠ Gaps (${gaps.length}) — not yet covered`}
                className="text-amber-400"
              />
              <div className="px-5 py-3 flex flex-wrap gap-2">
                {gaps.slice(0, 40).map((g, i) => (
                  <Badge key={i} className="bg-amber-500/10 text-amber-300 border-amber-500/25">
                    <span className="opacity-60 mr-1">{g.layer}</span>{g.target}
                  </Badge>
                ))}
              </div>
            </Card>
          )}

          {/* Gap-closure trend */}
          <GapTrend trend={coverage?.trend || []} />

          {/* Layer tabs + tables */}
          <Card padded={false}>
            <div className="flex items-center gap-1 px-3 pt-3 border-b border-border-subtle overflow-x-auto">
              {LAYERS.map((l) => {
                const n = (layers[l.key] || []).length;
                return (
                  <button
                    key={l.key}
                    onClick={() => setTab(l.key)}
                    className={`px-3 py-2 text-[13px] font-medium rounded-t-lg whitespace-nowrap transition-colors cursor-pointer ${
                      tab === l.key
                        ? "text-accent-hover border-b-2 border-accent -mb-px"
                        : "text-fg-muted hover:text-fg-secondary"
                    }`}
                  >
                    {l.label} <span className="text-fg-faint">({n})</span>
                  </button>
                );
              })}
            </div>
            <div className="p-2">
              {tab === "directive" ? (
                <GuidanceTab projectId={projectId} onChange={load} />
              ) : (
                <CoverageTable rows={layers[tab] || []} />
              )}
            </div>
          </Card>

          {/* Seed editor */}
          <SeedPanel projectId={projectId} />

          {/* Run history */}
          <Card padded={false}>
            <CardHeader title="Run history" />
            {runs.length === 0 ? (
              <EmptyState title="No test runs recorded yet." hint="Run the suite or build the project to populate history." />
            ) : (
              <div className="divide-y divide-border-subtle">
                {runs.map((r) => <RunRow key={r.id} run={r} />)}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function GapTrend({ trend }) {
  if (!trend || trend.length < 2) return null;
  const max = Math.max(1, ...trend.map((t) => t.gaps));
  const first = trend[0].gaps;
  const last = trend[trend.length - 1].gaps;
  const delta = last - first;
  return (
    <Card padded={false}>
      <CardHeader
        title="Gap-closure trend"
        actions={
          <span className={`text-[12px] font-medium ${delta < 0 ? "text-emerald-400" : delta > 0 ? "text-rose-400" : "text-fg-muted"}`}>
            {delta < 0 ? `▼ ${Math.abs(delta)} closed` : delta > 0 ? `▲ ${delta} new` : "no change"} over {trend.length} runs
          </span>
        }
      />
      <div className="px-5 py-4 flex items-end gap-1.5 h-20">
        {trend.map((t, i) => {
          const h = Math.round((t.gaps / max) * 100);
          return (
            <div key={i} className="flex-1 flex flex-col items-center justify-end group" title={`${t.gaps} gaps · ${t.status}`}>
              <span className="text-[10px] text-fg-faint mb-0.5 opacity-0 group-hover:opacity-100 transition-opacity">{t.gaps}</span>
              <div
                className={`w-full rounded-sm transition-all duration-300 ${t.status === "failed" ? "bg-rose-500/50" : "bg-accent/50"} group-hover:bg-accent`}
                style={{ height: `${Math.max(4, h)}%` }}
              />
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function CoverageTable({ rows }) {
  if (!rows.length) {
    return <EmptyState title="Nothing recorded for this layer yet." />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-fg-faint">
            <th className="px-3 py-2 font-medium">Target</th>
            <th className="px-3 py-2 font-medium">Test file</th>
            <th className="px-3 py-2 font-medium">Last run</th>
            <th className="px-3 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={`border-t border-border-subtle ${r.is_gap ? "opacity-60" : ""}`}>
              <td className="px-3 py-2 text-fg-secondary font-mono text-[12px]">{r.target}</td>
              <td className="px-3 py-2 text-fg-muted font-mono text-[12px]">{r.test_file || "—"}</td>
              <td className="px-3 py-2 text-fg-faint text-[12px]">{timeAgo(r.last_run_at)}</td>
              <td className="px-3 py-2"><StatusPill status={r.is_gap ? "uncovered" : r.last_status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunRow({ run }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const totals = (() => { try { return JSON.parse(run.totals_json || "{}"); } catch { return {}; } })();
  const passed = Object.values(totals).reduce((a, t) => a + (t.passed || 0), 0);
  const failed = Object.values(totals).reduce((a, t) => a + (t.failed || 0), 0);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      try { setDetail(await api.getTestRun(run.id)); } catch { setDetail({ cases: [] }); }
    }
  };

  return (
    <div className="px-5 py-3">
      <button onClick={toggle} className="w-full flex items-center gap-3 text-left cursor-pointer">
        <StatusPill status={run.status} />
        <Badge className="bg-surface-3 text-fg-muted border-border">{run.trigger}</Badge>
        <span className="text-[12px] text-fg-faint">{timeAgo(run.finished_at || run.started_at)}</span>
        <span className="text-[12px] ml-auto">
          <span className="text-emerald-400">{passed}✓</span>
          {failed > 0 && <span className="text-rose-400 ml-2">{failed}✗</span>}
        </span>
        <span className="text-fg-faint text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && detail && (
        <div className="mt-3 space-y-1">
          {(detail.cases || []).length === 0 ? (
            <p className="text-xs text-fg-faint">No per-case detail recorded.</p>
          ) : (
            detail.cases.map((c, i) => (
              <div key={i} className="flex items-center gap-2 text-[12px] font-mono">
                <span className={c.status === "passed" ? "text-emerald-400" : c.status === "failed" ? "text-rose-400" : "text-fg-faint"}>
                  {c.status === "passed" ? "✓" : c.status === "failed" ? "✗" : "○"}
                </span>
                <span className="text-fg-faint">{c.layer}</span>
                <span className="text-fg-secondary truncate">{c.test_name}</span>
                {c.message && <span className="text-rose-400/70 truncate">— {c.message.slice(0, 80)}</span>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ---- Test Guidance tab: user-authored directives ----

function GuidanceTab({ projectId, onChange }) {
  const [directives, setDirectives] = useState([]);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState(blankForm());
  const [busy, setBusy] = useState(false);

  function blankForm() {
    return { title: "", kind: "business_flow", priority: "medium", steps: "", expected_outcome: "" };
  }

  const load = useCallback(() => {
    api.listDirectives(projectId).then(setDirectives).catch(() => setDirectives([]));
  }, [projectId]);
  useEffect(load, [load]);

  const save = async () => {
    if (!form.title.trim()) return;
    setBusy(true);
    try {
      await api.addDirective({
        project_id: projectId,
        title: form.title,
        kind: form.kind,
        priority: form.priority,
        steps: form.steps.split("\n").map((s) => s.trim()).filter(Boolean),
        expected_outcome: form.expected_outcome,
      });
      setForm(blankForm());
      setAdding(false);
      load();
      onChange?.();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id) => {
    await api.deleteDirective(id, projectId);
    load();
    onChange?.();
  };

  return (
    <div className="p-2">
      <div className="flex items-center justify-between mb-3">
        <p className="text-[12px] text-fg-muted">
          Flows / edge cases you want covered. The test agent tackles these first and reports their status.
        </p>
        <Button size="xs" variant="secondary" onClick={() => setAdding((a) => !a)}>
          {adding ? "Cancel" : "+ Add directive"}
        </Button>
      </div>

      {adding && (
        <div className="mb-4 p-4 rounded-lg bg-surface-2 border border-border space-y-3">
          <Input
            placeholder="Title (e.g. New customer → geocoded → appears on map)"
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            className="w-full"
          />
          <div className="flex gap-3">
            <Select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
              {KINDS.map((k) => <option key={k} value={k}>{k.replace(/_/g, " ")}</option>)}
            </Select>
            <Select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
              {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
            </Select>
          </div>
          <Textarea
            placeholder="Steps (one per line)"
            rows={3}
            value={form.steps}
            onChange={(e) => setForm({ ...form, steps: e.target.value })}
            className="w-full"
          />
          <Input
            placeholder="Expected outcome"
            value={form.expected_outcome}
            onChange={(e) => setForm({ ...form, expected_outcome: e.target.value })}
            className="w-full"
          />
          <div className="flex justify-end">
            <Button size="sm" onClick={save} loading={busy} disabled={!form.title.trim()}>Save directive</Button>
          </div>
        </div>
      )}

      {directives.length === 0 ? (
        <EmptyState title="No directives yet." hint="Add the business/process/data flows you want the agent to test first." />
      ) : (
        <div className="space-y-2">
          {directives.map((d) => {
            const steps = (() => { try { return JSON.parse(d.steps_json || "[]"); } catch { return []; } })();
            return (
              <div key={d.id} className="p-3 rounded-lg bg-surface-2 border border-border">
                <div className="flex items-center gap-2">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${priorityColor(d.priority)}`}>
                    {d.priority}
                  </span>
                  <Badge className="bg-surface-3 text-fg-muted border-border">{d.kind?.replace(/_/g, " ")}</Badge>
                  <span className="text-sm text-fg-secondary font-medium">{d.title}</span>
                  <StatusPill status={d.status} />
                  <button onClick={() => remove(d.id)} className="ml-auto text-fg-faint hover:text-rose-400 text-xs cursor-pointer">
                    Delete
                  </button>
                </div>
                {(steps.length > 0 || d.expected_outcome) && (
                  <div className="mt-2 text-[12px] text-fg-muted pl-1">
                    {steps.length > 0 && <div>steps: {steps.join(" → ")}</div>}
                    {d.expected_outcome && <div className="text-fg-faint">expect: {d.expected_outcome}</div>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---- Seed data editor ----

function SeedPanel({ projectId }) {
  const [seed, setSeed] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const load = useCallback(() => {
    api.getSeed(projectId).then((d) => setSeed(JSON.stringify(d || {}, null, 2))).catch(() => setSeed("{}"));
  }, [projectId]);
  useEffect(load, [load]);

  const save = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const parsed = JSON.parse(seed);
      await api.saveSeed(projectId, parsed);
      setMsg("Seed saved.");
    } catch (e) {
      // Surface contract-validation issues from the 400 body.
      let detail = e.message;
      try { const j = JSON.parse(e.message); detail = (j.issues || [j.message]).join("; "); } catch { /* keep */ }
      setErr(detail || "Invalid JSON or contract violation");
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const res = await api.regenerateSeed(projectId);
      setMsg(`Regenerated (${res.status}).`);
      load();
    } catch (e) {
      setErr(e.message || "Regenerate failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card padded={false}>
      <CardHeader
        title="Seed data"
        actions={
          <button onClick={() => setOpen((o) => !o)} className="text-xs text-fg-muted hover:text-fg cursor-pointer">
            {open ? "Hide" : "Edit"}
          </button>
        }
      />
      {open && (
        <div className="p-5 space-y-3">
          <p className="text-[12px] text-fg-muted">
            Contract-derived dummy data used to populate list/filter screens and demos. Edits are validated
            against the API contract's enum vocabulary on save.
          </p>
          <Textarea
            rows={12}
            value={seed}
            onChange={(e) => setSeed(e.target.value)}
            className="w-full font-mono text-[12px]"
            spellCheck={false}
          />
          {msg && <div className="text-[12px] text-emerald-400">{msg}</div>}
          {err && <div className="text-[12px] text-rose-400">{err}</div>}
          <div className="flex gap-2">
            <Button size="sm" onClick={save} loading={busy}>Save</Button>
            <Button size="sm" variant="secondary" onClick={regenerate} loading={busy}>
              Regenerate from contract
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

function PlayIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
      <path d="M4 3l9 5-9 5V3z" />
    </svg>
  );
}
