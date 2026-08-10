/**
 * The overlay composition (SPEC §64, §66; decision D-008).
 *
 * A transparent canvas carrying captions and motion graphics, and **nothing
 * else**. The gameplay is never drawn here: Chromium rasterising twenty minutes
 * of 1080p60 footage is the cost this architecture exists to avoid, and FFmpeg
 * composites this layer over video it cut itself.
 *
 * The background is genuinely absent rather than black. `AbsoluteFill` with no
 * `backgroundColor` leaves the alpha channel at zero, which is what makes the
 * composite work — an opaque black rectangle over the gameplay would look like
 * a bug nobody notices until the video is watched.
 */

import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';

import {Caption} from './Caption';
import {Effect} from './Effects';
import type {Composition as CompositionData} from './schema';

/**
 * The composition description *is* the props object, so `--props=composition.json`
 * needs no wrapper. A nested `{composition: ...}` shape looks tidier and fails
 * silently: Remotion merges defaultProps, so a mismatched key leaves the
 * defaults in place and renders an empty 1080p canvas with no error.
 */
export const OverlayLayer: React.FC<CompositionData> = (composition) => {
  const {captions, effects, style, width, height} = composition;

  return (
    <AbsoluteFill style={{backgroundColor: 'transparent'}}>
      {/* Effects first, captions over them: text must stay readable, and a
          highlight box drawn on top of a caption would obscure the words. */}
      {effects.map((effect) => (
        <Sequence
          key={effect.id}
          from={effect.from}
          durationInFrames={effect.durationInFrames}
          // Remotion trims children to the sequence; the components read the
          // absolute frame themselves, so this only controls presence.
          layout="none"
        >
          <Effect effect={effect} style={style} width={width} height={height} />
        </Sequence>
      ))}

      {captions.map((caption) => (
        <Sequence
          key={caption.id}
          from={caption.from}
          durationInFrames={caption.durationInFrames}
          layout="none"
        >
          <Caption caption={caption} style={style} height={height} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
