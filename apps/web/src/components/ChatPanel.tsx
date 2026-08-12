/**
 * The chat panel (SPEC §63, and the interaction addendum).
 *
 * Three kinds of thing can be typed here, and the interaction layer already
 * distinguishes them: an *instruction* changes the editing intent, a *command*
 * changes the EDL, a *question* changes nothing. The panel shows which one it
 * decided, because "focus on clutches" quietly becoming a question is the
 * failure a user cannot diagnose.
 *
 * §63 is explicit that natural language must never touch files — it edits
 * project state. Nothing here bypasses the API, and the examples below are the
 * vocabulary the rule-based parser actually understands, not aspirations.
 */

import {useCallback, useRef, useState} from 'react';

import {api, type Project} from '../lib/api';

type Turn = {
  id: number;
  from: 'you' | 'editor';
  text: string;
  kind?: string;
  rerender?: boolean;
};

const EXAMPLES = [
  'focus on clutch moments',
  'delete clip 3',
  'trim 2 seconds off the end of clip 5',
  'make it funnier',
  'what was the best moment?',
  'revert to version 1',
];

export function ChatPanel({project, onClose}: {project: Project; onClose: () => void}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const nextId = useRef(0);

  const send = useCallback(
    async (message: string) => {
      const trimmed = message.trim();
      if (!trimmed || busy) return;

      setTurns((current) => [...current, {id: nextId.current++, from: 'you', text: trimmed}]);
      setText('');
      setBusy(true);
      try {
        const reply = await api.chat(project.id, trimmed);
        setTurns((current) => [
          ...current,
          {
            id: nextId.current++,
            from: 'editor',
            text: reply.message,
            kind: reply.interaction_type,
            rerender: reply.requires_rerender,
          },
        ]);
      } catch (failure) {
        setTurns((current) => [
          ...current,
          {
            id: nextId.current++,
            from: 'editor',
            text: failure instanceof Error ? failure.message : String(failure),
            kind: 'error',
          },
        ]);
      } finally {
        setBusy(false);
      }
    },
    [busy, project.id],
  );

  return (
    <aside className="chat">
      <header className="chat-header">
        <strong>Ask about your video</strong>
        <button type="button" className="link" onClick={onClose}>
          Close
        </button>
      </header>

      <div className="chat-log">
        {turns.length === 0 && (
          <div className="chat-examples">
            <p className="muted">Try:</p>
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="chip"
                onClick={() => void send(example)}
              >
                {example}
              </button>
            ))}
          </div>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className={`turn turn-${turn.from}`}>
            <p>{turn.text}</p>
            {turn.kind && turn.from === 'editor' && (
              <span className="muted chat-kind">
                {turn.kind.replace('_', ' ')}
                {turn.rerender ? ' · needs a re-render' : ''}
              </span>
            )}
          </div>
        ))}
      </div>

      <div className="chat-input">
        <input
          type="text"
          value={text}
          placeholder="focus on clutch moments"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && void send(text)}
          disabled={busy}
        />
        <button type="button" onClick={() => void send(text)} disabled={busy || !text.trim()}>
          Send
        </button>
      </div>
    </aside>
  );
}
