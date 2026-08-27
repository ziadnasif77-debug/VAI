/**
 * Timeline (SPEC §62, §78): remove, restore, move, split, trim.
 *
 * Every button here sends one operation and redraws from a fresh fetch, because
 * an edit re-flows the clips after it — a screen that patched the row it
 * changed would immediately be showing the wrong positions for everything else.
 *
 * "Remove" is a toggle, not a deletion. §78 gives the user the last word, and a
 * removed clip stays visible, greyed and struck through, so putting it back is
 * a click rather than a re-analysis. That is also the honest picture of what
 * the backend did: nothing was deleted (§42).
 *
 * Trim buttons speak the backend's own sign convention: positive `start_delta`
 * starts later, positive `end_delta` ends later. The buttons stay enabled even
 * when a trim looks doomed, because the backend refuses impossible edits with a
 * sentence written for humans — "would leave 0.2s; the minimum is 0.3s" — and
 * that sentence, shown verbatim on the row that asked, beats a greyed-out
 * button nobody can interrogate.
 *
 * There is no Undo button on purpose. The API lists edit versions (§19) but
 * exposes no revert route, and a button that cannot keep its promise is worse
 * than none; meanwhile Remove/Restore already makes the common mistakes cheap.
 */

import {useCallback, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Clip, type Project, type TimelineOperation} from '../lib/api';

/** A refused edit: the backend's message, pinned to the row that asked for it. */
type Refusal = {clipId: string | null; message: string};

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
  const [refusal, setRefusal] = useState<Refusal | null>(null);

  const fail = useCallback((clipId: string | null, failure: unknown) => {
    setRefusal({
      clipId,
      message: failure instanceof Error ? failure.message : String(failure),
    });
  }, []);

  const apply = useCallback(
    async (operation: TimelineOperation) => {
      setBusy(true);
      setRefusal(null);
      try {
        await api.timeline.apply(project.id, operation);
        timeline.refresh();
      } catch (failure) {
        // A refused edit is the user asking for something impossible; say
        // which, where they are looking, rather than reverting silently (§78).
        fail(operation.clip_id, failure);
      } finally {
        setBusy(false);
      }
    },
    [fail, project.id, timeline],
  );

  const regenerate = useCallback(async () => {
    setBusy(true);
    setRefusal(null);
    try {
      await api.timeline.regenerate(project.id);
      timeline.refresh();
    } catch (failure) {
      fail(null, failure);
    } finally {
      setBusy(false);
    }
  }, [fail, project.id, timeline]);

  const render = useCallback(async () => {
    setBusy(true);
    setRefusal(null);
    try {
      await api.render.start(project.id);
      onRender();
    } catch (failure) {
      fail(null, failure);
    } finally {
      setBusy(false);
    }
  }, [fail, onRender, project.id]);

  const clips = timeline.data?.clips ?? [];
  const enabled = clips.filter((clip) => clip.enabled);
  const total = timeline.data?.duration_seconds ?? 0;

  // A refusal renders inside its clip's row; anything without a row — a
  // rebuild failure, or a clip the last refresh removed — falls back to the
  // panel so no refusal is ever silently dropped.
  const panelRefusal =
    refusal && (refusal.clipId === null || !clips.some((clip) => clip.id === refusal.clipId))
      ? refusal.message
      : null;

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

        {panelRefusal && <p className="error">{panelRefusal}</p>}

        <ol className="clips">
          {clips.map((clip, position) => (
            <ClipRow
              key={clip.id}
              clip={clip}
              position={position}
              last={position === clips.length - 1}
              busy={busy}
              refusal={refusal?.clipId === clip.id ? refusal.message : null}
              onApply={apply}
            />
          ))}
        </ol>
      </section>

      <section className="panel">
        <div className="row">
          <button
            type="button"
            onClick={() => void (async () => {
              setBusy(true);
              setRefusal(null);
              try {
                await api.timeline.revert(project.id);
                timeline.refresh();
              } catch (failure) {
                fail(null, failure);
              } finally {
                setBusy(false);
              }
            })()}
            disabled={busy}
          >
            Undo last edit
          </button>
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
  refusal,
  onApply,
}: {
  clip: Clip;
  position: number;
  last: boolean;
  busy: boolean;
  refusal: string | null;
  onApply: (operation: TimelineOperation) => void;
}) {
  // Split takes a *timeline* position (the one the user can see); the midpoint
  // is the one split that never needs a scrubber to pick.
  const middle = (clip.timeline_start + clip.timeline_end) / 2;
  const still = busy || !clip.enabled;

  return (
    <li className={clip.enabled ? 'clip' : 'clip disabled'}>
      <span className="clip-index mono">{position + 1}</span>
      <span className="clip-body">
        <span className="clip-title">
          {clip.moment_type ?? 'clip'}
          {clip.role !== 'body' && <span className="badge">{clip.role}</span>}
          {!clip.enabled && (
            <span className="badge" title="Still here, just not rendered — Restore puts it back (§78)">
              removed
            </span>
          )}
        </span>
        <span className="muted mono">
          source {timecode(clip.source_in)}–{timecode(clip.source_out)}
          {/* A removed clip contributes no time, so its stored position is a
              placeholder; printing it would be the screen inventing a fact. */}
          {clip.enabled && (
            <> · at {timecode(clip.timeline_start)}–{timecode(clip.timeline_end)}</>
          )}
          {' · '}{clip.duration_seconds.toFixed(1)}s
        </span>
        {refusal && <span className="error clip-error">{refusal}</span>}
      </span>
      <span className="clip-actions">
        {clip.enabled ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onApply({action: 'delete', clip_id: clip.id})}
            title="Disable, don't delete — the footage stays (§78)"
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
          disabled={still}
          onClick={() => onApply({action: 'split', clip_id: clip.id, at_seconds: middle})}
          title="Split in the middle"
        >
          Split
        </button>
        {/* The ±1s buttons are the API's own deltas (§42): positive start_delta
            starts later, positive end_delta ends later. "Too short" and "before
            the file" are the backend's calls, and its refusals arrive written
            for humans — so no button second-guesses them by greying out. */}
        <span className="trim">
          <span className="trim-label">start</span>
          <button
            type="button"
            disabled={still}
            onClick={() => onApply({action: 'trim', clip_id: clip.id, start_delta: -1})}
            title="Start a second earlier — pulls in footage before the cut"
          >
            −1s
          </button>
          <button
            type="button"
            disabled={still}
            onClick={() => onApply({action: 'trim', clip_id: clip.id, start_delta: 1})}
            title="Start a second later"
          >
            +1s
          </button>
        </span>
        <span className="trim">
          <span className="trim-label">end</span>
          <button
            type="button"
            disabled={still}
            onClick={() => onApply({action: 'trim', clip_id: clip.id, end_delta: -1})}
            title="End a second earlier"
          >
            −1s
          </button>
          <button
            type="button"
            disabled={still}
            onClick={() => onApply({action: 'trim', clip_id: clip.id, end_delta: 1})}
            title="End a second later — pulls in footage after the cut"
          >
            +1s
          </button>
        </span>
      </span>
    </li>
  );
}
