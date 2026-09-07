# MiniMax H3 / ComfyUI Production Profile

This profile targets the local ComfyUI core MiniMax H3 nodes rather than assuming a third-party director UI.

## Verified core behavior

From ComfyUI 0.30.x core `nodes_minimax_h3.py`:
- native canvas logic is based on a 768px short edge, capped at 768×1344 pixel area and aligned to multiples of 32;
- generation runs at 24 fps;
- requested frame counts are aligned upward until `n % 17 == 5`;
- `MiniMaxH3ImageToVideo` accepts optional `first_frame` and `last_frame`;
- first frame is stretched to the target canvas as the geometry anchor; last frame uses aspect-preserving center cover-crop;
- the same joint AV latent carries video and audio; H3 conditioning is designed around `BasicGuider` rather than CFG negative prompting.

## Recommended local architecture

Do not build one giant render graph for an entire film. Maintain a shot manifest and render one production unit per job:

`Load models -> optional low-VRAM patches -> MiniMaxH3ImageToVideo / ReferenceToVideo -> BasicGuider -> sampler -> separate AV latent -> decode -> CreateVideo -> SaveVideo`

Each shot job must be independently rerunnable.

## Useful ideas from community Director workflows

Community MiniMax H3 Director projects demonstrate several useful workflow patterns:
- timeline/shot editor rather than hand-editing a monolithic prompt;
- snapping duration to legal H3 frame counts;
- first/last-frame and reference mode selection per shot;
- previous accepted shot's last frame as an optional continuity seed;
- selected-shot retakes and caching instead of full timeline reruns;
- prompt lint/preview before expensive sampling;
- postprocess only after a take is worth keeping.

Adopt these mechanisms even if the third-party Director node itself is not installed.

## 16GB VRAM policy

Prefer the already installed pruned INT8 FL2VA model and existing KJNodes low-VRAM/feed-forward patches when they have passed A/B quality checks. Do not install a new 21–34GB checkpoint merely because a community workflow names it if equivalent required functionality is already present.

Reference-conditioned R2V/RV2V requires the separate Ref2VA checkpoint. Treat that as an optional capability, not a silent substitute for FL2VA.

## Resolution provenance

- `NATIVE_1344x768`: H3 directly generated 1344×768.
- `TEST_*`: lower-resolution trial only.
- `DELIVERY_*_UPSCALED`: postprocessed delivery output.

Never infer provenance from filenames; probe actual media.
