/**
 * Timeline (SPEC §62, §78): remove, restore, move, split, trim.
 *
 * Every button here sends one operation and redraws from the response, because
 * an edit re-flows the clips after it — a screen that patched the row it
 * changed would immediately be showing the wrong positions for everything else.
 *
 * "Remove" is a toggle, not a deletion. §78 gives the user the last word, and a
 * removed clip stays visible and greyed so putting it back is a click rather
 * than a re-analysis. That is also the honest picture of what the backend did:
 * nothing was deleted.
 */

import {useCallback, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Clip, type Project, type TimelineOperation} from '../lib/api';

export function TimelineScreen({
  project,
  onRender,
}: {
  project: Project;
  onRender: () => void;
}) {
  const timeline = usePolling(() => api.timeline.get(project.id), {
    intervalMs: 8000,
    active: (value) => (value?.clips.length ?? 0) === 0,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback(
    async (operation: TimelineOperation) => {
      setBusy(true);
      setError(null);
      try {
        await api.timeline.apply(project.id, operation);
        timeline.refresh();
      } catch (failure) {
        // A refused edit is the user asking for something impossible; say
        // which, rather than reverting silently.
        setError(failure instanceof Error ? failure.message : String(failure));
      } finally {
        setBusy(false);
      }
    },
    [project.id, timeline],
  );

  const regenerate = useCallback(async () => {
    setBusy(true);
    try {
      await api.timeline.regenerate(project.id);
      timeline.refresh();
    } finally {
      setBusy(false);
    }
  }, [project.id, timeline]);

  const render = useCallback(async () => {
    setBusy(true);
    try {
      await api.render.start(project.id);
      onRender();
    } finally {
      setBusy(false);
    }
  }, [onRender, project.id]);

  const clips = timeline.data?.clips ?? [];
  const enabled = clips.filter((clip) => clip.enabled);
  const total = timeline.data?.duration_seconds ?? 0;

  return (
    <div className="screen">
      <section className="panel">
        <h2>
          Timeline <span className="muted">{timecode(total)} · {enabled.length} clips</span>
        </h2>

        {timeline.data && !timeline.data.valid && (
          <div className="warning">
            <strong>This edit cannot be rendered yet.</strong>
            <ul>
              {timeline.data.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        )}

        {clips.length === 0 && (
          <p className="muted">
            No edit yet. Run the analysis, or rebuild the edit from stored moments below.
          </p>
        )}

        {/* A proportional strip, so "one clip is a fifth of the video" is
            visible rather than something to work out from numbers. */}
        {enabled.length > 0 && (
          <div className="strip">
            {enabled.map((clip) => (
              <span
                key={clip.id}
                className={`strip-clip role-${clip.role}`}
                style={{flexGrow: clip.duration_seconds}}
                title={`${clip.moment_type ?? 'clip'} · ${clip.duration_seconds.toFixed(0)}s`}
              />
            ))}
          </div>
        )}

        {error && <p className="error">{error}</p>}

        <ol className="clips">
          {clips.map((clip, position) => (
            <ClipRow
              key={clip.id}
              clip={clip}
              position={position}
              last={position === clips.length - 1}
              busy={busy}
              onApply={apply}
            />
          ))}
        </ol>
      </section>

      <section className="panel">
        <div className="row">
          <button type="button" onClick={() => void regenerate()} disabled={busy}>
            Rebuild edit
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => void render()}
            disabled={busy || enabled.length === 0 || timeline.data?.valid === false}
          >
            Render
          </button>
        </div>
        <p className="muted">
          Rebuilding re-runs the story and EDL stages against stored moments — seconds,
          not another analysis (§127).
        </p>
      </section>
    </div>
  );
}

function ClipRow({
  clip,
  position,
  last,
  busy,
  onApply,
}: {
  clip: Clip;
  position: number;
  last: boolean;
  busy: boolean;
  onApply: (operation: TimelineOperation) => void;
}) {
  const middle = (clip.timeline_start + clip.timeline_end) / 2;

  return (
    <li className={clip.enabled ? 'clip' : 'clip disabled'}>
      <span className="clip-index mono">{position + 1}</span>
      <span className="clip-body">
        <span className="clip-title">
          {clip.moment_type ?? 'clip'}
          {clip.role !== 'body' && <span className="badge">{clip.role}</span>}
        </span>
        <span className="muted mono">
          source {timecode(clip.source_in)}–{timecode(clip.source_out)} · at{' '}
          {timecode(clip.timeline_start)} · {clip.duration_seconds.toFixed(0)}s
        </span>
      </span>
      <span className="clip-actions">
        {clip.enabled ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onApply({action: 'delete', clip_id: clip.id})}
          >
            Remove
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => onApply({action: 'restore', clip_id: clip.id})}
          >
            Restore
          </button>
        )}
        <button
          type="button"
          disabled={busy || position === 0}
          onClick={() => onApply({action: 'move', clip_id: clip.id, to_index: position - 1})}
          title="Move earlier"
        >
          ↑
        </button>
        <button
          type="button"
          disabled={busy || last}
          onClick={() => onApply({action: 'move', clip_id: clip.id, to_index: position + 1})}
          title="Move later"
        >
          ↓
        </button>
        <button
          type="button"
          disabled={busy || !clip.enabled}
          onClick={() => onApply({action: 'split', clip_id: clip.id, at_seconds: middle})}
          title="Split in the middle"
        >
          Split
        </button>
        <button
          type="button"
          disabled={busy || !clip.enabled}
          onClick={() => onApply({action: 'trim', clip_id: clip.id, end_delta: -1})}
          title="One second shorter"
        >
          −1s
        </button>
      </span>
    </li>
  );
}
