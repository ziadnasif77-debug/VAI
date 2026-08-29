/**
 * The overlay half of the effects library (SPEC §66, §68; decision D-008).
 *
 * Seven effects reach this renderer, and they share one property: they are
 * drawn *on top of* the gameplay rather than altering it. Anything that changes
 * the footage itself — zoom, speed, freeze, colour — is FFmpeg's, and never
 * appears in a composition description.
 *
 * Every effect is declarative data (§68). Nothing here decides whether an
 * effect belongs; the planner did that, and `strength` carries how firmly it
 * meant it.
 */

import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

import type {CaptionStyle, EffectElement} from './schema';

type Props = {
  readonly effect: EffectElement;
  readonly style: CaptionStyle;
  readonly width: number;
  readonly height: number;
};

/**
 * Route an effect to its renderer.
 *
 * An unknown type draws nothing rather than throwing: the effects library is
 * configuration, and a deployment that adds an entry this project has not
 * learned yet should lose one decoration, not the whole overlay.
 */
export const Effect: React.FC<Props> = (props) => {
  switch (props.effect.type) {
    case 'text_pop':
    case 'dynamic_captions':
      return <TextPop {...props} />;
    case 'highlight_box':
      return <HighlightBox {...props} />;
    case 'kill_counter':
      return <Counter {...props} />;
    case 'impact':
      return <Impact {...props} />;
    case 'crosshair':
      return <Crosshair {...props} />;
    case 'meme':
      return <Meme {...props} />;
    default:
      return null;
  }
};

/** Text that arrives with a spring and leaves quietly. */
const TextPop: React.FC<Props> = ({effect, style, height}) => {
  // Sequence-relative: 0 on the effect's first frame. See Caption.tsx.
  const local = useCurrentFrame();
  const {fps} = useVideoConfig();
  const text = String(effect.params.text ?? '');
  if (!text) {
    return null;
  }

  const scale = spring({
    frame: local,
    fps,
    config: {damping: 12, stiffness: 180},
    from: 0.6,
    to: 1,
  });
  const opacity = fadeInOut(local, effect.durationInFrames);
  const size = Math.round(style.fontSizePx * 1.5 * (0.7 + 0.3 * effect.strength));

  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        top: Math.round(height * Number(effect.params.y ?? 0.16)),
        display: 'flex',
        justifyContent: 'center',
        opacity,
        transform: `scale(${scale})`,
      }}
    >
      <span
        style={{
          fontFamily: `${style.fontFamily}, system-ui, sans-serif`,
          fontWeight: 900,
          fontSize: size,
          color: String(effect.params.color ?? style.highlightColor),
          WebkitTextStroke: `${style.outlineWidthPx}px ${style.outlineColor}`,
          paintOrder: 'stroke fill',
          textShadow: `0 6px 24px rgba(0,0,0,${style.shadowOpacity})`,
          letterSpacing: '0.02em',
        }}
      >
        {text}
      </span>
    </div>
  );
};

/** A rectangle drawn around something worth looking at. */
const HighlightBox: React.FC<Props> = ({effect, style, width, height}) => {
  const local = useCurrentFrame();
  const opacity = fadeInOut(local, effect.durationInFrames) * (0.5 + 0.5 * effect.strength);

  // Fractions of the frame, so the same effect lands in the same place at any
  // resolution.
  const x = Number(effect.params.x ?? 0.35);
  const y = Number(effect.params.y ?? 0.35);
  const boxWidth = Number(effect.params.width ?? 0.3);
  const boxHeight = Number(effect.params.height ?? 0.3);
  const thickness = Math.max(2, Math.round(height * 0.004));

  return (
    <div
      style={{
        position: 'absolute',
        left: Math.round(width * x),
        top: Math.round(height * y),
        width: Math.round(width * boxWidth),
        height: Math.round(height * boxHeight),
        border: `${thickness}px solid ${String(effect.params.color ?? style.highlightColor)}`,
        borderRadius: Math.round(height * 0.01),
        boxShadow: `0 0 ${thickness * 4}px rgba(0,0,0,${style.shadowOpacity})`,
        opacity,
      }}
    />
  );
};

