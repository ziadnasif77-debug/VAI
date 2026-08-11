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

import {useCallback, useState} from 'react';

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
  {label: 'Render', stages: ['render', 'qa']},
];

function mark(status: JobStatus | null): string {
  if (status === 'completed') return '✓';
  if (status === 'running') return '●';
  if (status === 'failed') return '✕';
  if (status === 'cancelled') return '⊘';
  return '○';
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
            const progress =
              stages.length > 0
                ? stages.reduce((total, stage) => total + stage.progress, 0) / stages.length
                : 0;
            return (
              <li key={group.label} className={`stage stage-${state ?? 'pending'}`}>
                <span className="stage-mark">{mark(state)}</span>
                <span className="stage-name">{group.label}</span>
                {state === 'running' && (
                  <span className="stage-bar">
                    <span className="stage-fill" style={{width: `${Math.round(progress * 100)}%`}} />
                  </span>
                )}
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
