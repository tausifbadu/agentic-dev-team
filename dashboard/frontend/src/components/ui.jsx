// Shared UI primitives for the dashboard. Built on the design tokens in
// tailwind.config.js (midnight-slate surfaces + green accent, see
// ../../DESIGN_SYSTEM.md). Pages import these instead of re-implementing cards,
// buttons, badges, spinners, and empty states inline.
//
// Motion follows the design system: ease-out, 150-300ms, transform/opacity only,
// user-triggered micro-interactions, and everything is neutralized by the global
// prefers-reduced-motion rule in index.css.

import { useEffect, useRef, useState } from "react";
import { statusStyle, eventColor } from "../lib/eventStyles";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

function ChevronDown({ className }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className}>
      <polyline points="4 6 8 10 12 6" />
    </svg>
  );
}

function CheckIcon({ className }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={className}>
      <polyline points="3.5 8.5 6.5 11.5 12.5 4.5" />
    </svg>
  );
}

// ---------- Layout ----------

export function PageHeader({ title, subtitle, actions, className }) {
  return (
    <div className={cx("flex items-start justify-between gap-4", className)}>
      <div className="min-w-0">
        <h2 className="text-xl font-semibold text-fg tracking-tight">{title}</h2>
        {subtitle && <p className="text-sm text-fg-muted mt-1">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>}
    </div>
  );
}

export function Card({ children, className, padded = true, hover = false, as: Tag = "div", ...rest }) {
  return (
    <Tag
      className={cx(
        "bg-surface-1 rounded-xl border border-border shadow-card",
        hover && "transition-all duration-200 hover:border-border-strong hover:-translate-y-0.5 hover:shadow-raised",
        padded && "p-6",
        className
      )}
      {...rest}
    >
      {children}
    </Tag>
  );
}

export function CardHeader({ title, actions, className }) {
  return (
    <div className={cx("flex items-center justify-between gap-3 px-5 py-3.5 border-b border-border-subtle", className)}>
      <h3 className="text-sm font-semibold text-fg-secondary">{title}</h3>
      {actions}
    </div>
  );
}

// ---------- Button ----------

const BTN_BASE =
  "relative inline-flex items-center justify-center gap-2 font-semibold rounded-lg cursor-pointer select-none " +
  "transition-all duration-150 ease-out active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed " +
  "disabled:hover:translate-y-0 disabled:active:scale-100";

const BTN_VARIANTS = {
  primary:
    "bg-accent text-accent-fg shadow-card hover:bg-accent-hover hover:shadow-glow hover:-translate-y-px",
  secondary:
    "bg-surface-3 text-fg-secondary border border-border hover:bg-surface-4 hover:text-fg hover:border-border-strong hover:-translate-y-px",
  ghost: "text-fg-muted hover:text-fg hover:bg-surface-3",
  outline:
    "border border-accent/40 text-accent-hover hover:bg-accent-muted hover:border-accent/70",
  danger:
    "bg-status-danger/90 text-white shadow-card hover:bg-status-danger hover:-translate-y-px",
};

const BTN_SIZES = {
  xs: "px-2.5 py-1 text-[12px]",
  sm: "px-3 py-1.5 text-[13px]",
  md: "px-4 py-2 text-sm",
  lg: "px-5 py-2.5 text-sm",
};

export function Button({
  children,
  variant = "primary",
  size = "md",
  loading = false,
  disabled = false,
  icon = null,
  className,
  type = "button",
  ...rest
}) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={cx(BTN_BASE, BTN_VARIANTS[variant], BTN_SIZES[size], className)}
      {...rest}
    >
      {loading ? (
        <Spinner size="sm" className={variant === "primary" ? "border-accent-fg/40 border-t-accent-fg" : ""} />
      ) : (
        icon && <span className="-ml-0.5 flex items-center">{icon}</span>
      )}
      {children}
    </button>
  );
}

