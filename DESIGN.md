# FaceGate layout and motion contract

FaceGate is a top-edge interaction, not a conventional window: a pure-black
island that grows down out of the monitor's top bezel, like a hardware notch.

## Layout

A single centered column on a 4/8-point grid. The island hugs its content, so
its height follows the state rather than leaving dead space.

| Element | Value |
| --- | ---: |
| Island width | 344 |
| Top inset (below bezel) | 24 |
| Side / bottom inset with buttons | 16 |
| Bottom inset without buttons | 28 |
| Presence mark | 56 × 56 |
| Mark → title | 16 |
| Title → detail | 4 |
| Detail → buttons | 24 |
| Buttons | 44 high pills, 8 apart, side by side |
| Lower corner radius | 38 (= 44 / 2 + 16, concentric with buttons) |
| Top shoulders | 10, concave, flush with the edge |
| Title / detail / button | 17 semibold / 13 regular / 15 semibold |

Text blocks are 280 wide (32 from each edge) so copy never runs to the rim.
Only two button levels exist: a white primary (**Approve**, right) and a dark
secondary (**Use Password**, left). Scanning and result states show no button,
because Howdy cannot be interrupted once it is running.

Colours follow Apple's dark palette: `#000` island, `#f5f5f7` label, `#98989d`
secondary label, `#1c1c1e` secondary fill; cyan `#64d2ff` for scanning, green
`#30d158` for success, red `#ff453a` for failure.

## Motion

- Driven by the monitor's frame clock (every refresh, e.g. 144/165 Hz), never
  a fixed timer.
- **Entrance:** the island springs out of the bezel from a 124-wide notch
  (width response 0.50, height 0.56, damping 0.8; under 3% overshoot). The
  mark, title, detail, and buttons follow with a 45 ms stagger, each fading
  in over 420 ms while settling 8 pt downward from 96% scale.
- **Reveal:** content opacity is also gated by the island's current size, so
  text is uncovered by the island and never drawn over the desktop.
- **State change:** the mark reacts immediately and blends over 500 ms. Copy
  leaves in 140 ms, then the island re-springs to the new height (0.50 / 0.86)
  while the new copy settles in.
- **Result:** success closes the scanning arc into a green ring with a small
  pulse; failure turns red with a short, decaying shake.
- **Exit:** content fades in 160 ms, then the island retracts into the bezel
  on a critically damped spring.

## Close

A 28 pt round close button sits 16 pt from the island's top and right edges.
It fades in over 180 ms once the mouse moves over the island (an island that
opens under a resting cursor does not reveal it) and fades out
120 ms after it leaves, so it never competes with the two primary choices.
Close (or Escape) declines a pending approval, which falls back to the
password prompt, or dismisses any other state. The island then stays hidden
for the rest of that attempt, even if Howdy reports a result afterwards.
