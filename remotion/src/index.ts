/**
 * Remotion entry point (SPEC §64).
 *
 * Registration only, and deliberately free of JSX so it can stay a `.ts` file:
 * the compositions live in `Root.tsx`, which is where anything visual belongs.
 */

import {registerRoot} from 'remotion';

import {Root} from './Root';

registerRoot(Root);
