/**
 * Analysis (SPEC §60): the live pipeline.
 *
 * §60 draws it exactly as it should look — `✓ Media ✓ Audio ✓ Speech ✓ Scenes
 * ● Gaming Events ○ Moments ○ Story ○ Render` — and the reason that shape works
 * is that it distinguishes three states, not two. A stage that has never run is
 * not the same as one that is running, and an interface showing only "done /
 * not done" makes a twenty-minute analysis look identical to a stalled one.
 *
 * The failure case gets the same care. §81 gives every stage a typed error, and
 * a failed stage here shows what failed and what to do, rather than a red mark.
 */

import {useCallback, useEffect, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, type JobStatus, type Project, type StageStatus} from '../lib/api';

/** §60's grouping: the pipeline has more stages than a person wants to read. */
const GROUPS: ReadonlyArray<{label: string; stages: string[]}> = [
  {label: 'Media', stages: ['import', 'probe', 'proxy', 'audio', 'frames']},
  {label: 'Speech', stages: ['transcript']},
  {label: 'Audio', stages: ['audio_events']},
  {label: 'Scenes', stages: ['scenes', 'vision']},
  {label: 'Gaming events', stages: ['ocr', 'game_events']},
  {label: 'Moments', stages: ['moments']},
  {label: 'Story', stages: ['story', 'edl']},
  // The Critic reads the assembled edit and may trim it before anything is
  // encoded (Phase E). Its own row: it is the one stage whose progress the
  // user may want to read afterwards, because it can change the video.
  {label: 'Review', stages: ['critique']},
  // Render and QA are their own rows rather than one "Render" group. Grouping
  // them averaged a stage that takes half an hour with one that takes seconds,
  // so a render at 59% displayed as 30% -- and a number that low, not moving,
  // reads as a stall. §60 lists them together; §60 was not watching a real
  // encode at the time.
  {label: 'Render', stages: ['render']},
  {label: 'Checks', stages: ['qa']},
];

/** How long a stage has been running, as "6m 12s". */
function elapsed(startedAt: string | null | undefined, now: number): string | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return null;
  const seconds = Math.max(0, Math.round((now - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
}

function mark(status: JobStatus | null): string {
  if (status === 'completed') return '✓';
  if (status === 'running') return '●';
  if (status === 'failed') return '✕';
  if (status === 'cancelled') return '⊘';
  return '○';
}

/**
 * How far a group has actually got, 0-1.
 *
 * A completed stage counts as done whatever its last reported progress was:
 * several stages finish without ever reporting 100%, and a group showing "83%"
 * beside a tick reads as a stall rather than a success. Averaging across the
 * group's stages means a two-stage group sits at 50% between them, which is
 * the truth — half the work is left.
 */
function groupProgress(stages: StageStatus[]): number {
  if (stages.length === 0) return 0;
  const total = stages.reduce(
    (sum, stage) => sum + (stage.status === 'completed' ? 1 : stage.progress),
    0,
  );
  return total / stages.length;
}

/** A group is as far along as its least advanced stage. */
function groupStatus(stages: StageStatus[]): JobStatus | null {
  if (stages.some((stage) => stage.status === 'failed')) return 'failed';
  if (stages.some((stage) => stage.status === 'running')) return 'running';
  if (stages.length > 0 && stages.every((stage) => stage.status === 'completed')) {
    return 'completed';
  }
  if (stages.some((stage) => stage.status === 'queued')) return 'queued';
  return null;
}

export function AnalysisScreen({project, onDone}: {project: Project; onDone: () => void}) {
  const [busy, setBusy] = useState(false);
  // A ticking clock, so "running for 6m 12s" keeps counting while a long
  // encode reports nothing. The whole point is that a frozen number should
  // not be the only thing on screen.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const status = usePolling(() => api.projects.status(project.id), {
    intervalMs: 1500,
    // Stop once nothing is moving: a local app should not spin while idle.
    active: (value) =>
      !value ||
      value.stages.some((stage) => stage.status === 'running' || stage.status === 'queued'),
  });
  const jobs = usePolling(() => api.jobs.list(project.id), {
    intervalMs: 3000,
    active: (value) =>
      !value || value.items.some((job) => job.status === 'running' || job.status === 'queued'),
  });

  const byStage = new Map((status.data?.stages ?? []).map((stage) => [stage.stage, stage]));
  const failed = (jobs.data?.items ?? []).filter((job) => job.status === 'failed');
  // A stage that completed by doing nothing, and said why. Without this row
  // a pipeline could show every mark green and no video behind it.
  const skipped = (jobs.data?.items ?? []).filter(
    (job) => job.status === 'completed' && job.result?.skipped === true,
  );
  const running = (jobs.data?.items ?? []).find((job) => job.status === 'running');
  const momentsDone = byStage.get('moments')?.status === 'completed';

  const cancel = useCallback(async () => {
    setBusy(true);
    try {
      await api.projects.cancel(project.id);
      status.refresh();
      jobs.refresh();
    } finally {
      setBusy(false);
    }
  }, [jobs, project.id, status]);

  return (
    <div className="screen">
      <section className="panel">
        <h2>Pipeline</h2>
        <ol className="pipeline">
          {GROUPS.map((group) => {
            const stages = group.stages
              .map((name) => byStage.get(name as StageStatus['stage']))
              .filter((stage): stage is StageStatus => Boolean(stage));
            const state = groupStatus(stages);
            const progress = groupProgress(stages);
            const percent = Math.round(progress * 100);
            const active = (jobs.data?.items ?? []).find(
              (job) => job.status === 'running' && group.stages.includes(job.stage),
            );
            const age = state === 'running' ? elapsed(active?.started_at, now) : null;
            return (
              <li key={group.label} className={`stage stage-${state ?? 'pending'}`}>
                <span className="stage-mark">{mark(state)}</span>
                <span className="stage-name">{group.label}</span>
                {/* The bar is drawn for every stage, not only the running one:
                    a row that gains a bar the moment it starts makes the list
                    jump, and a stage that failed at 40% should still show where
                    it got to. */}
                <span className="stage-bar">
                  <span className="stage-fill" style={{width: `${percent}%`}} />
                </span>
                <span className="stage-percent">
                  {percent}%
                  {age && <em className="stage-age">{age}</em>}
                </span>
              </li>
            );
          })}
        </ol>

        {running && (
          <p className="muted">
            {running.stage}: {running.message ?? 'working…'}
          </p>
        )}

        <div className="row">
          <button type="button" onClick={() => void cancel()} disabled={busy || !running}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!momentsDone} onClick={onDone}>
            Review moments
          </button>
        </div>
      </section>

      {skipped.length > 0 && (
        <section className="panel">
          <h2>Skipped</h2>
          <ul className="list">
            {skipped.map((job) => (
              <li key={job.id} className="list-row">
                <span className="list-title">{job.stage}</span>
                <span className="muted">
                  {typeof job.result?.reason === 'string'
                    ? job.result.reason
                    : 'skipped without a recorded reason'}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {failed.length > 0 && (
        <section className="panel">
          <h2>Failures</h2>
          {/* §81: a typed error, and what it means, rather than a red mark. */}
          <ul className="list">
            {failed.map((job) => (
              <li key={job.id} className="list-row">
                <span className="list-title">{job.stage}</span>
                <span className="error">{job.error_message ?? job.error_code}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
