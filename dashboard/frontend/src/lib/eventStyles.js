// Single source of truth for cross-page presentation constants.
//
// Before this file, EVENT_COLORS / AGENT_LABELS / formatTime were copy-pasted
// across LiveConsole.jsx, AgentComms.jsx, AgentMonitor.jsx, etc. and kept in
// sync by hand (LiveConsole literally carried a comment saying its map "mirrors
// AgentComms"). They now live here and are imported everywhere.

// Bus event-type → badge classes. Union of every map that existed across pages.
// Each event keeps its own hue so the comms/console streams stay scannable; this
// is intentionally a semantic categorical palette, not the brand accent.
export const EVENT_COLORS = {
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
  test_triage_request: "bg-indigo-500/20 text-indigo-400 border-indigo-500/30",
  triage_request: "bg-indigo-500/20 text-indigo-400 border-indigo-500/30",
  triage_request_reply: "bg-indigo-500/10 text-indigo-300 border-indigo-500/20",
  test_triage_result: "bg-violet-500/20 text-violet-400 border-violet-500/30",
  test_fix_route: "bg-sky-500/20 text-sky-400 border-sky-500/30",
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

export const EVENT_FALLBACK = "bg-slate-500/20 text-slate-400 border-slate-500/30";

export function eventColor(type) {
  return EVENT_COLORS[type] || EVENT_FALLBACK;
}

// Story / requirement / run status → badge classes. Outcome semantics: success
// is emerald (distinct from the green brand accent), failure rose/red, etc.
export const STATUS_STYLES = {
  pending_review: "bg-amber-500/15 text-amber-400 ring-amber-500/25",
  pending: "bg-slate-500/15 text-slate-400 ring-slate-500/25",
  approved: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/25",
  in_progress: "bg-blue-500/15 text-blue-400 ring-blue-500/25",
  running: "bg-blue-500/15 text-blue-400 ring-blue-500/25",
  completed: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/25",
  passed: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/25",
  failed: "bg-rose-500/15 text-rose-400 ring-rose-500/25",
  rejected: "bg-rose-500/15 text-rose-400 ring-rose-500/25",
  skipped: "bg-slate-500/15 text-slate-400 ring-slate-500/25",
};

export const STATUS_FALLBACK = "bg-slate-500/15 text-slate-400 ring-slate-500/25";

export function statusStyle(status) {
  return STATUS_STYLES[status] || STATUS_FALLBACK;
}

// Log levels → dot/text accent.
export const LEVEL_STYLES = {
  info: "text-fg-muted",
  warn: "text-status-warning-fg",
  warning: "text-status-warning-fg",
  error: "text-status-danger-fg",
  debug: "text-fg-faint",
};

export const AGENT_LABELS = {
  orchestrator: "Orchestrator",
  supervisor: "Supervisor",
  pm: "PM",
  backend: "Backend",
  frontend: "Frontend",
  test: "Test",
  testing: "Test",
  verifier: "Verifier",
};

export function agentLabel(id) {
  return AGENT_LABELS[id] || id;
}

export function formatTime(iso) {
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
