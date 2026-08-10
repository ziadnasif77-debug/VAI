/**
 * The shape of `composition.json` (SPEC §64).
 *
 * Written by `backend/rendering/composition.py`, read here. The two must agree,
 * so `version` is checked at load: a backend and a project that disagree should
 * fail loudly rather than render something subtly wrong.
 *
 * Everything is already in **frames**. The backend converted seconds once,
 * where a test can check the rounding; nothing here should divide by fps.
 */

/** Matches `COMPOSITION_VERSION` in the backend. */
export const COMPOSITION_VERSION = 1;

export type CaptionWord = {
  readonly word: string;
  /** Inclusive first frame. */
  readonly from: number;
  /** Exclusive last frame. */
  readonly to: number;
};

export type CaptionElement = {
  readonly id: string;
  readonly index: number;
  readonly from: number;
  readonly durationInFrames: number;
  readonly text: string;
  /** Pre-wrapped by the backend, so the sidecar files carry identical lines. */
  readonly lines: readonly string[];
  readonly words: readonly CaptionWord[];
  readonly language: string | null;
  readonly rtl: boolean;
  readonly clipId: string | null;
};

export type EffectElement = {
  readonly id: string;
  readonly type: string;
  readonly category: string;
  readonly from: number;
  readonly durationInFrames: number;
  /** 0..1, after style and intent scaling. */
  readonly strength: number;
  readonly params: Record<string, unknown>;
  readonly clipId: string | null;
  /** Why the planner chose it (§80). Not drawn; useful when debugging. */
  readonly reason: string;
};

export type CaptionStyle = {
  readonly fontFamily: string;
  readonly fontWeight: number;
  readonly fontSizePx: number;
  readonly primaryColor: string;
  readonly highlightColor: string;
  readonly outlineColor: string;
  readonly outlineWidthPx: number;
  readonly shadowOpacity: number;
  readonly position: 'bottom_center' | 'top_center' | 'center';
  readonly maxLines: number;
  readonly safeAreaBottomPx: number;
  readonly safeAreaHorizontalPx: number;
  readonly wordHighlighting: boolean;
  readonly animated: boolean;
  readonly emphasis: boolean;
};

export type OverlaySpan = {
  readonly startFrame: number;
  readonly endFrame: number;
};

export type Composition = {
  readonly version: number;
  readonly width: number;
  readonly height: number;
  readonly fps: number;
  readonly durationInFrames: number;
  readonly style: CaptionStyle;
  readonly captions: readonly CaptionElement[];
  readonly effects: readonly EffectElement[];
  readonly spans: readonly OverlaySpan[];
  readonly metadata: Record<string, unknown>;
};

/** A description with nothing in it: what Remotion renders when asked to draw nothing. */
export const EMPTY_COMPOSITION: Composition = {
  version: COMPOSITION_VERSION,
  width: 1920,
  height: 1080,
  fps: 60,
  durationInFrames: 60,
  style: {
    fontFamily: 'Inter',
    fontWeight: 800,
    fontSizePx: 52,
    primaryColor: '#FFFFFF',
    highlightColor: '#FFD400',
    outlineColor: '#000000',
    outlineWidthPx: 3,
    shadowOpacity: 0.55,
    position: 'bottom_center',
    maxLines: 2,
    safeAreaBottomPx: 130,
    safeAreaHorizontalPx: 115,
    wordHighlighting: true,
    animated: true,
    emphasis: true,
  },
  captions: [],
  effects: [],
  spans: [],
  metadata: {},
};

/**
 * Validate what was handed in, and say precisely what is wrong when it is.
 *
 * A render that silently falls back to defaults produces a video with no
 * captions and no error, which is the worst of both outcomes.
 */
export const parseComposition = (input: unknown): Composition => {
  if (typeof input !== 'object' || input === null) {
    throw new Error('composition.json must be an object');
  }
  const value = input as Partial<Composition>;
  if (value.version !== COMPOSITION_VERSION) {
    throw new Error(
      `composition.json is version ${String(value.version)}, this project reads ` +
        `version ${COMPOSITION_VERSION}. The backend and the Remotion project are out of step.`,
    );
  }
  for (const key of ['width', 'height', 'fps', 'durationInFrames'] as const) {
    const dimension = value[key];
    if (typeof dimension !== 'number' || !Number.isFinite(dimension) || dimension <= 0) {
      throw new Error(`composition.json: ${key} must be a positive number`);
    }
  }
  return {
    ...EMPTY_COMPOSITION,
    ...value,
    style: {...EMPTY_COMPOSITION.style, ...(value.style ?? {})},
    captions: value.captions ?? [],
    effects: value.effects ?? [],
    spans: value.spans ?? [],
    metadata: value.metadata ?? {},
  } as Composition;
};
