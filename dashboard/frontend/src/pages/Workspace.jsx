import { useState, useEffect } from "react";
import { api } from "../api";

export default function Workspace() {
  const [tree, setTree] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileContent, setFileContent] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.getWorkspaceFiles().then(setTree).catch(console.error);
  }, []);

  const openFile = async (path) => {
    setSelectedFile(path);
    setLoading(true);
    try {
      const data = await api.getWorkspaceFile(path);
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
        <h2 className="text-xl font-semibold text-white">Workspace</h2>
        <p className="text-sm text-slate-500 mt-1">Browse generated source files</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 min-h-[450px]">
        <div className="bg-surface-1 rounded-xl border border-border overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b border-border-subtle">
            <h3 className="text-[13px] font-semibold text-slate-300">Files</h3>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {tree.length === 0 ? (
              <p className="px-2 py-8 text-sm text-slate-600 text-center">
                Workspace is empty.
              </p>
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
        </div>

        <div className="md:col-span-2 bg-surface-1 rounded-xl border border-border overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b border-border-subtle flex items-center justify-between">
            <h3 className="text-[13px] font-semibold font-mono text-slate-400 truncate">
              {selectedFile || "Select a file"}
            </h3>
            {fileContent && (
              <span className="text-[11px] text-slate-600 font-mono ml-2 shrink-0">
                {(fileContent.size / 1024).toFixed(1)} KB
              </span>
            )}
          </div>
          <div className="flex-1 overflow-hidden">
            {loading ? (
              <div className="p-6 flex items-center gap-2 text-sm text-slate-500">
                <span className="w-3.5 h-3.5 border-2 border-slate-600 border-t-slate-400 rounded-full animate-spin" />
                Loading...
              </div>
            ) : fileContent ? (
              <pre className="p-5 text-xs font-mono text-slate-400 overflow-auto h-full max-h-[520px] whitespace-pre-wrap leading-relaxed selection:bg-accent/20">
                {fileContent.content}
              </pre>
            ) : (
              <div className="p-6 flex items-center justify-center h-full">
                <p className="text-sm text-slate-600">
                  Click a file to view its contents.
                </p>
              </div>
            )}
          </div>
        </div>
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
          className="flex items-center gap-1.5 px-2 py-1 rounded-md cursor-pointer hover:bg-surface-3 text-[13px] transition-colors duration-100"
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
            className={`text-slate-600 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
          >
            <polyline points="4 2 8 6 4 10" />
          </svg>
          <span className="font-medium text-slate-400">{node.name}</span>
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
      className={`flex items-center gap-1.5 px-2 py-1 rounded-md cursor-pointer text-[13px] transition-all duration-100 ${
        isSelected
          ? "bg-accent-muted text-accent-hover"
          : "text-slate-500 hover:bg-surface-3 hover:text-slate-300"
      }`}
      style={{ paddingLeft: `${depth * 14 + 26}px` }}
      onClick={() => onSelect(node.path)}
    >
      {node.name}
    </div>
  );
}
