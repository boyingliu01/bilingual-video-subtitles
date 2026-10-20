#!/usr/bin/env python3
"""Bilingual (zh/en) subtitle pipeline for training videos.

Proven workflow:
  1. setup    extract audio, cut into ~8-min parts at silence points
  2. transcribe  ASR with the reference document injected as terminology
              context -> accurate Chinese text. Backends (auto-detected):
                qwen-omni   original sandbox gateway (media-read describe.py)
                api         OpenAI-compatible endpoint (MiMo ASR / omni models)
                agent     exit 3 + parts_manifest.json; host agent or
                          scripts/agent_transcribe.py fills transcripts
                          (see references/host-adaptation.md)
  3. timing   faster-whisper (base, int8) for timestamps only (its text is
              discarded; only timing is used)
  4. align    global pinyin alignment of accurate text onto whisper timeline
  5. clean    remove fillers/stutters, merge fragments, split long lines
  6. translate   batch zh->en with a terminology glossary for consistency
  7. package  project.json (master, edit this for yearly revisions) + SRT + ASS
  8. render   optional preview clip + full burn-in video (ffmpeg/libass)

Subcommands:
  build    run the full pipeline (stages are cached; re-runs skip done stages)
  render   re-render SRT/ASS/video from an edited project.json
  status   show which stages are complete in a working dir

Designed for annual regulation-revision workflows: after minor edits, change
project.json (or the SRT) and run `render` — no re-transcription needed.
"""

import argparse
import difflib
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESCRIBE = os.path.normpath(
    os.path.join(SKILL_DIR, "..", "media-read", "scripts", "describe.py")
)
# Cross-host adaptation: the original sandbox's Omni gateway (media-read
# describe.py) only exists there. Elsewhere we fall back to env vars:
#   SUBTITLE_API_BASE / SUBTITLE_API_KEY  -> OpenAI-compatible endpoint
#   SUBTITLE_MODEL_OMNI                   -> audio->text model (optional)
#   SUBTITLE_MODEL_TEXT                   -> translate/punctuate model
ASR_BACKEND = os.environ.get("SUBTITLE_ASR_BACKEND", "auto")  # auto|qwen-omni|agent|api
# model size or local dir (set SUBTITLE_WHISPER_MODEL to a local path when
# huggingface downloads are blocked; see references/host-adaptation.md)
DEFAULT_WHISPER = os.environ.get("SUBTITLE_WHISPER_MODEL", "base")
CHUNK_TARGET_S = 8 * 60  # target part length (describe.py cap: 20 min)
MAX_CHARS_PER_LINE = 30  # Chinese chars before splitting a cue
TRANSLATE_BATCH = 22
MODEL_TRANSLATE = os.environ.get("SUBTITLE_MODEL_TEXT", "qwen3.7-plus")
MODEL_OMNI = os.environ.get("SUBTITLE_MODEL_OMNI", "")

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HUB_DISABLE_XET"] = "1"  # must force: xet bridge bypasses mirror


def resolve_api():
    """Return (base_url, api_key) from describe.py or the SUBTITLE_* env vars."""
    if os.path.exists(DESCRIBE):
        spec = importlib.util.spec_from_file_location("describe", DESCRIBE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.resolve_endpoint()
    base = os.environ.get("SUBTITLE_API_BASE", "").rstrip("/")
    key = os.environ.get("SUBTITLE_API_KEY", "")
    if base and key:
        return base, key
    return None, None


def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def stage_done(*paths):
    return all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths)


def run(cmd, **kw):
    kw.setdefault("check", True)
    return subprocess.run(cmd, **kw)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def norm(s):
    """Strip whitespace/punctuation (for alignment length math)."""
    return re.sub(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()《》\[\]【】·…—~-]", "", s)


def nlen(t):
    return len(norm(t))


# === STAGE: environment =====================================================

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


def _pkg_install_hint(tool):
    if IS_MACOS:
        return f"brew install {tool}   (or download a static build)"
    return f"sudo apt-get install -y {tool}"


def ensure_env():
    """Check/install runtime dependencies. Never auto-installs system
    packages — prints the platform-appropriate command and exits instead
    (macOS has no apt; silent sudo prompts broke unattended runs)."""
    # ffmpeg / ffprobe
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            continue
        sys.exit(
            f"error: {tool} not on PATH — install it first: " + _pkg_install_hint(tool)
        )
    # python packages
    need = {
        "faster_whisper": "faster-whisper",
        "pypinyin": "pypinyin",
        "docx": "python-docx",
    }
    missing = []
    for mod, pkg in need.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        log("pip installing: " + ", ".join(missing))
        cmd = [sys.executable, "-m", "pip", "install", "-q"] + missing
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and "--user" not in cmd:
            # PEP 668 (Homebrew/system python on macOS, distro python on
            # Linux): retry with --user before giving up
            log("pip failed (externally-managed env?) — retrying with --user")
            subprocess.run(cmd + ["--user"], check=True)
    # CJK font for ASS rendering (existence check only, per-platform paths)
    if IS_WINDOWS:
        fonts = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    elif IS_MACOS:
        fonts = [
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ]
    else:
        fonts = ["/usr/share/fonts", "/usr/local/share/fonts"]
    cjk_pat = re.compile(
        r"(yahei|pingfang|hei|song|cjk|noto sans sc|"
        r"source han|wenquanyi|wqy|droid sans fallback)",
        re.I,
    )
    found = False
    for d in fonts:
        for root, _, files in os.walk(d) if os.path.isdir(d) else []:
            if any(cjk_pat.search(f) for f in files):
                found = True
                break
        if found:
            break
    if not found and shutil.which("fc-list"):
        r = subprocess.run(
            ["bash", "-c", "fc-list | grep -icE 'cjk|hei|song|han' || true"],
            capture_output=True,
            text=True,
        )
        found = r.stdout.strip() not in ("", "0")
    if not found:
        sys.exit(
            "error: no CJK font found — install one: "
            + (
                "(PingFang ships with macOS; brew install font-noto-sans-cjk-sc if missing)"
                if IS_MACOS
                else "sudo apt-get install -y fonts-noto-cjk"
            )
        )
    globals()["_CJK_FONT"] = None  # resolved lazily by cjk_font_name()
    # ASR backend availability
    backend = ASR_BACKEND
    if backend == "auto":
        if os.path.exists(DESCRIBE):
            backend = "qwen-omni"
        elif resolve_api()[1]:
            backend = "api"
        else:
            backend = "agent"
    globals()["_RESOLVED_BACKEND"] = backend
    log(f"ASR backend: {backend}")
    if backend == "qwen-omni" and not os.path.exists(DESCRIBE):
        sys.exit(f"error: media-read describe.py not found at {DESCRIBE}")
    if backend == "api" and not MODEL_OMNI:
        sys.exit("error: SUBTITLE_ASR_BACKEND=api but SUBTITLE_MODEL_OMNI unset")


