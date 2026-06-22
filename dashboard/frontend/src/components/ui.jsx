// Shared UI primitives for the dashboard. Built on the design tokens in
// tailwind.config.js (midnight-slate surfaces + green accent, see
// ../../DESIGN_SYSTEM.md). Pages import these instead of re-implementing cards,
// buttons, badges, spinners, and empty states inline.

import { statusStyle, eventColor } from "../lib/eventStyles";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
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
        hover && "transition-all duration-150 hover:border-border-strong hover:-translate-y-0.5 hover:shadow-raised",
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
  "inline-flex items-center justify-center gap-2 font-semibold rounded-lg transition-all duration-150 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed select-none active:scale-[0.97]";

const BTN_VARIANTS = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover hover:shadow-glow",
  secondary: "bg-surface-3 text-fg-secondary border border-border hover:bg-surface-4 hover:text-fg",
  ghost: "text-fg-muted hover:text-fg hover:bg-surface-3",
  danger: "bg-status-danger/90 text-white hover:bg-status-danger",
};

const BTN_SIZES = {
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
      {loading && <Spinner size="sm" className={variant === "primary" ? "border-accent-fg/40 border-t-accent-fg" : ""} />}
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
        "inline-flex items-center justify-center w-8 h-8 rounded-lg text-fg-muted hover:text-fg hover:bg-surface-3 transition-colors duration-150 cursor-pointer",
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
        "inline-flex px-2.5 py-1 text-[11px] font-semibold rounded-md ring-1 ring-inset capitalize",
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
    <div className={cx("flex flex-col items-center justify-center text-center px-6 py-14", className)}>
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
  "bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-fg-secondary placeholder-fg-faint transition-all duration-150 focus:outline-none focus:border-accent/50 focus:ring-2 focus:ring-accent/30";

export function Input({ className, ...rest }) {
  return <input className={cx(FIELD_BASE, className)} {...rest} />;
}

export function Textarea({ className, ...rest }) {
  return <textarea className={cx(FIELD_BASE, "resize-y px-4 py-3", className)} {...rest} />;
}

export function Select({ className, children, ...rest }) {
  return (
    <select className={cx(FIELD_BASE, "cursor-pointer", className)} {...rest}>
      {children}
    </select>
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
        <span className="block w-9 h-5 bg-surface-3 rounded-full peer-checked:bg-accent transition-colors duration-200 border border-border" />
        <span className="absolute top-0.5 left-0.5 w-4 h-4 bg-fg-muted rounded-full peer-checked:translate-x-4 peer-checked:bg-accent-fg transition-all duration-200" />
      </span>
      {label && (
        <span className="text-sm text-fg-muted group-hover:text-fg-secondary transition-colors">{label}</span>
      )}
    </label>
  );
}
