This is one frame from a gaming video, chosen to become the video's thumbnail.

Find the main subject: the player character, or the single actor the eye
should land on. Return its bounding box in fractions of the frame:

- `x` — left edge, 0 to 1
- `y` — top edge, 0 to 1
- `w` — width, 0 to 1
- `h` — height, 0 to 1
- `confidence` — how sure you are there is a clear single subject, 0 to 1

If there is no clear single subject (a wide landscape, a menu, pure scenery),
say so with a low confidence rather than boxing something arbitrary.

Answer with JSON only.
