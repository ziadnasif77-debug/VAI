/**
 * The typed client for the local API (SPEC §57).
 *
 * One module, because every screen talks to the same backend and a fetch call
 * scattered through components is how a URL changes in six places and breaks in
 * a seventh.
 *
 * Errors carry the backend's `error_code` (§81). A UI that only has a message
 * cannot tell "the file is missing" from "the model is unavailable", and those
 * want different offers of help.
 */

const BASE = '/api';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: {'Content-Type': 'application/json'},
    ...init,
  });

  if (!response.ok) {
    // The API answers every failure with a typed body (§81); a proxy or a
    // crash might not, so the status is the fallback rather than a crash here.
    let code = `HTTP_${response.status}`;
    let message = response.statusText;
    try {
      const body = await response.json();
      code = body.error_code ?? code;
      message = body.message ?? body.detail ?? message;
    } catch {
      /* not JSON: keep the status line */
    }
    throw new ApiError(response.status, code, message);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {method: 'POST', body: body ? JSON.stringify(body) : undefined});
const patch = <T>(path: string, body: unknown) =>
  request<T>(path, {method: 'PATCH', body: JSON.stringify(body)});

// ---------------------------------------------------------------------------
// types — mirrors of the API's response models
// ---------------------------------------------------------------------------

export type VideoMode = 'story' | 'best_moments' | 'compilation';

export type Project = {
  id: string;
  name: string;
  status: string;
  mode: VideoMode;
  target_duration_seconds: number;
  /** Empty or "auto" means no profile: analysis runs on the generic path (§23). */
  game?: string | null;
  project_directory: string;
  created_at: string;
  updated_at: string;
};

/** A game profile as the import screen needs to list it (§22, §111). */
export type ProfileSummary = {
  id: string;
  name: string;
  description: string;
  generic: boolean;
  regions: number;
  ocr_regions: number;
  event_rules: number;
  hud_indicators: number;
};

export type Media = {
  id: string;
  project_id: string;
  filename: string;
  source_path: string;
  size_bytes: number;
  state: string;
  metadata: {
    duration_seconds?: number | null;
    width?: number | null;
    height?: number | null;
    fps?: number | null;
    audio_tracks?: number | null;
  };
};

export type JobStage =
  | 'import' | 'probe' | 'proxy' | 'audio' | 'frames'
  | 'transcript' | 'audio_events' | 'scenes' | 'vision' | 'ocr'
  | 'game_events' | 'moments' | 'story' | 'edl' | 'critique' | 'render' | 'qa'
  | 'export' | 'publish';

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export type Job = {
  id: string;
  stage: JobStage;
  status: JobStatus;
  progress: number;
  message: string | null;
  error_code: string | null;
  error_message: string | null;
  /** When the worker picked it up. The API has always sent this. */
  started_at: string | null;
  duration_seconds: number | null;
  result: Record<string, unknown>;
};

export type Moment = {
  id: string;
  media_id: string;
  moment_type: string;
  start_seconds: number;
  end_seconds: number;
  context_start: number;
  context_end: number;
  duration_seconds: number;
  score: number;
  confidence: number;
  score_breakdown: Record<string, number>;
  explanation: string[];
  needs_review: boolean;
  user_state: string;
};

export type Clip = {
  id: string;
  index: number;
  media_id: string;
  moment_id: string | null;
  moment_type: string | null;
  source_in: number;
  source_out: number;
  timeline_start: number;
  timeline_end: number;
  duration_seconds: number;
  enabled: boolean;
  role: string;
  score: number;
};

export type Timeline = {
  project_id: string;
  duration_seconds: number;
  clips: Clip[];
  captions: number;
  effects: number;
  valid: boolean;
  problems: string[];
};

export type TimelineOperation = {
  action: 'delete' | 'restore' | 'move' | 'split' | 'trim';
  clip_id: string;
  to_index?: number;
  at_seconds?: number;
  start_delta?: number;
  end_delta?: number;
};

export type QaFinding = {
  check: string;
  category: string;
  qa_status: 'passed' | 'warning' | 'failed';
  detail: string;
  remedy: string;
};

export type QaReport = {
  project_id: string;
  render_id: string | null;
  qa_status: 'passed' | 'warning' | 'failed';
  blocks_export: boolean;
  needs_review: boolean;
  findings: QaFinding[];
};

