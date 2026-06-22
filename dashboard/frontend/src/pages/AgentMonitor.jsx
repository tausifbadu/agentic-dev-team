import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { api } from "../api";
import { PageHeader, Card, CardHeader, EmptyState } from "../components/ui";

const AGENT_STYLES = {
  orchestrator: "bg-slate-500/15 text-slate-400",
  backend: "bg-violet-500/15 text-violet-400",
  frontend: "bg-cyan-500/15 text-cyan-400",
  testing: "bg-amber-500/15 text-amber-400",
  pm: "bg-fuchsia-500/15 text-fuchsia-400",
};

function extractReqId(storyId) {
  if (!storyId) return null;
  const match = storyId.match(/^(req_[a-f0-9]+)/);
  return match ? match[1] : null;
}

function groupLogsByRun(logs) {
  if (!logs.length) return [];

  const runBoundaries = [];
  let currentRunStart = 0;
  let currentReqId = null;

  for (let i = 0; i < logs.length; i++) {
    const reqId = extractReqId(logs[i].story_id);
    if (reqId && reqId !== currentReqId) {
      if (currentReqId !== null) {
        runBoundaries.push({ start: currentRunStart, end: i, reqId: currentReqId });
      }
      let lookBack = i;
      while (lookBack > (runBoundaries.length ? runBoundaries[runBoundaries.length - 1].end : 0)) {
        const prev = logs[lookBack - 1];
        if (prev && !extractReqId(prev.story_id) && prev.agent_type === "orchestrator") {
          lookBack--;
        } else {
          break;
        }
      }
      currentRunStart = lookBack;
      currentReqId = reqId;
    }
  }
  if (currentReqId !== null) {
    runBoundaries.push({ start: currentRunStart, end: logs.length, reqId: currentReqId });
  }

  const groups = [];
  const assigned = new Set();

  for (const bound of runBoundaries) {
    const group = {
      reqId: bound.reqId,
      logs: [],
      firstTime: null,
      lastTime: null,
      errorCount: 0,
    };
    for (let i = bound.start; i < bound.end; i++) {
      assigned.add(i);
      const log = logs[i];
      group.logs.push(log);
      if (!group.firstTime || log.created_at < group.firstTime) group.firstTime = log.created_at;
      if (!group.lastTime || log.created_at > group.lastTime) group.lastTime = log.created_at;
      if (log.level === "error") group.errorCount++;
    }
    groups.push(group);
  }

  const unassigned = [];
  for (let i = 0; i < logs.length; i++) {
    if (!assigned.has(i)) unassigned.push(logs[i]);
  }
  if (unassigned.length) {
    groups.unshift({
      reqId: "__global__",
      logs: unassigned,
      firstTime: unassigned[0]?.created_at,
      lastTime: unassigned[unassigned.length - 1]?.created_at,
      errorCount: unassigned.filter((l) => l.level === "error").length,
    });
  }

  return groups;
}

