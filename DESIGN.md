# FaceGate layout contract

FaceGate is a top-edge interaction, not a conventional window. Its layout is
therefore defined as a compact vertical module, on an 8-point baseline grid.

| Element | Value |
| --- | ---: |
| Island width / height | 360 × 320 |
| Content inset | 24 |
| Presence mark | 64 × 64 |
| Mark to copy | 8 |
| Copy to actions | 24 |
| Action gap | 8 |
| Action target | 44 high |
| Title / detail | 17 / 12 |

The column is horizontally centered. The mark, title, detail, and controls
share that centerline. Only two visual action levels are used: a high-contrast
approval action and a subdued password fallback. State changes preserve the
same grid; they never introduce arbitrary one-off spacing values.

Visual QA is done against a real layer-shell capture on the target KDE/Wayland
desktop, checking top-edge attachment, monitor centering, whitespace balance,
legibility, and the approval/scanning/result states.
