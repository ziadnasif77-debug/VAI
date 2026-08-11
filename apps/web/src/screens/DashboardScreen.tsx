/**
 * Dashboard (SPEC §58): projects, jobs, and what this machine can do.
 *
 * The machine's capabilities belong here rather than in a settings page nobody
 * opens. Whether the GPU is usable and whether the overlay renderer is
 * installed change what the finished video will be — a missing Remotion means
 * no captions (§95) — and finding that out after a twenty-minute render is too
 * late.
 */

import {useCallback, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, type Project} from '../lib/api';

export function DashboardScreen({onOpen}: {onOpen: (project: Project) => void}) {
  const projects = usePolling(() => api.projects.list(), {intervalMs: 5000});
  const health = usePolling(() => api.health(), {
    intervalMs: 30000,
    // The hardware does not change while the app is open; one look is enough
    // unless something is wrong and might be fixed.
    active: (value) => value?.status !== 'ok',
  });

  const [name, setName] = useState('');
  const [minutes, setMinutes] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = useCallback(async () => {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.projects.create({
        name: name.trim(),
        target_duration_seconds: minutes * 60,
        mode: 'story',
      });
      setName('');
      onOpen(project);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }, [minutes, name, onOpen]);

  return (
    <div className="screen">
      <section className="panel">
        <h2>New project</h2>
        <div className="row">
          <input
            type="text"
            placeholder="Session name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && void create()}
          />
          <label className="inline">
            Target
            <select value={minutes} onChange={(event) => setMinutes(Number(event.target.value))}>
              {[10, 15, 20, 25, 30, 40, 45, 50, 60].map((value) => (
                <option key={value} value={value}>
                  {value} min
                </option>
              ))}
            </select>
          </label>
          <button type="button" onClick={() => void create()} disabled={busy || !name.trim()}>
            Create
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </section>

      <section className="panel">
        <h2>Projects</h2>
        {projects.loading && <p className="muted">Loading…</p>}
        {projects.data?.items.length === 0 && (
          <p className="muted">No projects yet. Create one above.</p>
        )}
        <ul className="list">
          {projects.data?.items.map((project) => (
            <li key={project.id}>
              <button type="button" className="list-item" onClick={() => onOpen(project)}>
                <span className="list-title">{project.name}</span>
                <span className="muted">
                  {Math.round(project.target_duration_seconds / 60)} min ·{' '}
                  {project.mode.replace('_', ' ')} · {project.status}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h2>This machine</h2>
        {health.data && (
          <>
            <p className="muted">
              {health.data.application_version} · {health.data.hardware_profile} profile
            </p>
            <ul className="checks">
              {health.data.checks.map((check) => (
                <li key={check.name} className={`check check-${check.status}`}>
                  <span className="check-name">{check.name}</span>
                  <span className="check-detail">{check.detail}</span>
                  {check.remediation && (
                    <span className="check-remedy">→ {check.remediation}</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
