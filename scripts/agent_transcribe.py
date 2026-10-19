#!/usr/bin/env python3
"""Agent-mode transcription bridge: whisper -> transN.txt (pipeline-resumable).

When pipeline.py exits with code 3 in agent mode, run this script, then re-run
the same build command. This uses faster-whisper locally as the ASR for the
transcript text; terminology accuracy comes from --hotwords / initial_prompt
and from the document context written alongside.

For higher transcript quality, prefer a real multimodal path:
  set SUBTITLE_ASR_BACKEND=api + SUBTITLE_API_BASE/SUBTITLE_API_KEY/SUBTITLE_MODEL_OMNI
and skip this bridge entirely.

Usage:
  python agent_transcribe.py --work <workdir> [--model base] [--hotwords a,b,c]
"""
import argparse
import json
import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HUB_DISABLE_XET"] = "1"  # must force: xet bridge bypasses mirror


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--model", default=os.environ.get("SUBTITLE_WHISPER_MODEL", "base"))
    ap.add_argument("--hotwords", default="")
    args = ap.parse_args()

    manifest_p = os.path.join(args.work, "parts_manifest.json")
    if not os.path.exists(manifest_p):
        sys.exit("no parts_manifest.json — nothing pending (build is resumable)")
    manifest = json.load(open(manifest_p, encoding="utf-8"))

    doc_p = os.path.join(args.work, "doc.txt")
    doc = open(doc_p, encoding="utf-8").read() if os.path.exists(doc_p) else ""
    # whisper initial_prompt biases vocabulary toward document terms
    prompt_terms = ("，".join(args.hotwords.split(",")) if args.hotwords else "") \
        or doc[:200]

    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    n = 0
    for part in manifest["parts"]:
        if os.path.exists(part["out"]) and os.path.getsize(part["out"]) > 0:
            continue
        segments, _ = model.transcribe(
            part["audio"], language="zh", beam_size=1, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 450},
            initial_prompt=prompt_terms or None)
        lines = [s.text.strip() for s in segments if s.text.strip()]
        with open(part["out"], "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[bridge] part {part['idx']}: {len(lines)} lines -> {part['out']}")
        n += 1
    if n == 0:
        print("[bridge] all parts already transcribed")
    else:
        # consume manifest so pipeline won't re-enter agent mode for these parts
        os.remove(manifest_p)
        print(f"[bridge] done ({n} parts) — re-run build to continue")


if __name__ == "__main__":
    main()
