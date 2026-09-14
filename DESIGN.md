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
- The camera preview is the only imagery, and it is never the raw IR
  picture, which lights skin from within and looks ghostly. It is starlight:
  cool (`#d6f0ff`) particles that form the face, taking on the result colour
  only at a result.

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
  edges, never between invisible slots or containers. Hero art (the mark,
  the display, the shield) is drawn in one 72-unit square whose lowest
  visible edge is 10 from its bottom, so every piece sits the same distance
  above its title.
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
- **Every row is one pill layout** (64 high, radius 32, 12 padding):
  - A leading 40 icon sits in the padding, concentric with the rounded end,
    and its text follows 12 later (x 64).
  - Text with nothing before it starts **24** in. At the title's height the end
    cap curves ~6 pt inward, so 12 would look cramped against it.
  - Whatever trails the row (trash button, chevron, radio ring, switch thumb)
    is centred on the far end cap's centre, 32 from the right edge.
  - Title and detail (20 + 16) are centred vertically, 14 from top and bottom.
  - Section headings sit 24 in, level with text-only rows.
- Pills are fully rounded (radius = height / 2). Chips use 8.
- The island's top meets the bezel with concave 10 pt shoulders, so it looks
  machined into the edge rather than stuck on.

### Depth and materials

- Black is the only material. Depth comes from **fades and motion, not
  shadows**: headers and bottom bars fade content into black with gradients.
- A single 1 px hairline separates the collapsed header from the list.
- Imagery (the camera preview) dissolves into the surface: a 36 pt feathered
  rounded-rectangle mask, never a hard frame.

### Controls

