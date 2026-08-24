# Sprite provenance

## beetle.png

- **Source**: [`Old Bugs`](https://opengameart.org/content/old-bugs) by **Master484**, OpenGameArt.org
- **Original file**: `BugSprites.png` (https://opengameart.org/sites/default/files/BugSprites.png), part of Master484's
  "Bug Blaster GFX" set — the license is stated directly inside the source
  image itself: `LICENSE: PUBLIC DOMAIN / CC0`.
- **License**: [CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/) (public domain). No
  attribution required; credited here anyway as good practice.
- **What was kept**: the "SMALL BUGS" row, red variant only, 5 of its 6
  animation-cycle frames (34x34px each, the 6th frame in the original sheet
  bleeds into the next section and was dropped). Cropped from the master
  sheet, black background keyed to transparent. Nothing else in
  `BugSprites.png` was used or vendored (its yellow/green/grey variants and
  the "SPECIAL BUGS"/"BIG BUGS"/game-object/font sections were left out —
  out of scope for this project, not a licensing concern, all under the
  same CC0 grant if ever needed).
- **Why this pack over the alternative candidate** (`Ambient Pixel Art
  Insects` / `cutebugs_by_madameberry.zip`, also CC0, from
  https://opengameart.org/content/ambient-pixel-art-insects): also
  downloaded and inspected directly. Its 8x8 sprites did not read as
  recognizable insects even at 20x scale with proper alpha compositing --
  confirmed by rendering them, not assumed. `BugSprites.png`'s "SMALL BUGS"
  row is a top-down beetle silhouette (round body, two eye-dots, legs
  spread to the sides) that reads clearly at small map scale, with a real
  5-frame leg-scuttle animation cycle rather than a single 2-frame ambient
  loop -- a better fit on both legibility and animation grounds, despite
  coming from an arcade-shooter asset set rather than a nature/ambient one.
- **No directional (4/8-way) frames exist in the source.** The map draws
  this as a single top-down silhouette and rotates it in CSS to face along
  whichever edge angle it's traveling -- edges on a map run at arbitrary
  angles anyway, so fixed 4/8-direction frames would not have aligned
  cleanly regardless of whether the source had them.

Not hotlinked: `beetle.png` here is a local, cropped, re-saved copy checked
into this repo, not a runtime fetch from opengameart.org.
