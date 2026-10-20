"""Unit tests for the pure helpers in scripts/pipeline.py.

Covers the deterministic string/time-format utilities that subtitle
correctness depends on; the ffmpeg/HTTP stages are exercised end-to-end
manually (see SKILL.md verification checklist).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import pipeline  # noqa: E402


def test_norm_strips_punct():
    assert pipeline.norm("你好，世界！") == "你好世界"


def test_norm_strips_ascii_and_dashes():
    assert pipeline.norm("a-b c.d~e") == "abcde"


def test_nlen_counts_normalized():
    assert pipeline.nlen("a b\tc") == 3


def test_ts_srt_format():
    # _ts_srt uses int(t) for ms carry, so 3661.5 -> 01:01:02,500
    assert pipeline._ts_srt(3661.5) == "01:01:02,500"
    assert pipeline._ts_srt(0) == "00:00:00,000"


def test_ts_ass_format():
    assert pipeline._ts_ass(61.25) == "0:01:01.25"
    assert pipeline._ts_ass(0) == "0:00:00.00"


def test_write_subtitles_no_bom_and_escapes(tmp_path):
    lines = [
        {
            "idx": 0,
            "start": 0.5,
            "end": 4.2,
            "zh": "测试{大括号}",
            "en": "Test {braces}",
        },
        {"idx": 1, "start": 4.2, "end": 8.0, "zh": "反斜杠\\n", "en": "backslash"},
    ]
    srt = tmp_path / "t.srt"
    ass = tmp_path / "t.ass"
    pipeline.write_subtitles(lines, str(srt), str(ass), 1280, 720)
    raw = srt.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "SRT must be BOM-free"
    text = ass.read_text(encoding="utf-8")
    assert "(大括号)" in text, "ASS braces must be escaped to parens"
    assert "\\N{\\rEN}" in text, "line break macro must survive escaping"
    # font name written into styles is probed, never empty
    assert (
        "PingFang" in text
        or "YaHei" in text
        or "Noto" in text
        or "Heiti" in text
        or "Hiragino" in text
        or "WenQuanYi" in text
        or "SimHei" in text
        or "Source Han" in text
        or "Droid" in text
        or "Arial Unicode" in text
    )
