import { useState, useEffect } from "react";
import { api } from "../api";
import { PageHeader, Card, CardHeader, Field, Select, Spinner, EmptyState } from "../components/ui";

export default function Workspace() {
  const [tree, setTree] = useState([]);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState("default");
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileContent, setFileContent] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.listProjects().then((r) => setProjects(r.projects || [])).catch(console.error);
  }, []);

  useEffect(() => {
    api.getWorkspaceFiles(projectId).then(setTree).catch(console.error);
    setSelectedFile(null);
    setFileContent(null);
  }, [projectId]);

  const openFile = async (path) => {
    setSelectedFile(path);
    setLoading(true);
    try {
      const data = await api.getWorkspaceFile(path, projectId);
      setFileContent(data);
    } catch (err) {
      setFileContent({ path, content: `Error: ${err.message}`, size: 0 });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <PageHeader title="Workspace" subtitle="Browse generated source files" />
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Field label="Project" htmlFor="ws-project" className="flex items-center gap-3 [&>label]:mb-0">
            <Select
              id="ws-project"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="min-w-[200px]"
            >
              {(projects.length ? projects : [{ id: "default" }]).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id === "default" ? "default (workspace/)" : p.id}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 min-h-[450px]">
        <Card padded={false} className="overflow-hidden flex flex-col">
          <CardHeader title="Files" />
          <div className="flex-1 overflow-y-auto p-2">
            {tree.length === 0 ? (
              <EmptyState title="Workspace is empty." />
            ) : (
              tree.map((node) => (
                <TreeNode
                  key={node.path}
                  node={node}
                  onSelect={openFile}
                  selected={selectedFile}
                  depth={0}
                />
              ))
            )}
          </div>
        </Card>

        <Card padded={false} className="md:col-span-2 overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b border-border-subtle flex items-center justify-between">
            <h3 className="text-[13px] font-semibold font-mono text-fg-muted truncate">
              {selectedFile || "Select a file"}
            </h3>
            {fileContent && (
              <span className="text-[11px] text-fg-faint font-mono ml-2 shrink-0">
                {(fileContent.size / 1024).toFixed(1)} KB
              </span>
            )}
          </div>
          <div className="flex-1 overflow-hidden">
            {loading ? (
              <div className="p-6 flex items-center gap-2 text-sm text-fg-faint">
                <Spinner size="sm" />
                Loading...
              </div>
            ) : fileContent ? (
              <pre className="p-5 text-xs font-mono text-fg-muted overflow-auto h-full max-h-[520px] whitespace-pre-wrap leading-relaxed selection:bg-accent/20">
                {fileContent.content}
              </pre>
            ) : (
              <EmptyState title="Click a file to view its contents." className="h-full" />
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

function TreeNode({ node, onSelect, selected, depth }) {
  const [open, setOpen] = useState(depth < 2);
  const isDir = node.type === "directory";
  const isSelected = selected === node.path;

  if (isDir) {
    return (
      <div>
        <div
          className="flex items-center gap-1.5 px-2 py-1 rounded-md cursor-pointer hover:bg-surface-3 text-[13px] transition-colors duration-150"
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          onClick={() => setOpen(!open)}
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className={`text-fg-faint transition-transform duration-150 ${open ? "rotate-90" : ""}`}
          >
            <polyline points="4 2 8 6 4 10" />
          </svg>
          <span className="font-medium text-fg-muted">{node.name}</span>
        </div>
        {open &&
          node.children?.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              onSelect={onSelect}
              selected={selected}
              depth={depth + 1}
            />
          ))}
      </div>
    );
  }

  return (
    <div
      className={`flex items-center gap-1.5 px-2 py-1 rounded-md cursor-pointer text-[13px] transition-all duration-150 ${
        isSelected
          ? "bg-accent-muted text-accent-hover"
          : "text-fg-faint hover:bg-surface-3 hover:text-fg-secondary"
      }`}
      style={{ paddingLeft: `${depth * 14 + 26}px` }}
      onClick={() => onSelect(node.path)}
    >
      {node.name}
    </div>
  );
}
