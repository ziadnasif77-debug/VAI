/**
 * Export (SPEC §57, §76–§79): the render, and whether it should be published.
 *
 * The QA report is the substance of this screen rather than a footnote. §76
 * makes a technical failure blocking, and §78 makes a content warning a
 * question for a person — so the screen shows the difference plainly: what
 * stops an export, what merely deserves a look, and the remedy §79 requires
 * each finding to carry.
 */

import {useCallback, useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Project} from '../lib/api';

export function ExportScreen({
  project,
  onPreview,
}: {
  project: Project;
  onPreview: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState(project.name);
  const [visibility, setVisibility] = useState<'private' | 'unlisted' | 'public'>('private');
  const [authCode, setAuthCode] = useState<{url: string; code: string} | null>(null);
  const [publishedUrl, setPublishedUrl] = useState<string | null>(null);
  const targets = usePolling(() => api.publishing.targets(), {
    intervalMs: 4000,
    // Poll only while a sign-in is pending; otherwise one read is the truth.
    active: () => authCode !== null,
  });
  const status = usePolling(() => api.render.status(project.id), {
    intervalMs: 2000,
    active: (value) =>
      !value?.job || value.job.status === 'running' || value.job.status === 'queued',
  });
  const qa = usePolling(() => api.qa(project.id), {
    intervalMs: 5000,
    active: (value) => (value?.findings.length ?? 0) === 0,
  });

  const start = useCallback(async () => {
    setBusy(true);
    try {
      await api.render.start(project.id);
      status.refresh();
    } finally {
      setBusy(false);
    }
  }, [project.id, status]);

  const youtube = targets.data?.targets.find((item) => item.target === 'youtube');

  const connect = useCallback(async () => {
    const grant = await api.publishing.authStart();
    setAuthCode({url: grant.verification_url, code: grant.user_code});
    const poll = window.setInterval(async () => {
      try {
        const answer = await api.publishing.authPoll();
        if (answer.status === 'authorized' || answer.status === 'none') {
          window.clearInterval(poll);
          setAuthCode(null);
          targets.refresh();
        }
      } catch {
        // Denied or expired: stop polling and let the person start over.
        window.clearInterval(poll);
        setAuthCode(null);
        targets.refresh();
      }
    }, 5000);
  }, [targets]);

  const publishToYouTube = useCallback(async () => {
    setBusy(true);
    setPublishedUrl(null);
    try {
      await api.publishing.publish(project.id, {
        target: 'youtube',
        metadata: {title, visibility},
      });
      setPublishedUrl('queued');
    } finally {
      setBusy(false);
    }
  }, [project.id, title, visibility]);

  const job = status.data?.job ?? null;
  const latest = status.data?.latest ?? null;
  const rendering = job?.status === 'running' || job?.status === 'queued';
  const failures = (qa.data?.findings ?? []).filter((item) => item.qa_status === 'failed');
  const warnings = (qa.data?.findings ?? []).filter((item) => item.qa_status === 'warning');

  return (
    <div className="screen">
      <section className="panel">
        <h2>Render</h2>

        {rendering && job && (
          <>
            <div className="stage-bar wide">
              <span className="stage-fill" style={{width: `${Math.round(job.progress * 100)}%`}} />
            </div>
            <p className="muted">{job.message ?? 'Rendering…'}</p>
          </>
        )}

        {job?.status === 'failed' && (
          <p className="error">
            {job.error_message ?? job.error_code ?? 'The render failed.'}
          </p>
        )}

        {latest && !rendering && (
          <ul className="facts">
            <li>
              <span className="muted">Length</span>
              <span className="mono">{timecode(latest.duration_seconds ?? 0)}</span>
            </li>
            <li>
              <span className="muted">Format</span>
              <span className="mono">
                {latest.resolution ? `${latest.resolution}p` : '—'}
                {latest.fps ? `${Math.round(latest.fps)}` : ''}
              </span>
            </li>
            <li>
              <span className="muted">Encoder</span>
              <span className="mono">{latest.encoder ?? '—'}</span>
            </li>
            <li>
              <span className="muted">Size</span>
              <span className="mono">
                {latest.size_bytes ? `${(latest.size_bytes / 1e6).toFixed(0)} MB` : '—'}
              </span>
            </li>
          </ul>
        )}

        <div className="row">
          <button type="button" onClick={() => void start()} disabled={busy || rendering}>
            {latest ? 'Render again' : 'Render'}
          </button>
          <button type="button" className="primary" onClick={onPreview} disabled={!latest}>
            Watch it
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>
          Quality check{' '}
          {qa.data && (
            <span className={`badge badge-${qa.data.qa_status}`}>{qa.data.qa_status}</span>
          )}
        </h2>

        {!qa.data?.findings.length && (
          <p className="muted">No checks have run yet. They run automatically after a render.</p>
        )}

        {failures.length > 0 && (
          <div className="warning error-box">
            <strong>These stop the video being published (§76).</strong>
            <ul>
              {failures.map((finding) => (
                <li key={finding.check}>
                  <strong>{finding.check}</strong> — {finding.detail}
                  {finding.remedy && <div className="muted">→ {finding.remedy}</div>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {warnings.length > 0 && (
          <div className="warning">
            <strong>Worth a look before you publish (§78 — your call).</strong>
            <ul>
              {warnings.map((finding) => (
                <li key={finding.check}>
                  <strong>{finding.check}</strong> — {finding.detail}
                  {finding.remedy && <div className="muted">→ {finding.remedy}</div>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {qa.data && qa.data.findings.length > 0 && !failures.length && !warnings.length && (
          <p className="muted">
            All {qa.data.findings.length} checks passed.
          </p>
        )}

        {latest?.output_path && (
          <p className="muted mono file-path">{latest.output_path}</p>
        )}
      </section>

      <section className="panel">
        <h2>Shorts</h2>
        <p className="muted">
          The strongest moments as vertical 9:16 cuts, captions included — from the
          analysis this project already paid for.
        </p>
        <button
          onClick={async () => {
            setBusy(true);
            try {
              await api.publishing.shorts(project.id);
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy}
        >
          Generate Shorts
        </button>
        <p className="muted">Progress and file paths land on the shorts job in Analysis.</p>
      </section>

      <section className="panel">
        <h2>YouTube</h2>

        {!youtube?.available && (
          <p className="muted">
            Set <code>publishing.youtube.client_id</code> and{' '}
            <code>client_secret_file</code> in <code>config/publishing.yaml</code> to
            enable uploads. Your footage never leaves this machine until you press
            Publish.
          </p>
        )}

        {youtube?.available && !youtube.connected && !authCode && (
          <button className="primary" onClick={connect} disabled={busy}>
            Connect YouTube
          </button>
        )}

        {authCode && (
          <p>
            Open <a href={authCode.url} target="_blank" rel="noreferrer">{authCode.url}</a>{' '}
            and enter <strong className="mono">{authCode.code}</strong>. This page
            updates by itself once you approve.
          </p>
        )}

        {youtube?.connected && (
          <>
            <label className="field">
              <span>Title</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={100} />
            </label>
            <label className="field">
              <span>Visibility</span>
              <select
                value={visibility}
                onChange={(e) => setVisibility(e.target.value as typeof visibility)}
              >
                <option value="private">Private</option>
                <option value="unlisted">Unlisted</option>
                <option value="public">Public</option>
              </select>
            </label>
            <button
              className="primary"
              onClick={publishToYouTube}
              disabled={busy || !latest?.output_path || Boolean(failures.length)}
            >
              Publish to YouTube
            </button>
            {Boolean(failures.length) && (
              <p className="muted">QA failures block publishing (§76). Fix them first.</p>
            )}
            {publishedUrl === 'queued' && (
              <p className="muted">
                Upload queued. Progress appears in the Analysis screen; the result — the
                video link — lands on the publish job when it completes.
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
