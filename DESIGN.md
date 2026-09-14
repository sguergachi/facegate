# FaceGate design

FaceGate's interface is small, dark and physical. Its look follows Apple's
Human Interface Guidelines for macOS and iOS (clarity, deference, depth),
adapted to a KDE Plasma desktop. This document has two parts: the **system**
(principles and rules every surface follows) and the **contracts** (the
measured specs of each surface). New UI should follow the system first. When a
contract changes, update its numbers here, in the code constants, and in the
tests that guard them.

---

## Part 1: The system

### Principles

1. **One thing at a time.** Every screen has one job and one primary action.
   If a screen needs two primary buttons, it is two screens.
2. **Content leads, chrome recedes.** No title bars, borders, toolbars or
   visible chrome that isn't doing work. Controls appear where they're needed
   and nowhere else (the scroll indicator only while scrolling, Remove only
   after the trash button).
3. **Physical, not flat.** Surfaces behave like objects: the island grows out
   of the bezel, headers collapse under your finger, rows slide rather than
   pop. Every change is continuous from what's on screen. Nothing jumps.
4. **Honest about security.** Say who is asking, what will happen, and what
   happens to your face. Never imply that a button grants access when it only
   starts a check. The password is always a way through.
5. **Calm by default, expressive at moments that matter.** The mark breathes
   quietly at rest. Colour, pulses and shakes are saved for results.
6. **No dead ends.** Every state offers a way forward: Try Again, Not Now,
   Cancel, or the password.
