# Narration Production Orchestrator

Use this when the spoken narration/script is already final and the task is to build or rebuild the video around it.

## Source-of-truth order

1. Final narration text and approved timing/audio are immutable truth.
2. User-approved visual style and explicit prohibitions are next.
3. Continuity inventory and project-specific asset constraints come next.
4. Existing shots are evidence/references only; never force a new plan to preserve old segmentation.
5. Workflow convenience, model defaults, and old shot numbers are subordinate.

## Pipeline

1. Freeze narration and timing. Build a sentence/meaning ledger; each spoken idea must have a visible job.
2. Run `continuity-inventory` once across the whole piece. Freeze recurring characters, places, motifs, spatial rules and reusable visual metaphors before shot writing.
3. Run `narration-to-storyboard`. Split by semantic change, visual focus and model-safe generation duration, not by legacy segment numbers.
4. Run `shot-specifier` on every storyboard unit. Require a start state, visible event, end state and transition anchor.
5. Run `minimax-h3-prompt-workflow`. Choose T2V/I2V/FL2V/R2V per shot, legal frame count, native resolution, references and retake boundary.
6. Generate a small representative pilot before full production: at least one abstract-concept shot, one continuity-heavy shot and one human/character shot if applicable.
7. Run `generated-video-review`. Reject semantic mismatch before spending on upscale/subtitles/final assembly.
8. Only after all accepted shots pass, assemble to the locked narration, then perform technical delivery gates.

## Mandatory rules

- Never begin with an arbitrary target such as “42 segments”. The final shot count is an output of narration analysis.
- One visual beat must carry one dominant explanatory function. Decorative spectacle is not a substitute for semantic evidence.
- Repetition is allowed only when it creates deliberate visual grammar. Reusing the same weak metaphor for multiple distinct claims is a failure.
- Use continuity within a narrative space; use a motivated cut when the narration changes conceptual layer. Do not chain every shot only because last-frame chaining is available.
- Do not generate new stills merely because a workflow expects them. First decide whether a keyframe is actually needed.
- Keep low-resolution experiments explicitly labeled as tests. Final generation provenance must state whether it is native or upscaled.

## Production outputs

Produce and maintain:
- narration ledger with timing;
- continuity inventory;
- storyboard/shot manifest;
- prompt/workflow manifest;
- take-review log and retake decisions;
- final assembly manifest with source provenance.
