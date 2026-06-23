import { useEffect, useRef, useState, useCallback } from "react";
import { AGENT_LABELS, formatTime, eventColor } from "../lib/eventStyles";
import { PageHeader, Card, CardHeader, Button, Badge, EmptyState } from "../components/ui";

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
        <span className="text-[11px] text-fg-faint uppercase tracking-wider">
          {label}
        </span>
        <span className="text-[11px] text-fg-faint font-mono">{pct}%</span>
      </div>
      <div className="text-2xl font-bold text-fg mb-1">
        {Number(used || 0).toLocaleString()}
        <span className="text-fg-faint text-sm font-normal">
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
      <p className="text-[11px] text-fg-faint mt-2">
        {remaining.toLocaleString()}
        {suffix} remaining
      </p>
    </div>
  );
}

// Compact token-usage tile. The bar shows the cache-hit rate (higher = more of
// the prompt served from cache = cheaper); the big number is total tokens for the
// current run. Fed by the metrics snapshot's `usage` block.
function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + "k";
  return String(n || 0);
}

function TokenStat({ usage }) {
  const total = usage?.total_tokens || 0;
  const hit = Math.min(100, Math.max(0, usage?.cache_hit_pct || 0));
  return (
    <div className="bg-surface-2 rounded-lg border border-border p-4">
      <div className="flex justify-between items-baseline mb-2">
        <span className="text-[11px] text-fg-faint uppercase tracking-wider">Tokens</span>
        <span className="text-[11px] text-fg-faint font-mono">{(usage?.calls || 0)} calls</span>
      </div>
      <div className="text-2xl font-bold text-fg mb-1">{fmtTokens(total)}</div>
      <div className="h-2 bg-surface-3 rounded-full overflow-hidden">
        <div
          className="bg-accent h-full transition-all duration-500"
          style={{ width: `${hit}%` }}
        />
      </div>
      <p className="text-[11px] text-fg-faint mt-2">{hit}% served from cache</p>
    </div>
  );
}

function StatusPill({ running, runtime }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className={`w-2 h-2 rounded-full ${
          running ? "bg-accent animate-pulse" : "bg-fg-faint"
        }`}
      />
      <span className="text-sm text-fg-secondary">
        {running ? "Running" : "Idle"}
      </span>
      {runtime && (
        <Badge className="bg-cyan-500/20 text-cyan-400 border-cyan-500/30">
          {runtime}
        </Badge>
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
      <div className="px-3 py-2 text-[11px] text-fg-faint bg-surface-2 rounded-lg border border-border">
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
          <Badge className="bg-red-500/20 text-red-400 border-red-500/30">
            {`${w.consecutive_failures}x fail`}
          </Badge>
          <span className="text-sm text-fg font-mono">{w.tool_name}</span>
          <span className="text-xs text-fg-faint">
            on {AGENT_LABELS[w.agent_id] || w.agent_id}
          </span>
        </div>
      ))}
    </div>
  );
}

