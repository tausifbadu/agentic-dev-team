import { Routes, Route, NavLink, useLocation } from "react-router-dom";
import Requirements from "./pages/Requirements";
import StoryBoard from "./pages/StoryBoard";
import AgentMonitor from "./pages/AgentMonitor";
import TestResults from "./pages/TestResults";
import Workspace from "./pages/Workspace";
import DebugPanel from "./pages/DebugPanel";
import AgentComms from "./pages/AgentComms";
import Enhancements from "./pages/Enhancements";
import LiveConsole from "./pages/LiveConsole";
import WorkspaceChat from "./pages/WorkspaceChat";

const navItems = [
  { to: "/", label: "Requirements", icon: IconReq },
  { to: "/live", label: "Live", icon: IconLive },
  { to: "/agents", label: "Agents", icon: IconAgent },
  { to: "/comms", label: "Comms", icon: IconComms },
  { to: "/tests", label: "Tests", icon: IconTest },
  { to: "/workspace", label: "Workspace", icon: IconCode },
  { to: "/workspace-chat", label: "WS Chat", icon: IconChat },
  { to: "/enhance", label: "Enhance", icon: IconEnhance },
  { to: "/debug", label: "Debug", icon: IconDebug },
];

export default function App() {
  const location = useLocation();
  return (
    <div className="min-h-screen flex">
      <aside className="w-56 flex-shrink-0 bg-surface-1 border-r border-border flex flex-col">
        <div className="px-5 py-5">
          <h1 className="text-base font-bold text-fg tracking-tight">
            Agentic Dev Team
          </h1>
          <p className="text-[11px] text-fg-faint mt-0.5 font-medium tracking-wide uppercase">
            Dashboard v1
          </p>
        </div>

        <nav className="flex-1 px-3 space-y-0.5">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `relative flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] font-medium transition-all duration-150 cursor-pointer ${
                  isActive
                    ? "bg-accent-muted text-accent-hover before:absolute before:left-0 before:top-1.5 before:bottom-1.5 before:w-0.5 before:rounded-full before:bg-accent"
                    : "text-fg-muted hover:text-fg-secondary hover:bg-surface-3"
                }`
              }
            >
              <item.icon />
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="px-5 py-4 border-t border-border">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-accent animate-pulse-slow shadow-glow" />
            <span className="text-xs text-fg-muted">System Online</span>
          </div>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div key={location.pathname} className="max-w-6xl mx-auto px-8 py-8 animate-fade-in-up">
          <Routes>
            <Route path="/" element={<Requirements />} />
            <Route path="/stories/:packId" element={<StoryBoard />} />
            <Route path="/agents" element={<AgentMonitor />} />
            <Route path="/tests" element={<TestResults />} />
            <Route path="/workspace" element={<Workspace />} />
            <Route path="/workspace-chat" element={<WorkspaceChat />} />
            <Route path="/comms" element={<AgentComms />} />
            <Route path="/enhance" element={<Enhancements />} />
            <Route path="/live" element={<LiveConsole />} />
            <Route path="/debug" element={<DebugPanel />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

function IconReq() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="12" height="12" rx="2" />
      <line x1="5" y1="6" x2="11" y2="6" />
      <line x1="5" y1="9" x2="9" y2="9" />
    </svg>
  );
}

function IconAgent() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="5" r="3" />
      <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" />
    </svg>
  );
}

function IconTest() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 8 6.5 10.5 12 5" />
      <rect x="1.5" y="1.5" width="13" height="13" rx="2" />
    </svg>
  );
}

function IconCode() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="5.5 4.5 2 8 5.5 11.5" />
      <polyline points="10.5 4.5 14 8 10.5 11.5" />
    </svg>
  );
}

function IconComms() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 4h8a2 2 0 012 2v3a2 2 0 01-2 2H6l-3 2V6a2 2 0 012-2z" />
      <path d="M6 11v1a2 2 0 002 2h4l3 2v-5a2 2 0 00-2-2h-1" />
    </svg>
  );
}

function IconEnhance() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2v12M2 8h12" />
      <circle cx="8" cy="8" r="6" />
    </svg>
  );
}

function IconLive() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2" />
      <path d="M4.5 4.5a5 5 0 000 7" />
      <path d="M11.5 4.5a5 5 0 010 7" />
      <path d="M2 2a8 8 0 000 12" />
      <path d="M14 2a8 8 0 010 12" />
    </svg>
  );
}

function IconDebug() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="9" r="5" />
      <path d="M8 4V2" />
      <path d="M3.5 6.5L1.5 5" />
      <path d="M12.5 6.5L14.5 5" />
      <path d="M3.5 11.5L1.5 13" />
      <path d="M12.5 11.5L14.5 13" />
      <line x1="5.5" y1="9" x2="10.5" y2="9" />
      <line x1="5.5" y1="7" x2="10.5" y2="7" />
      <line x1="5.5" y1="11" x2="10.5" y2="11" />
    </svg>
  );
}

function IconChat() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 13V5a2 2 0 012-2h8a2 2 0 012 2v5a2 2 0 01-2 2H8l-5 4v-6z" />
      <circle cx="6" cy="7" r=".5" fill="currentColor" stroke="none" />
      <circle cx="9" cy="7" r=".5" fill="currentColor" stroke="none" />
      <circle cx="12" cy="7" r=".5" fill="currentColor" stroke="none" />
    </svg>
  );
}