| Control | Spec |
| --- | --- |
| Primary button | Full-width pill, 44 (compact) / 48 (configuration), white fill, black 15 semibold label |
| Secondary button | Same pill, `#1c1c1e` fill, white label. The second choice beside a primary (Deny, Not Now, Add Another Look), or a lone Cancel while a scan runs |
| Close | 32 circle, `#1c1c1e`, 1.75 pt ✕ glyph drawn at 9 pt, top-right on the corner centre |
| Icon button | 40 circle, transparent until hover. Destructive icons turn red on hover |
| Face row actions | Two 40 circles, 8 apart: Edit (pencil) and the trash on the end cap. One drawn control whose layout width is always the Remove pill's, so the row never reflows |
| Destructive | Red pill (40 high). Only reachable as a second step: the trash circle **grows leftward into** the **Remove** pill over both circles (right end fixed on the end cap, spring 0.40/0.90; icons fade out first, the word in once there's room). It narrows back after 4 s, on Escape, or when you click anywhere else (0.30/1.0), and into a spinner while admin approval is asked |
| Rename | Edit turns the name into a text field in the same slot, with all of it selected. The date fades as the name grows 15 → 20 and glides to the row's middle (spring 0.40/1.0, transform only), filling the row; saving reverses it back above the date. The row gains the 1.5 pt focus ring and the pencil becomes ✓. Return, ✓ or clicking away saves; Escape cancels. The new name shows at once and is put back if approval is declined |
| Password field | 44 high, radius 12, `#1c1c1e` fill, 1.5 pt inner ring on focus |
| Switch | Drawn, 52 × 32 with a 28 thumb. The thumb travels on a spring (0.35/1.0) while the track blends `#3a3a3c` → success green |
| Navigation row | The pill row with a 40 icon, title and detail, and a chevron |
| Choice row | The pill row with text only and a paint-only 16 pt radio ring |
| Scroll indicator | Overlay only: 4 pt slider at 24% white in the right gutter, centred between the rows and the island edge. 6 pt at 42% on hover. Its track spans only the visible content, from below the header's fade to above the bottom fade (the button bar, or the 48 corner), so a fade never draws over it. Hidden when idle |
| Footer action | Text only: 13 medium, tertiary grey, white on hover. 32 high hit target, centred, 24 from the bottom. Only for something rarely wanted (Replay Welcome) |
| Page dots | 6 pt dots, `#3a3a3c`. The current page is an 18 pt white capsule, which grows as paint (a spring on the frame clock), never by animating layout |

Rules:
- Hit targets are at least 32 pt (close), usually 40–48.
- There are no text-link buttons, with one exception: the main screen's
  footer action. Every other choice is a pill, and a pair of choices shares
  one row.
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
- **Scroll picks a state; springs move between states.** The collapsing header
  never rests part-way, so a slow scroll can't leave it half-collapsed.
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
- **Surfaces hand over in place.** Onboarding becomes the settings home with
  a 250 ms crossfade, because both share the same surface and corners.
  Settings destinations enter from the right and Back returns them to the
  left; the close button stays fixed throughout.
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
- **Changes need admin, every time.** Listing faces needs no prompt. Adding,
  renaming or removing one always asks for admin approval in the island
  (polkit `auth_admin`, never kept). That is why renaming starts from an
  explicit Edit button rather than a tap on the name.
- **Destructive actions take two steps** and revert themselves if not confirmed.
- **Escape always backs out**: it declines an approval, cancels a scan, or
  closes configuration back into the bezel.
- **Prompts wait for you.** An approval or password never times out on its
  own; if the island goes away, sudo falls back to its terminal prompt at once.
- **The password is always available.** Deny or a failed match
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

By default, each time the island appears it asks KWin for the active output
(`org.kde.KWin.activeOutputName`, which follows the pointer) and pins itself to
the top center of that monitor. View Options can instead persist one output's
connector and pin every appearance there. Choosing another monitor while
configuration is open never slides the island across the gap: it retracts
into its notch (the panel fades in 160 ms, the island collapses without
bounce), then grows open from the new monitor's notch as if just opened. If that output is disconnected,
FaceGate follows the pointer until it returns. Without KWin it falls back to
the output the compositor chooses.

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
with an empty field. When `pam_faillock` has locked sudo it refuses every
password, even the right one, so the island doesn't ask: the lock shakes red,
the field and Allow are disabled, the detail reads "Too many failed attempts.
Try again at ‹time›.", the keyboard is released, and the field returns on its
own when the lock expires. It keeps the requester title and command, replaces the hint with
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
gives way to a 312 × 208 live camera preview at the top inset, 16 pt above the
title. The island narrows from its configuration width to the compact 344
width and hugs the enrollment content.

- **Preview:** the camera crop follows the face (eased; the face is about half
  the height, eyes a little above center). It's drawn as starlight, after
  Apple Watch pairing: about 2,900 stars scattered through a field (5 apart,
  jittered), each a sharp core up to 1 pt whose size follows the brightness
  of the face beneath it in real time (60 ms), bloomed by a tight 1.6 and a
  wide 5 pt glow. Each frame is stretched between its 4th and 97th brightness
  percentiles, the dimmest 18% (noise, background) lights nothing, and a 2.2
  curve keeps eyes, nose and mouth readable. Every star twinkles on its own
  phase and rate (0.6–1.8 Hz, down to 65%). Cores are cool `#d6f0ff`, glows
  lean cyan; a rounded rectangle (radius 30) whose last 36 pt feather to
  nothing dissolves the field into the black island. Each captured sample
  brightens the stars briefly.
- **Materialize and vanish:** with the first frame the field comes alive over
  0.9 s, each star appearing at its own random moment and swelling into
  place, so the face sparkles into being. 0.45 s after a result the stars,
  already in the result's colour, wink out in random order over 0.7 s.
- **Fluidity:** the preview runs at the camera's own rate. In the root
  helper a camera thread reads every frame (OpenCV's buffer held to one, so
  nothing queues) and sends a cropped preview of each lit one at once, while
  an analysis thread takes the newest frame whenever it is free to find the
  face, which the crop follows, and collect samples; detection and
  recognition never hold the preview up, and only the newest preview is
  sent. IR emitters often light only every other frame, so unlit frames are
  skipped.
- **Rendering:** the preview is recorded entirely as GSK render nodes, which
  GTK's Vulkan renderer draws on the GPU; there is no GL and no CPU
  rasterising. Each display frame, NumPy writes one core radius and a lit
  flag per star into two tiny textures (even and odd rows), drawn
  nearest-neighbour so each fills its star's cell. A repeated, jittered tile
  holds the distance to each cell's star; a screen blend and a colour matrix
  turn radius minus distance into each core's anti-aliased edge in device
  pixels, two GPU blurs of the cores are the glow, a mask feathers the
  rounded rectangle, and the progress trace is a path stroke. Python's share
  is under a millisecond per frame.
- **Progress:** a trace on the preview's rim, clockwise from top center. A
  short comet circles until the first sample. Each of the five samples springs
  the trace forward with a brief glow. Success closes it green and turns the
  stars green. Failure turns it red with the mark's decaying shake.
- **Copy:** "Adding your face", with the detail line as the scan's live hint.
  The hint cross-fades (100 ms out, 240 ms in) while the island eases to the
  new height. A single full-width **Cancel** replaces the Deny/Approve pair.
  Results read "Face added" or "Couldn't add your face" with the reason, then
  the island returns to the onboarding or Face Approval state beneath it.

### Expanded configuration island

The top-edge island expands to a fixed 424 × 680 composition. It never creates
an xdg-shell window: its black material remains attached to the bezel, growing
from the same notch with concave shoulders. The round close button retracts
it. The main screen is the front door; setting screens add a matching Back
circle at the top-left, and Escape returns there before a second Escape closes
the surface.

The panel is always laid out at full size and clipped to the island's current
shape, so the growing island uncovers it (and a shrinking one covers it)
while it fades in or out. Content never waits for the island to settle, and
is never drawn over the desktop. Width, height and lower radius (38 ↔ 48)
share the island's springs.

#### Main screen

Every page after onboarding shares one hero (144 art, 24, 28 pt title, 8, one
line of detail, 292 high, collapsing to a 96 bar) and places its groups 16
below it, so moving between pages never changes proportion. The main screen's
hero is the living mark, **FaceGate**, and **Version ‹number›**: the only
version label in the interface, quiet in secondary text.

| Element | Value |
| --- | ---: |
| "General" heading | y 308; heading → row 8 |
| Use FaceGate row | y 332, 64 high |
| "Settings" heading | 24 below the switch row (y 420) |
| Face Approval / View Options rows | y 444 / 516, 8 apart |

- **Use FaceGate** reads "Approves sudo and admin prompts" when on and
  "sudo and admin prompts work as usual" when off, and the hero mark rests
  with its eyes closed. Off means entirely out of the way: the PAM module
  reads the switch itself (a regular file the user owns, links never
  followed) and returns before contacting the island, so sudo behaves as if
  FaceGate weren't installed even when the island isn't running. KDE's own
  agent takes polkit back. Faces and placement are kept.
- **Face Approval** shows the face count; **View Options** shows "Follows the
  pointer" or "Locked to ‹monitor›".
- **Replay Welcome**, the footer action, plays onboarding again in the same
  island (a crossfade, like onboarding handing over); finishing or Not Now
  returns to the main screen.
- Pages travel like onboarding copy (forward leaves 16 left, enters 24 from
  the right; Back mirrors it). Back appears top-left, mirroring the close
  button, and Escape goes back one level before it closes.

#### Face Approval page

The hero is composed exactly like an onboarding step (144 mark, 24, 28 pt
title, 8, one line of 15 pt body), so onboarding and the main screen hand over
to it without the mark or title changing proportion.

| Element | Value |
| --- | ---: |
| Expanded island | 424 × 680, lower radius 48, page margin 24 |
| Close button | 32 at (360, 32) |
| Hero mark (expanded) | 144, top at 56, centred (well clear of the close button horizontally) |
| Mark → large title | 24 |
| Large title → detail | 8; the detail is one 20-high line, ellipsized, so the header never changes height with its copy |
| Header (expanded / collapsed) | 292 / 96 |
| Detail → "Faces" heading | 24 (header bottom 8 + 16 below it); heading inset 24, level with text-only rows |
| Heading → rows | 8 |
| Rows | 64 high, radius 32, padding 12, 8 apart; 40 avatar, 40 row button |
| Bottom bar | 32 fade + 16 clearance + 48 button + 24 margin (button at y 608) |

- **Collapsing header:** only ever full (292) or a bar (96), never in
  between. A scroll's intent decides: 24 of travel down collapses it and 24
  up expands it, anywhere in the list. Each change runs on a no-bounce spring
  (0.35/1.0) while the list moves by the header's 196 difference, measured
  from where the gesture began, so the rows in view stay in view (from the
  top, that lands the first row 16 below the bar). Any list that overflows
  gets just enough invisible room at its end to reach the bar, so a short
  list collapses too; a list that fits never scrolls. Everything stays
  centred: the mark shrinks 144 → 32 to sit 24 from the top, the title scales
  28 → 17 and tucks directly beneath it, the detail fades out by
  `t = 0.45`, and a hairline fades in over the last 15%.
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

#### View Options page

The hero art is a display with the island at its top edge, drawn in the
mark's language (the same 72-unit square, a faint 1.5 outline, white
features) so it replaces the mark without changing weight, and shrinks into
the collapsed bar the same way. The same drawing at 40 is the View Options
row icon. It is not cyan: nothing is scanning.

