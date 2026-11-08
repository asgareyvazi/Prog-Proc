# Synthetic MinerU OCR corpus

The seven fixtures are generated only; no field DDR, user document, model, weight, or confidential
source is included. `manifest.json` records each image SHA-256, exact UTF-8 ground-truth SHA-256,
layout description, integer transform, and glyph scale. The `.txt` files are the exact reference text
used by CER/WER. Images are deterministic 8-bit grayscale PNGs built from an embedded 5×7 bitmap
alphabet using only the Python standard library.

Cases cover one column, two columns, aligned table-like rows mixed with a heading, small glyphs, low
contrast with deterministic sparse noise, an exact 90-degree clockwise rotation, and a deterministic
integer row shear. These are controlled fixtures, not realistic drilling reports and not evidence of
OCR accuracy on real scans. The existing `synthetic_scan.png` / `ground_truth.txt` pair is retained as
the original two-line fixture for compatibility.

`drillintel mineru validate` checks runtime readiness only. Model readiness remains `NOT_CHECKED`;
OCR remains `NOT_RUN` and metrics are null unless `--run-synthetic-ocr` explicitly invokes the local
MinerU 4 CLI against every generated case with `MINERU_MODEL_SOURCE=local`, ONNX small-model backend,
and Basic tier. This path does not select HTTP, upload documents, or download models. If a runtime
is missing, inference is not run and metrics remain null. Completed CER/WER are descriptive per-case
measurements with the normalization recorded in the manifest; no pass threshold is configured and a
synthetic result never qualifies general or field-document OCR accuracy.
