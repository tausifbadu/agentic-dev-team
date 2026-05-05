const BASE = "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  listProjects: () => request("/projects"),

  createProject: (slug) =>
    request("/projects", { method: "POST", body: JSON.stringify({ slug }) }),

  submitRequirement: (text, autoApprove = false, options = {}) =>
    request("/requirements", {
      method: "POST",
      body: JSON.stringify({
        text,
        auto_approve: autoApprove,
        ...options,
      }),
    }),

  submitFromFile: (filename, autoApprove = false, options = {}) =>
    request("/requirements/from-file", {
      method: "POST",
      body: JSON.stringify({
        filename,
        auto_approve: autoApprove,
        ...options,
      }),
    }),

  listRequirements: () => request("/requirements"),

  getRequirement: (id) => request(`/requirements/${id}`),

  listPromptFiles: () => request("/prompt-files"),

  listStorypacks: () => request("/storypacks"),

  getStorypack: (id) => request(`/storypacks/${id}`),

  getStories: (packId) => request(`/storypacks/${packId}/stories`),

  approveStorypack: (id, options = {}) =>
    request(`/storypacks/${id}/approve`, {
      method: "POST",
      body: JSON.stringify(Object.keys(options).length ? options : {}),
    }),

  rejectStorypack: (id) =>
    request(`/storypacks/${id}/reject`, { method: "POST" }),

  getAgentStatus: () => request("/agents/status"),

  getAgentMetrics: () => request("/agents/metrics"),

  getAgentLogs: (limit = 100) => request(`/agents/logs?limit=${limit}`),

  getStoryLogs: (storyId) => request(`/agents/logs/${storyId}`),

  getTestResults: (packId) =>
    request(`/tests/results${packId ? `?storypack_id=${packId}` : ""}`),

  getWorkspaceFiles: (projectId) =>
    request(
      `/workspace/files${projectId && projectId !== "default" ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),

  getWorkspaceFile: (path, projectId) =>
    request(
      `/workspace/file?path=${encodeURIComponent(path)}${
        projectId && projectId !== "default" ? `&project_id=${encodeURIComponent(projectId)}` : ""
      }`,
    ),

  submitFix: ({ storypack_id, story_id, agent_type, error_text, user_instructions }) =>
    request("/agents/fix", {
      method: "POST",
      body: JSON.stringify({ storypack_id, story_id, agent_type, error_text, user_instructions }),
    }),

  getFixStatus: () => request("/agents/fix/status"),

  listFixes: (storypackId) =>
    request(`/agents/fixes${storypackId ? `?storypack_id=${storypackId}` : ""}`),

  getFix: (fixId) => request(`/agents/fixes/${fixId}`),

  getComms: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.storypack_id) params.set("storypack_id", filters.storypack_id);
    if (filters.run_id) params.set("run_id", filters.run_id);
    if (filters.event_type) params.set("event_type", filters.event_type);
    if (filters.from_agent) params.set("from_agent", filters.from_agent);
    if (filters.to_agent) params.set("to_agent", filters.to_agent);
    if (filters.limit) params.set("limit", filters.limit);
    const qs = params.toString();
    return request(`/comms${qs ? `?${qs}` : ""}`);
  },

  getCommTimeline: (packId) => request(`/comms/timeline/${packId}`),

  getContracts: () => request("/comms/contracts"),

  getLearningPatterns: (limit = 20) => request(`/comms/learning?limit=${limit}`),

  getToolCalls: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.storypack_id) params.set("storypack_id", filters.storypack_id);
    if (filters.run_id) params.set("run_id", filters.run_id);
    if (filters.agent_id) params.set("agent_id", filters.agent_id);
    if (filters.story_id) params.set("story_id", filters.story_id);
    if (filters.limit) params.set("limit", filters.limit);
    const qs = params.toString();
    return request(`/comms/tool_calls${qs ? `?${qs}` : ""}`);
  },

  getToolCallSummary: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.storypack_id) params.set("storypack_id", filters.storypack_id);
    if (filters.run_id) params.set("run_id", filters.run_id);
    const qs = params.toString();
    return request(`/comms/tool_calls/summary${qs ? `?${qs}` : ""}`);
  },

  getInbox: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.to_agent) params.set("to_agent", filters.to_agent);
    if (filters.run_id) params.set("run_id", filters.run_id);
    if (filters.status) params.set("status", filters.status);
    if (filters.limit) params.set("limit", filters.limit);
    const qs = params.toString();
    return request(`/comms/inbox${qs ? `?${qs}` : ""}`);
  },

  submitEnhancement: ({ agent_type, description, context, project_id = "default" }) =>
    request("/enhance", {
      method: "POST",
      body: JSON.stringify({ agent_type, description, context, project_id }),
    }),

  getEnhanceStatus: () => request("/enhance/status"),

  listEnhancements: (limit = 50) => request(`/enhancements?limit=${limit}`),

  getEnhancement: (id) => request(`/enhancements/${id}`),

  rollbackEnhancement: (id) =>
    request(`/enhancements/${id}/rollback`, { method: "POST" }),
};
