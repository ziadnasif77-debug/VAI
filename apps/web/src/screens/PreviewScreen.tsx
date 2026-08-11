/**
 * Preview (SPEC §57): watch the finished video.
 *
 * The one screen whose whole job is to let a person judge the edit — which is
 * the judgement no test in this project can make. §78's human review happens
 * here or nowhere.
 *
 * The clip list beside the player is a map, not a control: clicking a clip
 * seeks to it, so "the third clip drags" can be checked in a second rather than
 * by scrubbing. Editing stays on the Timeline screen, where the consequences of
 * a change are visible.
 */

import {useCallback, useRef} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Project} from '../lib/api';

export function PreviewScreen({project}: {project: Project}) {
  const video = useRef<HTMLVideoElement>(null);
  const status = usePolling(() => api.render.status(project.id), {
    intervalMs: 4000,
    active: (value) => !value?.latest,
  });
  const timeline = usePolling(() => api.timeline.get(project.id), {
    intervalMs: 15000,
    active: (value) => (value?.clips.length ?? 0) === 0,
  });

  const seek = useCallback((seconds: number) => {
    const element = video.current;
    if (!element) return;
    element.currentTime = seconds;
    void element.play();
  }, []);

  const latest = status.data?.latest;
  const clips = (timeline.data?.clips ?? []).filter((clip) => clip.enabled);

  if (!latest) {
    return (
      <div className="screen">
        <section className="panel">
          <h2>Preview</h2>
          <p className="muted">
            Nothing has been rendered yet. Render the edit from the Timeline or Export
            screen, then come back.
          </p>
        </section>
      </div>
    );
  }

  return (
    <div className="screen preview">
      <section className="panel player-panel">
        {/* The API streams with range requests, so seeking works without
            downloading the whole file first. */}
        <video ref={video} controls src={api.render.previewUrl(project.id)} className="player" />
        <p className="muted">
          {timecode(latest.duration_seconds ?? 0)} · {latest.resolution}p
          {latest.fps ? Math.round(latest.fps) : ''} · {latest.encoder}
        </p>
      </section>

      <section className="panel">
        <h2>Clips</h2>
        <ol className="clips compact">
          {clips.map((clip, position) => (
            <li key={clip.id} className="clip">
              <button type="button" className="link" onClick={() => seek(clip.timeline_start)}>
                <span className="mono">{timecode(clip.timeline_start)}</span>{' '}
                {clip.moment_type ?? 'clip'}
                {clip.role !== 'body' && <span className="badge">{clip.role}</span>}
              </button>
              <span className="muted mono">{clip.duration_seconds.toFixed(0)}s</span>
              <span className="muted mono">#{position + 1}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