export type RenderStatus = {
  project_id: string;
  job: Job | null;
  latest: {
    id: string;
    status: string;
    output_path: string | null;
    duration_seconds: number | null;
    resolution: number | null;
    fps: number | null;
    encoder: string | null;
    size_bytes: number | null;
  } | null;
  blocked_by_qa: boolean;
};

export type HealthCheck = {
  name: string;
  status: 'ok' | 'warning' | 'failed' | 'skipped';
  required: boolean;
  detail: string;
  remediation: string | null;
};

export type Health = {
  status: string;
  application_version: string;
  hardware_profile: string;
  checks: HealthCheck[];
};

export type StageStatus = {
  stage: JobStage;
  /** Null when the stage has never been queued — §60's empty circle. */
  status: JobStatus | null;
  progress: number;
  error_code: string | null;
  updated_at: string | null;
};

export type ChatReply = {
  interaction_type: string;
  message: string;
  requires_rerender: boolean;
};

// ---------------------------------------------------------------------------
// endpoints
// ---------------------------------------------------------------------------

export const api = {
  health: () => get<Health>('/health'),

  projects: {
    list: () => get<{total: number; items: Project[]}>('/projects'),
    get: (id: string) => get<Project>(`/projects/${id}`),
    create: (body: {name: string; target_duration_seconds: number; mode: VideoMode}) =>
      post<Project>('/projects', body),
    update: (
      id: string,
      body: Partial<Pick<Project, 'name' | 'mode' | 'target_duration_seconds' | 'game'>>,
    ) =>
      patch<Project>(`/projects/${id}`, body),
    status: (id: string) => get<{project: Project; stages: StageStatus[]}>(`/projects/${id}/status`),
    analyze: (id: string) => post<{queued_stages: string[]}>(`/projects/${id}/analyze`),
    cancel: (id: string) => post<{cancelled_jobs: number}>(`/projects/${id}/cancel`),
  },

  profiles: {
    list: () => get<{generic: string; items: ProfileSummary[]}>('/profiles'),
  },

  system: {
    /** Open the OS file dialog on the machine the server runs on (§50). */
    pickFile: (initialDir?: string | null) =>
      post<{path: string | null}>('/system/pick-file', {initial_dir: initialDir ?? null}),
  },

  media: {
    list: (projectId: string) =>
      get<{total: number; items: Media[]}>(`/projects/${projectId}/media`),
    add: (projectId: string, path: string) =>
      post<Media>(`/projects/${projectId}/media`, {path}),
  },

  jobs: {
    list: (projectId: string) =>
      get<{total: number; items: Job[]}>(`/projects/${projectId}/jobs`),
  },

  moments: (projectId: string, filters?: {type?: string; minScore?: number}) => {
    const query = new URLSearchParams();
    if (filters?.type) query.set('type', filters.type);
    if (filters?.minScore) query.set('min_score', String(filters.minScore));
    const suffix = query.toString() ? `?${query}` : '';
    return get<{total: number; returned: number; by_type: Record<string, number>; items: Moment[]}>(
      `/projects/${projectId}/moments${suffix}`,
    );
  },

  timeline: {
    get: (projectId: string) => get<Timeline>(`/projects/${projectId}/timeline`),
    apply: (projectId: string, operation: TimelineOperation) =>
      post<Timeline>(`/projects/${projectId}/timeline/operations`, operation),
    regenerate: (projectId: string) =>
      post<{queued: string[]; message: string}>(`/projects/${projectId}/generate-edit`),
  },

  render: {
    start: (projectId: string) =>
      post<{queued: string[]; message: string}>(`/projects/${projectId}/render`),
    status: (projectId: string) => get<RenderStatus>(`/projects/${projectId}/render-status`),
    previewUrl: (projectId: string) => `${BASE}/projects/${projectId}/preview`,
  },

  qa: (projectId: string) => get<QaReport>(`/projects/${projectId}/qa`),

  chat: (projectId: string, text: string) =>
    post<ChatReply>(`/projects/${projectId}/chat`, {text}),
};

/** Seconds as `m:ss`, or `h:mm:ss` past an hour. */
export function timecode(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}