def cjk_font_name():
    """Font family name for ASS styles, probed against installed fonts.
    libass matches by family name, so a wrong guess silently renders tofu."""
    cached = globals().get("_CJK_FONT")
    if cached:
        return cached
    if IS_WINDOWS:
        cand = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"]
        probe_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    elif IS_MACOS:
        cand = [
            "PingFang SC",
            "Hiragino Sans GB",
            "Noto Sans CJK SC",
            "STHeiti",
            "Arial Unicode MS",
        ]
        probe_dir = "/System/Library/Fonts"
    else:
        cand = [
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "WenQuanYi Micro Hei",
            "Droid Sans Fallback",
        ]
        probe_dir = "/usr/share/fonts"
    # filename hints per candidate (cheap filesystem probe)
    hints = {
        "Microsoft YaHei": ["msyh"],
        "SimHei": ["simhei"],
        "PingFang SC": ["PingFang"],
        "Hiragino Sans GB": ["HiraginoSansGB"],
        "STHeiti": ["STHeiti", "Heiti"],
        "Noto Sans CJK SC": ["NotoSansCJK", "NotoSansSC"],
        "Source Han Sans SC": ["SourceHanSans", "NotoSansCJK"],
        "WenQuanYi Micro Hei": ["wqy-microhei"],
        "Droid Sans Fallback": ["DroidSansFallback"],
    }
    files = []
    for root, _, fs in os.walk(probe_dir):
        files.extend(f.lower() for f in fs)
    listing = (
        "\n".join(files)
        if files
        else subprocess.run(
            ["bash", "-c", "fc-list 2>/dev/null | tr '[:upper:]' '[:lower:]'"],
            capture_output=True,
            text=True,
        ).stdout
    )
    for name in cand:
        if any(h.lower() in listing for h in hints.get(name, [])):
            globals()["_CJK_FONT"] = name
            return name
    fallback = cand[0]
    log(f"font probe inconclusive — assuming {fallback}")
    globals()["_CJK_FONT"] = fallback
    return fallback


def extract_doc(path):
    """Extract plain text from .docx / .txt / .md reference document."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        import docx

        d = docx.Document(path)
        paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
        rows = []
        for t in d.tables:
            for row in t.rows:
                rows.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(paras + rows)
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8") as f:
            return f.read()
    sys.exit(f"unsupported doc format: {ext} (convert to .docx/.txt/.md first)")


# === STAGE: setup (audio extract, silence cuts, parts) ======================


def stage_setup(work, video, doc, chunk_s):
    meta_p = os.path.join(work, "meta.json")
    if stage_done(meta_p):
        log("setup: cached")
        return read_json(meta_p)
    assert os.path.exists(video), f"video not found: {video}"
    dur = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video,
            ],
            text=True,
        ).strip()
    )
    wav = os.path.join(work, "full_audio.wav")
    if not stage_done(wav):
        log("setup: extracting audio")
        run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                video,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                wav,
            ]
        )
    # silence points -> chunk boundaries near multiples of chunk_s
    r = run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            wav,
            "-af",
            "silencedetect=n=-35dB:d=0.45",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    sil = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", r.stderr)]
    cuts = []
    t = chunk_s
    while t < dur - 60:
        near = [s for s in sil if abs(s - t) < 60 and 30 < s < dur - 30]
        cuts.append(min(near, key=lambda s: abs(s - t)) if near else float(t))
        t += chunk_s
    bounds = [0.0] + cuts + [dur]
    # cut compressed parts (64k m4a mono keeps uploads small & fast)
    parts = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        p = os.path.join(work, f"part{i + 1}.m4a")
        if not stage_done(p):
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    wav,
                    "-ss",
                    f"{s}",
                    "-t",
                    f"{e - s}",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    p,
                ]
            )
        parts.append(p)
    doc_text = extract_doc(doc)
    meta = {
        "video": os.path.abspath(video),
        "doc": os.path.abspath(doc),
        "duration": dur,
        "bounds": bounds,
        "parts": parts,
        "doc_chars": len(doc_text),
    }
    write_json(meta_p, meta)
    open(os.path.join(work, "doc.txt"), "w", encoding="utf-8").write(doc_text)
    log(f"setup: {len(parts)} parts, duration {dur:.0f}s")
    return meta


# === STAGE: transcribe (Qwen3-Omni + document context) ======================

TRANS_PROMPT = """这是一段公司内部培训视频的中文普通话音频。请逐字完整转写音频内容，要求：
1. 只输出转写文本，不要总结、不要解释、不要添加任何内容，也不要遗漏任何内容；
2. 每句话单独一行，按说话的自然停顿断句，不要加时间戳；
3. 必须正确使用中文标点符号（逗号、句号、问号等），每个句子以句号/问号/感叹号结尾，全文不得省略标点；
4. 保留语气词（如"呃""嗯"）和口语重复，保持逐字稿原貌；
5. 遇到专业术语、角色名称、制度名称、系统名称时，务必参考下方参考文档中的标准用词，确保用字准确。

