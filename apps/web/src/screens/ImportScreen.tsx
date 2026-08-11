/**
 * Import (SPEC §59): video, target duration, mode.
 *
 * The file is chosen by **path**, not by upload. A browser file picker gives a
 * sandboxed handle, and this pipeline needs to read a three-gigabyte recording
 * from disk many times — proxy, audio, frames — so uploading it into the
 * project would copy gigabytes to reach a file already on the machine (§42
 * keeps originals in place by default).
 *
 * That is a genuine cost of running in a browser, and the honest fix is a
 * desktop shell later (the plan's open question), not an upload button that
 * would quietly duplicate every recording.
 */

import {useCallback, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Project, type VideoMode} from '../lib/api';

const MODES: ReadonlyArray<{value: VideoMode; label: string; blurb: string}> = [
  {value: 'story', label: 'Story', blurb: 'An arc: hook, build-up, climax, ending.'},
  {value: 'best_moments', label: 'Best moments', blurb: 'The strongest moments, types interleaved.'},
  {value: 'compilation', label: 'Compilation', blurb: 'Grouped by kind rather than by session.'},
];

export function ImportScreen({
  project,
  onChanged,
  onAnalyse,
}: {
  project: Project;
  onChanged: () => void;
  onAnalyse: () => void;
}) {
  const media = usePolling(() => api.media.list(project.id), {intervalMs: 4000});
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const add = useCallback(async () => {
    if (!path.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.media.add(project.id, path.trim());
      setPath('');
      media.refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }, [media, path, project.id]);

  const update = useCallback(
    async (changes: {mode?: VideoMode; target_duration_seconds?: number}) => {
      try {
        await api.projects.update(project.id, changes);
        onChanged();
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : String(failure));
      }
    },
    [onChanged, project.id],
  );

  const start = useCallback(async () => {
    setBusy(true);
    try {
      await api.projects.analyze(project.id);
      onAnalyse();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }, [onAnalyse, project.id]);

  const hasMedia = (media.data?.total ?? 0) > 0;

  return (
    <div className="screen">
      <section className="panel">
        <h2>Recordings</h2>
        <p className="muted">
          The file stays where it is; nothing is copied or modified (§42). Paste its full path.
        </p>
        <div className="row">
          <input
            type="text"
            placeholder="D:\Gaming 2026\2026-05-16 00-10-49.mkv"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && void add()}
          />
          <button type="button" onClick={() => void add()} disabled={busy || !path.trim()}>
            Add
          </button>
        </div>
        {error && <p className="error">{error}</p>}

        <ul className="list">
          {media.data?.items.map((item) => (
            <li key={item.id} className="list-row">
              <span className="list-title">{item.filename}</span>
              <span className="muted">
                {item.metadata.duration_seconds
                  ? timecode(item.metadata.duration_seconds)
                  : 'not probed yet'}
                {item.metadata.width ? ` · ${item.metadata.width}×${item.metadata.height}` : ''}
                {item.metadata.fps ? ` @ ${Math.round(item.metadata.fps)}` : ''}
                {' · '}
                {(item.size_bytes / 1e9).toFixed(2)} GB
              </span>
            </li>
          ))}
        </ul>
        {!hasMedia && <p className="muted">No recordings attached yet.</p>}
      </section>

      <section className="panel">
        <h2>What to make</h2>
        <div className="row">
          <label className="inline">
            Target length
            <select
              value={project.target_duration_seconds}
              onChange={(event) =>
                void update({target_duration_seconds: Number(event.target.value)})
              }
            >
              {[10, 15, 20, 25, 30, 40, 45, 50, 60].map((minutes) => (
                <option key={minutes} value={minutes * 60}>
                  {minutes} min
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="modes">
          {MODES.map((mode) => (
            <button
              key={mode.value}
              type="button"
              className={project.mode === mode.value ? 'mode active' : 'mode'}
              onClick={() => void update({mode: mode.value})}
            >
              <strong>{mode.label}</strong>
              <span className="muted">{mode.blurb}</span>
            </button>
          ))}
        </div>
        <p className="muted">
          Both can be changed later: re-editing costs seconds and never re-analyses the
          source (§127).
        </p>
      </section>

      <section className="panel">
        <button
          type="button"
          className="primary"
          disabled={!hasMedia || busy}
          onClick={() => void start()}
        >
          Analyse
        </button>
        {!hasMedia && <p className="muted">Add a recording first.</p>}
      </section>
    </div>
  );
}
