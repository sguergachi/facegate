# Enrollment preview quality audit — 2026-09-15

## Source → display

| Stage | Finding | Change / decision |
| --- | --- | --- |
| Camera / Gaze transport | Saved RGB preview is 848×480; inspected Gaze encoder caps width at 854, JPEG quality 88. | Keep Gaze capture ownership. Do not open a competing camera stream or change recognition settings. |
| Decode | Helper reduced the preview to 640 px before finding/cropping the face. | Preserve available detail up to 1280 px. The display-size fallback remains separate. |
| Detector | Bundled float32 ONNX detector is 128×128. Selecting one winning anchor shifts the face crop as scores trade places. | Score-weighted overlapping boxes. On a saved-frame translation probe, crop-center residual std fell from 0.904 to 0.715 px. This is one controlled probe, not a general accuracy benchmark. |
| Landmark model | Bundled FaceMesh V2 uses 256×256 input and emits 478 XYZ points. Checked real-frame and blank-frame presence logits. | Retain the model; preserve its inputs/outputs correctly. No demonstrated benefit from a model replacement in this audit. |
| Depth | Min/max normalization independently stretched depth every frame, then renderer rebuilt it using an arbitrary multiplier. | Preserve weak-perspective XYZ units through crop, framing, and versioned transport. Legacy helper payloads remain readable. |
| Topology | Previous pass restored the missing forehead crown with canonical connectivity. | Retain all 468 vertices, add canonical positions for sampling only. Live model points still set face geometry. |
| Particle sampling | Projected first-frame areas biased density by starting pose; contour bands and random clumps obscured shading. | Stratified canonical 3D area sampling, fewer feature strokes, no contour bands. |
| Lighting | Independent triangle flipping lit hidden surfaces; hard dither thresholds punched holes; highlights changed at camera cadence. | Signed normals, key/fill/rim lights, view-dependent highlight, soft shadow density, 65 ms radiance interpolation. Iris catchlights inherit their socket orientation. |
| Raster / optics | Integer-rounded DPI, four-tap spots, and clipped accumulated intensity made grain coarse and highlights muddy. | Supersampled Gaussian grains, fractional scale, physical point size, soft highlight shoulder, selective bloom. |
| Runtime | Higher-resolution dust increases CPU work. | Half-resolution atmosphere and separable Gaussian kernels; measured about 8 ms/frame CPU animation + node construction on this host. This excludes GPU rendering and camera inference. |

## Verification

109 tests pass; Python compilation and diff checks pass.

- Unit regressions: depth scale survives changes to unrelated points; uniform XYZ scale/translation preserves lighting; back-facing normals stay unlit; particle surface sampling is independent of first pose; camera detail survives decode; border crops replicate correctly; reset clears old face state.
- Existing enrollment, security boundary, guides, loss/dissolution, and settings tests retained.
- Offscreen GSK renders inspected: same saved portrait before/after; synthetic yaw −35°, −15°, 0°, +15°, +35°; formation, release, and every guide.
- The render tests use a previously captured frame, not a new live multi-pose RGB/IR enrollment. Synthetic rotation checks shading behavior, not landmark inference accuracy at those real poses.

## Source limits

This is a real-time monocular estimate, not a calibrated depth scan. MediaPipe does not supply hair, ears, skin pores, or hidden head geometry. Canonical sampling and added particles cannot invent those details. A denser reconstruction model would need a measured identity/pose/latency comparison before replacing the current model.

Sources: [MediaPipe Face Landmarker models](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker), [FaceMesh V2 model card](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20MediaPipe%20Face%20Mesh%20V2.pdf), [coordinate conventions](https://github.com/google-ai-edge/mediapipe/blob/master/docs/solutions/face_mesh.md), and Gaze source inspected locally at `/tmp/gaze-src/gaze/src/preview.rs`.
