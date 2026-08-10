/**
 * The composition registry (SPEC §64).
 *
 * The composition's dimensions and duration come from `composition.json`, not
 * from constants here: the backend decides the output format, and a project
 * that hard-coded 1080p60 would silently render the wrong size the first time
 * someone asked for 720p.
 *
 * `calculateMetadata` is how Remotion lets the props decide that, and it runs
 * both in the studio and in a CLI render, so the two cannot drift.
 */

import {Composition} from 'remotion';
import React from 'react';

import {OverlayLayer} from './OverlayLayer';
import {EMPTY_COMPOSITION, parseComposition} from './schema';

export const Root: React.FC = () => {
  return (
    <Composition
      id="OverlayLayer"
      component={OverlayLayer}
      // Replaced by calculateMetadata from the real description. These are the
      // studio's defaults before any props are supplied.
      durationInFrames={EMPTY_COMPOSITION.durationInFrames}
      fps={EMPTY_COMPOSITION.fps}
      width={EMPTY_COMPOSITION.width}
      height={EMPTY_COMPOSITION.height}
      defaultProps={EMPTY_COMPOSITION}
      calculateMetadata={({props}) => {
        const parsed = parseComposition(props);
        return {
          props: parsed,
          durationInFrames: parsed.durationInFrames,
          fps: parsed.fps,
          width: parsed.width,
          height: parsed.height,
        };
      }}
    />
  );
};
