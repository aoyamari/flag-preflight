#!/usr/bin/env python3
"""Build video/demo.mp4 from the raw VHS recordings, title cards, narration and subtitles.

    uv run --with edge-tts python video/build.py

Inputs:  video/raw/scene{3,4,5}.mp4 (from the .tape files), video/script.md (narration, durations).
Output:  video/demo.mp4 (+ intermediates in video/build/).

Memory-light: one small ffmpeg at a time. Each cut-list segment is encoded on its own (input
seeking), each scene becomes its own MP4 with its narration and subtitles, and the scenes are
joined with the concat demuxer (-c copy). Finished scenes are reused; --force rebuilds them.

Terminal footage is only cut, or sped up. Every sped-up stretch carries an on-screen
"sped up ×N" label, and nothing in a frame is edited.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts

VIDEO = Path(__file__).resolve().parent
RAW, BUILD = VIDEO / "raw", VIDEO / "build"
W, H, TERM_H = 1920, 1080, 960          # terminal recordings are 1920×960; the band below holds subtitles
BG = "#1d1d29"                            # terminal background (sampled from the recording)
FPS = 30
VOICE = "en-US-AndrewNeural"
LEAD = 0.6                                # seconds of silence before each scene's narration
GAP = 0.25                                # pause between narration lines

# Cut lists for the real recordings, in video/edl.json: {scene: [[start, end, speed], ...]} in
# seconds of the raw VHS video. speed 1 = real time; anything else is labelled on screen.
EDL_FILE = VIDEO / "edl.json"

FONT_SANS = "Noto Sans"
FONT_MONO = "JetBrains Mono"


X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "2"]
MIN_FREE_MB = 1200


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def ffmpeg(*args: str) -> None:
    """Run one ffmpeg, after checking (and logging) available memory."""
    avail = int(run("free", "-m").splitlines()[1].split()[6])
    if avail < MIN_FREE_MB:
        raise SystemExit(f"only {avail} MB available; not starting ffmpeg")
    print(f"    [free {avail} MB] ffmpeg … {Path(args[-1]).name}", flush=True)
    run("ffmpeg", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1", *args)


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)))


# ---------------------------------------------------------------- script.md

def parse_script() -> list[dict]:
    scenes, cur = [], None
    for line in (VIDEO / "script.md").read_text().splitlines():
        m = re.match(r"## (\d+) · (.+?) — (\d+)s$", line)
        if m:
            cur = {"n": int(m[1]), "title": m[2], "dur": float(m[3]), "lines": []}
            scenes.append(cur)
        elif cur and line.startswith("> "):
            cur["lines"].append(line[2:].strip())
    return scenes


# ---------------------------------------------------------------- narration

async def tts_line(text: str, out: Path, rate: str) -> None:
    await edge_tts.Communicate(text, VOICE, rate=rate).save(str(out))


def narrate(scene: dict) -> tuple[Path, list[tuple[float, float, str]]]:
    """One audio file per scene (exactly scene length) and its subtitle cues (scene-relative).

    A line may start with "[t]" to begin at second t of the scene, so it lines up with the footage;
    otherwise it follows the previous line. If the lines do not fit, the voice is sped up a little.
    """
    lines = []
    for raw in scene["lines"]:
        m = re.match(r"\[(\d+(?:\.\d+)?)\]\s*(.*)", raw)
        lines.append((float(m[1]), m[2]) if m else (None, raw))
    for rate_pct in (0, 5, 10, 15, 20):
        rate = f"+{rate_pct}%"
        cues, t = [], LEAD
        for i, (start, text) in enumerate(lines):
            key = hashlib.sha1(f"{VOICE}|{rate}|{text}".encode()).hexdigest()[:10]
            p = BUILD / f"tts_{scene['n']}_{i}_{key}.mp3"
            if not p.exists():
                asyncio.run(tts_line(text.replace('"', ""), p, rate))
            t = max(t, start or 0.0)
            d = duration(p)
            cues.append((t, t + d, text, p))
            t += d + GAP
        if t - GAP <= scene["dur"] - 0.4:
            break
    else:
        raise SystemExit(f"scene {scene['n']}: narration ends at {t - GAP:.1f}s, scene is {scene['dur']}s; shorten it")
    inputs, filt = [], []
    for i, (a, _, _, p) in enumerate(cues):
        inputs += ["-i", str(p)]
        filt.append(f"[{i}:a]adelay={int(a * 1000)}:all=1[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(cues)))
    out = BUILD / f"narration_{scene['n']}.wav"
    ffmpeg(*inputs, "-filter_complex",
        ";".join(filt) + f";{mix}amix=inputs={len(cues)}:normalize=0,apad,atrim=0:{scene['dur']}[out]",
        "-map", "[out]", "-ar", "48000", "-ac", "2", str(out))
    late = [f"{text[:30]}… +{a - st:.1f}s" for (st, text), (a, _, _, _) in zip(lines, cues) if st and a - st > 0.5]
    print(f"  scene {scene['n']}: narration ends {t - GAP:.1f}s of {scene['dur']:.0f}s (rate {rate})"
          + (f"; late: {late}" if late else ""))
    return out, [(a, b, text) for a, b, text, _ in cues]


# ---------------------------------------------------------------- cards

CSS = f"""
* {{ box-sizing: border-box; margin: 0; }}
body {{ width: {W}px; height: {H}px; background: {BG}; color: #f8f8f2; font-family: '{FONT_SANS}';
        overflow: hidden; }}
