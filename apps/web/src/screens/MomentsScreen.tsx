/**
 * Moments (SPEC §61, §79, §80): the ranking, and why.
 *
 * §61 lists the columns; §80 gives the screen its purpose. A list of clips
 * sorted by a number nobody can interrogate is a black box, and the moment
 * someone disagrees with the order they have no way in. So every row can be
 * opened to show the sentences that justify it and the ten §32 dimensions
 * behind the score.
 *
 * §79's "Needs Review" is a badge rather than a filter: a low-confidence moment
 * still belongs in the ranking, marked, because hiding it would be the system
 * quietly deciding it was wrong.
 */

import {useState} from 'react';

import {usePolling} from '../lib/usePolling';
import {api, timecode, type Moment, type Project} from '../lib/api';

/** §61's filter row. */
const FILTERS = ['epic', 'funny', 'clutch', 'reaction', 'fail', 'victory'] as const;

export function MomentsScreen({project}: {project: Project}) {
  const [type, setType] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const moments = usePolling(
    () => api.moments(project.id, type ? {type} : undefined),
    {intervalMs: 5000, active: (value) => (value?.total ?? 0) === 0},
  );

  // Score is how the pipeline ranks; time is how a person remembers the
  // session. Making the toggle exist was not enough: with score as the
  // default, the same viewer read the ranked list as "the video's order" a
  // second time and reported the story scrambled while the edit itself was
  // chronological. The default is the reading people bring to the screen;
  // score stays one click away.
  const [sortBy, setSortBy] = useState<'score' | 'when'>('when');
  const items = [...(moments.data?.items ?? [])].sort((a, b) =>
    sortBy === 'when' ? a.start_seconds - b.start_seconds : b.score - a.score,
  );
  const counts = moments.data?.by_type ?? {};

  return (
    <div className="screen">
      <section className="panel">
        <h2>
          Moments{' '}
          <span className="muted">
            {moments.data ? `${moments.data.returned} of ${moments.data.total}` : ''}
          </span>
        </h2>

        <div className="filters">
          <button
            type="button"
            className={type === null ? 'chip active' : 'chip'}
            onClick={() => setType(null)}
          >
            All
          </button>
          {FILTERS.map((name) => (
            <button
              key={name}
              type="button"
              className={type === name ? 'chip active' : 'chip'}
              onClick={() => setType(type === name ? null : name)}
              disabled={!counts[name]}
              title={counts[name] ? undefined : 'None of this kind were found'}
            >
              {name} {counts[name] ? `(${counts[name]})` : ''}
            </button>
          ))}
        </div>

        {items.length === 0 && (
          <p className="muted">
            No moments yet. They appear once the Moments stage has run.
          </p>
        )}

        <table className="moments">
          <thead>
            <tr>
              <th
                className={sortBy === 'when' ? 'sortable active' : 'sortable'}
                onClick={() => setSortBy('when')}
              >
                When {sortBy === 'when' ? '↓' : ''}
              </th>
              <th>Type</th>
              <th
                className={sortBy === 'score' ? 'sortable active' : 'sortable'}
                onClick={() => setSortBy('score')}
              >
                Score {sortBy === 'score' ? '↓' : ''}
              </th>
              <th>Confidence</th>
              <th>Length</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((moment) => (
              <MomentRow
                key={moment.id || `${moment.media_id}-${moment.start_seconds}`}
                moment={moment}
                open={open === moment.id}
                onToggle={() => setOpen(open === moment.id ? null : moment.id)}
              />
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function MomentRow({
  moment,
  open,
  onToggle,
}: {
  moment: Moment;
  open: boolean;
  onToggle: () => void;
}) {
  const dimensions = Object.entries(moment.score_breakdown).sort(([a], [b]) => a.localeCompare(b));

  return (
    <>
      <tr className={open ? 'moment-row open' : 'moment-row'}>
        <td className="mono">{timecode(moment.context_start)}</td>
        <td>
          {moment.moment_type}
          {moment.needs_review && (
            <span className="badge" title="Low confidence — worth a look (§79)">
              needs review
            </span>
          )}
        </td>
        <td className="mono">{moment.score.toFixed(2)}</td>
        <td className="mono">{moment.confidence.toFixed(2)}</td>
        <td className="mono">{moment.duration_seconds.toFixed(0)}s</td>
        <td>
          <button type="button" className="link" onClick={onToggle}>
            {open ? 'Hide' : 'Why?'}
          </button>
        </td>
      </tr>
      {open && (
        <tr className="moment-detail">
          <td colSpan={6}>
            {moment.explanation.length > 0 ? (
              <ul className="reasons">
                {moment.explanation.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">No explanation was recorded for this moment.</p>
            )}
            <div className="dimensions">
              {dimensions.map(([name, value]) => (
                <div key={name} className="dimension">
                  <span className="dimension-name">{name}</span>
                  <span className="dimension-bar">
                    <span
                      className="dimension-fill"
                      style={{width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%`}}
                    />
                  </span>
                  <span className="mono">{value.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