【参考文档（术语以此为准）】
{doc}
"""


def _mimo_asr_call(audio_path, base_url, api_key, language="zh", tries=3):
    """Xiaomi MiMo ASR (mimo-v2.5-asr): chat/completions + input_audio ONLY
    (no text parts allowed — gateway injects its own prompt). wav/mp3 only,
    base64 <=10MB (~17min at 8k mono mp3), max output 4096 tokens.
    Returns transcript with native punctuation (may be one paragraph)."""
    import base64 as b64mod

    ext = os.path.splitext(audio_path)[1].lstrip(".").lower()
    if ext == "m4a":
        tmp = audio_path + ".mp3"
        if not stage_done(tmp):
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    audio_path,
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-b:a",
                    "32k",
                    tmp,
                ]
            )
        audio_path, ext = tmp, "mp3"
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg"}.get(ext)
    if not mime:
        sys.exit(f"mimo asr: unsupported format .{ext} (convert to wav/mp3 first)")
    data = (
        f"data:{mime};base64,"
        + b64mod.b64encode(open(audio_path, "rb").read()).decode()
    )
    body = {
        "model": MODEL_OMNI or "mimo-v2.5-asr",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data, "format": ext},
                    }
                ],
            }
        ],
        "asr_options": {"language": language},
    }
    last = None
    for att in range(tries):
        try:
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            d = json.loads(urllib.request.urlopen(req, timeout=900).read())
            return d["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (att + 1))
    raise RuntimeError(f"mimo asr failed: {last}")


def _api_audio_chat(audio_path, prompt, base_url, api_key, max_tokens=8192, tries=3):
    """OpenAI-compatible audio transcription/chat with a local file."""
    import base64

    b64 = base64.b64encode(open(audio_path, "rb").read()).decode()
    ext = os.path.splitext(audio_path)[1].lstrip(".").lower() or "m4a"
    body = {
        "model": MODEL_OMNI,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:;base64,{b64}", "format": ext},
                    },
                ],
            }
        ],
    }
    last = None
    for att in range(tries):
        try:
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            d = json.loads(urllib.request.urlopen(req, timeout=900).read())
            return d["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (att + 1))
    raise RuntimeError(f"audio chat failed: {last}")


def stage_transcribe(work, meta, force=False):
    doc = open(os.path.join(work, "doc.txt"), encoding="utf-8").read()
    prompt = TRANS_PROMPT.format(doc=doc[:60000])
    open(os.path.join(work, "trans_prompt.txt"), "w", encoding="utf-8").write(prompt)
    backend = globals().get("_RESOLVED_BACKEND", "agent")
    pending = []
    for i, part in enumerate(meta["parts"], 1):
        dst = os.path.join(work, f"trans{i}.txt")
        if force or not stage_done(dst):
            pending.append((i, part, dst))
    if not pending:
        log("transcribe: cached")
        return
    if backend == "agent":
        # agent mode: the host agent reads parts_manifest.json, transcribes
        # each audio part itself (Read tool on the audio file), writes transN.txt,
        # then re-runs build — this stage becomes cached.
        manifest = {
            "backend": "agent",
            "prompt_file": "trans_prompt.txt",
            "parts": [
                {"idx": i, "audio": os.path.abspath(p), "out": os.path.abspath(d)}
                for i, p, d in pending
            ],
        }
        write_json(os.path.join(work, "parts_manifest.json"), manifest)
        log(
            f"transcribe: AGENT mode — {len(pending)} parts pending, "
            f"see {os.path.join(work, 'parts_manifest.json')}"
        )
        sys.exit(3)  # signal: needs agent transcription, then re-run build
    if backend == "qwen-omni":
        for i, part, dst in pending:
            for attempt in range(3):
                t0 = time.time()
                r = subprocess.run(
                    [
                        sys.executable,
                        DESCRIBE,
                        "--audio",
                        part,
                        "--prompt",
                        prompt,
                        "--timeout",
                        "900",
                        "--max-tokens",
                        "8192",
                    ],
                    capture_output=True,
                    text=True,
                )
                if r.returncode == 0 and len(r.stdout.strip()) > 50:
                    open(dst, "w", encoding="utf-8").write(r.stdout.strip() + "\n")
                    log(
                        f"transcribe: part {i}/{len(meta['parts'])} OK "
                        f"({len(r.stdout)} chars, {time.time() - t0:.0f}s)"
                    )
                    break
                log(
                    f"transcribe: part {i} attempt {attempt + 1} failed: "
                    f"{r.stderr[-200:].strip()}"
                )
                time.sleep(5 * (attempt + 1))
            else:
                sys.exit(
                    f"transcribe part {i} failed after 3 attempts — rerun to resume"
                )
        return
    # backend == "api": OpenAI-compatible endpoint. mimo-* models use the
    # dedicated MiMo ASR call shape (audio-only request, no prompt injection).
    base_url, api_key = resolve_api()
    is_mimo = "mimo" in (MODEL_OMNI or "").lower()
    for i, part, dst in pending:
        t0 = time.time()
        if is_mimo:
            out = _mimo_asr_call(part, base_url, api_key)
        else:
            out = _api_audio_chat(part, prompt, base_url, api_key)
        if out and len(out.strip()) > 50:
            open(dst, "w", encoding="utf-8").write(out.strip() + "\n")
            log(
                f"transcribe: part {i}/{len(meta['parts'])} OK "
                f"({len(out)} chars, {time.time() - t0:.0f}s)"
            )
        else:
            sys.exit(f"transcribe part {i}: empty output — rerun to resume")


# === STAGE: timing (faster-whisper timestamps only) =========================


def stage_timing(work, meta, hot_prompt="", force=False):
    out_p = os.path.join(work, "whisper_segments.json")
    if stage_done(out_p) and not force:
        log("timing: cached")
        return
    from faster_whisper import WhisperModel

    model = WhisperModel(DEFAULT_WHISPER, device="cpu", compute_type="int8")
    log("timing: whisper model loaded")
    all_segs = []
    for i, part in enumerate(meta["parts"], 1):
        t0 = time.time()
        segments, _ = model.transcribe(
            part,
            language="zh",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 450},
            initial_prompt=hot_prompt or None,
        )
        off = meta["bounds"][i - 1]
        cnt = 0
        for s in segments:
            all_segs.append(
                {
                    "start": round(s.start + off, 2),
                    "end": round(s.end + off, 2),
                    "text": s.text.strip(),
                }
            )
            cnt += 1
        log(
            f"timing: part {i}/{len(meta['parts'])} {cnt} segs "
            f"({time.time() - t0:.0f}s)"
        )
    write_json(out_p, all_segs)
    log(f"timing: total {len(all_segs)} segments")


# === STAGE: align (pinyin global alignment) =================================
# Whisper's Chinese text is unreliable, but its timestamps are good. Omni's
# text is accurate but untimestamped. Align them via pinyin strings so that
# homophone errors (the dominant whisper error type in Chinese) still match.


def _pinyin(s):
    from pypinyin import lazy_pinyin

    return "".join(lazy_pinyin(s))


def _restore_punctuation(text):
    """LLM pass to add punctuation/line breaks when transcription lacks them.
    Returns original text on any failure (caller falls back to space splitting)."""
    try:
        base_url, api_key = _resolve_endpoint()
        out = _text_chat(
            (
                "下面是一段没有标点的中文语音转写稿。请在不增删改任何文字的前提下，"
                "加上正确的标点符号（逗号、句号、问号等），并按句子分成多行输出。"
                "只输出处理后的文本。\n\n" + text
            ),
            base_url,
            api_key,
            max_tokens=7000,
            tries=3,
        )
        if (
            0.7 * max(1, len(norm(text)))
            <= len(norm(out))
            <= 1.35 * max(1, len(norm(text)))
        ):
            return out
    except Exception:  # noqa: BLE001
        pass
    return text


def _load_part_lines(work, idx):
    """Lines of a transcript; split giant/punctuation-less outputs into units."""
    raw = open(os.path.join(work, f"trans{idx}.txt"), encoding="utf-8").read()
    # if the transcription came back without sentence punctuation, restore it
    if len(re.findall(r"[。！？!?]", raw)) < max(2, len(norm(raw)) // 200):
        log(f"align: part {idx} transcript lacks punctuation — restoring via LLM")
        raw = _restore_punctuation(raw)
    txts = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) > 60:
            sub = [p.strip() for p in re.split(r"(?<=[。！？!?，,])", ln) if p.strip()]
            txts.extend(sub if len(sub) > 1 else [ln])
        else:
            txts.append(ln)
    # fallback: still-giant units -> split on whitespace, then hard chunks
    final = []
    for t in txts:
        if len(t) <= 60:
            final.append(t)
            continue
        pieces = [p for p in re.split(r"\s+", t) if p]
        buf = ""
        for p in pieces:
            if len(buf) + len(p) > 40:
                if buf:
                    final.append(buf)
                buf = p
            else:
                buf += p
        if buf:
            final.append(buf)
    out = []
    for t in final:
        while len(t) > 60:  # no spaces at all: hard chunk
            out.append(t[:40])
            t = t[40:]
        out.append(t)
    return out


def stage_align(work, meta, force=False):
    out_p = os.path.join(work, "aligned.json")
    if stage_done(out_p) and not force:
        log("align: cached")
        return
    wsegs = read_json(os.path.join(work, "whisper_segments.json"))
    bounds = meta["bounds"]
    res = []
    for pi in range(len(meta["parts"])):
        t0_, t1_ = bounds[pi], bounds[pi + 1]
        segs = [s for s in wsegs if s["end"] > t0_ - 12 and s["start"] < t1_ + 12]
        txts = _load_part_lines(work, pi + 1)
        if not segs or not txts:
            log(f"align: part {pi + 1} has no speech or text — skipped")
            continue
        B_parts, B_ranges, pos = [], [], 0
        for s in segs:
            p = _pinyin(norm(s["text"]))
            B_parts.append(p)
            B_ranges.append((pos, pos + len(p), s))
            pos += len(p)
        B = "".join(B_parts)
        A_parts, A_ranges, pos = [], [], 0
        for t in txts:
            p = _pinyin(norm(t))
            A_parts.append(p)
            A_ranges.append((pos, pos + len(p), t))
            pos += len(p)
        A = "".join(A_parts)
        blocks = difflib.SequenceMatcher(
            None, A, B, autojunk=False
        ).get_matching_blocks()

        def map_a_to_b(p_a):
            for a, b, sz in blocks:
                if a <= p_a <= a + sz:
                    return b + (p_a - a)
            prev = nxt = None
            for a, b, sz in blocks:
                if a + sz <= p_a:
                    prev = (a + sz, b + sz)
                elif nxt is None:
                    nxt = (a, b)
            if prev and nxt and nxt[0] > prev[0]:
                frac = (p_a - prev[0]) / (nxt[0] - prev[0])
                return prev[1] + frac * (nxt[1] - prev[1])
            if prev:
                return prev[1]
            if nxt:
                return nxt[1]
            return p_a * (len(B) / max(1, len(A)))

        def time_at(p_b):
            for c0, c1, seg in B_ranges:
                if c0 <= p_b <= c1:
                    if c1 == c0:
                        return seg["start"]
                    return seg["start"] + (seg["end"] - seg["start"]) * (p_b - c0) / (
                        c1 - c0
                    )
            return None

        for a0, a1, txt in A_ranges:
            ts = time_at(map_a_to_b(a0))
            te = time_at(map_a_to_b(a1))
            if ts is None:
                ts = t0_
            if te is None or te <= ts:
                te = (ts or t0_) + 0.6
            matched = sum(
                min(a1, a + sz) - max(a0, a)
                for a, b, sz in blocks
                if not (a + sz <= a0 or a >= a1) and sz > 0
            )
            res.append(
                {
                    "text": txt,
                    "start": max(t0_, ts),
                    "end": min(t1_ + 5, te),
                    "qual": round(matched / max(1, a1 - a0), 3),
                }
            )
    for idx, r in enumerate(res):
        if idx and r["start"] < res[idx - 1]["end"]:
            r["start"] = res[idx - 1]["end"]
        if r["end"] <= r["start"]:
            r["end"] = r["start"] + 0.6
        r["start"], r["end"] = round(r["start"], 2), round(r["end"], 2)
    write_json(out_p, res)
    q = [r["qual"] for r in res]
    log(
        f"align: {len(res)} lines, qual mean={statistics.mean(q):.2f}, "
        f"low(<0.5)={sum(1 for x in q if x < 0.5)}"
    )


# === STAGE: clean (fillers, merge, split) ===================================


def _cleanup(t):
    t = t.strip()
    for _ in range(3):
        t2 = re.sub(r"^(呃|嗯|啊|诶|哦|嗯哼)[，,、\s]*", "", t)
        t2 = re.sub(r"[，,\s]+(呃|嗯)[。\s]*$", "", t2)
        t2 = re.sub(r"(?<=[，,；;])\s*(呃|嗯)\s*(?=[，,])", "", t2)
        t2 = re.sub(r"([\u4e00-\u9fff]{2,4})\1+", r"\1", t2)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"^[，,、\s]+", "", t)
    t = re.sub(r"[，,、\s]+$", "", t)
    return t


_YEAR = {
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def stage_clean(work, meta, force=False):
    out_p = os.path.join(work, "zh_lines.json")
    if stage_done(out_p) and not force:
        log("clean: cached")
        return
    aligned = read_json(os.path.join(work, "aligned.json"))
    cleaned = []
    for r in aligned:
        c = _cleanup(r["text"])
        if not c:
            if cleaned:
                cleaned[-1]["end"] = r["end"]
            continue
        cleaned.append({"zh": c, "start": r["start"], "end": r["end"]})
    # split overlong lines at the punctuation nearest the middle
    split1 = []
    for s in cleaned:
        t = s["zh"]
        if nlen(t) <= MAX_CHARS_PER_LINE:
            split1.append(s)
            continue
        mids = [m.end() for m in re.finditer(r"[，。；、！？,.!?;]", t)]
        if not mids:
            split1.append(s)
            continue
        cut = min(mids, key=lambda p: abs(p - len(t) / 2))
        if cut < 6 or cut > len(t) - 4:
            split1.append(s)
            continue
        a, b = t[:cut], t[cut:].lstrip("，,、 ")
        la, lb = max(1, nlen(a)), max(1, nlen(b))
        tm = s["start"] + (s["end"] - s["start"]) * la / (la + lb)
        split1.append({"zh": a.rstrip("，,、 "), "start": s["start"], "end": tm})
        split1.append({"zh": b, "start": tm, "end": s["end"]})
    # merge tight fragments into one cue
    merged = []
    for ln in split1:
        if merged:
            p = merged[-1]
            if (
                ln["start"] - p["end"] <= 0.18
                and nlen(p["zh"]) + nlen(ln["zh"]) <= MAX_CHARS_PER_LINE - 2
                and ln["end"] - p["start"] <= 8
            ):
                p["zh"] = (
                    p["zh"].rstrip("，。、 ") + "，" + ln["zh"].lstrip("，、 ")
                    if nlen(p["zh"]) < 6
                    else p["zh"] + ln["zh"]
                )
                p["end"] = ln["end"]
                continue
        merged.append(dict(ln))
    dur = meta["duration"]
    for i, ln in enumerate(merged):
        # normalize 二零XX年 -> 20XX年
        ln["zh"] = re.sub(
            r"二零([零一二三四五六七八九]{2})年",
            lambda m: "20" + "".join(_YEAR[c] for c in m.group(1)) + "年",
            ln["zh"],
        )
        ln["idx"] = i
        nxt = merged[i + 1]["start"] if i + 1 < len(merged) else dur
        if ln["end"] - ln["start"] < 0.8:
            ln["end"] = min(ln["start"] + 0.8, nxt)
    write_json(out_p, merged)
    # transcript quality warning: whisper-bridge backend + no terminology
    # proofreading is the top source of wrong characters (field report)
    if globals().get("_RESOLVED_BACKEND") == "agent":
        log(
            "clean: ⚠ transcript came from local whisper — homophone errors "
            "are expected. Proofread zh against the reference document before "
            "delivery (see SKILL.md 校对步骤), or configure an api ASR backend "
            "(SUBTITLE_ASR_BACKEND=api; e.g. mimo-v2.5-asr ≈ zero CJK errors)."
        )
    log(f"clean: {len(merged)} subtitle cues")


# === STAGE: translate (glossary-based, batched) =============================


def _resolve_endpoint():
    return resolve_api()


def _text_chat(prompt, base_url, api_key, max_tokens=7000, tries=4):
    if base_url is None:
        raise RuntimeError(
            "no text-chat endpoint configured "
            "(set SUBTITLE_API_BASE / SUBTITLE_API_KEY)"
        )
    body = {
        "model": MODEL_TRANSLATE,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    last = None
    for att in range(tries):
        try:
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "media-describe/1.0",
                },
            )
            d = json.loads(urllib.request.urlopen(req, timeout=180).read())
            return d["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"translate: retry {att + 1} ({e})")
            time.sleep(3 * (att + 1))
    raise RuntimeError(f"text chat failed: {last}")


def stage_translate(work, glossary, force=False):
    lines_p = os.path.join(work, "zh_lines.json")
    lines = read_json(lines_p)
    todo = [ln for ln in lines if force or not ln.get("en")]
    if not todo:
        log("translate: cached")
        return
    base_url, api_key = _resolve_endpoint()
    if base_url is None:
        # agent mode: dump pending lines; the main agent translates
        # them into zh_lines.json ("en" field) then re-runs build.
        manifest = {
            "backend": "agent",
            "glossary": glossary or "",
            "lines_file": os.path.abspath(lines_p),
            "pending_idx": [ln["idx"] for ln in todo],
        }
        write_json(os.path.join(work, "translate_manifest.json"), manifest)
        log(
            f"translate: AGENT mode — {len(todo)} lines pending, "
            f"see {os.path.join(work, 'translate_manifest.json')}"
        )
        sys.exit(3)
    gl = (
        open(glossary, encoding="utf-8").read()
        if glossary and os.path.exists(glossary)
        else ""
    )
    if not gl:
        gl = "（无专用术语表，请按通用软件工程术语准确翻译）"
    done_idx = {ln["idx"] for ln in lines if ln.get("en")}
    for b in range(0, len(todo), TRANSLATE_BATCH):
        chunk = todo[b : b + TRANSLATE_BATCH]
        numbered = "\n".join(f"{ln['idx']}. {ln['zh']}" for ln in chunk)
        prompt = (
            f"把下面编号的中文培训字幕逐句翻译成英文。这是企业内部培训，"
            f'口语风格但需专业准确。\n要求：每行输出格式为 "编号. 英文译文"，'
            f"不要加引号、不要解释、不要遗漏任何编号。\n"
            f"术语对照表（必须严格遵守）：\n{gl}\n\n"
            f"字幕行（按顺序翻译，口语化表达可适度整理为通顺英文，但不得增删语义）：\n{numbered}"
        )
        out = _text_chat(prompt, base_url, api_key)
        trans = {}
        for ln in out.splitlines():
            mm = re.match(r"^\s*(\d+)[.、．]\s*(.+)$", ln.strip())
            if mm:
                trans[int(mm.group(1))] = mm.group(2).strip()
        for ln in chunk:  # retry any missing line individually
            if ln["idx"] not in trans:
                log(f"translate: line {ln['idx']} missing, retrying solo")
                trans[ln["idx"]] = _text_chat(
                    f"把这句中文培训字幕翻译成英文，只输出译文：{ln['zh']}\n\n术语对照表：\n{gl}",
                    base_url,
                    api_key,
                    max_tokens=500,
                ).strip()
        for ln in chunk:
            ln["en"] = trans[ln["idx"]]
        done_idx.update(ln["idx"] for ln in chunk)
        write_json(lines_p, lines)  # checkpoint every batch -> resumable
        log(f"translate: {len(done_idx)}/{len(lines)} lines done")


# === STAGE: package (project.json / SRT / ASS) ==============================


def _ts_srt(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02.0f}:{m:02.0f}:{s:02.0f},{round((t - int(t)) * 1000):03d}"


def _ts_ass(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:.0f}:{m:02.0f}:{s:02.0f}.{round((t - int(t)) * 100):02d}"


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CN,{font},{cn},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,3.4,0,2,60,60,{cnv},1
Style: EN,{font},{en},&H00D7EBFA,&H000000FF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,3,2.4,0,2,60,60,{env},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_subtitles(lines, srt_path, ass_path, width=1920, height=1080):
    # plain UTF-8 (no BOM): ffmpeg's mov_text embeds the BOM bytes into the
    # first subtitle frame when muxing — breaks soft-sub workflows on macOS
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, ln in enumerate(lines, 1):
            f.write(
                f"{i}\n{_ts_srt(ln['start'])} --> {_ts_srt(ln['end'])}\n"
                f"{ln['zh']}\n{ln['en']}\n\n"
            )
    scale = height / 1080

    def esc(s):
        return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(
            ASS_HEAD.format(
                w=width,
                h=height,
                font=cjk_font_name(),
                cn=int(42 * scale),
                en=int(29 * scale),
                cnv=int(97 * scale),
                env=int(26 * scale),
            )
        )
        for ln in lines:
            txt = f"{esc(ln['zh'])}\\N{{\\rEN}}{esc(ln['en'])}"
            f.write(
                f"Dialogue: 0,{_ts_ass(ln['start'])},{_ts_ass(ln['end'])},"
                f"CN,,0,0,0,,{txt}\n"
            )


def stage_package(work, meta, name, outdir, glossary):
    lines = read_json(os.path.join(work, "zh_lines.json"))
    missing = [ln["idx"] for ln in lines if not ln.get("en")]
    if missing:
        sys.exit(
            f"package: {len(missing)} lines lack translation — run translate first"
        )
    project = {
        "meta": {
            "name": name,
            "source_video": meta["video"],
            "doc": meta["doc"],
            "duration": meta["duration"],
            "glossary": glossary,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        },
        "lines": [
            {
                "idx": ln["idx"],
                "start": round(ln["start"], 2),
                "end": round(ln["end"], 2),
                "zh": ln["zh"],
                "en": ln["en"],
            }
            for ln in lines
        ],
    }
    os.makedirs(outdir, exist_ok=True)
    proj_p = os.path.join(outdir, f"{name}_字幕工程.json")
    srt_p = os.path.join(outdir, f"{name}_中英双语字幕.srt")
    ass_p = os.path.join(work, "subs.ass")
    write_json(proj_p, project)
    write_subtitles(lines, srt_p, ass_p)
    log(f"package: project={proj_p} srt={srt_p} ({len(lines)} cues)")
    return proj_p, ass_p


# === STAGE: render (ffmpeg burn-in) =========================================


def _video_wh(video):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            video,
        ],
        text=True,
    ).strip()
    w, h = out.split(",")
    return int(w), int(h)


def _video_bitrate(video):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video,
        ],
        text=True,
    ).strip()
    try:
        br = int(out)
        return br if br > 0 else None
    except ValueError:
        return None  # container-level estimate fallback


def render_video(video, ass_path, out_path, preview_s=0, crf=None, preset="veryfast"):
    """Burn ASS subtitles into video. preview_s>0 renders only the head clip.

    Bitrate policy: target ≈ source video bitrate × 1.06 (one-pass ABR), so
    output size stays close to the input. CRF mode is only a fallback when
    the source bitrate cannot be probed — re-encoding a low-bitrate screen
    recording at crf 22 can DOUBLE the file size (field report).
    preset default raised ultrafast→veryfast (~3-5% smaller for similar speed;
    x264 ultrafast is notorious for inflating bitrate).

    The ass/libass filters in some Windows ffmpeg builds mis-parse paths with
    a drive-letter colon or non-ASCII chars, so run ffmpeg with cwd set to the
    ASS file's directory and pass it as a relative filename.
    """
    ass_dir = os.path.dirname(os.path.abspath(ass_path))
    ass_rel = os.path.basename(ass_path)
    enc = ["-c:v", "libx264", "-preset", preset]
    src_br = _video_bitrate(video)
    if src_br and not crf:
        enc += ["-b:v", str(int(src_br * 1.06))]
        log(
            f"render: targeting bitrate {int(src_br * 1.06 / 1000)}kbps "
            f"(source {src_br // 1000}kbps)"
        )
    else:
        enc += ["-crf", str(crf or 22)]
        log("render: source bitrate unknown — falling back to crf mode")
    base_cmd = (
        ["ffmpeg", "-y", "-v", "error", "-i", video, "-vf", f"ass={ass_rel}"]
        + enc
        + ["-c:a", "copy"]
    )
    kw = {"cwd": ass_dir}
    if preview_s > 0:
        base_cmd[2:2] = ["-t", str(preview_s)]
        base_cmd += [os.path.abspath(out_path)]
        log(f"render: preview {preview_s}s -> {out_path}")
        run(base_cmd, **kw)
        return
    log(f"render: full video -> {out_path} (this takes a while)")
    run(base_cmd + [os.path.abspath(out_path)], **kw)
    log("render: done")


# === split (theme/role-based short clips) ===================================


def cmd_split(args):
    """Cut video into named short clips per a chapter plan (JSON).

    Plan format: {"base": "培训视频名", "lead_in": 0.35,
                  "chapters": [{"start": 0, "end": 288.4,
                                 "title": "01_开篇_目的与概述"}, ...]}
    Titles become part of the file name (auto-numbering if missing).
    Outputs go to --out/<base>_拆分视频/ together with an index file.
    """
    ensure_env()
    plan = read_json(args.plan)
    video = args.video or plan.get("video", "")
    if not video or not os.path.exists(video):
        sys.exit("split: video not found (pass --video or set plan.video)")
    base = plan.get("base") or os.path.splitext(os.path.basename(video))[0]
    lead_in = float(plan.get("lead_in", 0.35))
    dur = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video,
            ],
            text=True,
        ).strip()
    )
    outdir = os.path.join(args.out, f"{base}_拆分视频")
    os.makedirs(outdir, exist_ok=True)
    index = []
    for i, ch in enumerate(plan["chapters"], 1):
        s = max(0.0, float(ch["start"]) - lead_in)
        e = min(dur, float(ch["end"]))
        title = ch.get("title") or f"{i:02d}"
        if not re.match(r"^\d+", title):
            title = f"{i:02d}_{title}"
        title = re.sub(r"[\\/:*?\"<>|\s]+", "_", title).strip("_")
        outp = os.path.join(outdir, f"{base}_{title}.mp4")
        if stage_done(outp) and not args.force:
            log(f"split {i}/{len(plan['chapters'])}: cached {os.path.basename(outp)}")
        else:
            if e - s > 15 * 60:
                log(f"split {i}: WARNING clip {e - s:.0f}s exceeds 15 min")
            src_br = _video_bitrate(video)
            enc = ["-b:v", str(int(src_br * 1.06))] if src_br else ["-crf", "22"]
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{s:.3f}",
                    "-i",
                    video,
                    "-t",
                    f"{e - s:.3f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                ]
                + enc
                + ["-c:a", "aac", "-b:a", "128k", outp]
            )
            log(
                f"split {i}/{len(plan['chapters'])}: {os.path.basename(outp)} "
                f"({e - s:.0f}s)"
            )
        index.append(
            {
                "file": os.path.basename(outp),
                "range": f"{_ts_srt(ch['start'])[:8]} - {_ts_srt(ch['end'])[:8]}",
                "duration_s": round(e - ch["start"], 1),
                "title": ch.get("note", title),
            }
        )
    idx_p = os.path.join(outdir, "00_拆分目录.txt")
    with open(idx_p, "w", encoding="utf-8") as f:
        f.write(f"{base} 拆分目录（共 {len(index)} 集）\n" + "=" * 40 + "\n")
        for it in index:
            f.write(
                f"{it['file']}\n    {it['range']}  时长 {it['duration_s']:.0f} 秒\n"
            )
    log(f"split complete: {outdir}")


# === subcommands ============================================================


def cmd_build(args):
    ensure_env()
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    name = args.name or os.path.splitext(os.path.basename(args.video))[0]
    meta = stage_setup(args.work, args.video, args.doc, args.chunk_s)
    if args.stage in ("all", "transcribe"):
        stage_transcribe(args.work, meta, force=args.force)
    if args.stage == "transcribe":
        return
    if args.stage in ("all", "timing"):
        stage_timing(args.work, meta, hot_prompt=args.hotwords or "", force=args.force)
    if args.stage == "timing":
        return
    if args.stage in ("all", "align"):
        stage_align(args.work, meta, force=args.force)
    if args.stage == "align":
        return
    if args.stage in ("all", "clean"):
        stage_clean(args.work, meta, force=args.force)
    if args.stage == "clean":
        return
    if args.stage in ("all", "translate"):
        stage_translate(args.work, args.glossary, force=args.force)
    if args.stage == "translate":
        return
    proj_p, ass_p = stage_package(args.work, meta, name, args.out, args.glossary)
    if args.skip_render:
        return
    w, h = _video_wh(meta["video"])
    if w > 1920:  # ASS was sized for <=1080p; downscale for render if needed
        log(f"render: downscaling {w}x{h} -> 1920")
    if args.preview > 0:
        pv = os.path.join(args.out, f"{name}_双语字幕_预览{args.preview}秒.mp4")
        if args.force or not stage_done(pv):
            render_video(meta["video"], ass_p, pv, preview_s=args.preview)
    full = os.path.join(args.out, f"{name}_中英双语字幕_完整版.mp4")
    if args.force or not stage_done(full):
        render_video(meta["video"], ass_p, full)
    log(f"build complete:\n  project : {proj_p}\n  video   : {full}")


def cmd_render(args):
    """Re-render from an edited project.json (yearly revision workflow)."""
    ensure_env()
    project = read_json(args.project)
    lines = project["lines"]
    for ln in lines:
        if not ln.get("en") or not ln.get("zh"):
            sys.exit("every line in project.json needs both zh and en")
    video = args.video or project["meta"].get("source_video")
    if not video or not os.path.exists(video):
        sys.exit("source video not found — pass --video")
    name = args.name or project["meta"].get("name", "video")
    work = args.work or os.path.join(
        os.path.dirname(os.path.abspath(args.project)), "work"
    )
    os.makedirs(work, exist_ok=True)
    ass_p = os.path.join(work, "subs.ass")
    outdir = args.out or os.path.dirname(os.path.abspath(args.project))
    srt_p = os.path.join(outdir, f"{name}_中英双语字幕.srt")
    write_subtitles(lines, srt_p, ass_p, *_video_wh(video))
    log(f"render: regenerated {srt_p}")
    if args.srt_only:
        return
    full = os.path.join(outdir, f"{name}_中英双语字幕_完整版.mp4")
    render_video(video, ass_p, full)
    log(f"render complete: {full}")


def cmd_status(args):
    w = args.work
    stages = [
        ("setup", "meta.json"),
        ("transcribe", "trans1.txt"),
        ("timing", "whisper_segments.json"),
        ("align", "aligned.json"),
        ("clean", "zh_lines.json"),
    ]
    for label, f in stages:
        p = os.path.join(w, f)
        if label == "transcribe":
            try:
                meta = read_json(os.path.join(w, "meta.json"))
                n = len(meta["parts"])
                done = sum(
                    1
                    for i in range(1, n + 1)
                    if stage_done(os.path.join(w, f"trans{i}.txt"))
                )
                print(f"transcribe: {done}/{n} parts")
                continue
            except FileNotFoundError:
                pass
        print(f"{label}: {'DONE' if stage_done(p) else 'pending'} ({f})")
    lines_p = os.path.join(w, "zh_lines.json")
    if stage_done(lines_p):
        lines = read_json(lines_p)
        done = sum(1 for ln in lines if ln.get("en"))
        print(f"translate : {done}/{len(lines)} lines")


def main():
    ap = argparse.ArgumentParser(description="Bilingual subtitle pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="run full pipeline (resumable)")
    b.add_argument("--video", required=True)
    b.add_argument(
        "--doc", required=True, help="reference regulation document (.docx/.txt/.md)"
    )
    b.add_argument("--out", required=True, help="output dir for deliverables")
    b.add_argument(
        "--work",
        required=True,
        help="working dir for intermediates (keep for revisions!)",
    )
    b.add_argument(
        "--glossary", default="", help="term pairs file for translation (optional)"
    )
    b.add_argument(
        "--hotwords",
        default="",
        help="comma-separated hotwords for whisper segmentation",
    )
    b.add_argument(
        "--name", default="", help="base name for outputs (default: video filename)"
    )
    b.add_argument("--chunk-s", type=int, default=CHUNK_TARGET_S)
    b.add_argument(
        "--preview", type=int, default=90, help="preview clip seconds, 0 to skip"
    )
    b.add_argument("--skip-render", action="store_true")
    b.add_argument(
        "--stage",
        default="all",
        choices=["all", "transcribe", "timing", "align", "clean", "translate"],
        help="stop after this stage",
    )
    b.add_argument("--force", action="store_true", help="redo cached stages")
    b.set_defaults(fn=cmd_build)

    r = sub.add_parser("render", help="re-render from edited project.json")
    r.add_argument("--project", required=True, help="字幕工程.json file")
    r.add_argument("--video", default="", help="override source video")
    r.add_argument("--out", default="", help="output dir (default: project dir)")
    r.add_argument(
        "--work", default="", help="working dir (default: <project dir>/work)"
    )
    r.add_argument("--name", default="")
    r.add_argument(
        "--srt-only", action="store_true", help="only regenerate SRT/ASS, skip video"
    )
    r.set_defaults(fn=cmd_render)

    s = sub.add_parser("status", help="show stage progress")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=cmd_status)

    sp = sub.add_parser(
        "split", help="cut video into named short clips per chapter plan"
    )
    sp.add_argument("--plan", required=True, help="chapter plan JSON")
    sp.add_argument("--video", default="", help="video to split (overrides plan.video)")
    sp.add_argument("--out", required=True, help="output base dir")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_split)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
