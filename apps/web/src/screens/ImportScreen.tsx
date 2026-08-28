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
import {api, ApiError, timecode, type Project, type VideoMode} from '../lib/api';

/** Where the picker last found a recording; the dialog reopens there. */
const LAST_DIR_KEY = 'vai.recordings.lastDir';

/** The value that means "no profile"; the backend treats it as generic (§23). */
const NO_GAME = 'auto';

const MODES: ReadonlyArray<{value: VideoMode; label: string; blurb: string}> = [
  {value: 'story', label: 'Story', blurb: 'An arc: hook, build-up, climax, ending.'},
  {value: 'best_moments', label: 'Best moments', blurb: 'The strongest moments, types interleaved.'},
  {value: 'compilation', label: 'Compilation', blurb: 'Grouped by kind rather than by session.'},
];

/** What naming a game buys, in the terms the profile itself declares (§22). */
function describeProfile(
  game: string | null | undefined,
  items: ReadonlyArray<{id: string; event_rules: number; hud_indicators: number}> | undefined,
): string {
  if (!game || game === NO_GAME) {
    return 'Vision, OCR, audio and speech only. Nothing about a specific game is assumed.';
  }
  const profile = items?.find((item) => item.id === game);
  if (!profile) {
    return 'No profile for this game yet; analysis falls back to the generic path.';
  }
  const parts: string[] = [];
  if (profile.event_rules) parts.push(plural(profile.event_rules, 'text rule'));
  if (profile.hud_indicators) parts.push(plural(profile.hud_indicators, 'HUD indicator'));
  return parts.length ? `Adds ${parts.join(' and ')}.` : 'This profile declares nothing yet.';
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

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
  // Profiles ship with the code and cannot change while the page is open, so
  // `active: false` fetches once and stops. `intervalMs: 0` would busy-loop.
  const profiles = usePolling(() => api.profiles.list(), {
    intervalMs: 60_000,
    active: () => false,
  });
  const [path, setPath] = useState('');
  const [outputDir, setOutputDir] = useState(project.output_directory ?? '');
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  // Hidden after a 501: a headless or Tk-less machine cannot show a dialog,
  // and a button that always errors is worse than no button (§95).
  const [pickerAvailable, setPickerAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const browse = useCallback(async () => {
    setPicking(true);
    setError(null);
    try {
      const chosen = await api.system.pickFile(window.localStorage.getItem(LAST_DIR_KEY));
      if (chosen.path) {
        setPath(chosen.path);
        const cut = Math.max(chosen.path.lastIndexOf('\\'), chosen.path.lastIndexOf('/'));
        if (cut > 0) window.localStorage.setItem(LAST_DIR_KEY, chosen.path.slice(0, cut));
      }
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === 'FILE_PICKER_UNAVAILABLE') {
        setPickerAvailable(false);
      }
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setPicking(false);
    }
  }, []);

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
    async (changes: {
      mode?: VideoMode;
      target_duration_seconds?: number;
      game?: string;
      captions_enabled?: boolean;
      output_directory?: string;
      auto_publish?: boolean;
    }) => {
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
          {pickerAvailable && (
            <button type="button" onClick={() => void browse()} disabled={picking || busy}>
              {picking ? 'Choosing…' : 'Browse…'}
            </button>
          )}
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
        <div className="row">
          <label className="inline">
            Game
            <select
              value={project.game || NO_GAME}
              onChange={(event) => void update({game: event.target.value})}
            >
              <option value={NO_GAME}>No profile — generic analysis</option>
              {(profiles.data?.items ?? [])
                .filter((profile) => !profile.generic)
                .map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                  </option>
                ))}
            </select>
          </label>
          <span className="muted">
            {describeProfile(project.game, profiles.data?.items)}
          </span>
        </div>
        <p className="muted">
          All three can be changed later: re-editing costs seconds and never re-analyses
          the source (§127). The game is the exception — it decides what the analysis
          looks for, so changing it after analysing means analysing again.
        </p>
      </section>

      <section className="panel">
        <h2>Delivery</h2>
        <label className="inline">
          <input
            type="checkbox"
            checked={Boolean(project.captions_enabled)}
            onChange={(event) => void update({captions_enabled: event.target.checked})}
          />
          Write captions inside the video
        </label>
        <p className="muted">
          Checked, the model transcribes the speech and burns it into the frame — the long
          video and the Shorts both. Unchecked, the speech is still analysed for the edit,
          but nothing is written on the picture.
        </p>
        <div className="row">
          <label className="inline" style={{flex: 1}}>
            Output folder
            <input
              type="text"
              placeholder="D:\Videos\VAI — empty keeps the file in the project only"
              value={outputDir}
              onChange={(event) => setOutputDir(event.target.value)}
              onBlur={() => void update({output_directory: outputDir})}
              onKeyDown={(event) =>
                event.key === 'Enter' && void update({output_directory: outputDir})
              }
            />
          </label>
        </div>
        <p className="muted">
          Every finished render is copied there under the project's name. Leave it empty and
          the file stays in the project's renders folder, as always.
        </p>
        <label className="inline">
          <input
            type="checkbox"
            checked={Boolean(project.auto_publish)}
            onChange={(event) => void update({auto_publish: event.target.checked})}
          />
          Publish to YouTube by itself when QA passes
        </label>
        <p className="muted">
          Ticking this is the asking (§51): after a green QA the upload runs with metadata
          written from the analysis — no button press. Needs YouTube connected on the
          Export screen; visibility follows the publishing default.
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