export function IconButton({ children, className, label, ...rest }) {
  return (
    <button
      aria-label={label}
      title={label}
      className={cx(
        "inline-flex items-center justify-center w-8 h-8 rounded-lg text-fg-muted cursor-pointer " +
        "transition-all duration-150 ease-out hover:text-fg hover:bg-surface-3 active:scale-90",
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

// ---------- Badges ----------

export function Badge({ children, className }) {
  return (
    <span
      className={cx(
        "inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium border",
        className
      )}
    >
      {children}
    </span>
  );
}

export function StatusBadge({ status, className }) {
  if (!status) return null;
  return (
    <span
      className={cx(
        "inline-flex px-2.5 py-1 text-[11px] font-semibold rounded-md ring-1 ring-inset capitalize " +
        "transition-colors duration-200",
        statusStyle(status),
        className
      )}
    >
      {String(status).replace(/_/g, " ")}
    </span>
  );
}

export function EventBadge({ type, label, className }) {
  return (
    <Badge className={cx(eventColor(type), className)}>{label ?? type}</Badge>
  );
}

// ---------- Feedback ----------

export function Spinner({ size = "md", className }) {
  const sizes = { sm: "w-3.5 h-3.5 border-2", md: "w-5 h-5 border-2", lg: "w-8 h-8 border-[3px]" };
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cx(
        "inline-block rounded-full animate-spin border-fg-faint/30 border-t-fg-secondary",
        sizes[size],
        className
      )}
    />
  );
}

export function Skeleton({ className }) {
  // Shimmer (a sweeping highlight) reads as "loading" more clearly than a pulse,
  // and animates transform-only so it stays cheap. Falls back to static under
  // prefers-reduced-motion (the global rule freezes the sweep).
  return (
    <div className={cx("relative overflow-hidden rounded-md bg-surface-3/60", className)}>
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/5 to-transparent" />
    </div>
  );
}

export function EmptyState({ icon, title, hint, action, className }) {
  return (
    <div className={cx("flex flex-col items-center justify-center text-center px-6 py-14 animate-fade-in", className)}>
      {icon && <div className="text-fg-faint mb-3">{icon}</div>}
      <p className="text-sm text-fg-muted">{title}</p>
      {hint && <p className="text-xs text-fg-faint mt-1.5 max-w-sm">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

// ---------- Form fields ----------

export function Label({ children, htmlFor, className }) {
  return (
    <label
      htmlFor={htmlFor}
      className={cx("block text-xs font-medium text-fg-muted uppercase tracking-wider mb-2", className)}
    >
      {children}
    </label>
  );
}

const FIELD_BASE =
  "bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-fg-secondary placeholder-fg-faint " +
  "transition-all duration-150 ease-out hover:border-border-strong " +
  "focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/30 focus:bg-surface-2/80";

export function Input({ className, ...rest }) {
  return <input className={cx(FIELD_BASE, className)} {...rest} />;
}

export function Textarea({ className, ...rest }) {
  return <textarea className={cx(FIELD_BASE, "resize-y px-4 py-3", className)} {...rest} />;
}

// Native select, styled to match the theme: custom chevron, no native arrow,
// same focus/hover treatment as the other fields. Keeps full native a11y +
// behavior. For a richer animated menu use <Dropdown> instead.
export function Select({ className, children, ...rest }) {
  return (
    <div className="relative inline-block">
      <select
        className={cx(FIELD_BASE, "appearance-none pr-9 cursor-pointer w-full", className)}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-fg-muted" />
    </div>
  );
}

export function Field({ label, htmlFor, children, className }) {
  return (
    <div className={className}>
      {label && <Label htmlFor={htmlFor}>{label}</Label>}
      {children}
    </div>
  );
}

export function Toggle({ checked, onChange, label, className }) {
  return (
    <label className={cx("flex items-center gap-2 cursor-pointer select-none group", className)}>
      <span className="relative">
        <input type="checkbox" checked={checked} onChange={onChange} className="sr-only peer" />
        <span className="block w-9 h-5 bg-surface-3 rounded-full border border-border transition-colors duration-200 peer-checked:bg-accent peer-checked:border-accent peer-checked:shadow-glow" />
        <span className="absolute top-0.5 left-0.5 w-4 h-4 bg-fg-muted rounded-full transition-all duration-200 ease-out peer-checked:translate-x-4 peer-checked:bg-accent-fg group-active:w-5" />
      </span>
      {label && (
        <span className="text-sm text-fg-muted group-hover:text-fg-secondary transition-colors">{label}</span>
      )}
    </label>
  );
}

// ---------- Dropdown (animated, accessible custom menu) ----------
//
// A richer alternative to the native <Select>: an animated popover listbox with
// a rotating chevron, click-outside + Escape to close, and keyboard arrow/Enter
// navigation. `options` is an array of {value, label} or plain strings;
// `onChange(value)` is called with the chosen value (not an event).
export function Dropdown({
  value,
  onChange,
  options = [],
  placeholder = "Select…",
  size = "md",
  className,
  buttonClassName,
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const ref = useRef(null);

  const norm = options.map((o) => (typeof o === "string" ? { value: o, label: o } : o));
  const current = norm.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setOpen(false);
      else if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(norm.length - 1, i + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
      else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        const opt = norm[active];
        if (opt) { onChange?.(opt.value); setOpen(false); }
      }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, active, norm, onChange]);

  const sizeCls = size === "sm" ? "px-3 py-1.5 text-[13px]" : "px-3 py-2 text-sm";

  return (
    <div ref={ref} className={cx("relative inline-block", className)}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => { setOpen((o) => !o); setActive(norm.findIndex((o) => o.value === value)); }}
        className={cx(
          "flex items-center justify-between gap-2 w-full rounded-lg bg-surface-2 border text-fg-secondary " +
          "transition-all duration-150 ease-out hover:border-border-strong cursor-pointer " +
          "focus:outline-none focus:ring-2 focus:ring-accent/30",
          open ? "border-accent/60 ring-2 ring-accent/30" : "border-border",
          sizeCls,
          buttonClassName
        )}
      >
        <span className={cx("truncate", !current && "text-fg-faint")}>{current?.label ?? placeholder}</span>
        <ChevronDown className={cx("text-fg-muted transition-transform duration-200 shrink-0", open && "rotate-180")} />
      </button>

      {open && (
        <ul
          role="listbox"
          className="absolute z-30 mt-1.5 w-full max-h-64 overflow-auto rounded-lg border border-border bg-surface-2 shadow-raised p-1 origin-top animate-scale-in"
        >
          {norm.length === 0 && (
            <li className="px-3 py-2 text-sm text-fg-faint">No options</li>
          )}
          {norm.map((o, i) => {
            const selected = o.value === value;
            return (
              <li
                key={o.value}
                role="option"
                aria-selected={selected}
                onMouseEnter={() => setActive(i)}
                onClick={() => { onChange?.(o.value); setOpen(false); }}
                className={cx(
                  "flex items-center justify-between gap-2 px-3 py-2 rounded-md text-sm cursor-pointer transition-colors duration-100",
                  i === active ? "bg-surface-3 text-fg" : "text-fg-secondary",
                  selected && "text-accent-hover"
                )}
              >
                <span className="truncate">{o.label}</span>
                {selected && <CheckIcon className="text-accent shrink-0" />}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