function StreamRow({ row, isContinuation }) {
  const [expanded, setExpanded] = useState(false);
  const colorClass = eventColor(row.event_type);

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
      className={`animate-fade-in-up flex gap-3 px-4 py-3 border-l-2 ${
        isContinuation
          ? "border-teal-500/40 bg-surface-2/40"
          : "border-transparent"
      } hover:bg-surface-2/60 transition-colors duration-150`}
    >
      <div className="text-[11px] text-fg-faint font-mono w-16 shrink-0 pt-0.5">
        {formatTime(row.created_at)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <Badge className={colorClass}>{row.event_type.replace(/_/g, " ")}</Badge>
          <span className="text-xs text-fg-muted">
            <span className="text-fg-faint">{fromLabel}</span>
            <span className="mx-1">{"\u2192"}</span>
            <span className="text-fg-faint">{toLabel}</span>
          </span>
          {row.story_id && (
            <span className="text-[11px] text-fg-faint font-mono">
              {row.story_id}
            </span>
          )}
        </div>
        <p className="text-sm text-fg whitespace-pre-wrap break-words">
          {headline}
        </p>
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
  const usage = metrics?.usage || {};
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
      <PageHeader
        title="Live Agent Console"
        subtitle="Real-time agent interaction stream and runtime metrics, pushed via Server-Sent Events."
        className="flex-wrap"
        actions={
          <>
            <ConnectionPill state={conn} />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setPaused((p) => !p)}
              className={
                paused
                  ? "bg-amber-500/20 text-amber-400 border-amber-500/30 hover:bg-amber-500/30 hover:text-amber-400"
                  : ""
              }
            >
              {paused ? "Resume" : "Pause"}
            </Button>
          </>
        }
      />

      {/* Status row + budgets */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <p className="text-[11px] text-fg-faint uppercase tracking-wider mb-2">
            Status
          </p>
          <StatusPill running={!!exec.running} runtime={exec.runtime} />
          <div className="mt-3 space-y-1">
            <div className="text-[11px] text-fg-faint">
              Phase:{" "}
              <span className="text-fg-secondary">{exec.phase || "—"}</span>
            </div>
            <div className="text-[11px] text-fg-faint">
              Active:{" "}
              <span className="text-fg-secondary">{activeAgentLabel}</span>
            </div>
            <div className="text-[11px] text-fg-faint">
              Story:{" "}
              <span className="text-fg-secondary font-mono">
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
        <TokenStat usage={usage} />

        <div className="bg-surface-1 rounded-lg border border-border p-4">
          <p className="text-[11px] text-fg-faint uppercase tracking-wider mb-2">
            Breaker Warnings
          </p>
          <BreakerWarnings warnings={breakers} />
        </div>
      </div>

      {/* Per-agent activity (tool calls + bus messages so PM and other
          message-only agents stay visible even with zero tool invocations). */}
      {metrics?.by_agent?.length > 0 && (
        <Card padded={false} className="p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-fg-secondary">
              Per-Agent Activity
            </h3>
            <span className="text-[11px] text-fg-faint">
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
                  className={`px-3 py-2 rounded-lg border transition-all duration-150 ${
                    isIdle
                      ? "bg-surface-2/40 border-border opacity-60"
                      : "bg-surface-2 border-border"
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <Badge className="bg-blue-500/20 text-blue-400 border-blue-500/30">
                      {label}
                    </Badge>
                    {isIdle && (
                      <span className="text-[10px] text-fg-faint uppercase tracking-wider">
                        idle
                      </span>
                    )}
                  </div>
                  <div className="flex items-baseline gap-3">
                    <div>
                      <p className="text-lg font-bold text-fg leading-none">
                        {a.tool_calls}
                      </p>
                      <p className="text-[10px] text-fg-faint uppercase tracking-wider mt-0.5">
                        tools
                      </p>
                    </div>
                    <div>
                      <p className="text-lg font-bold text-fg leading-none">
                        {msgs}
                      </p>
                      <p className="text-[10px] text-fg-faint uppercase tracking-wider mt-0.5">
                        msgs
                      </p>
                    </div>
                  </div>
                  <p className="text-[11px] text-fg-faint mt-2">
                    {okRate !== null && (
                      <span className="text-emerald-400/80">{okRate}% ok</span>
                    )}
                    {okRate !== null && msgs > 0 && (
                      <span className="mx-1 text-fg-faint">|</span>
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
                          <span className="mx-1 text-fg-faint">|</span>
                        )}
                        {Math.round((a.total_ms || 0) / 1000)}s
                      </>
                    )}
                  </p>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Live interaction stream */}
      <Card padded={false} className="overflow-hidden">
        <CardHeader
          title="Agent Interaction Stream"
          actions={
            <span className="text-[11px] text-fg-faint">
              {rows.length} event{rows.length === 1 ? "" : "s"}
              {paused && " (paused)"}
            </span>
          }
        />
        <div
          ref={streamElRef}
          onScroll={onStreamScroll}
          className="overflow-y-auto max-h-[640px] divide-y divide-border/40"
        >
          {rows.length === 0 ? (
            <EmptyState
              title="Waiting for agent activity."
              hint="Submit a requirement or run an enhancement to see live communication."
            />
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
      </Card>
    </div>
  );
}
