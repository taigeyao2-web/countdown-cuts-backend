"""
video_pipeline.py

Parameterized version of the ranking-video builder, refactored so any
number of clips / any title / any captions can be processed programmatically
(used by the FastAPI backend in main.py).
"""

import subprocess
import os
import re
import shlex
from PIL import ImageFont

OUT_W, OUT_H = 720, 1280
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
OUTPUT_FPS = 30

COLOR_BLUE = "#29ABE2"
COLOR_GOLD = "#FFC107"
COLOR_GREEN = "#33CC33"
COLOR_ORANGE = "#FF7A00"
COLOR_WHITE = "white"

OUTLINE_WIDTH = 0
OUTLINE_COLOR = "black"

TITLE_FONT_SIZE = 53
TITLE_LINE1_Y = 93
TITLE_LINE_GAP = 73
TITLE_WORD_GAP = 15

LIST_FONT_SIZE = 39
LIST_LEFT_MARGIN = 53
LIST_RIGHT_MARGIN = 20
LIST_START_Y = 453
LIST_ROW_SPACING = 93
LIST_NUMBER_CAPTION_GAP = 9
LIST_MIN_CAPTION_FONT_SIZE = 20


class PipelineError(Exception):
    pass


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(f"Command failed:\n{cmd}\n--- stderr ---\n{result.stderr}")
    return result.stdout + result.stderr


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text):
    cleaned = _EMOJI_PATTERN.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def escape_text(text):
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def text_width(text, font_size):
    font = ImageFont.truetype(FONT_PATH, font_size)
    return font.getlength(text)


def get_duration(path):
    out = run(
        f"ffprobe -v error -show_entries format=duration "
        f"-of default=noprint_wrappers=1:nokey=1 {shlex.quote(path)}"
    )
    return float(out.strip())


def wrap_two_lines(text):
    words = text.split()
    if len(words) <= 2:
        return text, ""
    mid = len(words) // 2
    return " ".join(words[:mid]), " ".join(words[mid:])


def auto_title_lines(title_text):
    """
    Turn an arbitrary title string into a 2-line, 2-color-per-line structure:
    line 1 -> (first word: blue, rest: gold)
    line 2 -> (first word: green, rest: white)
    This generalizes the "accent word + neutral rest" look used across
    the reference designs, for any topic/title.
    """
    line1, line2 = wrap_two_lines(strip_emoji(title_text))
    lines = []

    for line, colors in ((line1, (COLOR_BLUE, COLOR_GOLD)), (line2, (COLOR_GREEN, COLOR_WHITE))):
        if not line:
            continue
        words = line.split()
        if len(words) == 1:
            lines.append([(words[0], colors[0])])
        else:
            lines.append([(words[0], colors[0]), (" ".join(words[1:]), colors[1])])
    return lines


def auto_rank_colors(num_ranks):
    """rank 1 (best) is always gold; the rest alternate white/orange."""
    colors = {1: COLOR_GOLD}
    cycle = [COLOR_WHITE, COLOR_ORANGE]
    for i, rank in enumerate(range(2, num_ranks + 1)):
        colors[rank] = cycle[i % 2]
    return colors


def trim_and_normalize_clip(src, out_path, trim_seconds):
    """Trim to trim_seconds (if given) and normalize to the output canvas + fps in one pass."""
    vf = f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H}"
    trim_flag = f"-t {trim_seconds}" if trim_seconds else ""
    cmd = (
        f"ffmpeg -y -i {shlex.quote(src)} {trim_flag} "
        f'-vf "{vf}" -r {OUTPUT_FPS} '
        f"-c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p -threads 1 "
        f"-c:a aac -b:a 128k -ar 44100 "
        f"{shlex.quote(out_path)}"
    )
    run(cmd)


def concat_clips(clip_paths, output_path, tmp_dir):
    list_file = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    cmd = (
        f"ffmpeg -y -f concat -safe 0 -i {shlex.quote(list_file)} "
        f"-c copy {shlex.quote(output_path)}"
    )
    run(cmd)


