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
secondary (**Deny**, left; declining the scan leaves sudo at its password prompt). Scanning and result states show no button,
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
- **Result:** the ring is one continuous conic-gradient stroke, so success grows
  the scanning comet forward (clockwise, like the scan), evens its tail, and
  closes it into a green ring with no
  redraw or swap; the small pulse lands as it closes. State blends always start
  from the values on screen, so a result arriving mid-transition cannot jump.
  Failure turns red with a short, decaying shake.
- **Exit:** content fades in 160 ms, then the island retracts into the bezel
  on a critically damped spring.

## Dismissal

There is no separate close control: **Deny** declines the request (sudo falls
back to its password prompt). Escape does the same, or dismisses any other
state and keeps the island hidden for the rest of that attempt. A 20 s
watchdog closes a scan that never receives a result.

## Placement

Each time the island appears it asks KWin for the active output
(`org.kde.KWin.activeOutputName`, which follows the mouse by default) and pins
itself to the top center of that monitor. Without KWin it falls back to the
output the compositor chooses.

## Requester

The PAM adapter runs inside `sudo`, so it reads the exact command from its own
argv and walks up the process tree, skipping shells and wrappers, to name the
app that ran it and the app hosting that (for example **Claude Code** via
**T3 Code**). The approval title becomes "‹app› wants sudo", the command sits
in a single-line monospace chip (8 pt below the title, middle-ellipsized,
full content width), then the detail: "via ‹host›" when there is one, above
"Approve to verify with your face." The text is flattened to
printable characters and length-limited. It is informative only: a process can
rename itself, so it never influences authentication.

## Password (polkit)

When FaceGate is the polkit agent, a declined, timed-out, or failed face step
turns the island into a password view rather than handing off to another
dialog. It keeps the requester title and command, replaces the hint with
"Enter your password to allow this.", and adds a 44 pt password field 16 pt
below the copy with **Cancel** / **Allow** 16 pt below it. The island takes
exclusive keyboard focus only while this field is shown. While PAM checks the
password the field locks and the ring spins; a wrong password shakes the mark
and reads "Incorrect password. Try again." After a failed face match, "Not
recognized" holds for 0.9 s before the field appears.
