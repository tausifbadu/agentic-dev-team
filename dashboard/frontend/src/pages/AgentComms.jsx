import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import { AGENT_LABELS, formatTime, eventColor } from "../lib/eventStyles";
import {
  PageHeader,
  Card,
  CardHeader,
  Badge,
  EmptyState,
  Spinner,
} from "../components/ui";

function StatCard({ label, value, accent = "text-fg" }) {
  return (
    <Card padded={false} className="p-4">
      <p className="text-[11px] text-fg-faint uppercase tracking-wider mb-1">
        {label}
      </p>
      <p className={`text-2xl font-bold ${accent}`}>{value}</p>
    </Card>
  );
}

function TimelineEvent({ event, isLast }) {
  const colorClass = eventColor(event.event_type);
  const [expanded, setExpanded] = useState(false);
  let payload = null;
  try {
    payload = JSON.parse(event.payload_json || "{}");
  } catch {
    payload = {};
  }
  const hasPayload = Object.keys(payload).length > 0;

  return (
    <div className="relative flex gap-4 pb-6 animate-fade-in">
      <div className="flex flex-col items-center">
        <div className="w-3 h-3 rounded-full bg-accent border-2 border-surface-1 z-10 mt-1.5" />
        {!isLast && (
          <div className="w-px flex-1 bg-border" />
        )}
      </div>
      <div className="flex-1 min-w-0 pb-1">
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <span className="text-[11px] text-fg-faint font-mono">
            {formatTime(event.created_at)}
          </span>
          <Badge className={colorClass}>{event.event_type.replace(/_/g, " ")}</Badge>
          {event.cycle_number > 0 && (
            <span className="text-[11px] text-fg-faint">
              cycle {event.cycle_number}
            </span>
          )}
        </div>
        <p className="text-sm text-fg-secondary mb-1">
          <span className="text-fg-faint">
            {AGENT_LABELS[event.from_agent] || event.from_agent}
          </span>
          {" → "}
          <span className="text-fg-faint">
            {AGENT_LABELS[event.to_agent] || event.to_agent}
          </span>
        </p>
        <p className="text-sm text-fg">{event.summary}</p>
        {hasPayload && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[11px] text-accent hover:text-accent-hover mt-1 transition-colors duration-150 cursor-pointer"
          >
            {expanded ? "Hide details" : "Show details"}
          </button>
        )}
        {expanded && hasPayload && (
          <pre className="mt-2 p-3 bg-surface-3 rounded-lg text-xs text-fg-muted overflow-x-auto max-h-64 border border-border animate-scale-in origin-top">
            {JSON.stringify(payload, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

function ContractViewer({ contract }) {
  if (!contract || (!contract.routes?.length && !contract.models?.length)) {
    return (
      <EmptyState
        title="No API contract published yet."
        hint="Run the backend agent to generate one."
      />
    );
  }

  return (
    <div className="space-y-6">
      {contract.routes?.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-fg-secondary mb-3">
            Routes ({contract.routes.length})
          </h4>
          <div className="space-y-1">
            {contract.routes.map((r, i) => (
              <div
                key={i}
                className="flex items-center gap-3 px-3 py-2 bg-surface-3 rounded-lg border border-border"
              >
                <Badge
                  className={
                    r.method === "GET"
                      ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                      : r.method === "POST"
                      ? "bg-blue-500/20 text-blue-400 border-blue-500/30"
                      : r.method === "PUT"
                      ? "bg-amber-500/20 text-amber-400 border-amber-500/30"
                      : r.method === "DELETE"
                      ? "bg-red-500/20 text-red-400 border-red-500/30"
                      : "bg-slate-500/20 text-slate-400 border-slate-500/30"
                  }
                >
                  {r.method}
                </Badge>
                <span className="text-sm text-fg font-mono">{r.path}</span>
                {r.response_model && (
                  <span className="text-xs text-fg-faint ml-auto">
                    → {r.response_model}
                  </span>
                )}
                <span className="text-[11px] text-fg-faint">{r.file}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {contract.models?.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-fg-secondary mb-3">
            Models ({contract.models.length})
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {contract.models.map((m, i) => (
              <div
                key={i}
                className="p-3 bg-surface-3 rounded-lg border border-border"
              >
                <p className="text-sm font-medium text-cyan-400 mb-2">
                  {m.name}
                </p>
                <div className="space-y-0.5">
                  {Object.entries(m.fields || {}).map(([fname, ftype]) => (
                    <div
                      key={fname}
                      className="flex items-center gap-2 text-xs"
                    >
                      <span className="text-fg-muted">{fname}:</span>
                      <span className="text-fg-faint font-mono">{ftype}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ConversationsView({ events }) {
  // Group events with a non-empty correlation_id together so request → reply
  // pairs read as dialogs. Events without a correlation_id are excluded
  // (they're shown in the main Timeline tab).
  const groups = {};
  for (const ev of events) {
    const cid = ev.correlation_id;
    if (!cid) continue;
    if (!groups[cid]) groups[cid] = [];
    groups[cid].push(ev);
  }

  const conversations = Object.entries(groups)
    .map(([cid, msgs]) => ({
      cid,
      msgs: msgs.sort((a, b) => (a.created_at < b.created_at ? -1 : 1)),
      first: msgs[0],
      last: msgs[msgs.length - 1],
    }))
    .sort((a, b) => (a.last.created_at < b.last.created_at ? 1 : -1));

  if (!conversations.length) {
    return (
      <EmptyState
        title="No agent-to-agent conversations yet."
        hint={
          <>
            These appear when an agent calls
            <code className="text-accent mx-1">ask_pm</code>,
            <code className="text-accent mx-1">query_agent</code>, or
            <code className="text-accent mx-1">request_review</code>.
          </>
        }
      />
    );
  }

  return (
    <div className="space-y-4">
      {conversations.map((conv) => {
        const opener = conv.first;
        const ended = conv.msgs.some((m) =>
          m.event_type.endsWith("_reply") || m.event_type.endsWith("_timeout")
        );
        const status = conv.msgs.some((m) => m.event_type.endsWith("_timeout"))
          ? { label: "no reply", cls: "bg-red-500/20 text-red-400 border-red-500/30" }
          : ended
          ? { label: "answered", cls: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30" }
          : { label: "waiting", cls: "bg-amber-500/20 text-amber-400 border-amber-500/30" };

        return (
          <div
            key={conv.cid}
            className="bg-surface-2 border border-border rounded-lg overflow-hidden"
          >
            <div className="px-4 py-2 bg-surface-3 border-b border-border flex items-center gap-3 flex-wrap">
              <span className="text-[11px] text-fg-faint font-mono">
                {formatTime(opener.created_at)}
              </span>
              <Badge className={status.cls}>{status.label}</Badge>
              <span className="text-sm text-fg-secondary">
                <span className="text-fg-faint">
                  {AGENT_LABELS[opener.from_agent] || opener.from_agent}
                </span>
                {" ↔ "}
                <span className="text-fg-faint">
                  {AGENT_LABELS[opener.to_agent] || opener.to_agent}
                </span>
              </span>
              <span className="text-[11px] text-fg-faint ml-auto font-mono">
                cid {conv.cid.slice(0, 8)}
              </span>
            </div>
            <div className="divide-y divide-border/50">
              {conv.msgs.map((m) => {
                const isReply =
                  m.event_type.endsWith("_reply") ||
                  m.event_type.endsWith("_timeout");
                const colorClass = eventColor(m.event_type);
                let payload = {};
                try {
                  payload = JSON.parse(m.payload_json || "{}");
                } catch {
                  payload = {};
                }
                return (
                  <div
                    key={m.id}
                    className={`flex gap-3 px-4 py-3 ${
                      isReply ? "bg-surface-1/30" : ""
                    }`}
                  >
                    <div className="text-[11px] text-fg-faint font-mono w-20 shrink-0">
                      {formatTime(m.created_at)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <Badge className={colorClass}>
                          {m.event_type.replace(/_/g, " ")}
                        </Badge>
                        <span className="text-xs text-fg-muted">
                          {AGENT_LABELS[m.from_agent] || m.from_agent}
                          {" → "}
                          {AGENT_LABELS[m.to_agent] || m.to_agent}
                        </span>
                      </div>
                      <p className="text-sm text-fg whitespace-pre-wrap break-words">
                        {payload.question ||
                          payload.answer ||
                          payload.content ||
                          payload.notes ||
                          payload.instructions ||
                          payload.fix_instructions ||
                          payload.reason ||
                          m.summary}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}


function ToolCallsView({ rows, summary }) {
  if (!rows?.length) {
    return (
      <EmptyState
        title="No tool calls recorded yet."
        hint={
          <>
            Tool calls appear when the agentic runtime is active (set{" "}
            <code className="text-accent">USE_LEGACY_PIPELINE</code> unset and
            run a storypack).
          </>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      {summary?.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-fg-secondary mb-3">
            Tool Usage Summary
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {summary.map((s, i) => (
              <div
                key={i}
                className="px-3 py-2 bg-surface-3 rounded-lg border border-border flex items-center gap-3"
              >
                <Badge className="bg-blue-500/20 text-blue-400 border-blue-500/30">
                  {AGENT_LABELS[s.agent_id] || s.agent_id}
                </Badge>
                <span className="text-sm text-fg font-mono">
                  {s.tool_name}
                </span>
                <span className="ml-auto text-xs text-fg-muted">
                  {s.count} call{s.count === 1 ? "" : "s"} · {s.ok_count} ok ·{" "}
                  {Math.round((s.total_ms || 0) / 1000)}s
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <h4 className="text-sm font-semibold text-fg-secondary mb-3">
          Recent Tool Calls ({rows.length})
        </h4>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
                  Time
                </th>
                <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
                  Agent
                </th>
                <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
                  Tool
                </th>
                <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
                  Status
                </th>
                <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
                  Result
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  className="border-b border-border/50 hover:bg-surface-3/50 transition-colors duration-150"
                >
                  <td className="py-2 px-3 text-fg-faint text-xs font-mono">
                    {formatTime(r.created_at)}
                  </td>
                  <td className="py-2 px-3 text-fg-secondary">
                    {AGENT_LABELS[r.agent_id] || r.agent_id}
                  </td>
                  <td className="py-2 px-3 text-fg font-mono text-xs">
                    {r.tool_name}
                  </td>
                  <td className="py-2 px-3">
                    {r.ok ? (
                      <Badge className="bg-emerald-500/20 text-emerald-400 border-emerald-500/30">
                        OK
                      </Badge>
                    ) : (
                      <Badge className="bg-red-500/20 text-red-400 border-red-500/30">
                        ERROR
                      </Badge>
                    )}
                  </td>
                  <td className="py-2 px-3 text-fg-muted max-w-md truncate text-xs">
                    {(r.result_excerpt || "").slice(0, 200)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}


function LearningTable({ patterns }) {
  if (!patterns?.length) {
    return (
      <EmptyState
        title="No failure patterns recorded yet."
        hint="Patterns are saved when auto-heal resolves issues."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
              Agent
            </th>
            <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
              Category
            </th>
            <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
              Root Cause
            </th>
            <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
              Resolution
            </th>
            <th className="text-left py-2 px-3 text-[11px] text-fg-faint uppercase tracking-wider">
              Story
            </th>
          </tr>
        </thead>
        <tbody>
          {patterns.map((p, i) => (
            <tr
              key={i}
              className="border-b border-border/50 hover:bg-surface-3/50 transition-colors duration-150"
            >
              <td className="py-2 px-3 text-fg-secondary">
                {AGENT_LABELS[p.agent_type] || p.agent_type}
              </td>
              <td className="py-2 px-3">
                <Badge className="bg-amber-500/20 text-amber-400 border-amber-500/30">
                  {p.error_category}
                </Badge>
              </td>
              <td className="py-2 px-3 text-fg-muted max-w-xs truncate">
                {p.root_cause}
              </td>
              <td className="py-2 px-3 text-fg-muted max-w-xs truncate">
                {p.resolution}
              </td>
              <td className="py-2 px-3 text-fg-faint text-xs">
                {p.story_title}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AgentComms() {
  const [tab, setTab] = useState("timeline");
  const [timeline, setTimeline] = useState([]);
  const [contract, setContract] = useState(null);
  const [patterns, setPatterns] = useState([]);
  const [toolCalls, setToolCalls] = useState([]);
  const [toolSummary, setToolSummary] = useState([]);
  const [packs, setPacks] = useState([]);
  const [selectedPack, setSelectedPack] = useState("");
  const [loading, setLoading] = useState(false);

  const loadPacks = useCallback(async () => {
    try {
      const data = await api.listStorypacks();
      setPacks(data);
      if (data.length > 0 && !selectedPack) {
        setSelectedPack(data[0].id);
      }
    } catch {
      /* ignore */
    }
  }, [selectedPack]);

  useEffect(() => {
    loadPacks();
  }, [loadPacks]);

  const loadTimeline = useCallback(async (showSpinner = false) => {
    if (!selectedPack) return;
    if (showSpinner) setLoading(true);
    try {
      const data = await api.getCommTimeline(selectedPack);
      setTimeline(data);
    } catch {
      setTimeline([]);
    }
    if (showSpinner) setLoading(false);
  }, [selectedPack]);

  const loadContract = useCallback(async () => {
    try {
      const data = await api.getContracts();
      setContract(data);
    } catch {
      setContract(null);
    }
  }, []);

  const loadPatterns = useCallback(async () => {
    try {
      const data = await api.getLearningPatterns();
      setPatterns(data);
    } catch {
      setPatterns([]);
    }
  }, []);

  const loadToolCalls = useCallback(async () => {
    try {
      const filters = selectedPack ? { storypack_id: selectedPack, limit: 200 } : { limit: 200 };
      const [rows, summary] = await Promise.all([
        api.getToolCalls(filters),
        api.getToolCallSummary(selectedPack ? { storypack_id: selectedPack } : {}),
      ]);
      setToolCalls(rows);
      setToolSummary(summary);
    } catch {
      setToolCalls([]);
      setToolSummary([]);
    }
  }, [selectedPack]);

  useEffect(() => {
    if (tab === "timeline" || tab === "conversations" || tab === "heal") {
      loadTimeline(true);
    } else if (tab === "contract") loadContract();
    else if (tab === "learning") loadPatterns();
    else if (tab === "tools") loadToolCalls();
  }, [tab, loadTimeline, loadContract, loadPatterns, loadToolCalls]);

  useEffect(() => {
    if (tab !== "timeline" && tab !== "conversations" && tab !== "heal") return;
    const interval = setInterval(() => loadTimeline(false), 5000);
    return () => clearInterval(interval);
  }, [tab, loadTimeline]);

  const healEvents = timeline.filter(
    (e) =>
      e.event_type.startsWith("heal_") ||
      e.event_type === "fix_instructions" ||
      e.event_type === "replan" ||
      e.event_type.startsWith("rescope_") ||
      e.event_type.startsWith("test_")
  );

  const dialogEventTypes = new Set([
    "question",
    "query",
    "review_request",
    "heal_request",
    "rescope_request",
    "triage_request",
  ]);
  const dialogCount = timeline.filter((e) =>
    dialogEventTypes.has(e.event_type)
  ).length;

  const stats = {
    total: timeline.length,
    heals: timeline.filter((e) => e.event_type === "heal_request").length,
    dialogs: dialogCount,
    contracts: timeline.filter((e) => e.event_type === "contract.published").length,
  };

  const tabs = [
    { id: "timeline", label: "Timeline" },
    { id: "conversations", label: "Conversations" },
    { id: "heal", label: "Heal Loop" },
    { id: "contract", label: "API Contract" },
    { id: "learning", label: "Cross-Run Learning" },
    { id: "tools", label: "Tool Calls" },
  ];

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Agent Communications"
        subtitle="Structured inter-agent communication log, API contracts, and cross-run learning patterns."
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Total Events" value={stats.total} />
        <StatCard
          label="Heal Requests"
          value={stats.heals}
          accent="text-amber-400"
        />
        <StatCard
          label="Agent Dialogs"
          value={stats.dialogs}
          accent="text-teal-400"
        />
        <StatCard
          label="Contracts Published"
          value={stats.contracts}
          accent="text-cyan-400"
        />
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex bg-surface-2 rounded-lg border border-border p-0.5">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-150 cursor-pointer ${
                tab === t.id
                  ? "bg-accent text-accent-fg"
                  : "text-fg-muted hover:text-fg-secondary"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {(tab === "timeline" ||
          tab === "heal" ||
          tab === "tools" ||
          tab === "conversations") && (
          <select
            value={selectedPack}
            onChange={(e) => setSelectedPack(e.target.value)}
            className="bg-surface-2 border border-border rounded-lg px-3 py-1.5 text-sm text-fg-secondary cursor-pointer transition-all duration-150 focus:outline-none focus:ring-1 focus:ring-accent"
          >
            {packs.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id} ({p.status})
              </option>
            ))}
            {!packs.length && <option value="">No storypacks</option>}
          </select>
        )}
      </div>

      <Card>
        {loading ? (
          <div className="flex justify-center py-12">
            <Spinner size="md" />
          </div>
        ) : tab === "timeline" ? (
          timeline.length === 0 ? (
            <EmptyState
              title="No communication events yet."
              hint="Submit a requirement and approve the storypack to see agent interactions."
            />
          ) : (
            <div>
              {timeline.map((event, i) => (
                <TimelineEvent
                  key={event.id}
                  event={event}
                  isLast={i === timeline.length - 1}
                />
              ))}
            </div>
          )
        ) : tab === "conversations" ? (
          <ConversationsView events={timeline} />
        ) : tab === "heal" ? (
          healEvents.length === 0 ? (
            <EmptyState
              title="No heal loop events yet."
              hint="These appear when the PM agent auto-heals failing stories."
            />
          ) : (
            <div>
              {healEvents.map((event, i) => (
                <TimelineEvent
                  key={event.id}
                  event={event}
                  isLast={i === healEvents.length - 1}
                />
              ))}
            </div>
          )
        ) : tab === "contract" ? (
          <ContractViewer contract={contract} />
        ) : tab === "learning" ? (
          <LearningTable patterns={patterns} />
        ) : tab === "tools" ? (
          <ToolCallsView rows={toolCalls} summary={toolSummary} />
        ) : null}
      </Card>
    </div>
  );
}
