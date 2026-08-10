/**
 * One caption, drawn on the transparent canvas (SPEC §71).
 *
 * Timing is not decided here. Every frame number arrived in the composition
 * description, computed from transcript timestamps by the backend — §71 says
 * caption timing always derives from the transcript, and a component that
 * spread words evenly across its own duration would quietly break that the
 * moment someone spoke faster at the end of a sentence.
 *
 * What *is* decided here is presentation: where the text sits inside the safe
 * area, how it enters and leaves, and which word is lit.
 */

import React from 'react';
import {interpolate, useCurrentFrame} from 'remotion';

import type {CaptionElement, CaptionStyle} from './schema';

/** Frames over which a caption fades in and out. Short: a caption that takes
 * half a second to arrive has already missed the word it belongs to. */
const FADE_FRAMES = 3;

type Props = {
  readonly caption: CaptionElement;
  readonly style: CaptionStyle;
  readonly height: number;
};

export const Caption: React.FC<Props> = ({caption, style, height}) => {
  // Two clocks, and confusing them is a real bug this had. Inside a Sequence,
  // `useCurrentFrame()` is already **relative** to the sequence start -- it
  // returns 0 on the caption's first frame. The word timings, by contrast, are
  // in **programme** frames, because that is where the transcript put them.
  // Converting once, here, is the same discipline the timeline applies to
  // source and programme time.
  const local = useCurrentFrame();
  const frame = local + caption.from;
  const last = caption.durationInFrames;

  const opacity = style.animated
    ? interpolate(
        local,
        [0, FADE_FRAMES, Math.max(FADE_FRAMES, last - FADE_FRAMES), last],
        [0, 1, 1, 0],
        {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
      )
    : 1;

  // A small rise on entry. Enough to read as deliberate, not enough to draw
  // attention away from the gameplay it is drawn over.
  const rise = style.animated
    ? interpolate(local, [0, FADE_FRAMES], [style.fontSizePx * 0.18, 0], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      })
    : 0;

  const outline = style.outlineWidthPx;
  const container: React.CSSProperties = {
    position: 'absolute',
    left: style.safeAreaHorizontalPx,
    right: style.safeAreaHorizontalPx,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: Math.round(style.fontSizePx * 0.12),
    opacity,
    transform: `translateY(${rise}px)`,
    ...verticalPlacement(style, height),
  };

  const line: React.CSSProperties = {
    fontFamily: `${style.fontFamily}, "Segoe UI", system-ui, sans-serif`,
    fontWeight: style.fontWeight,
    fontSize: style.fontSizePx,
    lineHeight: 1.18,
    color: style.primaryColor,
    direction: caption.rtl ? 'rtl' : 'ltr',
    textAlign: 'center',
    // Painted rather than stroked: an outline plus a soft shadow stays legible
    // over both a bright sky and a dark corridor, which is the whole problem
    // with captions over gameplay.
    WebkitTextStroke: outline > 0 ? `${outline}px ${style.outlineColor}` : undefined,
    paintOrder: 'stroke fill',
    textShadow: `0 ${Math.round(outline * 0.8)}px ${outline * 2}px rgba(0,0,0,${style.shadowOpacity})`,
    margin: 0,
    whiteSpace: 'pre-wrap',
  };

  return (
    <div style={container}>
      {caption.lines.map((text, index) => (
        <p key={`${caption.id}-${index}`} style={line}>
          {style.wordHighlighting && caption.words.length > 0
            ? renderWords(text, caption, frame, style)
            : text}
        </p>
      ))}
    </div>
  );
};

/**
 * Light the word currently being spoken.
 *
 * Matched by position rather than by string: "no no no" is three words, and
 * highlighting whichever one `indexOf` found first would light the wrong one
 * for two thirds of the phrase.
 */
const renderWords = (
  text: string,
  caption: CaptionElement,
  frame: number,
  style: CaptionStyle,
): React.ReactNode => {
  const tokens = text.split(/\s+/).filter(Boolean);
  let cursor = consumedBefore(caption, text);

  return tokens.map((token, index) => {
    const timing = caption.words[cursor + index];
    const active =
      timing !== undefined && frame >= timing.from && frame < timing.to;
    return (
      <span
        key={`${caption.id}-w-${cursor + index}`}
        style={{
          color: active ? style.highlightColor : style.primaryColor,
          // Scaling the active word would reflow the line and make it jitter;
          // colour alone reads as deliberate and stays still.
          transition: 'none',
        }}
      >
        {token}
        {index < tokens.length - 1 ? ' ' : ''}
      </span>
    );
  });
};

/** How many of the caption's words appear on lines before this one. */
const consumedBefore = (caption: CaptionElement, line: string): number => {
  let consumed = 0;
  for (const candidate of caption.lines) {
    if (candidate === line) {
      return consumed;
    }
    consumed += candidate.split(/\s+/).filter(Boolean).length;
  }
  return 0;
};

const verticalPlacement = (
  style: CaptionStyle,
  height: number,
): React.CSSProperties => {
  if (style.position === 'top_center') {
    return {top: style.safeAreaBottomPx};
  }
  if (style.position === 'center') {
    return {top: Math.round(height / 2 - style.fontSizePx)};
  }
  return {bottom: style.safeAreaBottomPx};
};
