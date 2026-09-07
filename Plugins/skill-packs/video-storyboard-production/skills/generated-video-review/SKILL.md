# Generated Video Review

Review each generated take before upscale, subtitles or final assembly. Compare against the approved narration/storyboard/shot manifest, not against how impressive the clip looks in isolation.

## Review order

1. Semantic match: does the clip visibly support the spoken claim at the intended moment?
2. Concept discrimination: if narration distinguishes A/B/C, are those differences actually visible without text?
3. Continuity: recurring character, world, map topology, screen direction and symbolic grammar remain coherent where continuity is required.
4. Causality/action: visible motion has a plausible start, connection, result and end state.
5. Composition/readability: the intended subject/relation is visually dominant, not buried in effects.
6. Model artifacts: anatomy, identity drift, unwanted text/UI, repeated blobs, broken geometry, frame duplication or obvious interpolation.
7. Technical provenance: actual resolution, fps, frame count, duration, audio state and SHA match the job manifest.

## Severity

- BLOCK: wrong meaning, missing explanatory evidence, contradiction with narration, severe continuity break, wrong provenance/resolution.
- RETAKE: meaning is basically right but shot execution, composition, motion or identity is not acceptable.
- TRIM/ASSEMBLY FIX: source take is good; only timing/cut point needs adjustment.
- PASS: semantically and technically usable.

## Retake discipline

Retake the smallest failing unit. Do not regenerate upstream accepted shots merely to preserve a chain. If continuity requires a new starting image, derive it from an accepted take or explicitly approved asset rather than rerolling the whole sequence.

## Final acceptance

Before assembly, every narration clause must map to at least one PASS visual event. Repeated footage must have an explicit narrative reason. No low-resolution TEST asset may enter a native-delivery path without an explicit upscale provenance record.
