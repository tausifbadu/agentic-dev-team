import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Toggle,
  Field,
  Label,
  Input,
  Dropdown,
  Textarea,
  StatusBadge,
  EmptyState,
} from "../components/ui";

export default function Requirements() {
  const [requirements, setRequirements] = useState([]);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState("default");
  const [promptFiles, setPromptFiles] = useState([]);
  const [text, setText] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const navigate = useNavigate();

  const [newProjectSlug, setNewProjectSlug] = useState("");

  const refresh = () => {
    api.listRequirements().then(setRequirements).catch(console.error);
    api.listPromptFiles().then(setPromptFiles).catch(console.error);
    api.listProjects().then((r) => setProjects(r.projects || [])).catch(console.error);
  };

  useEffect(refresh, []);

  const createProject = async () => {
    if (!newProjectSlug.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const r = await api.createProject(newProjectSlug.trim());
      setNewProjectSlug("");
      setProjectId(r.id);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.submitRequirement(text.trim(), autoApprove, { project_id: projectId });
      setText("");
      refresh();
      navigate(`/stories/${result.storypack_id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const submitFromFile = async (filename) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.submitFromFile(filename, autoApprove, { project_id: projectId });
      refresh();
      navigate(`/stories/${result.storypack_id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-8">
      <PageHeader
        title="Requirements"
        subtitle="Submit a new requirement or load from a prompt file"
      />

      <Card>
        <form onSubmit={submit} className="space-y-4">
          <div className="flex flex-wrap items-end gap-4">
            <Field label="Project workspace">
              <Dropdown
                value={projectId}
                onChange={setProjectId}
                className="min-w-[200px]"
                options={(projects.length ? projects : [{ id: "default" }]).map((p) => ({
                  value: p.id,
                  label: p.id === "default" ? "default (legacy workspace/)" : p.id,
                }))}
              />
            </Field>
            <div className="flex items-end gap-2">
              <Field label="New project slug" htmlFor="new-project-slug">
                <Input
                  id="new-project-slug"
                  type="text"
                  value={newProjectSlug}
                  onChange={(e) => setNewProjectSlug(e.target.value)}
                  placeholder="my-app"
                  className="w-40"
                />
              </Field>
              <Button
                variant="secondary"
                onClick={createProject}
                disabled={loading || !newProjectSlug.trim()}
                className="mb-0.5"
              >
                Create
              </Button>
            </div>
          </div>
          <Field label="Describe what you want to build" htmlFor="req-input">
            <Textarea
              id="req-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Build a REST API that..."
              rows={5}
              className="w-full"
            />
          </Field>
          <div className="flex items-center gap-4 flex-wrap">
            <Button type="submit" size="lg" loading={loading} disabled={loading || !text.trim()}>
              {loading ? "Creating stories..." : "Submit to PM Agent"}
            </Button>
            <Toggle
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
              label="Auto-approve & run"
            />
            {error && <span className="text-sm text-status-danger-fg">{error}</span>}
          </div>
        </form>

        {promptFiles.length > 0 && (
          <div className="mt-5 pt-5 border-t border-border-subtle">
            <p className="text-[11px] text-fg-faint mb-3 font-medium uppercase tracking-wider">
              Or load from prompt file
            </p>
            <div className="flex flex-wrap gap-2">
              {promptFiles.map((f) => (
                <Button
                  key={f.name}
                  variant="secondary"
                  size="sm"
                  onClick={() => submitFromFile(f.filename)}
                  disabled={loading}
                  title={f.first_line}
                >
                  {f.name}
                </Button>
              ))}
            </div>
          </div>
        )}
      </Card>

      <Card padded={false} className="overflow-hidden">
        <CardHeader title="History" />
        {requirements.length === 0 ? (
          <EmptyState title="No requirements submitted yet." />
        ) : (
          <div className="divide-y divide-border-subtle">
            {requirements.map((req) => (
              <div
                key={req.id}
                className="px-6 py-4 hover:bg-surface-2/50 cursor-pointer transition-colors duration-150"
                onClick={() => req.storypack_id && navigate(`/stories/${req.storypack_id}`)}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-mono text-fg-faint">{req.id}</p>
                    <p className="text-sm text-fg-secondary mt-1 line-clamp-2">
                      {req.text.slice(0, 200)}
                      {req.text.length > 200 && "..."}
                    </p>
                  </div>
                  <StatusBadge status={req.storypack_status} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
