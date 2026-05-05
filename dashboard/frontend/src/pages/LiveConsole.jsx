import { useEffect, useRef, useState, useCallback } from "react";

// EVENT_COLORS mirrors AgentComms.jsx so badges look consistent across pages.
const EVENT_COLORS = {
  story_assignment: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  build_result: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  heal_request: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  heal_request_reply: "bg-amber-500/10 text-amber-300 border-amber-500/20",
  heal_outcome: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  heal_handoff: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  fix_instructions: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  replan: "bg-fuchsia-500/20 text-fuchsia-400 border-fuchsia-500/30",
  rescope_request: "bg-red-500/20 text-red-400 border-red-500/30",
  rescope_request_reply: "bg-red-500/10 text-red-300 border-red-500/20",
  rescope_result: "bg-rose-500/20 text-rose-400 border-rose-500/30",
  contract_publish: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  triage_request: "bg-indigo-500/20 text-indigo-400 border-indigo-500/30",
  triage_request_reply: "bg-indigo-500/10 text-indigo-300 border-indigo-500/20",
  question: "bg-teal-500/20 text-teal-400 border-teal-500/30",
  question_reply: "bg-teal-500/10 text-teal-300 border-teal-500/20",
  query: "bg-teal-500/20 text-teal-400 border-teal-500/30",
  query_reply: "bg-teal-500/10 text-teal-300 border-teal-500/20",
  review_request: "bg-pink-500/20 text-pink-400 border-pink-500/30",
  review_request_reply: "bg-pink-500/10 text-pink-300 border-pink-500/20",
  message: "bg-slate-500/20 text-slate-300 border-slate-500/30",
  question_timeout: "bg-red-500/20 text-red-400 border-red-500/30",
  query_timeout: "bg-red-500/20 text-red-400 border-red-500/30",
  review_request_timeout: "bg-red-500/20 text-red-400 border-red-500/30",
  handler_error: "bg-red-500/20 text-red-400 border-red-500/30",
  "story.completed": "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  "story.failed": "bg-red-500/20 text-red-400 border-red-500/30",
  "story.assigned": "bg-blue-500/20 text-blue-400 border-blue-500/30",
  "run.started": "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  "run.completed": "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  "run.failed": "bg-red-500/20 text-red-400 border-red-500/30",
  "contract.published": "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  "smoke.completed": "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

const AGENT_LABELS = {
  orchestrator: "Orchestrator",
  supervisor: "Supervisor",
  pm: "PM",
  backend: "Backend",
  frontend: "Frontend",
  test: "Test",
  testing: "Test",
};

function formatTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function Badge({ text, className = "" }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium border ${className}`}
    >
      {text}
    </span>
  );
}

// Choose a progress-bar color based on the percentage of budget consumed.
function gaugeColor(pct) {
  if (pct >= 90) return "bg-red-500";
  if (pct >= 75) return "bg-amber-500";
  return "bg-emerald-500";
}

function Gauge({ label, used, max, suffix = "" }) {
  const safeMax = max || 1;
  const pct = Math.min(100, Math.round((used / safeMax) * 100));
  const remaining = Math.max(0, max - used);
  return (
    <div className="bg-surface-2 rounded-lg border border-border p-4">
      <div className="flex justify-between items-baseline mb-2">
        <span className="text-[11px] text-slate-500 uppercase tracking-wider">
          {label}
        </span>
        <span className="text-[11px] text-slate-500 font-mono">{pct}%</span>
      </div>
      <div className="text-2xl font-bold text-slate-100 mb-1">
        {Number(used || 0).toLocaleString()}
        <span className="text-slate-500 text-sm font-normal">
          {" / "}
          {Number(max || 0).toLocaleString()}
          {suffix}
        </span>
      </div>
      <div className="h-2 bg-surface-3 rounded-full overflow-hidden">
        <div
          className={`${gaugeColor(pct)} h-full transition-all duration-500`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-[11px] text-slate-500 mt-2">
        {remaining.toLocaleString()}
        {suffix} remaining
      </p>
    </div>
  );
}

function StatusPill({ running, runtime }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className={`w-2 h-2 rounded-full ${
          running ? "bg-emerald-400 animate-pulse" : "bg-slate-600"
        }`}
      />
      <span className="text-sm text-slate-300">
        {running ? "Running" : "Idle"}
      </span>
      {runtime && (
        <Badge
          text={runtime}
          className="bg-cyan-500/20 text-cyan-400 border-cyan-500/30"
        />
      )}
    </div>
  );
}

function ConnectionPill({ state }) {
  const cfg = {
    connecting: { label: "Connecting", cls: "bg-slate-500/20 text-slate-400 border-slate-500/30", dot: "bg-slate-500" },
    open: { label: "Live", cls: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30", dot: "bg-emerald-400 animate-pulse" },
    reconnecting: { label: "Reconnecting", cls: "bg-amber-500/20 text-amber-400 border-amber-500/30", dot: "bg-amber-500 animate-pulse" },
    paused: { label: "Paused", cls: "bg-slate-500/20 text-slate-300 border-slate-500/30", dot: "bg-slate-400" },
    error: { label: "Disconnected", cls: "bg-red-500/20 text-red-400 border-red-500/30", dot: "bg-red-500" },
  }[state] || { label: state, cls: "bg-slate-500/20", dot: "bg-slate-500" };

  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium border ${cfg.cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      {cfg.label}
    </span>
  );
}

function BreakerWarnings({ warnings }) {
  if (!warnings?.length) {
    return (
      <div className="px-3 py-2 text-[11px] text-slate-500 bg-surface-2 rounded-lg border border-border">
        All tools healthy
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {warnings.map((w, i) => (
        <div
          key={i}
          className="px-3 py-2 bg-red-500/5 border border-red-500/30 rounded-lg flex items-center gap-3"
        >
          <Badge
            text={`${w.consecutive_failures}x fail`}
            className="bg-red-500/20 text-red-400 border-red-500/30"
          />
          <span className="text-sm text-slate-200 font-mono">{w.tool_name}</span>
          <span className="text-xs text-slate-500">
            on {AGENT_LABELS[w.agent_id] || w.agent_id}
          </span>
        </div>
      ))}
    </div>
  );
}

function StreamRow({ row, isContinuation }) {
  const [expanded, setExpanded] = useState(false);
  const colorClass =
    EVENT_COLORS[row.event_type] ||
    "bg-slate-500/20 text-slate-400 border-slate-500/30";

  let payload = {};
  try {
    payload = JSON.parse(row.payload_json || "{}");
  } catch {
    payload = {};
  }
  const hasPayload = Object.keys(payload).length > 0;

  // Pull the most informative piece of text we can find.
  const headline =
    payload.question ||
    payload.answer ||
    payload.content ||
    payload.notes ||
    payload.instructions ||
    payload.fix_instructions ||
    payload.reason ||
    row.summary ||
    "";

  const fromLabel = AGENT_LABELS[row.from_agent] || row.from_agent;
  const toLabel = row.to_agent.startsWith("topic:")
    ? row.to_agent
    : AGENT_LABELS[row.to_agent] || row.to_agent;

  return (
    <div
      className={`flex gap-3 px-4 py-3 border-l-2 ${
        isContinuation
          ? "border-teal-500/40 bg-surface-2/40"
          : "border-transparent"
      } hover:bg-surface-2/60 transition-colors`}
    >
      <div className="text-[11px] text-slate-500 font-mono w-16 shrink-0 pt-0.5">
        {formatTime(row.created_at)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <Badge text={row.event_type.replace(/_/g, " ")} className={colorClass} />
          <span className="text-xs text-slate-400">
            <span className="text-slate-500">{fromLabel}</span>
            <span className="mx-1">{"\u2192"}</span>
            <span className="text-slate-500">{toLabel}</span>
          </span>
          {row.story_id && (
            <span className="text-[11px] text-slate-600 font-mono">
              {row.story_id}
            </span>
          )}
        </div>
        <p className="text-sm text-slate-200 whitespace-pre-wrap break-words">
          {headline}
        </p>
        {hasPayload && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[11px] text-accent hover:text-accent-hover mt-1 transition-colors"
          >
            {expanded ? "Hide details" : "Show details"}
          </button>
        )}
        {expanded && hasPayload && (
          <pre className="mt-2 p-3 bg-surface-3 rounded-lg text-xs text-slate-400 overflow-x-auto max-h-64 border border-border">
            {JSON.stringify(payload, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

const MAX_ROWS = 500;

export default function LiveConsole() {
  const [metrics, setMetrics] = useState(null);
  const [rows, setRows] = useState([]); // newest at the END
  const [conn, setConn] = useState("connecting");
  const [paused, setPaused] = useState(false);

  const lastSeenIdRef = useRef(0);
  const esRef = useRef(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  const userScrolledUpRef = useRef(false);
  const streamElRef = useRef(null);
  const pausedRef = useRef(false);

  // Keep pausedRef in sync so the EventSource handler closure sees the latest value.
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  const appendRow = useCallback((row) => {
    if (pausedRef.current) return;
    setRows((prev) => {
      // Skip duplicates by id (live events from the bus have id=0).
      if (row.id > 0 && prev.some((r) => r.id === row.id)) return prev;
      const next = [...prev, row];
      if (next.length > MAX_ROWS) next.splice(0, next.length - MAX_ROWS);
      return next;
    });
  }, []);

  const connect = useCallback(() => {
    if (esRef.current) {
      try {
        esRef.current.close();
      } catch {
        /* noop */
      }
      esRef.current = null;
    }

    setConn(reconnectAttemptsRef.current === 0 ? "connecting" : "reconnecting");
    const url = `/api/events/stream?since_id=${lastSeenIdRef.current}`;
    let es;
    try {
      es = new EventSource(url);
    } catch (err) {
      setConn("error");
      return;
    }
    esRef.current = es;

    es.onopen = () => {
      setConn("open");
      reconnectAttemptsRef.current = 0;
    };

    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "metrics") {
          setMetrics(msg.snapshot);
        } else if (msg.type === "comm" && msg.row) {
          if (msg.row.id && msg.row.id > lastSeenIdRef.current) {
            lastSeenIdRef.current = msg.row.id;
          }
          appendRow(msg.row);
        }
        // heartbeat is ignored.
      } catch {
        /* ignore malformed frames */
      }
    };

    es.onerror = () => {
      setConn("reconnecting");
      try {
        es.close();
      } catch {
        /* noop */
      }
      esRef.current = null;
      // Exponential backoff: 1s, 2s, 4s, max 10s.
      reconnectAttemptsRef.current += 1;
      const delay = Math.min(10000, 1000 * 2 ** (reconnectAttemptsRef.current - 1));
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = setTimeout(connect, delay);
    };
  }, [appendRow]);

  // Mount: open SSE. Pause when tab hidden, reopen when visible.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        if (esRef.current) {
          try {
            esRef.current.close();
          } catch {
            /* noop */
          }
          esRef.current = null;
        }
        setConn("paused");
      } else {
        connect();
      }
    };

    connect();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (esRef.current) {
        try {
          esRef.current.close();
        } catch {
          /* noop */
        }
      }
    };
  }, [connect]);

  // Auto-scroll only when the user is near the bottom (mirrors AgentMonitor).
  useEffect(() => {
    const el = streamElRef.current;
    if (!el) return;
    if (!userScrolledUpRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [rows]);

  const onStreamScroll = (e) => {
    const el = e.target;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledUpRef.current = distanceFromBottom > 80;
  };

  const exec = metrics?.execution_state || {};
  const budget = metrics?.budget || {};
  const breakers = metrics?.breaker_warnings || [];

  const activeAgentLabel = AGENT_LABELS[exec.current_agent] || exec.current_agent || "—";

  // Group consecutive rows that share a correlation_id so Q&A pairs look threaded.
  let prevCid = null;
  const renderedRows = rows.map((row) => {
    const cid = row.correlation_id || "";
    const isContinuation = cid !== "" && cid === prevCid;
    prevCid = cid || null;
    return { row, isContinuation };
  });

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-bold text-white">Live Agent Console</h2>
          <p className="text-sm text-slate-500 mt-1">
            Real-time agent interaction stream and runtime metrics, pushed via
            Server-Sent Events.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <ConnectionPill state={conn} />
          <button
            onClick={() => setPaused((p) => !p)}
            className={`px-3 py-1.5 rounded-md text-xs font-medium border transition-all ${
              paused
                ? "bg-amber-500/20 text-amber-400 border-amber-500/30 hover:bg-amber-500/30"
                : "bg-surface-2 text-slate-300 border-border hover:bg-surface-3"
            }`}
          >
            {paused ? "Resume" : "Pause"}
          </button>
        </div>
      </div>

      {/* Status row + budgets */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <p className="text-[11px] text-slate-500 uppercase tracking-wider mb-2">
            Status
          </p>
          <StatusPill running={!!exec.running} runtime={exec.runtime} />
          <div className="mt-3 space-y-1">
            <div className="text-[11px] text-slate-500">
              Phase:{" "}
              <span className="text-slate-300">{exec.phase || "—"}</span>
            </div>
            <div className="text-[11px] text-slate-500">
              Active:{" "}
              <span className="text-slate-300">{activeAgentLabel}</span>
            </div>
            <div className="text-[11px] text-slate-500">
              Story:{" "}
              <span className="text-slate-300 font-mono">
                {exec.current_story_id || "—"}
              </span>
            </div>
          </div>
        </div>

        <Gauge
          label="Tool Calls"
          used={budget.tool_calls_used || 0}
          max={budget.max_tool_calls || 0}
        />
        <Gauge
          label="Wall Clock"
          used={Math.round(budget.elapsed_seconds || 0)}
          max={Math.round(budget.max_wall_seconds || 0)}
          suffix="s"
        />

        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <p className="text-[11px] text-slate-500 uppercase tracking-wider mb-2">
            Breaker Warnings
          </p>
          <BreakerWarnings warnings={breakers} />
        </div>
      </div>

      {/* Per-agent activity (tool calls + bus messages so PM and other
          message-only agents stay visible even with zero tool invocations). */}
      {metrics?.by_agent?.length > 0 && (
        <div className="bg-surface-1 rounded-xl border border-border p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-slate-300">
              Per-Agent Activity
            </h3>
            <span className="text-[11px] text-slate-500">
              {metrics.total_tool_calls || 0} tool calls
              {" \u00b7 "}
              {metrics.total_messages || 0} bus messages
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {metrics.by_agent.map((a) => {
              const okRate =
                a.tool_calls > 0
                  ? Math.round((a.ok_calls / a.tool_calls) * 100)
                  : null;
              const msgs =
                (a.messages_sent || 0) + (a.messages_received || 0);
              const isIdle = a.tool_calls === 0 && msgs === 0;
              const label = AGENT_LABELS[a.agent_id] || a.agent_id;

              return (
                <div
                  key={a.agent_id}
                  className={`px-3 py-2 rounded-lg border transition-all ${
                    isIdle
                      ? "bg-surface-2/40 border-border opacity-60"
                      : "bg-surface-2 border-border"
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <Badge
                      text={label}
                      className="bg-blue-500/20 text-blue-400 border-blue-500/30"
                    />
                    {isIdle && (
                      <span className="text-[10px] text-slate-600 uppercase tracking-wider">
                        idle
                      </span>
                    )}
                  </div>
                  <div className="flex items-baseline gap-3">
                    <div>
                      <p className="text-lg font-bold text-slate-100 leading-none">
                        {a.tool_calls}
                      </p>
                      <p className="text-[10px] text-slate-500 uppercase tracking-wider mt-0.5">
                        tools
                      </p>
                    </div>
                    <div>
                      <p className="text-lg font-bold text-slate-100 leading-none">
                        {msgs}
                      </p>
                      <p className="text-[10px] text-slate-500 uppercase tracking-wider mt-0.5">
                        msgs
                      </p>
                    </div>
                  </div>
                  <p className="text-[11px] text-slate-500 mt-2">
                    {okRate !== null && (
                      <span className="text-emerald-400/80">{okRate}% ok</span>
                    )}
                    {okRate !== null && msgs > 0 && (
                      <span className="mx-1 text-slate-700">|</span>
                    )}
                    {msgs > 0 && (
                      <span>
                        {a.messages_sent || 0}{"\u2191"}{" "}
                        {a.messages_received || 0}{"\u2193"}
                      </span>
                    )}
                    {a.tool_calls > 0 && (
                      <>
                        {(okRate !== null || msgs > 0) && (
                          <span className="mx-1 text-slate-700">|</span>
                        )}
                        {Math.round((a.total_ms || 0) / 1000)}s
                      </>
                    )}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Live interaction stream */}
      <div className="bg-surface-1 rounded-xl border border-border overflow-hidden">
        <div className="px-4 py-2 bg-surface-2 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-300">
            Agent Interaction Stream
          </h3>
          <span className="text-[11px] text-slate-500">
            {rows.length} event{rows.length === 1 ? "" : "s"}
            {paused && " (paused)"}
          </span>
        </div>
        <div
          ref={streamElRef}
          onScroll={onStreamScroll}
          className="overflow-y-auto max-h-[640px] divide-y divide-border/40"
        >
          {rows.length === 0 ? (
            <div className="text-center py-12 text-slate-500 text-sm">
              Waiting for agent activity. Submit a requirement or run an
              enhancement to see live communication.
            </div>
          ) : (
            renderedRows.map(({ row, isContinuation }) => (
              <StreamRow
                key={row.id > 0 ? `db-${row.id}` : `live-${row.created_at}-${row.from_agent}-${row.event_type}`}
                row={row}
                isContinuation={isContinuation}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