/** A running count, in the corner, for a streak worth naming. */
const Counter: React.FC<Props> = ({effect, style, width, height}) => {
  const local = useCurrentFrame();
  const {fps} = useVideoConfig();
  const value = Number(effect.params.count ?? 1);
  const label = String(effect.params.label ?? 'x');

  const pop = spring({frame: local, fps, config: {damping: 14}, from: 0.75, to: 1});
  const opacity = fadeInOut(local, effect.durationInFrames);

  return (
    <div
      style={{
        position: 'absolute',
        right: style.safeAreaHorizontalPx,
        top: Math.round(height * 0.12),
        display: 'flex',
        alignItems: 'baseline',
        gap: Math.round(width * 0.004),
        opacity,
        transform: `scale(${pop})`,
        transformOrigin: 'right center',
      }}
    >
      <span
        style={{
          fontFamily: `${style.fontFamily}, system-ui, sans-serif`,
          fontWeight: 900,
          fontSize: Math.round(style.fontSizePx * 1.8),
          color: style.highlightColor,
          WebkitTextStroke: `${style.outlineWidthPx}px ${style.outlineColor}`,
          paintOrder: 'stroke fill',
        }}
      >
        {label}
        {value}
      </span>
    </div>
  );
};

/** A brief flash at the moment something lands. */
const Impact: React.FC<Props> = ({effect, style}) => {
  const local = useCurrentFrame();
  // Front-loaded: an impact that fades symmetrically reads as a lighting
  // change rather than a hit.
  const opacity =
    interpolate(local, [0, 2, effect.durationInFrames], [0, 0.55, 0], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    }) * effect.strength;

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        background: `radial-gradient(circle at 50% 50%, ${String(
          effect.params.color ?? style.highlightColor,
        )}00 40%, ${String(effect.params.color ?? style.highlightColor)} 100%)`,
        opacity,
      }}
    />
  );
};

/** A marker at the centre of frame, for a shot worth pointing at. */
const Crosshair: React.FC<Props> = ({effect, style, width, height}) => {
  const local = useCurrentFrame();
  const opacity = fadeInOut(local, effect.durationInFrames) * (0.4 + 0.6 * effect.strength);
  const size = Math.round(height * 0.06);
  const thickness = Math.max(2, Math.round(height * 0.003));
  const colour = String(effect.params.color ?? style.highlightColor);

  return (
    <div
      style={{
        position: 'absolute',
        left: Math.round(width * Number(effect.params.x ?? 0.5)) - size,
        top: Math.round(height * Number(effect.params.y ?? 0.5)) - size,
        width: size * 2,
        height: size * 2,
        opacity,
      }}
    >
      <div
        style={{
          position: 'absolute',
          left: size - thickness / 2,
          top: 0,
          width: thickness,
          height: size * 2,
          background: colour,
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: size - thickness / 2,
          left: 0,
          height: thickness,
          width: size * 2,
          background: colour,
        }}
      />
    </div>
  );
};

/**
 * A user-supplied image, placed in a corner.
 *
 * Only ever a local file the user provided: §73's rule about not fetching
 * copyrighted material applies to pictures as much as to music, and a
 * `staticFile` reference cannot reach the network.
 */
const Meme: React.FC<Props> = ({effect, width, height}) => {
  const local = useCurrentFrame();
  const source = String(effect.params.src ?? '');
  if (!source) {
    return null;
  }
  const opacity = fadeInOut(local, effect.durationInFrames);
  const scale = Number(effect.params.scale ?? 0.22);

  return (
    <img
      src={source}
      alt=""
      style={{
        position: 'absolute',
        left: Math.round(width * Number(effect.params.x ?? 0.72)),
        top: Math.round(height * Number(effect.params.y ?? 0.1)),
        width: Math.round(width * scale),
        opacity,
      }}
    />
  );
};

/** Shared entry/exit ramp, in frames. The ramp shrinks for short elements:
 * interpolate() demands a STRICTLY increasing input range, and a jump-cut
 * piece can hand an effect fewer frames than two full ramps. */
const fadeInOut = (local: number, duration: number, ramp = 4): number => {
  if (duration <= 2) {
    return 1;
  }
  const r = Math.max(1, Math.min(ramp, Math.floor((duration - 1) / 2)));
  return interpolate(
    local,
    [0, r, duration - r, duration],
    [0, 1, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
};
