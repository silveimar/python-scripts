#!/usr/bin/env python3
"""
Read video IDs from a file and download transcripts via youtube-transcript-api.

For each video, writes:
  - {video_id}_{language}.json  — raw transcript (list of {text, start, duration})
  - {video_id}_{language}.md    — prose markdown (paragraphs, no per-line timestamps)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)


def extract_video_id(line: str) -> str | None:
    """Return a YouTube video ID from a bare ID or a common watch/short URL."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    # youtu.be/VIDEO_ID
    m = re.search(r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})", s)
    if m:
        return m.group(1)

    # youtube.com/watch?v=VIDEO_ID
    if "youtube.com" in s or "youtu.be" in s:
        parsed = urlparse(s if "://" in s else "https://" + s.lstrip("/"))
        if parsed.hostname and "youtube.com" in parsed.hostname:
            q = parse_qs(parsed.query)
            v = q.get("v", [None])[0]
            if v and len(v) >= 6:
                return v[:11] if len(v) >= 11 else v
        path = parsed.path.strip("/")
        if path.startswith("shorts/"):
            part = path.split("/")[1] if "/" in path else ""
            if part:
                return part[:11]

    # Bare ID (11 chars typical; allow shorter for edge cases)
    if re.fullmatch(r"[a-zA-Z0-9_-]{6,}", s):
        return s

    return None


def sanitize_lang_for_filename(lang: str) -> str:
    """Safe fragment for filenames (e.g. pt-BR → pt-BR)."""
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", lang.strip()) or "lang"


def _normalize_cue_text(text: str) -> str:
    """Single-line cue text suitable for joining."""
    t = (text or "").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _simple_html_to_markdown(text: str) -> str:
    """Convert common YouTube caption tags to Markdown."""
    s = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    s = re.sub(r"<strong>(.*?)</strong>", r"**\1**", s, flags=re.I | re.S)
    s = re.sub(r"<b>(.*?)</b>", r"**\1**", s, flags=re.I | re.S)
    s = re.sub(r"<em>(.*?)</em>", r"*\1*", s, flags=re.I | re.S)
    s = re.sub(r"<i>(.*?)</i>", r"*\1*", s, flags=re.I | re.S)
    return s


def _split_long_paragraph(text: str, max_chars: int = 2200) -> list[str]:
    """Break an overly long paragraph at sentence or word boundaries."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    parts: list[str] = []
    rest = text
    sentence_after = re.compile(r"(?<=[.!?…])\s+")

    while len(rest) > max_chars:
        window = rest[:max_chars]
        # Prefer splitting after a sentence end in the last third of the window
        min_cut = max(len(window) // 3, 80)
        best = -1
        for m in sentence_after.finditer(window):
            if m.end() >= min_cut:
                best = m.end()
        if best > 0:
            cut = best
        else:
            # Last space in trailing ~180 chars — avoids orphan one-word lines
            lo = max(0, max_chars - 180)
            cut = rest.rfind(" ", lo, max_chars)
            if cut <= 0:
                cut = max_chars
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()

    if rest:
        parts.append(rest)
    return parts


def raw_to_paragraphs(
    raw: list[dict],
    pause_sec: float = 2.5,
) -> list[str]:
    """
    Merge cues into paragraphs: break when the gap between consecutive non-empty
    cues exceeds pause_sec (natural pause / topic shift in speech).
    """
    if not raw:
        return []

    chunks: list[str] = []
    buf: list[str] = []
    prev_end: float | None = None

    for item in raw:
        text = _normalize_cue_text(item.get("text") or "")
        if not text:
            continue
        start = float(item.get("start", 0.0))
        dur = float(item.get("duration", 0.0))
        end = start + dur

        if buf and prev_end is not None:
            gap = start - prev_end
            if gap > pause_sec:
                merged = " ".join(buf)
                chunks.extend(_split_long_paragraph(merged))
                buf = []

        buf.append(text)
        prev_end = end

    if buf:
        merged = " ".join(buf)
        chunks.extend(_split_long_paragraph(merged))

    return [c for c in chunks if c]


def raw_to_markdown(
    video_id: str,
    language_code: str,
    language_name: str,
    raw: list[dict],
    pause_sec: float = 2.5,
) -> str:
    header_lines = [
        "# Transcript",
        "",
        f"- **Video ID:** `{video_id}`",
        f"- **Language:** {language_name} (`{language_code}`)",
        "",
        "---",
        "",
    ]
    paragraphs = raw_to_paragraphs(raw, pause_sec=pause_sec)
    body_parts: list[str] = []
    for p in paragraphs:
        prose = _simple_html_to_markdown(p)
        prose = re.sub(r"\s+", " ", prose).strip()
        if prose:
            body_parts.append(prose)
    body = "\n\n".join(body_parts)
    return "\n".join(header_lines) + body + "\n"


def load_video_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    ids: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        vid = extract_video_id(line)
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download YouTube transcripts from a list of video IDs.",
    )
    parser.add_argument(
        "input_file",
        type=Path,
        help="Text file with one video ID or YouTube URL per line (# comments allowed).",
    )
    parser.add_argument(
        "-l",
        "--language",
        default="en",
        help="Preferred language code (default: en). Passed to fetch(languages=[...]).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for .json and .md files (default: current directory).",
    )
    parser.add_argument(
        "--preserve-formatting",
        action="store_true",
        help="Keep HTML tags like <i> and <b> in text (youtube-transcript-api option).",
    )
    parser.add_argument(
        "--paragraph-pause",
        type=float,
        default=2.5,
        metavar="SEC",
        help="Min. silence gap (seconds) between cues to start a new paragraph in .md (default: 2.5).",
    )
    args = parser.parse_args()

    if not args.input_file.is_file():
        print(f"Error: input file not found: {args.input_file}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    langs = [args.language]
    api = YouTubeTranscriptApi()

    video_ids = load_video_ids(args.input_file)
    if not video_ids:
        print("No video IDs found in input file.", file=sys.stderr)
        return 1

    ok = 0
    for video_id in video_ids:
        lang_tag = sanitize_lang_for_filename(args.language)
        base = f"{video_id}_{lang_tag}"
        json_path = args.output_dir / f"{base}.json"
        md_path = args.output_dir / f"{base}.md"

        try:
            fetched = api.fetch(
                video_id,
                languages=langs,
                preserve_formatting=args.preserve_formatting,
            )
        except (VideoUnavailable, TranscriptsDisabled, NoTranscriptFound) as e:
            print(f"[skip] {video_id}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"[error] {video_id}: {e}", file=sys.stderr)
            continue

        raw = fetched.to_raw_data()
        lang_code = getattr(fetched, "language_code", None) or args.language
        lang_name = getattr(fetched, "language", None) or lang_code

        json_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        md_path.write_text(
            raw_to_markdown(
                video_id,
                lang_code,
                lang_name,
                raw,
                pause_sec=args.paragraph_pause,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {json_path.name} and {md_path.name}")
        ok += 1

    print(f"Done: {ok}/{len(video_ids)} transcript(s) saved under {args.output_dir.resolve()}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