def build_title_filters(title_text):
    title_lines = auto_title_lines(title_text)
    filters = []
    for line_idx, line_words in enumerate(title_lines):
        y = TITLE_LINE1_Y + line_idx * TITLE_LINE_GAP
        widths = [text_width(w, TITLE_FONT_SIZE) for w, _ in line_words]
        total_width = sum(widths) + TITLE_WORD_GAP * (len(line_words) - 1)
        start_x = (OUT_W - total_width) / 2

        x = start_x
        for (word, color), w in zip(line_words, widths):
            esc = escape_text(word)
            filters.append(
                f"drawtext=fontfile={FONT_PATH}:text='{esc}':"
                f"fontsize={TITLE_FONT_SIZE}:fontcolor={color}:"
                f"borderw={OUTLINE_WIDTH}:bordercolor={OUTLINE_COLOR}:"
                f"x={x:.1f}:y={y}"
            )
            x += w + TITLE_WORD_GAP
    return filters


def fit_caption_font_size(caption_text, available_width):
    size = LIST_FONT_SIZE
    while size > LIST_MIN_CAPTION_FONT_SIZE:
        if text_width(caption_text, size) <= available_width:
            break
        size -= 4
    return size


def build_list_filters(clips):
    """
    clips: list of dicts in PLAY ORDER (worst to best), each with:
        {"path": normalized_clip_path, "rank": int, "caption": str}
    """
    filters = []
    num_ranks = len(clips)
    rank_colors = auto_rank_colors(num_ranks)

    cursor = 0.0
    start_times = {}
    for item in clips:
        start_times[item["rank"]] = cursor
        cursor += get_duration(item["path"])

    caption_by_rank = {item["rank"]: item["caption"] for item in clips}

    for rank in range(1, num_ranks + 1):
        color = rank_colors.get(rank, COLOR_WHITE)
        row_y = LIST_START_Y + (rank - 1) * LIST_ROW_SPACING
        number_text = escape_text(f"{rank}.")

        filters.append(
            f"drawtext=fontfile={FONT_PATH}:text='{number_text}':"
            f"fontsize={LIST_FONT_SIZE}:fontcolor={color}:"
            f"borderw={OUTLINE_WIDTH}:bordercolor={OUTLINE_COLOR}:"
            f"x={LIST_LEFT_MARGIN}:y={row_y}"
        )

        if rank in caption_by_rank:
            number_w = text_width(f"{rank}.", LIST_FONT_SIZE)
            caption_x = LIST_LEFT_MARGIN + number_w + LIST_NUMBER_CAPTION_GAP
            caption_raw = strip_emoji(caption_by_rank[rank])
            available_width = OUT_W - LIST_RIGHT_MARGIN - caption_x
            caption_font_size = fit_caption_font_size(caption_raw, available_width)
            caption_esc = escape_text(caption_raw)
            start_t = start_times[rank]
            caption_y = row_y + (LIST_FONT_SIZE - caption_font_size) / 2

            filters.append(
                f"drawtext=fontfile={FONT_PATH}:text='{caption_esc}':"
                f"fontsize={caption_font_size}:fontcolor={color}:"
                f"borderw={OUTLINE_WIDTH}:bordercolor={OUTLINE_COLOR}:"
                f"x={caption_x:.1f}:y={caption_y:.1f}:"
                f"enable='gte(t,{start_t:.3f})'"
            )

    return filters


def build_ranking_video(clips, title_text, output_path, tmp_dir):
    """
    Main entry point.

    clips: list of dicts in PLAY ORDER (worst to best), each with:
        {"path": <source file path>, "rank": int, "caption": str, "trim_seconds": float|None}
    title_text: e.g. "Top 5 Various Ankle Breaks"
    output_path: where to write the final mp4
    tmp_dir: scratch directory for intermediate files (caller creates/cleans it up)
    """
    os.makedirs(tmp_dir, exist_ok=True)

    normalized = []
    for idx, item in enumerate(clips):
        norm_path = os.path.join(tmp_dir, f"norm_{idx:03d}.mp4")
        trim_and_normalize_clip(item["path"], norm_path, item.get("trim_seconds"))
        normalized.append({"path": norm_path, "rank": item["rank"], "caption": item["caption"]})

    base_path = os.path.join(tmp_dir, "base_concat.mp4")
    concat_clips([c["path"] for c in normalized], base_path, tmp_dir)

    filters = build_title_filters(title_text) + build_list_filters(normalized)
    vf = ",".join(filters)

    cmd = (
        f"ffmpeg -y -i {shlex.quote(base_path)} "
        f'-vf "{vf}" '
        f"-c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p -threads 1 "
        f"-c:a copy "
        f"{shlex.quote(output_path)}"
    )
    run(cmd)
    return output_path