export default function AgentMonitor() {
  const [status, setStatus] = useState(null);
  const [logs, setLogs] = useState([]);
  const logsContainerRef = useRef(null);
  const userScrolledUp = useRef(false);

  const handleScroll = useCallback(() => {
    const el = logsContainerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledUp.current = distanceFromBottom > 80;
  }, []);

  useEffect(() => {
    const pollMs = status?.running ? 1500 : 3000;
    const poll = setInterval(() => {
      api.getAgentStatus().then(setStatus).catch(console.error);
      api.getAgentLogs(500).then(setLogs).catch(console.error);
    }, pollMs);

    api.getAgentStatus().then(setStatus).catch(console.error);
    api.getAgentLogs(500).then(setLogs).catch(console.error);

    return () => clearInterval(poll);
  }, [status?.running]);

  useEffect(() => {
    const el = logsContainerRef.current;
    if (!userScrolledUp.current && el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logs]);

  const reversedLogs = useMemo(() => [...logs].reverse(), [logs]);
  const groups = useMemo(() => groupLogsByRun(reversedLogs), [reversedLogs]);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Agent Monitor"
        subtitle="Real-time agent execution and generated code"
      />

      {status && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricCard
            label="Status"
            value={status.running ? "Running" : "Idle"}
            accent={status.running ? "emerald" : "slate"}
          />
          <MetricCard label="Phase" value={status.phase || "\u2014"} accent="accent" />
          <MetricCard label="Current Agent" value={status.current_agent || "\u2014"} accent="cyan" />
          <MetricCard label="Current Story" value={status.current_story_id || "\u2014"} mono accent="violet" />
        </div>
      )}

      {status && (
        <div className="flex gap-4 flex-wrap">
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-500/10 ring-1 ring-inset ring-emerald-500/20">
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            <span className="text-xs font-medium text-emerald-400">
              Completed: {status.completed_stories?.length || 0}
            </span>
          </div>
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-rose-500/10 ring-1 ring-inset ring-rose-500/20">
            <div className="w-1.5 h-1.5 rounded-full bg-rose-400" />
            <span className="text-xs font-medium text-rose-400">
              Failed: {status.failed_stories?.length || 0}
            </span>
          </div>
          {(status.skipped_stories?.length || 0) > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-amber-500/10 ring-1 ring-inset ring-amber-500/20">
              <div className="w-1.5 h-1.5 rounded-full bg-amber-400" />
              <span className="text-xs font-medium text-amber-400">
                Skipped: {status.skipped_stories.length}
              </span>
            </div>
          )}
        </div>
      )}

      {status?.conclusion && !status.running && (
        <div
          className={`rounded-xl border p-4 flex items-center gap-3 ${
            status.conclusion === "completed"
              ? "bg-emerald-500/10 border-emerald-500/30"
              : status.conclusion === "failed"
              ? "bg-rose-500/10 border-rose-500/30"
              : "bg-amber-500/10 border-amber-500/30"
          }`}
        >
          <div
            className={`w-8 h-8 rounded-full flex items-center justify-center text-lg ${
              status.conclusion === "completed"
                ? "bg-emerald-500/20 text-emerald-400"
                : status.conclusion === "failed"
                ? "bg-rose-500/20 text-rose-400"
                : "bg-amber-500/20 text-amber-400"
            }`}
          >
            {status.conclusion === "completed" ? "\u2713" : status.conclusion === "failed" ? "\u2717" : "~"}
          </div>
          <div>
            <p
              className={`text-sm font-semibold ${
                status.conclusion === "completed"
                  ? "text-emerald-400"
                  : status.conclusion === "failed"
                  ? "text-rose-400"
                  : "text-amber-400"
              }`}
            >
              Pipeline {status.conclusion === "completed" ? "Completed" : status.conclusion === "failed" ? "Failed" : "Partially Completed"}
            </p>
            <p className="text-xs text-fg-faint mt-0.5">
              {status.completed_stories?.length || 0} completed, {status.failed_stories?.length || 0} failed, {status.skipped_stories?.length || 0} skipped
            </p>
          </div>
        </div>
      )}

      <Card padded={false} className="overflow-hidden">
        <CardHeader
          title="Live Logs"
          actions={<span className="text-[11px] text-fg-faint font-mono">{logs.length} entries</span>}
        />
        <div
          ref={logsContainerRef}
          onScroll={handleScroll}
          className="max-h-[700px] overflow-y-auto"
        >
          {logs.length === 0 ? (
            <EmptyState
              title="No logs yet."
              hint="Submit and approve a requirement to start."
            />
          ) : (
            <div className="divide-y divide-border">
              {groups.map((group) => (
                <LogGroup key={group.reqId} group={group} isRunning={status?.running} />
              ))}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function LogGroup({ group, isRunning }) {
  const isGlobal = group.reqId === "__global__";
  const [expanded, setExpanded] = useState(() => {
    if (isGlobal) return true;
    return true;
  });

  const label = isGlobal ? "System / Orchestrator" : group.reqId;
  const timeRange = formatTimeShort(group.firstTime) + " \u2013 " + formatTimeShort(group.lastTime);

  const agentTypes = [...new Set(group.logs.map((l) => l.agent_type))];

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full px-5 py-3 flex items-center gap-3 hover:bg-surface-2/40 transition-colors duration-100"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          className={`text-fg-faint transition-transform duration-150 shrink-0 ${expanded ? "rotate-90" : ""}`}
        >
          <polyline points="5 3 9 7 5 11" />
        </svg>

        <span className="text-[13px] font-semibold text-fg font-mono">
          {label}
        </span>

        <div className="flex items-center gap-1.5 ml-2">
          {agentTypes.map((a) => (
            <span
              key={a}
              className={`inline-flex px-1.5 py-0.5 text-[9px] font-semibold rounded ${
                AGENT_STYLES[a] || "bg-slate-500/15 text-slate-400"
              }`}
            >
              {a}
            </span>
          ))}
        </div>

        <span className="ml-auto flex items-center gap-3">
          {group.errorCount > 0 && (
            <span className="text-[11px] font-medium text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded">
              {group.errorCount} error{group.errorCount > 1 ? "s" : ""}
            </span>
          )}
          <span className="text-[11px] text-fg-faint font-mono tabular-nums">
            {group.logs.length} logs
          </span>
          <span className="text-[11px] text-fg-faint font-mono tabular-nums whitespace-nowrap">
            {timeRange}
          </span>
        </span>
      </button>

      {expanded && (
        <div className="border-t border-border-subtle/50">
          {group.logs.map((log) => (
            <LogEntry key={log.id} log={log} />
          ))}
        </div>
      )}
    </div>
  );
}

function LogEntry({ log }) {
  const [open, setOpen] = useState(false);
  const hasDetail = !!log.detail;
  const isCodeLog = hasDetail && (
    log.message.includes("Plan ready") ||
    log.message.includes("Code generated") ||
    log.message.includes("Tests generated") ||
    log.message.includes("Test plan ready")
  );

  return (
    <div className="border-b border-border-subtle/30 last:border-b-0">
      <div
        className={`px-5 pl-10 py-2 flex items-start gap-3 text-[13px] transition-colors duration-100 ${
          hasDetail ? "hover:bg-surface-2/40 cursor-pointer" : ""
        }`}
        onClick={() => hasDetail && setOpen(!open)}
      >
        <span className="text-[11px] text-fg-faint whitespace-nowrap font-mono mt-0.5 tabular-nums">
          {formatTimeShort(log.created_at)}
        </span>
        <span
          className={`inline-flex px-2 py-0.5 text-[10px] font-semibold rounded shrink-0 ${
            AGENT_STYLES[log.agent_type] || "bg-slate-500/15 text-slate-400"
          }`}
        >
          {log.agent_type}
        </span>
        {log.story_id && (
          <span className="text-[10px] text-fg-faint font-mono shrink-0 mt-0.5">
            {log.story_id.replace(/^req_[a-f0-9]+_/, "")}
          </span>
        )}
        <span
          className={`flex-1 leading-relaxed ${
            log.level === "error" ? "text-rose-400" : "text-fg-muted"
          }`}
        >
          {log.message}
        </span>
        {hasDetail && (
          <span className="shrink-0 mt-0.5">
            <svg
              width="14"
              height="14"
              viewBox="0 0 14 14"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className={`text-fg-faint transition-transform duration-150 ${open ? "rotate-90" : ""}`}
            >
              <polyline points="5 3 9 7 5 11" />
            </svg>
          </span>
        )}
      </div>
      {open && hasDetail && (
        <div className="px-5 pl-10 pb-3">
          <div className={`rounded-lg border overflow-hidden ${
            isCodeLog ? "border-accent/20" : "border-border-subtle"
          }`}>
            {isCodeLog && (
              <div className="px-3 py-1.5 border-b border-border-subtle bg-surface-2/50 flex items-center gap-2">
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-accent">
                  <polyline points="5.5 4.5 2 8 5.5 11.5" />
                  <polyline points="10.5 4.5 14 8 10.5 11.5" />
                </svg>
                <span className="text-[10px] font-medium text-accent uppercase tracking-wider">
                  Generated Code
                </span>
              </div>
            )}
            <pre className="p-4 bg-surface-0 text-xs text-fg-muted font-mono overflow-x-auto max-h-[400px] overflow-y-auto whitespace-pre-wrap leading-relaxed selection:bg-accent/20">
              {log.detail}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

function formatTimeShort(iso) {
  if (!iso) return "";
  try {
    let str = iso;
    if (!str.endsWith("Z") && !str.includes("+") && !str.includes("-", 10)) {
      str += "Z";
    }
    const d = new Date(str);
    if (isNaN(d.getTime())) return iso.slice(11, 19) || "";
    return d.toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
    });
  } catch {
    return iso.slice(11, 19) || "";
  }
}

function MetricCard({ label, value, accent, mono }) {
  const accentMap = {
    emerald: "border-emerald-500/30",
    rose: "border-rose-500/30",
    cyan: "border-cyan-500/30",
    violet: "border-violet-500/30",
    amber: "border-amber-500/30",
    accent: "border-accent/30",
    slate: "border-slate-500/20",
  };
  const textMap = {
    emerald: "text-emerald-400",
    rose: "text-rose-400",
    cyan: "text-cyan-400",
    violet: "text-violet-400",
    amber: "text-amber-400",
    accent: "text-accent",
    slate: "text-slate-400",
  };

  return (
    <Card padded={false} className={`p-4 ${accentMap[accent] || ""}`}>
      <p className="text-[11px] text-fg-faint uppercase tracking-wider font-medium">{label}</p>
      <p
        className={`text-sm font-semibold mt-1.5 truncate ${mono ? "font-mono text-xs" : ""} ${
          textMap[accent] || "text-fg"
        }`}
      >
        {value}
      </p>
    </Card>
  );
}