7. **One continuous space.** Moving between steps should feel like moving
   within a single object, not flipping between screens (after Benji Taylor's
   [Family](https://benji.org/family-values) and
   [Honk](https://benji.org/honkish) write-ups).

### Colour

A true-black, dark-only palette taken from Apple's dark system colours. Black
is the material; content is light on it.

| Role | Value | Use |
| --- | --- | --- |
| Background | `#000000` | Island, expanded configuration, collapsed bars |
| Fill | `#1c1c1e` | Rows, secondary buttons, close button, chips |
| Fill, hover | `#2c2c2e` | Hover on any fill |
| Fill, pressed | `#3a3a3c` | Active/pressed fill |
| Label | `#f5f5f7` | Titles, row titles, primary button fill |
| Secondary label | `#98989d` | Body, detail, quiet buttons |
| Tertiary label | `#8e8e93` | Section headings, dates, icons at rest |
| Quaternary label | `#6e6e73` | Placeholders, empty states |
| Hairline | `rgba(255,255,255,0.08)` | Collapsed-bar separator |
| Scanning (cyan) | `#64d2ff` | Scanning, informational icons |
| Success (green) | `#30d158` | Verified, face added |
| Failure (red) | `#ff453a` | Not recognized, errors, destructive buttons |

Rules:
- Colour carries meaning, never decoration. Cyan means looking, green means
  done, red means failed or destructive. Nothing else is coloured.
- Primary buttons are white with black text. Secondary buttons are `#1c1c1e`
  with white text. There is no blue accent button.
- The IR preview is the only imagery. It is shown as luminance, tinted cool
  (`#d6f0ff`), and takes on the result colour only at a result.

### Typography

Inter (falling back to SF Pro Text, then sans-serif) with tabular figures.
Type sizes and weights, not colours, carry the hierarchy.

| Style | Size / weight | Line height | Tracking | Use |
| --- | --- | ---: | --- | --- |
| Page title | 28 bold | 32 | -0.5 | Onboarding step titles |
| Large title | 28 bold | 32 | -0.5 | Configuration header (expanded), same as a page title |
| Title | 17 semibold | (island) | -0.2 | Island title, collapsed header title |
| Body | 15 regular | 20 | 0 | Onboarding body |
| Button | 15 semibold | 20 | 0 | Primary and secondary buttons |
| Row title | 15 semibold | 20 | 0 | List rows, promise titles |
| Detail | 13–14 regular | 20 | 0 | Header detail, step text, promise text |
| Caption | 12–13 | 16 | 0 | Row dates, section headings, status |

Line heights sit on the 4-point grid, so every text block's height is a
multiple of 4 and the rhythm holds through wrapped copy.

Rules:
- Center-align text on single-column surfaces (island, hero, onboarding).
  Left-align text inside rows and lists.
- Wrap at word boundaries and keep text off the rim: island text is 280 wide
  (32 from each edge); configuration text keeps the page margin.
- Commands use JetBrains Mono 12 in a `#1c1c1e` chip, middle-ellipsized to one
  line.

### Copy and voice

Apple's voice: short, direct, friendly, and in the second person.

- **Titles say what's happening or what you can do**: "Adding your face",
  "Face added", "Private by design". Use sentence case for titles and body.
- **Buttons are verbs in title case**: Approve, Deny, Add a Face, Set Up Face,
  Try Again, Not Now, Remove. Two words at most where possible.
- **Name the actor**: "Claude Code wants sudo". Don't write "An application
  is requesting elevated privileges".
- **Errors say what happened, then what to do**: "Too dark. Move a little
  closer." Never show raw error text, exit codes or stack traces.
- **Never overclaim.** Approve starts a check; it doesn't grant access. Say
  "Approve to verify with your face."
- Use typographic punctuation: the ellipsis `…` for ongoing states ("Waiting
  for approval…"), curly apostrophes where the font supports them.

### Space and layout

#### The spacing scale

Everything is on an **8-point rhythm with 4-point half steps**. Every length
is a multiple of 4. Structural lengths (margins, radii, row and button
heights, gaps between groups) are multiples of 8. Use the named steps for
gaps; never invent an in-between value (no 6, 10 or 14).

| Token | Value | Use |
| --- | ---: | --- |
| `SPACE_XS` | 4 | A label and its own caption |
| `SPACE_S` | 8 | Things that belong together: title → body, row → row, paired buttons |
| `SPACE_M` | 12 | A control's inner inset, icon → text, row padding |
| `SPACE_L` | 16 | Parts of one group: mark → title, list items, header → list, button clearance |
| `SPACE_XL` | 24 | Separate groups: page margin, art → title, copy → content, dots → buttons |
| `SPACE_XXL` | 32 | Top-bar inset (close button), bottom fade |

The closer two things are related, the smaller the step between them: a
title is 8 from its body but 24 from the art above it. If a gap looks wrong,
move to the next step on the scale instead of nudging by pixels.

#### Rules

- **Measure from what you see.** Spacing is the distance between visible
  edges, never between invisible slots or containers. A 144 mark and a 64
  shield both sit exactly 24 above their title.
- **Margins:** configuration's page margin is 24 on every side; compact island
  states use a 16 inset. Content never touches an edge.
- **Hug or commit.** Compact island states are exactly as tall as their
  content. Configuration expands the island to a fixed 424 × 680 composition,
  and its scroll areas fill whatever space is left.
- **Balance short content.** When a step's content is shorter than its
  space, the whole group (art, title, body) sits a little above centre (40%
  of the spare space above, 60% below, snapped to the grid), rather than
  leaving a hole above the buttons.
- **Actions live at the bottom**, 24 from the bottom and sides, 48 high. A
  second choice is a secondary button **beside** the primary (8 apart,
  secondary on the left, primary on the right), never a text link stacked
  above it.
- **Clearance above floating actions:** scrolling content fades out over 32,
  then leaves 16 of clear black before the button. A list row must never
  touch a button.
- **Clearance below headers:** content starts 16 below the header's bottom
  edge, clear of its fade.
- **Verify by measurement.** Check layouts against real widget bounds
  (every edge on the grid, every gap a named step), not by eye.

### Shape: concentric corners

Nested rounded shapes share a centre, as on Apple hardware:
**outer radius = inner radius + the gap between them.**

- Island lower corners: `44 / 2 + 16 = 38`, concentric with its pill buttons.
- Expanded configuration: `48 / 2 + 24 = 48`, concentric with its primary buttons.
- The configuration close button (32) is centred on the corner radius's centre:
  inset `48 − 16 = 32` from the top and right. The collapsed header's content
  sits on that same centre line (y = 48), in a 96 bar.
- Face rows: a 40 avatar and a 40 row button inside 12 padding make a 64 row
  with radius `40 / 2 + 12 = 32`, so rows are pills concentric with both.
- Pills are fully rounded (radius = height / 2). Chips use 8.
- The island's top meets the bezel with concave 10 pt shoulders, so it looks
  machined into the edge rather than stuck on.

### Depth and materials

- Black is the only material. Depth comes from **fades and motion, not
  shadows**: headers and bottom bars fade content into black with gradients.
- A single 1 px hairline separates the collapsed header from the list.
- Imagery (the IR preview) dissolves into the surface: a 36 pt feathered
  rounded-rectangle mask, never a hard frame.

### Controls

| Control | Spec |
| --- | --- |
| Primary button | Full-width pill, 44 (compact) / 48 (configuration), white fill, black 15 semibold label |
| Secondary button | Same pill, `#1c1c1e` fill, white label. The second choice beside a primary (Deny, Not Now, Add Another Look), or a lone Cancel while a scan runs |
| Close | 32 circle, `#1c1c1e`, 1.75 pt ✕ glyph drawn at 9 pt, top-right on the corner centre |
| Icon button | 40 circle, transparent until hover. Destructive icons turn red on hover |
| Destructive | Red pill (40 high). Only reachable as a second step: trash, then **Remove** (reverts after 4 s) |
| Password field | 44 high, radius 12, `#1c1c1e` fill, 1.5 pt inner ring on focus |
| Scroll indicator | Overlay only: 4 pt slider at 24% white in the right gutter, centred between the rows and the island edge. 6 pt at 42% on hover. Never drawn over content, hidden when idle |
| Page dots | 6 pt dots, `#3a3a3c`. The current page is an 18 pt white capsule, which grows as paint (a spring on the frame clock), never by animating layout |

Rules:
- Hit targets are at least 32 pt (close), usually 40–48.
- There are no text-link buttons. Every choice is a pill, and a pair of
  choices shares one row.
- A disabled primary becomes a secondary-coloured pill with tertiary text
  that says why it's waiting ("Waiting for approval…"). It is never a greyed
  copy of the enabled button.
- **Every button gives under the pointer:** `scale(0.97)` on press, over
  160 ms on the strong ease-out. Colour and fill changes on hover take 180 ms
  with `ease`.

### Motion

Motion explains cause and effect. It never decorates. These rules follow Emil
Kowalski's animation standards ([emilkowal.ski/skill](https://emilkowal.ski/skill)),
Apple's fluid-interface talks, and the Family and Honk write-ups.

#### Should it animate?

Decide by how often someone sees it:

| Frequency | Examples in FaceGate | Motion |
| --- | --- | --- |
| Many times a day | Escape, Enter, hover | None, or colour only |
| A few times a day | The sudo/polkit island | Standard: quick, springy, never in the way |
| Occasionally | Adding or removing a face, the collapsing header | Standard |
| Rarely or once | Onboarding, Face added, the How it works story | May add delight |

Every animation needs a reason: spatial continuity, a change of state,
explanation, feedback, or avoiding a jarring jump. "It looks nice" on
something seen often is not a reason. When unsure, delete it.

#### Easing

- **Entering and leaving: strong ease-out**, `cubic-bezier(0.23, 1, 0.32, 1)`
  (`EASE_OUT`). Things leaving also start fast; **ease-in is never used on UI**,
  because it delays the moment someone is watching.
- **Moving or morphing on screen: strong ease-in-out**,
  `cubic-bezier(0.77, 0, 0.175, 1)` (`EASE_IN_OUT`). Example: letters gliding
  in a label morph.
- **Colour and hover:** `ease`.
- **Constant motion** (the scanning comet, IR frame blend): linear.
- Built-in easings (`ease-out`, quartic, smoothstep) are too weak for UI
  transitions. Use the named curves.

#### Duration

- **UI motion stays under 300 ms.** Press feedback 160 ms, hover 180 ms, copy
  out 100–140 ms, copy in 180–260 ms, label morph 300 ms, surface crossfade
  250 ms, row slide 260 ms.
- **Exit faster than enter** (asymmetric): the system's response snaps, and
  arrival settles.
- **Allowed to run longer**, because they explain or celebrate rather than
  respond: the How it works story loop, the success ring closing (the mark
  blends over 550 ms), and a new face's green glow fading over 900 ms.
- **Stagger** related content by 40 ms, top to bottom. Stagger is decorative
  and never blocks input.

#### Springs

- Use springs for shape and anything **alive or interruptible**: the island
  (a Dynamic Island), step content travel, art scale, and page dots. Springs
  keep their velocity when retargeted, so a change mid-flight never restarts.
- They are parameterised like SwiftUI (response, damping ratio): 0.50/0.80
  for the island's entrance, 0.45–0.50/0.86–0.90 for re-layout and arrivals,
  and 1.0 (no bounce) for exits and dots.
- Keep bounce barely there (damping ≥ 0.8). Bounce is for playful moments,
  and FaceGate is a security surface: calm, precise, trustworthy.

#### Physicality

- **Never appear from nothing.** Entrances start at 95–97% scale (never 0)
  with opacity 0, then settle up or into place by 6–24 pt.
- **Blur masks cross-fades.** Content that is fading blurs by
  `4 px × (1 − opacity)` (about 2 px at the midpoint), so an old and a new
  state read as one thing changing, not two things overlapping.
- **Only transform and opacity animate.** Never animate width, height,
  margins or minimum sizes. Page dots are drawn; the status line fades in
  place over reserved clearance instead of sliding open. (GTK revealers on
  list rows are the one tolerated exception: rows are few and rare.)
- **Blend from what's visible.** Tweens retarget from the current value, and
  springs from the current position and velocity. An interrupted transition
  never jumps.
- **Direction means something.** Progress runs clockwise from 12 o'clock.
  Forward content enters from the right, and island content settles downward,
  following the island's growth.
- **Scroll-linked motion is 1:1.** The collapsing header maps the scroll
  offset straight to its progress, with no easing and no delay.
- **Frame-clock driven.** Continuous motion runs on the display's frame clock
  (every refresh at 144/165 Hz), never on fixed timers.
- **Results get one gesture each:** success is a green close plus a small
  pulse, failure a red, decaying shake (5.5 Hz, about 0.4 s).

#### Reduced motion

Plasma's *Animation speed: Instant* turns off `gtk-enable-animations`, and
FaceGate treats that as a request for reduced motion (`reduced_motion()`).
Reduced means gentler, not none. Fades and colour changes stay, but travel,
scale, lift, letter glides and bounce are removed.

### Fluidity

Rules for moving between steps, pages and states:

- **Persistent elements never move.** Anything present before and after a
  transition (primary button, page dots, close button, the mark, the island
  surface) stays exactly where it is and changes in place. Only content that
  is actually different moves. Duplicating a persistent element and sliding
  both copies is forbidden.
- **Pin controls; never stack them.** The dots and the button row are placed
  independently, so a step without dots leaves the buttons exactly where they
  were. A choice that exists on some steps joins the primary's row as a
  secondary button: the primary narrows on a spring (0.40/1.0) while the
  secondary fades in, and it widens back as the secondary fades out, quicker
  than it came. The status line fades in place over reserved clearance.
- **Labels morph, they don't swap.** A button's label changes in place over
  300 ms. Runs of two or more shared letters glide on the ease-in-out curve
  (Continue → Confirm keeps "Con"), while the rest fade out upward and in from
  below on the ease-out curve. Lone shared letters and long glides are not
  matched, because letters flying across a button read as noise.
- **Directional travel.** Forward content leaves 16 pt to the left and enters
  24 pt from the right, and back does the opposite. Incoming spring 0.45/0.90,
  outgoing 0.40/1.0.
- **Sequence, don't overlap.** Outgoing copy fades in 140 ms. Incoming copy
  starts 130 ms later and fades in over 260 ms, staggered 40 ms per piece, so
  two lines of text never share a position.
- **Art changes in place.** When a step's illustration differs, the old art
  fades in 150 ms and settles to 95%, and the new one grows from 95% on a
  near-critical spring (0.45/0.86), 80 ms later, over 280 ms. When the art is
  the same (the mark across Welcome, Setup and Face added), it doesn't
  transition at all; only its state changes.
- **Copy changes within a step** fade out (100 ms), swap, then settle 6 pt up
  into place (220 ms), while anything below springs to its new position.
- **Surfaces hand over in place.** Onboarding becomes the face list with a
  250 ms crossfade, because both share the same surface, corners and button
  position.
- **Motion responds to state.** Colour and fill changes (a button becoming
  secondary or disabled) use the 180 ms `ease` transition and are never
  instant.

### The presence mark

FaceGate's character: an abstract lens ring with two lights and a smile. It is
friendly and sensing, and never a literal or mask-like face.

- **At rest** (approval/password): a faint ring and a gentle 3.4 s breath, with
  a blink every 4.6 s.
- **Scanning:** a cyan comet spins clockwise around the ring.
- **Success:** the comet grows forward into a closed green ring, then pulses.
- **Failure:** red, a frown, a decaying shake.
- **Password:** the face belongs to the camera, so whenever a password is being
  typed or checked the mark is a **lock** instead. The face leaves in the first
  half of the blend and the lock arrives in the second, never overlapping.
  Checking spins a white comet (cyan means the camera is looking). A wrong
  password shakes the lock red; an accepted one closes the ring green while the
  shackle lifts open.
- The same mark scales from 32 (collapsed header) to 148 (onboarding). Draw
  it as vectors at its displayed size where possible. Row avatars are a
  still 36 pt mark.

### Interaction and security rules

- **Consent before camera.** The camera never starts without an explicit
  Approve, or an admin approval in the enrollment flow.
- **Changes need admin, every time.** Listing faces needs no prompt. Adding or
  removing one always asks for admin approval in the island (polkit
  `auth_admin`, never kept).
- **Destructive actions take two steps** and revert themselves if not confirmed.
- **Escape always backs out**: it declines an approval, cancels a scan, or
  closes configuration back into the bezel.
- **The password is always available.** Deny, a timeout, or a failed match
  leads to the password, never to a dead end.
- **Say who's asking** (the app and host), but treat it as information only;
  it must never affect authentication.
- **Preview frames are ephemeral**: streamed to the island, never saved.

### Accessibility

- Text on black meets WCAG AA: `#f5f5f7` and `#98989d` on `#000` pass.
  Don't put tertiary grey (`#6e6e73`) on content that must be read.
- Every icon-only button has an accessible label ("Close", "Remove this face").
- Everything can be done with the keyboard: Enter confirms, Escape backs out,
  and the password field takes focus when it appears.
- Never use colour alone: a result always changes the title too ("Verified",
  "Not recognized").

---

## Part 2: Surface contracts

### The island

FaceGate's primary surface is a top-edge interaction, not a conventional
window: a pure-black island that grows down out of the monitor's top bezel,
like a hardware notch. It's a Wayland layer-shell surface on the overlay layer.

#### Layout

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
secondary (**Deny**, left; declining the scan leaves sudo at its password
prompt). Scanning and result states show no button, because Howdy cannot be
interrupted once it is running.

#### Motion

- **Entrance:** the island springs out of the bezel from a 124-wide notch
  (width response 0.50, height 0.56, damping 0.8; under 3% overshoot). The
  mark, title, detail, and buttons follow with a 40 ms stagger, each fading
  in over 300 ms (strong ease-out) while settling 8 pt downward from 96%
  scale.
- **Reveal:** content opacity is also gated by the island's current size, so
  text is uncovered by the island and never drawn over the desktop.
- **State change:** the mark reacts immediately and blends over 550 ms. Copy
  leaves in 140 ms, then the island re-springs to the new height (0.50 / 0.86)
  while the new copy fades in over 260 ms.
- **Result:** the ring is one continuous conic-gradient stroke, so success
  grows the scanning comet forward (clockwise, like the scan), evens its tail,
  and closes it into a green ring with no redraw or swap; the small pulse lands
  as it closes. Failure turns red with a short, decaying shake.
- **Exit:** content fades in 160 ms, then the island retracts into the bezel
  on a critically damped spring.

#### Dismissal

There is no separate close control: **Deny** declines the request (sudo falls
back to its password prompt). Escape does the same, or dismisses any other
state and keeps the island hidden for the rest of that attempt. A 20 s
watchdog closes a scan that never receives a result. (Expanded configuration
is the exception: it has a round close button, and Escape inside an approval
or scan it started returns to it.)

#### Placement

Each time the island appears it asks KWin for the active output
(`org.kde.KWin.activeOutputName`, which follows the mouse by default) and pins
itself to the top center of that monitor. Without KWin it falls back to the
output the compositor chooses.

#### Requester

The PAM adapter runs inside `sudo`, so it reads the exact command from its own
argv and walks up the process tree, skipping shells and wrappers, to name the
app that ran it and the app hosting that (for example **Claude Code** via
**T3 Code**). The approval title becomes "‹app› wants sudo". The command sits
in a single-line monospace chip (8 pt below the title, middle-ellipsized, full
content width), then the detail: "via ‹host›" when there is one, above
"Approve to verify with your face." The text is flattened to printable
characters and length-limited. It is informative only: a process can rename
itself, so it never influences authentication.

#### Password (polkit, and sudo without a face)

When FaceGate is the polkit agent, a declined, timed-out, or failed face step
turns the island into a password view rather than handing off to another
dialog. `sudo` uses the same view when no face is enrolled: the password goes
back to PAM for `pam_unix` to check, a rejected one returns as "Incorrect
password. Try again." on sudo's next try, and the account rule reports
"Verified". If no verdict arrives within 15 s the island retracts; every attempt starts
with an empty field. It keeps the requester title and command, replaces the hint with
"Enter your password to allow this.", and adds a 44 pt password field 16 pt
below the copy, with **Cancel** / **Allow** 16 pt below that. The island takes
exclusive keyboard focus only while this field is shown. While PAM checks the
password, the field locks and the ring spins. A wrong password shakes the mark
and reads "Incorrect password. Try again." After a failed face match, "Not
recognized" holds for 0.9 s before the field appears.

#### Add Face (enrollment)

Configuration lives in the island's own process, so it hands the scan over
with a direct call, never a socket round-trip (a blocking handshake would wait
for a reply its own main loop has to produce). It announces the scan before
asking for admin approval, so the same surface goes from configuration, to
approval or password, to the scan, and back, with no configuration showing in
between:

- Approved by password, the island turns into the scan at once. Approved by
  face, "Verified" holds for 0.6 s first.
- While the root helper opens the camera, the scan shows its comet and
  "Waiting for the camera…"; its first frame fades straight into the preview.
- A late face result for that approval never replaces the scan.

External tools (the enrollment demo) still stream over the HUD socket. The mark
gives way to a 312 × 208 live IR preview at the top inset, 16 pt above the
title. The island narrows from its configuration width to the compact 344
width and hugs the enrollment content.

- **Preview:** the camera crop follows the face (eased; the face is about half
  the height, eyes a little above center). It's shown as luminance tinted a
  cool `#d6f0ff`, and masked by a rounded rectangle (radius 30) whose last
  36 pt feather to nothing, so the image dissolves into the black island.
- **Fluidity:** IR emitters often light only every other frame, so unlit
  frames are skipped and each lit frame cross-fades into the next over 70 ms
  on the frame clock.
- **Progress:** a trace on the preview's rim, clockwise from top center. A
  short comet circles until the first sample. Each of the five samples springs
  the trace forward with a brief glow. Success closes it green and tints the
  image green. Failure turns it red with the mark's decaying shake.
- **Copy:** "Adding your face", with the detail line as the scan's live hint.
  The hint cross-fades (100 ms out, 240 ms in) while the island eases to the
  new height. A single full-width **Cancel** replaces the Deny/Approve pair.
  Results read "Face added" or "Couldn't add your face" with the reason, then
  the island returns to the onboarding or face-list state beneath it.

### Expanded configuration island

The top-edge island expands to a fixed 424 × 680 composition. It never creates
an xdg-shell window: its black material remains attached to the bezel, growing
from the same notch with concave shoulders. The round close button and Escape
retract it.

The panel is always laid out at full size and clipped to the island's current
shape, so the growing island uncovers it (and a shrinking one covers it)
while it fades in or out. Content never waits for the island to settle, and
is never drawn over the desktop. Width, height and lower radius (38 ↔ 48)
share the island's springs.

#### Manage page

The hero is composed exactly like an onboarding step (144 mark, 24, 28 pt
title, 8, one line of 15 pt body), so onboarding hands over to the list
without the mark or title changing proportion.

| Element | Value |
| --- | ---: |
| Expanded island | 424 × 680, lower radius 48, page margin 24 |
| Close button | 32 at (360, 32) |
| Hero mark (expanded) | 144, top at 56, centred (well clear of the close button horizontally) |
| Mark → large title | 24 |
| Large title → detail | 8; the detail is one 20-high line, ellipsized, so the header never changes height with its copy |
| Header (expanded / collapsed) | 292 / 96 |
| Detail → "Faces" heading | 24 (header bottom 8 + 16 below it); heading inset 12, level with row content |
| Heading → rows | 8 |
| Rows | 64 high, radius 32, padding 12, 8 apart; 40 avatar, 40 row button |
| Bottom bar | 32 fade + 16 clearance + 48 button + 24 margin (button at y 608) |

- **Collapsing header:** as the list scrolls, progress
  `t = offset / (header height − 96)` drives it 1:1. The mark shrinks
  144 → 32 and moves 12 to the left of the title. The title scales 28 → 17
  and rises until the pair is centred as a group on the close button's line
  (y = 48). The detail rides up with the title and is gone by `t = 0.45`. The
  header shrinks to a 96 pt bar, and a hairline fades in over the last 15%.
  The header's visual layer ignores input so the list scrolls beneath it, and a
  transparent scroll target matching its current height sits on top.
- **Rows** slide in (260 ms) when they arrive, and a newly added face flashes
  green (22%) and settles over 900 ms. Removal: trash → **Remove** (red), then
  a spinner while admin approval is asked, then the row slides away. If
  approval is declined, the row returns and the status reads "Not approved, so
  the face was kept."
- **Primary button:** Set Up Face (no faces) / Add a Face. It becomes a
  disabled "Waiting for approval…" until the island takes over for approval
  and the scan. The status line (red, or green for
  success) fades in place in the clearance above it.

#### Onboarding

Shown on first launch (until finished or skipped, stored in
`~/.config/facegate/settings.json`), or with `facegate-settings --welcome`.
Onboarding is one frame, not a stack of pages (see Fluidity):

| Element | Value |
| --- | ---: |
| Top bar | 96; matching 32 circles at (32, 32): arrow at top-left after the first step, X at top-right; content starts below it |
| Art | mark 144, story 376 × 176, shield 64 |
| Art → title | 24 |
| Title → body | 8 |
| Copy → content | 24 (lists inside use 16 between items, icon → text 12) |
| Page dots | 8, 8 apart (current: 24 capsule), 24 above the buttons |
| Buttons | 48, 24 from the bottom and sides (y 608); a pair is 184 + 8 + 184 |

The group of art, title, body and content is balanced in the space between
the top bar and whatever sits above the buttons (see Space and layout). The
dots and the button row persist across steps. The primary is one button
throughout: it narrows to share its row when a step has a second choice, and
widens back when it doesn't. Only the step's title, body and content travel.
Back reverses that same directional transition. During setup it also ends any
pending approval or scan before returning to Private by design.

1. **Meet FaceGate:** the 144 pt living mark. "Approve sudo and admin prompts
   with a glance. Your password always works too." → Continue.
2. **How it works:** a miniature desktop (376 × 176, radius 22) whose island
   loops through approval → scanning → verified → retract (7 s), using the
   real island path and mark at 0.6 scale. Three numbered steps below light up
   in sync with it (others dim to 38%). → Continue.
3. **Private by design:** three cyan-icon promises: camera only when you
   approve, stays on this computer, changes need admin. → Not Now | Set Up
   Face.
4. **Setup:** with no existing face, "Enter your sudo password" explains that
   the first face cannot approve itself; otherwise the mark follows the flow:
   "Authenticate to add a face" (waiting),
   "Look at the camera" (scanning, Cancel), or "Face not added" with the reason
   (Not Now | Try Again).
5. **Face added:** a green mark. → Add Another Look (for example with
   glasses) | Done.