Choices apply immediately; there is no Save:

- **Automatic:** Follow Pointer, "Shows on the monitor you're using."
- **Lock to a monitor:** every connected output by its description, then
  `width × height · connector`. Choosing one stores its connector and moves the
  open island there at once. The list updates as monitors are plugged in or
  out.

A locked monitor that is unplugged stays listed and selected as "Not connected
· following the pointer", so the stored choice and what actually happens are
both visible. Selection only repaints the radio rings; rows never rebuild or
move.

#### Onboarding

Shown on first launch (until finished or skipped, stored in
`~/.config/facegate/settings.json`), or with `facegate-settings --welcome`.
Onboarding is one frame, not a stack of pages (see Fluidity):

| Element | Value |
| --- | ---: |
| Top bar | 96; matching 32 circles at (32, 32): arrow at top-left after the first step, X at top-right; content starts below it |
| Art | mark 144, story 376 × 176, shield 144 |
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
   loops through approval → scanning → verified → retract, using the real
   island path and mark at 0.6 scale. Its heights hug its content with the
   real island's 16 inset (approval 220, scanning and verified 152). Three
   numbered steps below light up in sync with it (others dim to 38%), each for
   as long as it takes to read: its words at 3 per second, plus 1.2 s to find
   the line (about 17 s a loop). → Continue.
3. **Private by design:** a shield holding a padlock, drawn in the mark's
   language, over three promises: camera only when you approve, stays on this
   computer, changes need admin. Their cyan icons are drawn at one 1.5 weight
   in a 24 column, level with each title line, and the list sits as one
   block centred under the title. → Not Now | Set Up Face.
4. **Setup:** with no existing face, "Enter your sudo password" explains that
   the first face cannot approve itself; otherwise the mark follows the flow:
   "Authenticate to add a face" (waiting),
   "Look at the camera" (scanning, Cancel), or "Face not added" with the reason
   (Not Now | Try Again).
5. **Face added:** a green mark. → Add Another Look (for example with
   glasses) | Done.
