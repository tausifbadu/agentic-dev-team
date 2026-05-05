---
name: principal-reactjs-developer
description: >-
  Principal-level React.js engineering guidelines for building production-grade
  frontends. Covers component architecture, state management, performance,
  accessibility, testing, and project structure. Use when building, reviewing,
  or refactoring React applications, components, hooks, or frontend architecture.
---

# Principal React.js Developer

Expert-level React engineering guidelines. Apply when building, reviewing, or refactoring any React frontend code.

## Architecture Principles

### Component Design

- **Single Responsibility**: One component = one job. If a component handles data fetching AND rendering AND form logic, split it.
- **Composition over Inheritance**: Build complex UIs from small, composable pieces. Prefer `children` and render props over deep component hierarchies.
- **Colocation**: Keep state as close to where it's used as possible. Don't lift state higher than necessary.
- **Controlled vs Uncontrolled**: Default to controlled components for forms. Use uncontrolled only for simple, isolated inputs.

### Project Structure

```
src/
├── components/        # Shared/reusable UI components
│   ├── Button.jsx
│   ├── Modal.jsx
│   └── StatusBadge.jsx
├── pages/             # Route-level page components
│   ├── Dashboard.jsx
│   └── DetailView.jsx
├── hooks/             # Custom hooks
│   ├── useApi.js
│   └── useDebounce.js
├── services/          # API client functions
│   └── api.js
├── utils/             # Pure helper functions
├── styles/            # Global styles, CSS variables, theme
├── App.jsx            # Root component with routing
└── main.jsx           # Entry point
```

### File Naming

- Components: `PascalCase.jsx` (e.g., `StatusBadge.jsx`)
- Hooks: `camelCase.js` prefixed with `use` (e.g., `useItems.js`)
- Utilities: `camelCase.js` (e.g., `formatDate.js`)
- Styles: `kebab-case.css` or co-located `Component.module.css`

## Component Patterns

### Functional Components Only

Never use class components in new code. Use function components with hooks exclusively.

```jsx
function ItemCard({ item, onClick }) {
  return (
    <article className="item-card" onClick={() => onClick(item.id)}>
      <h3>{item.title}</h3>
      <span className={`badge badge-${item.status}`}>
        {item.status}
      </span>
    </article>
  );
}
```

### Props

- Destructure props in the function signature
- Use default values via destructuring, not `defaultProps`
- Keep prop count under 7; if more, group into an object or rethink the component
- Boolean props: use positive naming (`isOpen`, not `isNotClosed`)

### Children Pattern

```jsx
function Card({ title, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {title && <h2 className="card-title">{title}</h2>}
      {children}
    </section>
  );
}
```

### Compound Components

For complex, related UI pieces (tabs, accordions, dropdowns):

```jsx
function Tabs({ children, defaultTab }) { /* manages active state */ }
Tabs.Tab = function Tab({ id, children }) { /* tab button */ };
Tabs.Panel = function Panel({ id, children }) { /* tab content */ };
```

## State Management

### Priority Order

1. **Local state** (`useState`) — default choice for UI state
2. **Derived state** — compute from existing state, don't duplicate
3. **URL state** — filters, pagination, selected IDs (use query params)
4. **Lifted state** — share between siblings via nearest common parent
5. **Context** — theme, auth, locale — things that rarely change
6. **External store** (Zustand/Redux) — only for complex cross-cutting state

### useState Rules