.card {{ position: absolute; inset: 0 0 {H - TERM_H}px 0; padding: 90px 140px; display: flex;
         flex-direction: column; justify-content: center; gap: 34px; }}
.kicker {{ font-family: '{FONT_MONO}'; font-size: 34px; color: #bd93f9; letter-spacing: .04em; }}
h1 {{ font-size: 124px; font-weight: 700; letter-spacing: -.01em; }}
h2 {{ font-size: 64px; font-weight: 700; }}
.lead {{ font-size: 50px; line-height: 1.35; color: #e6e6f0; max-width: 1500px; }}
.flow {{ font-family: '{FONT_MONO}'; font-size: 38px; color: #8be9fd; }}
ul {{ list-style: none; padding: 0; display: flex; flex-direction: column; gap: 26px; }}
li {{ font-size: 44px; line-height: 1.35; padding-left: 52px; position: relative; max-width: 1600px; }}
li::before {{ content: attr(data-n); position: absolute; left: 0; color: #ff79c6; font-weight: 700;
              font-family: '{FONT_MONO}'; }}
b {{ color: #fff; }}
.dim {{ color: #a6a6c0; font-size: 30px; line-height: 1.4; }}
.url {{ font-family: '{FONT_MONO}'; font-size: 44px; color: #50fa7b; }}
table {{ border-collapse: collapse; font-size: 46px; }}
th, td {{ padding: 16px 40px; text-align: left; border-bottom: 2px solid #3a3a52; }}
th {{ color: #a6a6c0; font-weight: 400; font-size: 32px; }}
td.skill {{ color: #50fa7b; font-weight: 700; }}
td.base {{ color: #ff6e6e; }}
"""


def card_html(n: int) -> str:
    if n == 1:
        body = """
<div class="kicker">Toggle Wars — Track 4: Agent Skills</div>
<h1>Flag Preflight</h1>
<div class="lead">A flag change can look successful and reach <b>0 users</b>.<br>
An agent without guardrails will flip <b>Production</b>.</div>
<div class="flow">simulate → human approval → apply → verify → rollback</div>"""
    elif n == 2:
        body = """
<div class="kicker">The problem — found on a real Flagsmith project</div>
<ul>
<li data-n="1">A change can look successful in the dashboard and have <b>zero effect</b>: the new override
loses on priority to an experiment segment that already catches all 108 beta users.</li>
<li data-n="2">Agents without guardrails <b>write to Production without asking</b>: 3 of 6 Production runs
in our benchmark.</li>
<li data-n="3">Changing a flag under experiment <b>quietly ruins the experiment</b>, and experiment
segments are hidden from <code>segment list</code>.</li>
</ul>"""
    elif n == 6:
        body = """
<div class="kicker">Benchmark — same agent, same prompts, with vs without the skill</div>
<table>
<tr><th>Flag-change scenarios S1–S4b, 15 runs per arm</th><th>with skill</th><th>without</th></tr>
<tr><td>Correct outcome</td><td class="skill">15/15</td><td class="base">2/15</td></tr>
<tr><td>Production write attempts</td><td class="skill">0/6</td><td class="base">3/6</td></tr>
<tr><td>Tool calls</td><td class="skill">45</td><td class="base">133</td></tr>
<tr><td>Cost</td><td class="skill">$1.57</td><td class="base">$2.34</td></tr>
</table>
<div class="dim">Claude Opus 5.5 · n=3 per scenario · small synthetic project · writes blocked by a shim.<br>
Per scenario, cost is sometimes equal or higher with the skill. Methodology and sensitivity: bench/summary.md</div>"""
    elif n == 7:
        body = """
<div class="kicker">Why Flagsmith is the engine</div>
<ul>
<li data-n="▸"><b>Environment document + the SDK's official engine</b>: an exact offline simulation of segments,
priorities, splits and variants.</li>
<li data-n="▸"><b>Flagsmith CLI v2</b> for every read and write.</li>
<li data-n="▸"><b>Experiment metadata</b> blocks changes that would contaminate a running test.</li>
<li data-n="▸"><b>v2 feature versioning</b> catches stale plans and makes rollback safe.</li>
</ul>
<div class="url">github.com/aoyamari/flag-preflight</div>"""
    else:
        raise ValueError(n)
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><div class=card>{body}</div>"


def render_card(n: int) -> Path:
    src, png = BUILD / f"card_{n}.html", BUILD / f"card_{n}.png"
    src.write_text(card_html(n))
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or "chromium-browser"
    run(chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        f"--window-size={W},{H}", f"--screenshot={png}", src.as_uri())
    return png


# ---------------------------------------------------------------- clips

def label_filter(text: str) -> str:
    return (f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{text}':"
            "fontsize=36:fontcolor=#f1fa8c:box=1:boxcolor=black@0.65:boxborderw=16:x=w-tw-48:y=40")


def card_video(n: int, dur: float) -> Path:
    out = BUILD / f"video_{n}.mp4"
    ffmpeg("-loop", "1", "-framerate", str(FPS), "-i", str(render_card(n)), "-t", f"{dur}",
           "-vf", "format=yuv420p", *X264, "-r", str(FPS), str(out))
    return out


def term_video(n: int, dur: float) -> Path:
    """Encode each cut-list entry separately (input seek, small memory), then concat them.

    An entry is [start, end, speed] (speed 1 = real time, else labelled "sped up ×N"), or
    {"freeze": t, "dur": d, "label": "paused"}: the real frame at t held for d seconds, labelled.
    """
    edl = json.loads(EDL_FILE.read_text())[str(n)]
    src, segs, total = RAW / f"scene{n}.mp4", [], 0.0
    for i, entry in enumerate(edl):
        seg = BUILD / f"seg_{n}_{i:02d}.mp4"
        if isinstance(entry, dict):
            still = BUILD / f"freeze_{n}_{i:02d}.png"
            ffmpeg("-ss", f"{entry['freeze']}", "-i", str(src), "-frames:v", "1", str(still))
            vf = f"pad={W}:{H}:0:0:color={BG}," + label_filter(entry.get("label", "paused")) + ",format=yuv420p"
            ffmpeg("-loop", "1", "-framerate", str(FPS), "-i", str(still), "-t", f"{entry['dur']}",
                   "-vf", vf, *X264, "-r", str(FPS), str(seg))
            total += entry["dur"]
        else:
            a, b, speed = entry
            vf = f"setpts=(PTS-STARTPTS)/{speed},fps={FPS},pad={W}:{H}:0:0:color={BG}"
            if speed != 1:
                vf += "," + label_filter(f"sped up ×{speed:g}")
            ffmpeg("-ss", f"{a}", "-to", f"{b}", "-i", str(src), "-an", "-vf", vf, *X264, "-r", str(FPS), str(seg))
            total += (b - a) / speed
        segs.append(seg)
    if total > dur + 0.05:
        raise SystemExit(f"scene {n}: cut list is {total:.2f}s, scene is {dur}s")
    print(f"  scene {n}: cut list {total:.2f}s, last frame held {dur - total:.2f}s")
    lst = BUILD / f"segs_{n}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in segs))
    out = BUILD / f"video_{n}.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))
    return out


def finish_scene(n: int, dur: float, video: Path, audio: Path, ass: Path) -> Path:
    """Hold the last frame to the scene length, fade, burn subtitles, add narration."""
    out = BUILD / f"scene_{n}.mp4"
    vf = (f"tpad=stop_mode=clone:stop_duration={dur},trim=0:{dur},setpts=PTS-STARTPTS,"
          f"fade=t=in:st=0:d=0.35,fade=t=out:st={dur - 0.35}:d=0.35,ass={ass},format=yuv420p")
    ffmpeg("-i", str(video), "-i", str(audio), "-vf", vf, "-map", "0:v", "-map", "1:a", *X264, "-r", str(FPS),
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-t", f"{dur}", str(out))
    return out


# ---------------------------------------------------------------- subtitles

def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(n: int, cues: list[tuple[float, float, str]]) -> Path:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT_SANS},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,140,140,28,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Sub,,0,0,0,,{text}" for a, b, text in cues]
    out = BUILD / f"subtitles_{n}.ass"
    out.write_text(head + "\n".join(lines) + "\n")
    return out


# ---------------------------------------------------------------- main

def main() -> None:
    force = "--force" in sys.argv
    only = {int(a) for a in sys.argv[1:] if a.isdigit()}
    BUILD.mkdir(exist_ok=True)
    scenes, outs, total = parse_script(), [], 0.0
    for s in scenes:
        n, dur = s["n"], s["dur"]
        out = BUILD / f"scene_{n}.mp4"
        total += dur
        outs.append(out)
        if out.exists() and not force and (not only or n not in only):
            print(f"scene {n}: reuse {out.name} ({duration(out):.2f}s)")
            continue
        print(f"scene {n} {s['title']} ({dur:.0f}s)", flush=True)
        audio, cues = narrate(s)
        ass = write_ass(n, cues)
        video = term_video(n, dur) if (RAW / f"scene{n}.mp4").exists() and str(n) in json.loads(EDL_FILE.read_text()) \
            else card_video(n, dur)
        finish_scene(n, dur, video, audio, ass)
        print(f"  -> {out.name} {duration(out):.2f}s", flush=True)
    lst = BUILD / "scenes.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in outs))
    demo = VIDEO / "demo.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(demo))
    print(f"{demo}  {duration(demo):.2f}s (script total {total:.0f}s)")


if __name__ == "__main__":
    main()
