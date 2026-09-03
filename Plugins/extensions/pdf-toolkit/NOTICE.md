# PDF Toolkit Notices

PDF Toolkit is an original FolderBridge external Extension implementation. The following open-source projects were reviewed as design references; their code is not bundled wholesale in this Extension.

- jztan/pdf-mcp — MIT — https://github.com/jztan/pdf-mcp
- AryanBV/pdf-toolkit-mcp — MIT — https://github.com/AryanBV/pdf-toolkit-mcp
- espresso3389/pdf-splitter-mcp — inspect the cloned repository license before copying any code — https://github.com/espresso3389/pdf-splitter-mcp
- paradyno/pdf-mcp-server — repository advertises Apache-2.0 — https://github.com/paradyno/pdf-mcp-server
- nfsarch33/pdf-mcp-server — research-only reference; project code advertises Apache-2.0 but its runtime dependency stack includes PyMuPDF/AGPL-3.0 — https://github.com/nfsarch33/pdf-mcp-server

## v0.6 runtime dependency set

PDF Toolkit 0.6.0 no longer vendors or imports pypdf. Text inspection is performed only by the fixed out-of-process `pdf_inspect.ps1` seam using the exact reviewed .NET assembly set below. The production loader is `Assembly.LoadFrom` after exact path, SHA-256, and assembly-identity checks; there is no host/global package fallback.

- PdfPig 0.1.16 (`net471`) — Apache-2.0 — nupkg SHA-256 `d67171846ea8c28f50359137065fec4514266d7a32b23eae6c5f2ebed8ffcfc4`
- Microsoft.Bcl.HashCode 6.0.0 (`net462`) — MIT — nupkg SHA-256 `f3b9b2bab0bf8cc717d5fdf6d7aee3ec54e36d9e85bd41347acae161319cbd6b`
- System.Memory 4.6.3 (`net462`) — MIT — nupkg SHA-256 `26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955`
- System.Buffers 4.6.1 (`net462`) — MIT — nupkg SHA-256 `b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79`
- System.Numerics.Vectors 4.6.1 (`net462`) — MIT — nupkg SHA-256 `2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7`
- System.Runtime.CompilerServices.Unsafe 6.1.2 (`net462`) — MIT — nupkg SHA-256 `5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937`

The installer records the selected NuGet TFM/dependency groups, official SHA-512 values, nupkg SHA-256 values, repository provenance, the exact twelve runtime DLL SHA-256/assembly identities, and the installed-file inventory in `VENDOR-PROVENANCE.json`. Apache-2.0 and MIT license texts are installed under `licenses/` and included in the exact approved Extension tree.

## Unicode case folding

Case-insensitive literal search uses a deterministic Unicode 14.0.0 default-full-fold map generated from the reviewed `CaseFolding.txt`, not host-culture lowercasing. The locked source SHA-256 is `a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f`; generated `casefold-map.json` SHA-256 is `77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504`; Unicode license SHA-256 is `e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96`.

## Rendering and distribution boundary

Visual page rendering remains a separate Windows platform layer using `Windows.Data.Pdf` through the fixed `pdf_render.ps1`. `render-pages` does not invoke PdfPig or the inspection backend. No third-party PDF renderer is redistributed for that path.

The installer stages and verifies the complete candidate tree outside the hot-scan root, publishes by whole-directory namespace moves under a destination-scoped lock, and requires a new FolderBridge exact-tree-hash review/approval after installation. Trust is not inherited from v0.5.x.

The research fetcher may clone `nfsarch33/pdf-mcp-server` for comparative inspection, but that checkout is never bundled into the Extension and no PyMuPDF-dependent implementation is adopted. This Extension deliberately avoids that licensing path.