- One concern per `useState` call (don't merge unrelated state into one object)
- Use functional updater when new state depends on previous: `setState(prev => prev + 1)`
- Initialize with a function for expensive computations: `useState(() => computeExpensive())`

### useEffect Rules

- Every effect must have a **clear purpose** (fetch data, subscribe, sync DOM)
- Always include a cleanup function for subscriptions and timers
- Never lie about dependencies — include everything referenced inside
- If an effect runs too often, restructure (extract to custom hook, use `useCallback`)
- Avoid effects for **derived state** — use `useMemo` instead

```jsx
// BAD: effect to derive filtered list
useEffect(() => {
  setFiltered(items.filter(i => i.status === filter));
}, [items, filter]);

// GOOD: compute directly
const filtered = useMemo(
  () => items.filter(i => i.status === filter),
  [items, filter]
);
```

### Custom Hooks

Extract reusable logic into custom hooks:

```jsx
function useApi(url) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(url)
      .then(res => {
        if (!res.ok) throw new Error(`${res.status}`);
        return res.json();
      })
      .then(data => { if (!cancelled) setData(data); })
      .catch(err => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url]);

  return { data, loading, error };
}
```

## Performance

### Rendering Optimization

| Technique | When to Use |
|-----------|-------------|
| `React.memo` | Component re-renders with same props frequently |
| `useMemo` | Expensive computation derived from props/state |
| `useCallback` | Passing callbacks to memoized children |
| Key prop | Always use stable, unique keys for lists (never `index` unless list is static) |
| Virtualization | Lists with 50+ items (use `react-window` or `@tanstack/virtual`) |

### Code Splitting

- Split by route using `React.lazy` + `Suspense`
- Lazy load heavy components (charts, editors, modals) on demand
- Use dynamic `import()` for large utility libraries

```jsx
const DetailView = React.lazy(() => import("./pages/DetailView"));

<Suspense fallback={<LoadingSkeleton />}>
  <DetailView />
</Suspense>
```

### Avoid Re-render Traps

- Never create objects/arrays/functions inline in JSX if passed to memoized children
- Never call `setState` during render
- Never use `useEffect` to sync state that can be derived
- Avoid deep prop drilling — use Context or composition

## Data Fetching

### Pattern

```jsx
async function fetchItems(status) {
  const params = status ? `?status=${status}` : "";
  const res = await fetch(`/api/items${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}
```

### Rules

- Centralize API calls in a `services/api.js` file
- Always handle loading, error, and empty states in the UI
- Use `AbortController` for cancellable requests in effects
- Parse error responses from the API (`{ detail: "..." }`) and show them to the user
- Never swallow errors silently

### Loading States

```jsx
{loading && <Skeleton />}
{error && <ErrorBanner message={error} onRetry={refetch} />}
{!loading && !error && data.length === 0 && <EmptyState />}
{!loading && !error && data.length > 0 && <DataTable rows={data} />}
```

## Forms

### Controlled Form Pattern

```jsx
function CreateItemForm({ onSubmit, onCancel }) {
  const [form, setForm] = useState({ title: "", description: "", category: "" });
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);

  const onChange = (e) => {
    const { name, value } = e.target;
    setForm(prev => ({ ...prev, [name]: value }));
    setErrors(prev => ({ ...prev, [name]: "" }));
  };

  const validate = () => {
    const next = {};
    if (!form.title.trim()) next.title = "Title is required";
    if (!form.category) next.category = "Select a category";
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!validate()) return;
    setSubmitting(true);
    try {
      await onSubmit(form);
    } catch (err) {
      setErrors({ form: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} noValidate>
      {/* fields with visible labels, error messages below each field */}
    </form>
  );
}
```

### Form Rules

- Always use visible `<label>` elements (never placeholder-only)
- Show errors **below** the relevant field
- Disable submit button during async submission, show loading indicator
- Use `noValidate` on `<form>` and handle validation in JS for consistent UX
- Reset form state on successful submission

## Accessibility

### Non-Negotiable

| Rule | Implementation |
|------|---------------|
| Semantic HTML | Use `button`, `nav`, `main`, `section`, `article`, `table` — not div-for-everything |
| Labels | Every input has a `<label htmlFor>` or `aria-label` |
| Keyboard | All interactive elements reachable and operable via keyboard |
| Focus management | Move focus to modal on open, restore on close. Focus first error on validation failure |
| Alt text | Every meaningful `<img>` has descriptive `alt`. Decorative images use `alt=""` |
| ARIA | Use `aria-live="polite"` for dynamic messages, `role="alert"` for errors |
| Color contrast | 4.5:1 minimum for text, 3:1 for large text and UI elements |
| Reduced motion | Respect `prefers-reduced-motion` — disable/reduce animations |

### Modal Accessibility

```jsx
<div
  className="modal-overlay"
  role="presentation"
  onClick={onClose}
  onKeyDown={e => e.key === "Escape" && onClose()}
>
  <div
    role="dialog"
    aria-modal="true"
    aria-labelledby="modal-title"
    onClick={e => e.stopPropagation()}
  >
    <h2 id="modal-title">Create Item</h2>
    {/* content */}
  </div>
</div>
```

## Error Handling

### Error Boundaries

Wrap route-level components with error boundaries to prevent full-app crashes:

```jsx
class ErrorBoundary extends React.Component {
  state = { hasError: false, error: null };
  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }
  render() {
    if (this.state.hasError) {
      return <ErrorFallback error={this.state.error} />;
    }
    return this.props.children;
  }
}
```

### API Error Display

- Show error messages near the relevant UI element, not in a global toast
- Include a retry action for recoverable errors
- Log unexpected errors to console in development

## Styling

### CSS Variables for Theming

```css
:root {
  --bg: #0b1220;
  --surface: #111a2b;
  --text: #e5e7eb;
  --muted: #9ca3af;
  --primary: #3b82f6;
  --border: #243247;
  --danger: #f87171;
  --success: #34d399;
}
```

### Rules

- Use CSS custom properties (variables) for all colors, spacing, and typography tokens
- Use semantic class names (`.stat-card`, `.status-badge`) not utility-first in generated code
- Mobile-first responsive design with `min-width` media queries
- Standard breakpoints: 640px (sm), 768px (md), 1024px (lg), 1280px (xl)
- Minimum touch target: 44x44px
- Consistent spacing scale: 4px increments (4, 8, 12, 16, 24, 32, 48)

## Testing

### What to Test

| Priority | Type | Example |
|----------|------|---------|
| HIGH | User interactions | Click button → modal opens, form submits |
| HIGH | Conditional rendering | Loading/error/empty/data states |
| MEDIUM | Props/state changes | `StatusBadge` renders correct class based on value |
| LOW | Snapshot tests | Only for stable, shared components |

### Testing Library Pattern

```jsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

test("shows item list after loading", async () => {
  render(<Dashboard />);
  expect(screen.getByText("Loading...")).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByText("Sample Item")).toBeInTheDocument();
  });
});
```

## Anti-Patterns to Avoid

| Anti-Pattern | Do Instead |
|-------------|-----------|
| `useEffect` to sync derived state | Use `useMemo` |
| Prop drilling through 3+ levels | Use Context or composition |
| Index as key for dynamic lists | Use unique ID |
| Inline object/function props to memo'd children | Extract to `useMemo`/`useCallback` |
| Giant monolithic components (200+ lines) | Split into focused sub-components |
| Direct DOM manipulation | Use refs only when necessary |
| Fetching in `useEffect` without cleanup | Use `AbortController` or cancelled flag |
| Suppressing lint warnings (`// eslint-disable`) | Fix the underlying issue |
| `any` types in TypeScript | Use proper types/interfaces |
| Console.log left in production code | Remove or use proper logging |
