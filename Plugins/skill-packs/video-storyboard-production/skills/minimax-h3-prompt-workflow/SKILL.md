# MiniMax H3 Prompt and Workflow

Compile approved shot specs into MiniMax H3 / ComfyUI execution jobs. This skill is model-specific and must not alter narration meaning.

## H3 constraints

- Native H3 canvas uses a 768px short edge and is capped around 768×1344, axes aligned to multiples of 32.
- At 24 fps, requested length snaps upward to the model's `17k+5` frame grid. Plan durations from legal frame counts rather than cutting arbitrary frames after generation whenever possible.
- Prefer the trained practical range for one generation unit; split long sequences into shots and chain only where continuity is meaningful.
- H3 is CFG-free in the local core workflow: use `BasicGuider`, not a negative-prompt CFG branch.
- First-frame I2V and first+last-frame FL2V are geometry/continuity controls, not substitutes for a clear motion prompt.

## Mode selection

- T2V: new visual system or motivated hard reset; no exact composition to inherit.
- I2V: exact start composition/identity matters, destination can emerge naturally.
- FL2V: both starting state and destination state are semantically required and the in-between motion is physically plausible.
- R2V/Ref2VA: recurring identity/style/scene needs reference conditioning and the ref2va checkpoint is installed and validated.
- V2V/RV2V: source motion/timing itself is worth preserving; do not use merely to avoid designing a shot.

## Prompt assembly

Write one independently executable prompt per generation unit:
1. minimal stable style/continuity baseline;
2. exact start state when not supplied by an image;
3. ordered visible events, each tied to a narration beat;
4. camera behavior with a reason;
5. required end state;
6. concise prohibitions only for known recurring failure modes.

Do not stuff the prompt with abstract goals such as “explain cognition clearly”. Translate them into observable geometry, action and state change.

## Job manifest

For every render record:
- shot ID;
- narration time range;
- mode;
- width × height and provenance class (`NATIVE`, `TEST`, `UPSCALED`);
- legal frame count and expected duration;
- prompt;
- first/last/reference assets;
- seed, steps, sampler, scheduler;
- expected output prefix;
- semantic acceptance criterion;
- retake scope.

## Retakes

One failed shot must be rerunnable without rebuilding unrelated accepted shots. Preserve accepted outputs and their hashes. Do not create an all-or-nothing long render chain.

## Resolution rule

Never call a delivery resolution “native” unless the H3 generation itself produced that resolution. Upscaling is a separate postprocess provenance state.
