#!/usr/bin/env python3
"""
Snipforge — Video Toolkit
Single-file app: screen record, edit, trim, merge, convert, compress, silence removal.

Usage:
  pip install flask
  python snipforge.py
  Open http://localhost:5000
"""

import os, re, sys, json, uuid, shutil, threading, tempfile, subprocess, time, hashlib, sqlite3, datetime
from pathlib import Path
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, jsonify, render_template_string, request, send_file, session, redirect, url_for

# ─── FFmpeg discovery ─────────────────────────────────────────────────────────
_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_FFMPEG_EXE  = "ffmpeg"
_FFPROBE_EXE = "ffprobe"

def _find_ffmpeg():
    global _FFMPEG_EXE, _FFPROBE_EXE
    import shutil as _sh
    search = [
        _SCRIPT_DIR,
        r"C:\ffmpeg\bin",
        os.path.join(os.path.expanduser("~"), "ffmpeg", "bin"),
        os.path.join(os.path.expanduser("~"), "AppData","Local","Microsoft","WinGet",
                     "Packages","Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
                     "ffmpeg-8.1-full_build","bin"),
    ]
    ext = ".exe" if sys.platform == "win32" else ""
    for name, var in [("ffmpeg","_FFMPEG_EXE"),("ffprobe","_FFPROBE_EXE")]:
        if _sh.which(name): continue
        for d in search:
            c = os.path.join(d, name+ext)
            if os.path.isfile(c):
                globals()[var] = c
                break
_find_ffmpeg()

# ─── Core helpers ─────────────────────────────────────────────────────────────
def run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode

def get_duration(path):
    out, err, rc = run([_FFPROBE_EXE,"-v","error","-show_entries","format=duration",
                        "-of","default=noprint_wrappers=1:nokey=1", str(path)])
    v = out.strip()
    if not v: raise RuntimeError(f"ffprobe failed: {err.strip()}")
    return float(v)

def get_info(path):
    out, _, _ = run([_FFPROBE_EXE,"-v","error","-print_format","json",
                     "-show_streams","-show_format", str(path)])
    try: return json.loads(out)
    except: return {}

def fmt_time(sec):
    m=int(sec//60); s=sec%60
    return f"{m}:{s:05.2f}"

def detect_silences(path, threshold_db=-40, min_silence_ms=300):
    dur = min_silence_ms/1000.0
    r = subprocess.run([_FFMPEG_EXE,"-i",str(path),"-af",
                        f"silencedetect=noise={threshold_db}dB:duration={dur}",
                        "-f","null","-"], capture_output=True, text=True)
    combined = r.stdout + r.stderr
    starts = re.findall(r'silence_start: ([0-9.]+)', combined)
    ends   = re.findall(r'silence_end: ([0-9.]+)', combined)
    return [(float(s),float(e)) for s,e in zip(starts,ends)]

def silences_to_keeps(silences, total, pad_ms=80):
    pad=pad_ms/1000.0; keeps=[]; cursor=0.0
    for ss,se in silences:
        ke=ss+pad
        if ke>cursor: keeps.append((cursor,ke))
        cursor=max(cursor,se-pad)
    if cursor<total: keeps.append((cursor,total))
    merged=[]
    for seg in keeps:
        if merged and seg[0]<=merged[-1][1]: merged[-1]=(merged[-1][0],max(merged[-1][1],seg[1]))
        else: merged.append(list(seg))
    return merged


def op_denoise(jid, src, dst, strength="medium"):
    """Remove background noise using FFmpeg arnndn (neural network) filter."""
    try:
        prog(jid, "Analysing audio…", 10)
        # arnndn = AI-based noise reduction, afftdn = FFT denoising
        # Use different strengths
        strength_map = {
            "light":  "afftdn=nf=-20",          # gentle, within valid range
            "medium": "afftdn=nf=-30",           # balanced
            "heavy":  "afftdn=nf=-50:nt=w",     # aggressive
        }
        af = strength_map.get(strength, strength_map["medium"])
        prog(jid, f"Removing noise ({strength} strength)…", 30)
        _, err, rc = run([
            _FFMPEG_EXE, "-y", "-i", src,
            "-af", af,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(dst)
        ])
        if rc != 0:
            # Fallback: use just afftdn if arnndn model not available
            prog(jid, "Trying fallback denoiser…", 40)
            strength_map2 = {
                "light":  "afftdn=nf=-20",
                "medium": "afftdn=nf=-30",
                "heavy":  "afftdn=nf=-50:nt=w",
            }
            af2 = strength_map2.get(strength, "afftdn=nf=-25")
            _, err, rc = run([
                _FFMPEG_EXE, "-y", "-i", src,
                "-af", af2,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                str(dst)
            ])
            if rc != 0:
                raise RuntimeError(f"Denoise failed: {err[-300:]}")
        new_dur = get_duration(dst)
        prog(jid, f"Done! Background noise removed.", 100)
        done(jid, dst, {"new": round(new_dur, 2)})
    except Exception as e:
        fail(jid, e)

def op_rotate(jid, src, dst, angle):
    try:
        prog(jid,f"Rotating {angle}°…",10)
        filters = {"90":"transpose=1","180":"transpose=1,transpose=1",
                   "270":"transpose=2","-90":"transpose=2",
                   "hflip":"hflip","vflip":"vflip"}
        vf = filters.get(str(angle),"transpose=1")
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-vf",vf,
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","copy", str(dst)])
        if rc!=0: raise RuntimeError(f"Rotate failed: {err[-300:]}")
        prog(jid,"Done!",100)
        done(jid,dst,{"angle":angle})
    except Exception as e: fail(jid,e)

def op_volume(jid, src, dst, volume):
    try:
        prog(jid,f"Adjusting volume to {volume}x…",10)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,
                          "-af",f"volume={volume}",
                          "-c:v","copy","-c:a","aac","-b:a","192k", str(dst)])
        if rc!=0: raise RuntimeError(f"Volume failed: {err[-300:]}")
        prog(jid,"Done!",100)
        done(jid,dst,{"volume":volume})
    except Exception as e: fail(jid,e)


def op_crop(jid, src, dst, preset):
    try:
        prog(jid,f"Resizing for {preset}…",10)
        presets = {
            "youtube":   "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "instagram": "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2",
            "tiktok":    "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "linkedin":  "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "twitter":   "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "square":    "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2",
        }
        vf = presets.get(preset, presets["youtube"])
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-vf",vf,
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","copy", str(dst)])
        if rc!=0: raise RuntimeError(f"Resize failed: {err[-300:]}")
        prog(jid,f"Done! Resized for {preset}",100)
        done(jid,dst,{"preset":preset})
    except Exception as e: fail(jid,e)

def op_watermark(jid, src, dst, text):
    """Add watermark using a 2-pass approach that avoids path/font issues on Windows."""
    try:
        prog(jid, "Adding watermark...", 10)
        safe_text = re.sub(r"[^a-zA-Z0-9 @._-]", "", text)[:50]

        # Use temp dir (short path, no spaces)
        tmp_in  = os.path.join(tempfile.gettempdir(), "snip_wm_in.mp4")
        tmp_out = os.path.join(tempfile.gettempdir(), "snip_wm_out.mp4")
        shutil.copy2(str(src), tmp_in)

        # Try approach 1: drawtext with font file
        font_paths = [
            os.path.join(os.environ.get("WINDIR","C:/Windows"), "Fonts", "arial.ttf"),
            os.path.join(os.environ.get("WINDIR","C:/Windows"), "Fonts", "calibri.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        font_file = next((f for f in font_paths if os.path.exists(f)), None)
        rc = 1

        if font_file:
            ff = font_file.replace("\\", "/")
            # Use %3A for colon in fontfile path to avoid filter parser issues
            ff_safe = ff.replace(":", "\\\\:")
            vf = (f"drawtext=fontfile='{ff_safe}'"
                  f":text='{safe_text}'"
                  f":fontsize=32:fontcolor=white@0.9"
                  f":x=w-tw-20:y=h-th-20"
                  f":box=1:boxcolor=black@0.5:boxborderw=5")
            _, err, rc = run([_FFMPEG_EXE, "-y", "-i", tmp_in,
                              "-vf", vf,
                              "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-c:a", "copy", tmp_out])

        # Fallback approach: use subtitles/ass format burned in (no font needed)
        if rc != 0:
            prog(jid, "Trying subtitle watermark method...", 40)
            # Create a simple ASS subtitle file for the watermark
            ass_file = os.path.join(tempfile.gettempdir(), "snip_wm.ass")
            ass_content = (
                "[Script Info]\nScriptType: v4.00+\n"
                "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,BackColour,"
                "Bold,Italic,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV\n"
                "Style: Watermark,Arial,28,&H00FFFFFF,&H80000000,1,0,3,1,1,3,10,10,10\n"
                "[Events]\nFormat: Start,End,Style,Text\n"
            )
            # Add watermark for full duration (0 to 9 hours)
            ass_content += f"Dialogue: 0:00:00.00,9:00:00.00,Watermark,{safe_text}\n"
            with open(ass_file, "w") as f:
                f.write(ass_content)
            ass_safe = ass_file.replace("\\", "/").replace(":", "\\\\:")
            vf2 = f"ass='{ass_safe}'"
            _, err, rc = run([_FFMPEG_EXE, "-y", "-i", tmp_in,
                              "-vf", vf2,
                              "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-c:a", "copy", tmp_out])

        # Final fallback: overlay a semi-transparent color bar with no text
        if rc != 0:
            prog(jid, "Using basic overlay watermark...", 60)
            vf3 = ("drawbox=x=iw-200:y=ih-40:w=200:h=40:"
                   "color=black@0.5:t=fill,"
                   "drawbox=x=iw-200:y=ih-40:w=200:h=40:"
                   "color=white@0.3:t=2")
            _, err, rc = run([_FFMPEG_EXE, "-y", "-i", tmp_in,
                              "-vf", vf3,
                              "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-c:a", "copy", tmp_out])

        if rc != 0:
            raise RuntimeError("Watermark failed: " + err[-300:])

        shutil.move(tmp_out, str(dst))
        prog(jid, "Done! Watermark added.", 100)
        done(jid, dst, {})
    except Exception as e:
        fail(jid, e)

def op_stabilize(jid, src, dst):
    try:
        prog(jid,"Analysing shake…",10)
        trf = str(dst)+".trf"
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,
                          "-vf",f"vidstabdetect=result={trf}:shakiness=10",
                          "-f","null","-"])
        if rc!=0: raise RuntimeError(f"Stabilize detect failed: {err[-300:]}")
        prog(jid,"Stabilizing…",50)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,
                          "-vf",f"vidstabtransform=input={trf}:smoothing=30",
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","copy", str(dst)])
        try: os.unlink(trf)
        except: pass
        if rc!=0: raise RuntimeError(f"Stabilize transform failed: {err[-300:]}")
        prog(jid,"Done! Video stabilized.",100)
        done(jid,dst,{})
    except Exception as e: fail(jid,e)

# ─── Job store ────────────────────────────────────────────────────────────────
UPLOAD_DIR = Path(_SCRIPT_DIR) / "uploads"; UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path(_SCRIPT_DIR) / "outputs"; OUTPUT_DIR.mkdir(exist_ok=True)
jobs = {}

def new_job():
    jid = str(uuid.uuid4())[:8]
    jobs[jid] = {"status":"running","progress":0,"log":[],"result":None,"error":None,
                  "orig_filename":"","user_id":"","operation":"","orig_size_mb":0}
    return jid

def prog(jid, msg, pct=None):
    jobs[jid]["log"].append(msg)
    if pct is not None: jobs[jid]["progress"] = pct

def done(jid, path, stats=None):
    # Auto-watermark for free plan users
    try:
        if jobs[jid].get('add_watermark') and path and os.path.exists(str(path)):
            ext = Path(str(path)).suffix.lower()
            if ext in ('.mp4','.mov','.webm','.avi'):
                wm_path = str(path)
                tmp_wm = os.path.join(tempfile.gettempdir(), f"snip_autowm_{jid}.mp4")
                tmp_in = os.path.join(tempfile.gettempdir(), f"snip_wm_in_{jid}.mp4")
                shutil.copy2(wm_path, tmp_in)
                font_paths = [
                    os.path.join(os.environ.get("WINDIR","C:/Windows"), "Fonts", "arial.ttf"),
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                ]
                font_file = next((f for f in font_paths if os.path.exists(f)), None)
                ff = font_file.replace("\\","/").replace("\\","/") if font_file else None
                vf = (f"drawtext=fontfile='{ff}':text='Made with Snipforge':fontsize=16:fontcolor=white@0.7:x=10:y=h-th-10:box=1:boxcolor=black@0.3:boxborderw=3"
                      if ff else "drawtext=text='Made with Snipforge':fontsize=16:fontcolor=white@0.7:x=10:y=h-th-10:box=1:boxcolor=black@0.3:boxborderw=3")
                r = subprocess.run([_FFMPEG_EXE,"-y","-i",tmp_in,"-vf",vf,
                    "-c:v","libx264","-preset","fast","-crf","22","-c:a","copy",tmp_wm],
                    capture_output=True)
                if r.returncode == 0:
                    shutil.move(tmp_wm, wm_path)
                try: os.unlink(tmp_in)
                except: pass
    except Exception:
        pass  # Never fail due to watermark issues

    jobs[jid]["status"]="done"; jobs[jid]["result"]=str(path)
    jobs[jid]["stats"]=stats; jobs[jid]["progress"]=100
    # Save to history if user is known
    try:
        uid = jobs[jid].get("user_id")
        if uid and stats:
            save_history(
                user_id    = uid,
                job_id     = jid,
                filename   = jobs[jid].get("orig_filename","unknown"),
                operation  = jobs[jid].get("operation","process"),
                orig_dur   = stats.get("original", stats.get("original_duration", 0)),
                new_dur    = stats.get("new", stats.get("new_duration", 0)),
                orig_size_mb = jobs[jid].get("orig_size_mb", 0),
                new_size_mb  = round(os.path.getsize(str(path))/1e6, 1) if path and os.path.exists(str(path)) else 0,
            )
    except: pass

def fail(jid, err):
    jobs[jid]["status"]="error"; jobs[jid]["error"]=str(err)

# ─── Video operations ─────────────────────────────────────────────────────────

def op_shorten(jid, src, dst, threshold=-40, min_silence=300, pad=80, speed=1.3, do_speed=True):
    try:
        prog(jid,"Analysing video…",0)
        total = get_duration(src)
        prog(jid,f"Duration: {fmt_time(total)}",5)
        prog(jid,f"Detecting silences…",10)
        silences = detect_silences(src, threshold, min_silence)
        prog(jid,f"Found {len(silences)} silence(s)",20)
        segments = silences_to_keeps(silences, total, pad)
        prog(jid,f"Keeping {len(segments)} segment(s)",25)

        # Build valid segment list with sequential indices
        valid_segs = [(ts,te) for ts,te in segments if (te-ts)>=0.05]

        # If only 1 segment = full video, skip complex filtering
        single_seg = len(valid_segs) == 1 and abs(valid_segs[0][0]) < 0.1 and abs(valid_segs[0][1] - total) < 0.5

        if not valid_segs or single_seg:
            if not do_speed or abs(speed-1.0) < 0.01:
                # No silences, no speed — just copy
                prog(jid,"No silences found — copying original…",40)
                out = str(dst)
                if out.endswith(".tmp.mp4"):
                    out = out.replace(".tmp.mp4", ".mp4")
                elif not out.endswith(".mp4"):
                    out = out + ".mp4"
                shutil.copy2(src, out)
                new_dur = get_duration(out)
                prog(jid,f"Done! Duration unchanged: {fmt_time(new_dur)}",100)
                done(jid, out, {"original":round(total,2),"new":round(new_dur,2),"saved":0,"pct":0})
                return
            else:
                # No silences but speed requested — apply speed directly
                prog(jid,f"No silences — applying {speed:.2f}× speed…",40)
                # Fix path - dst might already have .mp4
                out = str(dst)
                if out.endswith(".tmp.mp4"):
                    out = out.replace(".tmp.mp4", ".mp4")
                elif not out.endswith(".mp4"):
                    out = out + ".mp4"
                rem=speed; atempos=[]
                while rem>2.0: atempos.append("atempo=2.0"); rem/=2.0
                atempos.append(f"atempo={rem:.4f}")
                tmp_speed = os.path.join(tempfile.gettempdir(), f"snip_speed_{jid}.mp4")
                # Try 1: with audio
                _, err2, rc2 = run([_FFMPEG_EXE,"-y",
                                    "-i",src,
                                    "-filter_complex",
                                    f"[0:v]setpts={1/speed:.6f}*PTS[v];[0:a]{','.join(atempos)}[a]",
                                    "-map","[v]","-map","[a]",
                                    "-c:v","libx264","-preset","ultrafast","-crf","22",
                                    "-c:a","aac","-b:a","128k",
                                    "-movflags","+faststart",
                                    tmp_speed])
                if rc2 != 0:
                    # Try 2: video only (no audio stream)
                    _, err2, rc2 = run([_FFMPEG_EXE,"-y",
                                        "-i",src,
                                        "-vf",f"setpts={1/speed:.6f}*PTS",
                                        "-an",
                                        "-c:v","libx264","-preset","ultrafast","-crf","22",
                                        "-movflags","+faststart",
                                        tmp_speed])
                if rc2 != 0:
                    # Try 3: copy streams, just change pts
                    _, err2, rc2 = run([_FFMPEG_EXE,"-y",
                                        "-i",src,
                                        "-vf",f"setpts={1/speed:.6f}*PTS",
                                        "-c:v","libx264","-preset","ultrafast",
                                        "-c:a","copy",
                                        "-movflags","+faststart",
                                        tmp_speed])
                if rc2 != 0:
                    raise RuntimeError(f"Speed failed: {err2[-200:]}")
                shutil.move(tmp_speed, out)
                new_dur = get_duration(out)
                saved = total - new_dur
                prog(jid,f"Done! {fmt_time(total)} → {fmt_time(new_dur)}",100)
                done(jid, out, {"original":round(total,2),"new":round(new_dur,2),
                                "saved":round(max(0,saved),2),"pct":round(max(0,saved)/total*100,1)})
                return

        nv = len(valid_segs)

        # Check if video has audio stream
        _, probe_err, _ = run([_FFMPEG_EXE, "-i", src])
        has_audio = "Audio:" in probe_err

        filter_parts=[]
        for i,(ts,te) in enumerate(valid_segs):
            if has_audio:
                filter_parts.append(
                    f"[0:v]trim=start={ts:.4f}:end={te:.4f},setpts=PTS-STARTPTS[v{i}];"
                    f"[0:a]atrim=start={ts:.4f}:end={te:.4f},asetpts=PTS-STARTPTS[a{i}]"
                )
            else:
                filter_parts.append(
                    f"[0:v]trim=start={ts:.4f}:end={te:.4f},setpts=PTS-STARTPTS[v{i}]"
                )

        vi="".join(f"[v{i}]" for i in range(nv))
        fc=";".join(filter_parts)

        prog(jid,"Cutting silences…",40)
        tmp = str(dst)+".tmp.mp4"

        if has_audio:
            ai="".join(f"[a{i}]" for i in range(nv))
            fc+=f";{vi}concat=n={nv}:v=1:a=0[vout];{ai}concat=n={nv}:v=0:a=1[aout]"
            _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-filter_complex",fc,
                              "-map","[vout]","-map","[aout]",
                              "-c:v","libx264","-preset","fast","-crf","20",
                              "-c:a","aac","-b:a","128k", tmp])
        else:
            fc+=f";{vi}concat=n={nv}:v=1:a=0[vout]"
            _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-filter_complex",fc,
                              "-map","[vout]",
                              "-c:v","libx264","-preset","fast","-crf","20", tmp])

        if rc!=0: raise RuntimeError(f"Silence cut failed: {err[-300:]}")

        src2=tmp
        if do_speed and abs(speed-1.0)>0.01:
            prog(jid,f"Applying {speed:.2f}× speed…",75)
            rem=speed; atempos=[]
            while rem>2.0: atempos.append("atempo=2.0"); rem/=2.0
            atempos.append(f"atempo={rem:.4f}")
            _, err, rc = run([_FFMPEG_EXE,"-y","-i",src2,
                              "-vf",f"setpts={1/speed:.6f}*PTS",
                              "-af",",".join(atempos),
                              "-c:v","libx264","-preset","fast","-crf","20",
                              "-c:a","aac","-b:a","128k", str(dst)])
            if rc!=0: raise RuntimeError(f"Speed failed: {err[-300:]}")
            os.unlink(tmp)
        else:
            shutil.move(tmp, str(dst))

        new_dur=get_duration(dst); saved=total-new_dur
        prog(jid,f"Done! {fmt_time(total)} → {fmt_time(new_dur)}",100)
        done(jid, dst, {"original":round(total,2),"new":round(new_dur,2),"saved":round(max(0,saved),2),"pct":round(max(0,saved)/total*100,1)})
    except Exception as e:
        fail(jid, e)

def op_trim(jid, src, dst, start, end):
    try:
        prog(jid,f"Trimming {fmt_time(start)} → {fmt_time(end)}…",10)
        dur=end-start
        _, err, rc = run([_FFMPEG_EXE,"-y","-ss",str(start),"-i",src,"-t",str(dur),
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","aac","-b:a","128k", str(dst)])
        if rc!=0: raise RuntimeError(f"Trim failed: {err[-300:]}")
        new_dur=get_duration(dst)
        prog(jid,f"Done! Duration: {fmt_time(new_dur)}",100)
        done(jid,dst,{"new":round(new_dur,2)})
    except Exception as e:
        fail(jid,e)

def op_multi_trim(jid, src, dst, segments):
    try:
        prog(jid,f"Multi-trim: {len(segments)} segments…",10)
        total=get_duration(src)
        valid=[(s,e) for s,e in segments if e>s and s>=0 and e<=total]
        if not valid: raise RuntimeError("No valid segments.")
        filter_parts=[]
        for i,(ts,te) in enumerate(valid):
            filter_parts.append(
                f"[0:v]trim=start={ts:.4f}:end={te:.4f},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim=start={ts:.4f}:end={te:.4f},asetpts=PTS-STARTPTS[a{i}]"
            )
        n=len(valid)
        vi="".join(f"[v{i}]" for i in range(n))
        ai="".join(f"[a{i}]" for i in range(n))
        fc=";".join(filter_parts)+f";{vi}concat=n={n}:v=1:a=0[vout];{ai}concat=n={n}:v=0:a=1[aout]"
        prog(jid,"Stitching segments…",50)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-filter_complex",fc,
                          "-map","[vout]","-map","[aout]",
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","aac","-b:a","128k", str(dst)])
        if rc!=0: raise RuntimeError(f"Multi-trim failed: {err[-300:]}")
        new_dur=get_duration(dst)
        prog(jid,f"Done! {fmt_time(new_dur)}",100)
        done(jid,dst,{"new":round(new_dur,2),"segments":n})
    except Exception as e:
        fail(jid,e)

def op_merge(jid, srcs, dst):
    try:
        prog(jid,f"Merging {len(srcs)} videos…",10)
        tmpdir=tempfile.mkdtemp()
        clips=[]

        # Step 1: Re-encode every clip to identical specs
        # Fix: use -vsync cfr and -async 1 to lock frame/audio rates
        # Fix: reset timestamps with -start_at_zero so concat doesn't drift
        for i,s in enumerate(srcs):
            c=os.path.join(tmpdir,f"c{i}.mp4")
            prog(jid,f"Normalising clip {i+1}/{len(srcs)}…", 10+int(i/len(srcs)*40))
            _, err, rc = run([
                _FFMPEG_EXE,"-y","-i",s,
                # Video: fixed 30fps, H264, normalised resolution
                "-vf","scale=1280:720:force_original_aspect_ratio=decrease,"
                      "pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
                "-c:v","libx264","-preset","fast","-crf","20",
                "-vsync","cfr",                   # constant frame rate — fixes video speed
                # Audio: fixed 44100Hz stereo AAC
                "-c:a","aac","-b:a","128k",
                "-ar","44100","-ac","2",
                "-async","1",                     # fix audio drift
                # Reset timestamps so each clip starts at 0
                "-avoid_negative_ts","make_zero",
                c
            ])
            if rc==0 and os.path.exists(c):
                clips.append(c)
            else:
                prog(jid,f"Warning: clip {i+1} failed, skipping",None)

        if not clips:
            raise RuntimeError("No clips could be prepared.")

        prog(jid,f"Normalised {len(clips)} clips, concatenating…",60)

        # Step 2: Write concat file with forward slashes (Windows safe)
        concat_f=os.path.join(tmpdir,"concat.txt")
        with open(concat_f,"w") as f:
            for c in clips:
                f.write(f"file '{c.replace(chr(92), chr(47))}'\n")

        # Step 3: Concat and re-encode (NOT copy) so timestamps are rebuilt cleanly
        _, err, rc = run([
            _FFMPEG_EXE,"-y",
            "-f","concat","-safe","0","-i",concat_f,
            "-c:v","libx264","-preset","fast","-crf","20",
            "-vsync","cfr",
            "-c:a","aac","-b:a","128k",
            "-ar","44100","-ac","2",
            "-movflags","+faststart",
            str(dst)
        ])
        if rc!=0:
            raise RuntimeError(f"Merge failed: {err[-400:]}")

        shutil.rmtree(tmpdir,ignore_errors=True)
        new_dur=get_duration(dst)
        prog(jid,f"Done! {fmt_time(new_dur)}",100)
        done(jid,dst,{"new":round(new_dur,2),"clips":len(clips)})
    except Exception as e:
        fail(jid,e)

def op_convert(jid, src, dst, fmt):
    try:
        prog(jid,f"Converting to {fmt}…",10)
        args=[_FFMPEG_EXE,"-y","-i",src]
        if fmt=="mp3":
            args+=["-vn","-acodec","libmp3lame","-b:a","192k"]
        elif fmt=="wav":
            args+=["-vn","-acodec","pcm_s16le"]
        elif fmt=="gif":
            args+=["-vf","fps=15,scale=640:-1:flags=lanczos","-loop","0"]
        elif fmt=="webm":
            args+=["-c:v","libvpx-vp9","-crf","30","-b:v","0","-c:a","libopus"]
        elif fmt=="mov":
            args+=["-c:v","libx264","-preset","fast","-crf","20","-c:a","aac"]
        else:  # mp4
            args+=["-c:v","libx264","-preset","fast","-crf","20","-c:a","aac","-b:a","128k"]
        args.append(str(dst))
        _, err, rc = run(args)
        if rc!=0: raise RuntimeError(f"Convert failed: {err[-300:]}")
        prog(jid,f"Done! Converted to {fmt}",100)
        done(jid,dst,{"format":fmt})
    except Exception as e:
        fail(jid,e)

def op_compress(jid, src, dst, quality):
    try:
        prog(jid,f"Compressing (quality: {quality})…",10)
        crf={"low":28,"medium":32,"high":38}.get(quality,32)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,
                          "-c:v","libx264","-preset","slow","-crf",str(crf),
                          "-c:a","aac","-b:a","96k","-movflags","+faststart", str(dst)])
        if rc!=0: raise RuntimeError(f"Compress failed: {err[-300:]}")
        orig=os.path.getsize(src); new=os.path.getsize(dst)
        saved_pct=round((1-new/orig)*100,1)
        prog(jid,f"Done! Saved {saved_pct}% ({round(orig/1e6,1)}MB → {round(new/1e6,1)}MB)",100)
        done(jid,dst,{"original_mb":round(orig/1e6,1),"new_mb":round(new/1e6,1),"saved_pct":saved_pct})
    except Exception as e:
        fail(jid,e)

def op_speed(jid, src, dst, speed):
    try:
        prog(jid,f"Changing speed to {speed}×…",10)
        rem=speed; atempos=[]
        while rem>2.0: atempos.append("atempo=2.0"); rem/=2.0
        if rem<0.5:
            while rem<0.5: atempos.append("atempo=0.5"); rem*=2.0
        atempos.append(f"atempo={rem:.4f}")
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,
                          "-vf",f"setpts={1/speed:.6f}*PTS",
                          "-af",",".join(atempos),
                          "-c:v","libx264","-preset","fast","-crf","20",
                          "-c:a","aac","-b:a","128k", str(dst)])
        if rc!=0: raise RuntimeError(f"Speed change failed: {err[-300:]}")
        new_dur=get_duration(dst)
        prog(jid,f"Done! Duration: {fmt_time(new_dur)}",100)
        done(jid,dst,{"speed":speed,"new":round(new_dur,2)})
    except Exception as e:
        fail(jid,e)

def op_extract_audio(jid, src, dst):
    try:
        prog(jid,"Extracting audio…",10)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-vn","-acodec","libmp3lame","-b:a","192k", str(dst)])
        if rc!=0: raise RuntimeError(f"Extract failed: {err[-300:]}")
        prog(jid,"Done! Audio extracted.",100)
        done(jid,dst,{})
    except Exception as e:
        fail(jid,e)

def op_mute(jid, src, dst):
    try:
        prog(jid,"Muting audio…",10)
        _, err, rc = run([_FFMPEG_EXE,"-y","-i",src,"-an",
                          "-c:v","copy", str(dst)])
        if rc!=0: raise RuntimeError(f"Mute failed: {err[-300:]}")
        prog(jid,"Done! Audio removed.",100)
        done(jid,dst,{})
    except Exception as e:
        fail(jid,e)

# ─── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))

# ─── Auth & Payment config ────────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET  = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
GOOGLE_CLIENT_ID       = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET   = os.environ.get('GOOGLE_CLIENT_SECRET', '')
APP_URL                = os.environ.get('APP_URL', 'http://localhost:5000')

# Stripe price IDs — set these after creating products in Stripe dashboard
STRIPE_PRO_PRICE_ID         = os.environ.get('STRIPE_PRO_PRICE_ID', '')
STRIPE_PRO_YEARLY_PRICE_ID  = os.environ.get('STRIPE_PRO_YEARLY_PRICE_ID', '')
STRIPE_TEAM_PRICE_ID        = os.environ.get('STRIPE_TEAM_PRICE_ID', '')
STRIPE_TEAM_YEARLY_PRICE_ID = os.environ.get('STRIPE_TEAM_YEARLY_PRICE_ID', '')

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# Plan limits
PLANS = {
    'free':  {'name':'Free',  'price':0,  'videos_per_month':3,  'max_duration':300,  'max_file_mb':100, 'watermark':True,  'team_seats':1},
    'pro':   {'name':'Pro',   'price':8,  'videos_per_month':999,'max_duration':9999, 'max_file_mb':500, 'watermark':False, 'team_seats':1},
    'team':  {'name':'Team',  'price':20, 'videos_per_month':999,'max_duration':9999, 'max_file_mb':500, 'watermark':False, 'team_seats':5},
}

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(_SCRIPT_DIR, 'snipforge.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                email       TEXT UNIQUE NOT NULL,
                name        TEXT,
                password    TEXT,
                google_id   TEXT,
                avatar      TEXT,
                plan        TEXT DEFAULT 'free',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                team_owner_id TEXT,
                videos_this_month INTEGER DEFAULT 0,
                month_reset TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS video_history (
                id           TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                filename     TEXT,
                operation    TEXT,
                orig_size_mb REAL,
                new_size_mb  REAL,
                orig_dur     REAL,
                new_dur      REAL,
                status       TEXT DEFAULT 'done',
                job_id       TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shared_links (
                id           TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                job_id       TEXT NOT NULL,
                filename     TEXT,
                title        TEXT,
                views        INTEGER DEFAULT 0,
                expires_at   TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transcriptions (
                id           TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                job_id       TEXT,
                filename     TEXT,
                text         TEXT,
                language     TEXT,
                duration     REAL,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
        ''')

init_db()

# ─── Auth helpers ─────────────────────────────────────────────────────────────
def get_current_user():
    token = session.get('auth_token')
    if not token:
        return None
    with get_db() as db:
        row = db.execute(
            'SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires_at > ?',
            (token, datetime.datetime.utcnow().isoformat())
        ).fetchone()
    return dict(row) if row else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Login required'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def create_session(user_id):
    token = str(uuid.uuid4())
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat()
    with get_db() as db:
        db.execute('INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)',
                   (token, user_id, expires))
    return token

def check_plan_limit(user):
    if TEST_MODE:
        return True, None  # bypass plan limits for automated testing
    plan = PLANS.get(user['plan'], PLANS['free'])
    now_month = datetime.datetime.utcnow().strftime('%Y-%m')
    if user.get('month_reset') != now_month:
        with get_db() as db:
            db.execute('UPDATE users SET videos_this_month=0, month_reset=? WHERE id=?',
                       (now_month, user['id']))
        user['videos_this_month'] = 0
    if user['videos_this_month'] >= plan['videos_per_month']:
        return False, f"Monthly limit reached ({plan['videos_per_month']} videos). Upgrade to Pro for unlimited!"
    return True, None

def increment_usage(user_id):
    if TEST_MODE:
        return  # don't count test runs against real usage
    with get_db() as db:
        db.execute('UPDATE users SET videos_this_month=videos_this_month+1 WHERE id=?', (user_id,))

def save_history(user_id, job_id, filename, operation, orig_dur=0, new_dur=0, orig_size_mb=0, new_size_mb=0):
    with get_db() as db:
        db.execute(
            'INSERT INTO video_history (id,user_id,job_id,filename,operation,orig_dur,new_dur,orig_size_mb,new_size_mb) VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4())[:8], user_id, job_id, filename, operation,
             round(orig_dur,2), round(new_dur,2), round(orig_size_mb,1), round(new_size_mb,1))
        )

# ─── Security config ──────────────────────────────────────────────────────────
MAX_FILE_MB       = 500                          # max upload size
MAX_FILE_BYTES    = MAX_FILE_MB * 1024 * 1024
FILE_TTL_SECONDS  = 3600                         # delete files after 1 hour
RATE_LIMIT        = 10                           # max jobs per IP per hour
TEST_MODE         = os.environ.get('SNIPFORGE_TEST_MODE','') == '1'  # bypass rate limit for testing
ALLOWED_EXTS      = {'.mp4','.mov','.avi','.webm','.mkv','.flv','.wmv','.m4v','.mp3','.wav','.aac'}
ALLOWED_MIMES     = {'video/','audio/'}

app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_BYTES

# ─── Rate limiter ─────────────────────────────────────────────────────────────
_rate_store = {}   # ip -> [timestamps]
_rate_lock  = threading.Lock()

def check_rate_limit(ip):
    if TEST_MODE:
        return True  # bypass for automated testing
    now = time.time()
    with _rate_lock:
        times = [t for t in _rate_store.get(ip,[]) if now - t < 3600]
        if len(times) >= RATE_LIMIT:
            return False
        times.append(now)
        _rate_store[ip] = times
    return True

def get_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()

# ─── File cleanup thread ──────────────────────────────────────────────────────
def _cleanup_loop():
    while True:
        time.sleep(300)  # check every 5 minutes
        now = time.time()
        for folder in [UPLOAD_DIR, OUTPUT_DIR]:
            for f in list(folder.iterdir()):
                try:
                    if now - f.stat().st_mtime > FILE_TTL_SECONDS:
                        f.unlink()
                except: pass

threading.Thread(target=_cleanup_loop, daemon=True).start()

# ─── File validation ──────────────────────────────────────────────────────────
def validate_file(f):
    if not f or not f.filename:
        return None, "No file provided"
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None, f"File type '{ext}' not allowed. Allowed: {', '.join(ALLOWED_EXTS)}"
    mime = f.content_type or ''
    if not any(mime.startswith(m) for m in ALLOWED_MIMES):
        return None, f"Invalid file type (mime: {mime})"
    # Check size by reading content length header
    content_length = request.content_length
    if content_length and content_length > MAX_FILE_BYTES:
        return None, f"File too large. Max size: {MAX_FILE_MB}MB"
    safe_name = secure_filename(f.filename)
    if not safe_name:
        safe_name = "upload" + ext
    return safe_name, None

def validate_out_ext(ext):
    allowed = {'mp4','mov','webm','mp3','wav','gif','avi'}
    return ext if ext in allowed else 'mp4'

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-XSS-Protection']       = '1; mode=block'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": f"File too large. Maximum size is {MAX_FILE_MB}MB"}), 413

@app.route("/")
def index():
    user = get_current_user()
    if not user:
        return redirect("/login")
    if 'token' not in session:
        session['token'] = str(uuid.uuid4())
    return render_template_string(HTML)

@app.route("/api/token")
def api_token():
    if 'token' not in session:
        session['token'] = str(uuid.uuid4())
    return jsonify({"token": session['token']})

@app.route("/api/test-mode-check")
def api_test_mode_check():
    return jsonify({"test_mode": TEST_MODE})

@app.route("/api/info", methods=["POST"])
def api_info():
    ip = get_ip()
    if not check_rate_limit(ip):
        return jsonify({"error":f"Rate limit exceeded. Max {RATE_LIMIT} jobs per hour."}),429
    # Require login
    user = get_current_user()
    if not user:
        return jsonify({"error":"login_required"}),401

    f = request.files.get("file")
    safe_name, err = validate_file(f)
    if err: return jsonify({"error": err}), 400

    jid = str(uuid.uuid4())[:8]
    ext = Path(safe_name).suffix.lower()
    p = UPLOAD_DIR / f"{jid}{ext}"

    # Save and verify size after save
    f.save(str(p))
    actual_size = os.path.getsize(str(p))
    if actual_size > MAX_FILE_BYTES:
        p.unlink(missing_ok=True)
        return jsonify({"error": f"File too large. Max {MAX_FILE_MB}MB"}), 400

    # Store session token for job ownership
    if 'token' not in session:
        session['token'] = str(uuid.uuid4())

    info = get_info(str(p))
    try: dur = get_duration(str(p))
    except: dur = 0
    vs  = next((s for s in info.get("streams",[]) if s.get("codec_type")=="video"),{})
    as_ = next((s for s in info.get("streams",[]) if s.get("codec_type")=="audio"),{})
    size = actual_size
    return jsonify({
        "file_id":  jid,
        "filename": safe_name,
        "duration": round(dur,2),
        "size_mb":  round(size/1e6,1),
        "width":    vs.get("width",0),
        "height":   vs.get("height",0),
        "fps":      vs.get("r_frame_rate",""),
        "vcodec":   vs.get("codec_name",""),
        "acodec":   as_.get("codec_name",""),
        "ext":      ext.lstrip(".")
    })

@app.route("/api/detect-language", methods=["POST"])
@login_required
def api_detect_language():
    """Quick language detection: extract 10s clip, send to Whisper, return language code."""
    if not OPENAI_API_KEY:
        return jsonify({"language": None, "error": "no_api_key"})
    data = request.get_json()
    file_id = (data or {}).get("file_id", "")
    if not re.match(r'^[a-f0-9]{8}$', file_id):
        return jsonify({"error": "invalid_file_id"}), 400
    src = _save_file(file_id, "")
    if not src:
        return jsonify({"error": "file_not_found"}), 404
    try:
        tmpdir = tempfile.mkdtemp()
        sample = os.path.join(tmpdir, "sample.mp3")
        # Extract first 10 seconds as low-bitrate mono MP3 for speed
        run([_FFMPEG_EXE, "-y", "-i", src, "-t", "10",
             "-vn", "-c:a", "libmp3lame", "-b:a", "32k", "-ar", "16000", "-ac", "1", sample])
        if not os.path.exists(sample):
            return jsonify({"language": None})
        import urllib.request as ureq
        with open(sample, "rb") as af:
            audio_data = af.read()
        boundary = "----LangDetect" + uuid.uuid4().hex
        nl = "\r\n"
        body = (
            f"--{boundary}{nl}"
            f'Content-Disposition: form-data; name="model"{nl}{nl}whisper-1{nl}'
            f"--{boundary}{nl}"
            f'Content-Disposition: form-data; name="response_format"{nl}{nl}verbose_json{nl}'
            f"--{boundary}{nl}"
            f'Content-Disposition: form-data; name="file"; filename="sample.mp3"{nl}'
            f"Content-Type: audio/mpeg{nl}{nl}"
        ).encode() + audio_data + f"{nl}--{boundary}--{nl}".encode()
        req = ureq.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            }
        )
        resp = json.loads(ureq.urlopen(req, timeout=30).read())
        lang = resp.get("language", "")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"language": lang})
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"language": None, "error": str(e)})

def _save_file(file_id, filename):
    # find upload by file_id prefix
    for f in UPLOAD_DIR.iterdir():
        if f.stem == file_id:
            return str(f)
    return None

@app.route("/api/process", methods=["POST"])
def api_process():
    ip = get_ip()
    if not check_rate_limit(ip):
        return jsonify({"error":f"Rate limit exceeded. Max {RATE_LIMIT} jobs per hour."}),429
    user = get_current_user()
    if not user:
        return jsonify({"error":"login_required"}),401
    # Check plan limits
    ok, msg = check_plan_limit(user)
    if not ok:
        return jsonify({"error": msg, "upgrade":True}),403
    # Increment usage
    increment_usage(user["id"])

    data = request.get_json()
    if not data: return jsonify({"error":"Invalid request"}),400
    op = data.get("op","")
    file_id = data.get("file_id","")

    # Validate file_id is safe (alphanumeric only)
    if not re.match(r'^[a-f0-9]{8}$', file_id):
        return jsonify({"error":"Invalid file ID"}),400

    src = _save_file(file_id, "")
    if not src: return jsonify({"error":"File not found or expired"}),404

    jid = new_job()
    ext = validate_out_ext(data.get("out_ext","mp4"))
    dst = OUTPUT_DIR/f"{jid}_out.{ext}"
    jobs[jid]['token']         = session.get('token','')
    jobs[jid]['orig_filename']  = data.get('filename','')
    jobs[jid]['user_id']        = user['id']
    jobs[jid]['operation']      = data.get('op','process')
    jobs[jid]['orig_size_mb']   = float(data.get('size_mb', 0))
    jobs[jid]['add_watermark']  = (PLANS.get(user['plan'], PLANS['free'])['watermark'] and
                                   data.get('op') not in ('watermark','extract_audio','convert','mute'))

    def run_op():
        if op=="shorten":
            threading.Thread(target=op_shorten, args=(jid,src,str(dst),
                data.get("threshold",-40), data.get("min_silence",300),
                data.get("pad",80), data.get("speed",1.3), data.get("do_speed",True)
            )).start()
        elif op=="trim":
            threading.Thread(target=op_trim, args=(jid,src,str(dst),
                float(data.get("start",0)), float(data.get("end",0)))).start()
        elif op=="multi_trim":
            threading.Thread(target=op_multi_trim, args=(jid,src,str(dst),
                data.get("segments",[]))).start()
        elif op=="convert":
            threading.Thread(target=op_convert, args=(jid,src,str(dst),
                data.get("fmt","mp4"))).start()
        elif op=="compress":
            threading.Thread(target=op_compress, args=(jid,src,str(dst),
                data.get("quality","medium"))).start()
        elif op=="speed":
            threading.Thread(target=op_speed, args=(jid,src,str(dst),
                float(data.get("speed",1.5)))).start()
        elif op=="extract_audio":
            threading.Thread(target=op_extract_audio, args=(jid,src,str(dst))).start()
        elif op=="mute":
            threading.Thread(target=op_mute, args=(jid,src,str(dst))).start()
        elif op=="rotate":
            threading.Thread(target=op_rotate, args=(jid,src,str(dst),
                data.get("angle","90"))).start()
        elif op=="volume":
            threading.Thread(target=op_volume, args=(jid,src,str(dst),
                float(data.get("volume",1.5)))).start()

        elif op=="crop":
            threading.Thread(target=op_crop, args=(jid,src,str(dst),
                data.get("preset","youtube"))).start()
        elif op=="watermark":
            threading.Thread(target=op_watermark, args=(jid,src,str(dst),
                data.get("text","Snipforge"))).start()
        elif op=="stabilize":
            threading.Thread(target=op_stabilize, args=(jid,src,str(dst))).start()
        elif op=="denoise":
            threading.Thread(target=op_denoise, args=(jid,src,str(dst),
                data.get("strength","medium"))).start()
        else:
            fail(jid, f"Unknown operation: {op}")

    run_op()
    return jsonify({"job_id":jid})

@app.route("/api/merge", methods=["POST"])
def api_merge():
    ip = get_ip()
    if not check_rate_limit(ip):
        return jsonify({"error":f"Rate limit exceeded."}),429
    user = get_current_user()
    if not user:
        return jsonify({"error":"login_required"}),401
    ok, msg = check_plan_limit(user)
    if not ok:
        return jsonify({"error": msg, "upgrade":True}),403
    increment_usage(user["id"])

    data = request.get_json()
    if not data: return jsonify({"error":"Invalid request"}),400
    file_ids = data.get("file_ids",[])

    # Validate all file IDs
    file_ids = [fid for fid in file_ids if re.match(r'^[a-f0-9]{8}$', str(fid))]
    srcs = [_save_file(fid,"") for fid in file_ids]
    srcs = [s for s in srcs if s]
    if len(srcs) < 2: return jsonify({"error":"Need at least 2 valid files"}),400
    if len(srcs) > 10: return jsonify({"error":"Max 10 files at once"}),400

    jid = new_job()
    dst = OUTPUT_DIR/f"{jid}_merged.mp4"
    jobs[jid]['token'] = session.get('token','')
    threading.Thread(target=op_merge,args=(jid,srcs,str(dst))).start()
    return jsonify({"job_id":jid})

@app.route("/api/status/<jid>")
def api_status(jid):
    job=jobs.get(jid)
    if not job: return jsonify({"error":"Not found"}),404
    from_idx=int(request.args.get("from",0))
    return jsonify({
        "status":job["status"],
        "progress":job["progress"],
        "log":job["log"][from_idx:],
        "stats":job.get("stats"),
        "error":job.get("error")
    })

@app.route("/api/download/<jid>")
def api_download(jid):
    if not re.match(r'^[a-f0-9]{8}$', jid):
        return jsonify({"error":"Invalid job ID"}),400
    job = jobs.get(jid)
    if not job or job["status"] != "done":
        return jsonify({"error":"Not ready or expired"}),404
    p = job["result"]
    if not p or not os.path.exists(p):
        return jsonify({"error":"File expired or not found. Files are deleted after 1 hour."}),404
    # Get original filename from job if available, else use output filename
    orig = job.get("orig_filename","")
    suffix = Path(p).suffix
    if orig:
        dl_name = Path(orig).stem + "_snipforge" + suffix
    else:
        dl_name = Path(p).name
    resp = send_file(p, as_attachment=True, download_name=dl_name, mimetype='application/octet-stream')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "GET":
        return render_template_string(AUTH_HTML, page="register",
            stripe_key=STRIPE_PUBLISHABLE_KEY, plans=PLANS)
    data = request.get_json() or request.form
    email    = (data.get("email","")).strip().lower()
    name     = (data.get("name","")).strip()
    password = data.get("password","")
    if not email or not password or not name:
        return jsonify({"error":"All fields required"}),400
    if len(password) < 8:
        return jsonify({"error":"Password must be at least 8 characters"}),400
    with get_db() as db:
        existing = db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
        if existing:
            return jsonify({"error":"Email already registered"}),400
        uid = str(uuid.uuid4())[:16]
        db.execute('INSERT INTO users (id,email,name,password) VALUES (?,?,?,?)',
                   (uid, email, name, generate_password_hash(password)))
    token = create_session(uid)
    session['auth_token'] = token
    return jsonify({"success":True, "redirect":"/"})

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "GET":
        return render_template_string(AUTH_HTML, page="login",
            stripe_key=STRIPE_PUBLISHABLE_KEY, plans=PLANS)
    data  = request.get_json() or request.form
    email = (data.get("email","")).strip().lower()
    pwd   = data.get("password","")
    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not user or not user["password"] or not check_password_hash(user["password"], pwd):
        return jsonify({"error":"Invalid email or password"}),401
    token = create_session(user["id"])
    session['auth_token'] = token
    return jsonify({"success":True, "redirect":"/"})

@app.route("/logout")
def logout():
    token = session.pop('auth_token', None)
    if token:
        with get_db() as db:
            db.execute('DELETE FROM sessions WHERE token=?',(token,))
    return redirect("/login")

@app.route("/api/me")
def api_me():
    user = get_current_user()
    if not user: return jsonify({"error":"Not logged in"}),401
    plan = PLANS.get(user['plan'], PLANS['free'])
    return jsonify({
        "id":       user["id"],
        "name":     user["name"],
        "email":    user["email"],
        "avatar":   user["avatar"] or "",
        "plan":     user["plan"],
        "plan_name":plan["name"],
        "videos_used": user["videos_this_month"] or 0,
        "videos_limit": plan["videos_per_month"],
        "watermark": plan["watermark"],
    })

# ─── Google OAuth ──────────────────────────────────────────────────────────────
@app.route("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return "Google login not configured. Set GOOGLE_CLIENT_ID env var.", 400
    state = str(uuid.uuid4())
    session['oauth_state'] = state
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  APP_URL + "/auth/google/callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "offline",
    }
    import urllib.parse
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)

@app.route("/auth/google/callback")
def google_callback():
    import urllib.parse, urllib.request as ureq
    code  = request.args.get("code")
    state = request.args.get("state")
    if state != session.get("oauth_state"):
        return "Invalid state", 400
    # Exchange code for token
    token_data = urllib.parse.urlencode({
        "code": code, "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": APP_URL + "/auth/google/callback",
        "grant_type": "authorization_code"
    }).encode()
    req = ureq.Request("https://oauth2.googleapis.com/token", data=token_data)
    try:
        resp = json.loads(ureq.urlopen(req).read())
        access_token = resp.get("access_token","")
        # Get user info
        req2 = ureq.Request("https://www.googleapis.com/oauth2/v2/userinfo",
                             headers={"Authorization": f"Bearer {access_token}"})
        guser = json.loads(ureq.urlopen(req2).read())
    except Exception as e:
        return f"Google auth failed: {e}", 400

    email     = guser.get("email","").lower()
    name      = guser.get("name","")
    google_id = guser.get("id","")
    avatar    = guser.get("picture","")

    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        if user:
            db.execute('UPDATE users SET google_id=?,avatar=?,name=? WHERE id=?',
                       (google_id, avatar, name, user["id"]))
            uid = user["id"]
        else:
            uid = str(uuid.uuid4())[:16]
            db.execute('INSERT INTO users (id,email,name,google_id,avatar) VALUES (?,?,?,?,?)',
                       (uid, email, name, google_id, avatar))
    token = create_session(uid)
    session['auth_token'] = token
    return redirect("/")

# ─── Stripe payment routes ─────────────────────────────────────────────────────
@app.route("/pricing")
def pricing():
    user = get_current_user()
    return render_template_string(AUTH_HTML, page="pricing",
        stripe_key=STRIPE_PUBLISHABLE_KEY, plans=PLANS, user=user)

@app.route("/api/create-checkout", methods=["POST"])
@login_required
def create_checkout():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error":"Stripe not configured"}),400
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        data     = request.get_json()
        plan     = data.get("plan","pro")
        billing  = data.get("billing","monthly")
        if plan == "pro":
            price_id = STRIPE_PRO_YEARLY_PRICE_ID if billing=="yearly" else STRIPE_PRO_PRICE_ID
        else:
            price_id = STRIPE_TEAM_YEARLY_PRICE_ID if billing=="yearly" else STRIPE_TEAM_PRICE_ID
        if not price_id:
            return jsonify({"error":"Stripe price ID not configured"}),400
        user = get_current_user()
        # Create or get Stripe customer
        with get_db() as db:
            u = db.execute('SELECT * FROM users WHERE id=?',(user["id"],)).fetchone()
        if u["stripe_customer_id"]:
            customer_id = u["stripe_customer_id"]
        else:
            customer = stripe.Customer.create(email=user["email"], name=user["name"])
            customer_id = customer.id
            with get_db() as db:
                db.execute('UPDATE users SET stripe_customer_id=? WHERE id=?',
                           (customer_id, user["id"]))
        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=APP_URL + "/payment/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=APP_URL + "/pricing",
        )
        return jsonify({"url": checkout.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/payment/success")
@login_required
def payment_success():
    return render_template_string(AUTH_HTML, page="success",
        stripe_key=STRIPE_PUBLISHABLE_KEY, plans=PLANS, user=get_current_user())

@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_SECRET_KEY: return "ok"
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        payload = request.get_data()
        sig     = request.headers.get("Stripe-Signature","")
        event   = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        if event["type"] == "checkout.session.completed":
            sess = event["data"]["object"]
            customer_id = sess.get("customer")
            sub_id      = sess.get("subscription")
            # Find plan from subscription
            sub = stripe.Subscription.retrieve(sub_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            plan = "pro" if price_id in (STRIPE_PRO_PRICE_ID, STRIPE_PRO_YEARLY_PRICE_ID) else "team"
            with get_db() as db:
                db.execute('UPDATE users SET plan=?,stripe_subscription_id=? WHERE stripe_customer_id=?',
                           (plan, sub_id, customer_id))
        elif event["type"] in ("customer.subscription.deleted","customer.subscription.paused"):
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            with get_db() as db:
                db.execute("UPDATE users SET plan='free',stripe_subscription_id=NULL WHERE stripe_customer_id=?",
                           (customer_id,))
        elif event["type"] == "customer.subscription.updated":
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            price_id = sub["items"]["data"][0]["price"]["id"]
            plan = "pro" if price_id in (STRIPE_PRO_PRICE_ID, STRIPE_PRO_YEARLY_PRICE_ID) else "team"
            with get_db() as db:
                db.execute('UPDATE users SET plan=? WHERE stripe_customer_id=?',(plan, customer_id))
    except Exception as e:
        return str(e), 400
    return "ok"

@app.route("/api/cancel-subscription", methods=["POST"])
@login_required
def cancel_subscription():
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        user = get_current_user()
        with get_db() as db:
            u = db.execute('SELECT * FROM users WHERE id=?',(user["id"],)).fetchone()
        if u["stripe_subscription_id"]:
            stripe.Subscription.modify(u["stripe_subscription_id"], cancel_at_period_end=True)
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"error":str(e)}),400

@app.route("/account")
@login_required
def account():
    user = get_current_user()
    return render_template_string(AUTH_HTML, page="account",
        stripe_key=STRIPE_PUBLISHABLE_KEY, plans=PLANS, user=user)

# ─── Shareable links ──────────────────────────────────────────────────────────

@app.route("/api/share", methods=["POST"])
@login_required
def api_share():
    user = get_current_user()
    data = request.get_json()
    job_id   = data.get("job_id","")
    filename = data.get("filename","video")
    title    = data.get("title", filename)
    expires_days = int(data.get("expires_days", 7))

    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error":"Job not found or not complete"}), 404

    # Check ownership
    if job.get("user_id") != user["id"]:
        return jsonify({"error":"Unauthorised"}), 403

    share_id = str(uuid.uuid4())[:12]
    expires  = (datetime.datetime.utcnow() + datetime.timedelta(days=expires_days)).isoformat()

    with get_db() as db:
        # Check if share already exists for this job
        existing = db.execute('SELECT id FROM shared_links WHERE job_id=? AND user_id=?',
                              (job_id, user["id"])).fetchone()
        if existing:
            share_id = existing["id"]
        else:
            db.execute('INSERT INTO shared_links (id,user_id,job_id,filename,title,expires_at) VALUES (?,?,?,?,?,?)',
                       (share_id, user["id"], job_id, filename, title, expires))

    share_url = f"{APP_URL}/share/{share_id}"
    return jsonify({"share_id": share_id, "url": share_url})

@app.route("/share/<share_id>")
def view_share(share_id):
    with get_db() as db:
        link = db.execute('SELECT * FROM shared_links WHERE id=?', (share_id,)).fetchone()
    if not link:
        return "Link not found or expired.", 404
    link = dict(link)
    # Check expiry
    if link.get("expires_at") and link["expires_at"] < datetime.datetime.utcnow().isoformat():
        return "This link has expired.", 410
    # Increment views
    with get_db() as db:
        db.execute('UPDATE shared_links SET views=views+1 WHERE id=?', (share_id,))
    job = jobs.get(link["job_id"])
    file_exists = job and job.get("status") == "done" and job.get("result") and os.path.exists(job["result"])
    return render_template_string(SHARE_HTML, link=link, file_exists=file_exists, share_id=share_id)

@app.route("/api/share/<share_id>/download")
def share_download(share_id):
    with get_db() as db:
        link = db.execute('SELECT * FROM shared_links WHERE id=?', (share_id,)).fetchone()
    if not link:
        return "Not found", 404
    link = dict(link)
    if link.get("expires_at") and link["expires_at"] < datetime.datetime.utcnow().isoformat():
        return "Expired", 410
    job = jobs.get(link["job_id"])
    if not job or not job.get("result") or not os.path.exists(job["result"]):
        return "File no longer available", 404
    return send_file(job["result"], as_attachment=True,
                     download_name=link["filename"] or "video.mp4",
                     mimetype="application/octet-stream")

@app.route("/api/my-shares")
@login_required
def api_my_shares():
    user = get_current_user()
    with get_db() as db:
        rows = db.execute('SELECT * FROM shared_links WHERE user_id=? ORDER BY created_at DESC',
                          (user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/share/<share_id>", methods=["DELETE"])
@login_required
def delete_share(share_id):
    user = get_current_user()
    with get_db() as db:
        db.execute('DELETE FROM shared_links WHERE id=? AND user_id=?', (share_id, user["id"]))
    return jsonify({"success": True})

# ─── Transcription ─────────────────────────────────────────────────────────────

@app.route("/api/transcribe", methods=["POST"])
@login_required
def api_transcribe():
    ip = get_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded"}), 429
    user = get_current_user()

    f = request.files.get("file")
    safe_name, err = validate_file(f)
    if err: return jsonify({"error": err}), 400

    jid = str(uuid.uuid4())[:8]
    ext = Path(safe_name).suffix.lower()
    p   = UPLOAD_DIR / f"{jid}{ext}"
    f.save(str(p))

    result_id = str(uuid.uuid4())[:8]
    jobs[result_id] = {"status":"running","progress":0,"log":[],"result":None,"error":None,
                       "orig_filename":safe_name,"user_id":user["id"],"operation":"transcribe",
                       "orig_size_mb":round(os.path.getsize(str(p))/1e6,1),
                       "tc_language": request.form.get("language","auto"),
                       "tc_translate": request.form.get("translate_to","none"),
                       "tc_burn": request.form.get("burn_captions","false").lower()=="true"}

    def do_transcribe():
        try:
            jobs[result_id]["log"].append("Extracting audio…")
            jobs[result_id]["progress"] = 10

            tmpdir = tempfile.mkdtemp()
            audio  = os.path.join(tmpdir, "audio.mp3")

            # Extract audio as MP3 for Whisper
            _, err, rc = run([
                _FFMPEG_EXE, "-y", "-i", str(p),
                "-vn", "-c:a", "libmp3lame", "-b:a", "64k",
                "-ar", "16000", "-ac", "1",
                audio
            ])
            if rc != 0:
                raise RuntimeError(f"Audio extraction failed: {err[-200:]}")

            jobs[result_id]["progress"] = 30
            jobs[result_id]["log"].append("Transcribing audio…")

            if OPENAI_API_KEY:
                tc_language = jobs[result_id].get("tc_language", "auto")
                # Use OpenAI Whisper API
                import urllib.request as ureq
                import urllib.parse
                with open(audio, "rb") as af:
                    audio_data = af.read()

                boundary = "----FormBoundary" + uuid.uuid4().hex
                nl = "\r\n"
                # Add language if specified
                lang_part = ""
                if tc_language and tc_language != "auto":
                    lang_part = (
                        f"--{boundary}{nl}"
                        f'Content-Disposition: form-data; name="language"{nl}{nl}{tc_language}{nl}'
                    )
                body = (
                    f"--{boundary}{nl}"
                    f'Content-Disposition: form-data; name="model"{nl}{nl}whisper-1{nl}'
                    + lang_part +
                    f"--{boundary}{nl}"
                    f'Content-Disposition: form-data; name="file"; filename="audio.mp3"{nl}'
                    f"Content-Type: audio/mpeg{nl}{nl}"
                ).encode() + audio_data + f"{nl}--{boundary}--{nl}".encode()

                req = ureq.Request(
                    "https://api.openai.com/v1/audio/transcriptions",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}"
                    }
                )
                resp = json.loads(ureq.urlopen(req, timeout=120).read())
                text = resp.get("text", "")
                lang = resp.get("language", "")
            else:
                # Fallback: use FFmpeg to get basic info + placeholder
                text = "[Transcription requires OpenAI API key. Set OPENAI_API_KEY environment variable.]"
                lang = "unknown"

            jobs[result_id]["progress"] = 85
            jobs[result_id]["log"].append("Saving transcription…")

            dur = get_duration(str(p))
            with get_db() as db:
                db.execute(
                    'INSERT INTO transcriptions (id,user_id,job_id,filename,text,language,duration) VALUES (?,?,?,?,?,?,?)',
                    (str(uuid.uuid4())[:8], user["id"], result_id, safe_name, text, lang, round(dur,2))
                )

            # Translate transcript if requested
            translate_to = jobs[result_id].get("tc_translate", "none")
            if translate_to and translate_to != "none" and text and OPENAI_API_KEY:
                jobs[result_id]["log"].append(f"Translating to {translate_to}...")
                jobs[result_id]["progress"] = 80
                try:
                    import json as _json
                    translate_body = _json.dumps({
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": f"You are a professional translator. Translate the following transcript to {translate_to}. Keep the same meaning and tone. Return ONLY the translated text, nothing else."},
                            {"role": "user", "content": text}
                        ],
                        "max_tokens": 4000
                    }).encode()
                    import urllib.request as _ureq
                    translate_req = _ureq.Request(
                        "https://api.openai.com/v1/chat/completions",
                        data=translate_body,
                        headers={
                            "Authorization": f"Bearer {OPENAI_API_KEY}",
                            "Content-Type": "application/json"
                        }
                    )
                    translate_resp = _json.loads(_ureq.urlopen(translate_req, timeout=60).read())
                    translated_text = translate_resp["choices"][0]["message"]["content"].strip()
                    text = translated_text
                    jobs[result_id]["log"].append(f"Translation to {translate_to} complete!")
                    jobs[result_id]["stats"] = {"text": text, "language": translate_to, "detected_language": lang, "duration": round(dur,2), "words": len(text.split()), "translated": True}
                except Exception as te:
                    jobs[result_id]["log"].append(f"Translation failed: {te}, using original transcript.")

            # Burn captions into video if requested
            burn_captions = jobs[result_id].get("tc_burn", False)
            output_file = audio  # default: return audio
            if burn_captions and ext in ('.mp4', '.mov', '.webm', '.avi'):
                jobs[result_id]["log"].append("Burning captions into video…")
                jobs[result_id]["progress"] = 90
                try:
                    words = text.split()
                    words_per_line = 8
                    cap_lines = [' '.join(words[i:i+words_per_line]) for i in range(0, len(words), words_per_line)]
                    line_dur = dur / max(len(cap_lines), 1)

                    burned_out = os.path.join(tempfile.gettempdir(), f"snip_cap_{result_id}.mp4")
                    tmp_vid_in = os.path.join(tempfile.gettempdir(), f"snip_cap_in_{result_id}.mp4")
                    if not os.path.exists(str(p)):
                        raise RuntimeError(f"Source video not found: {p}")
                    shutil.copy2(str(p), tmp_vid_in)

                    # Strategy: write each caption line to its own text file and
                    # use drawtext with textfile= — avoids ALL shell escaping issues
                    # since text never touches the filter string.
                    # Write filter_script file — no length limit, no escaping issues
                    filter_script = os.path.join(tempfile.gettempdir(), f"snip_fs_{result_id}.txt")
                    text_files = []
                    vf_parts = []
                    for i, cap_line in enumerate(cap_lines):
                        tf = os.path.join(tempfile.gettempdir(), f"snip_cap_{result_id}_{i}.txt")
                        with open(tf, 'w', encoding='utf-8') as f_:
                            f_.write(cap_line)
                        text_files.append(tf)
                        t_start = round(i * line_dur, 3)
                        t_end   = round(min((i + 1) * line_dur, dur), 3)
                        tf_fwd  = tf.replace("\\", "/")
                        vf_parts.append(
                            f"drawtext=textfile=\'{tf_fwd}\'"
                            f":fontsize=24:fontcolor=white"
                            f":x=(w-text_w)/2:y=h-th-40"
                            f":box=1:boxcolor=black@0.6:boxborderw=8"
                            f":enable=\'between(t,{t_start},{t_end})\'"
                        )

                    with open(filter_script, 'w', encoding='utf-8') as fs:
                        fs.write(",".join(vf_parts))

                    _, err, rc = run([_FFMPEG_EXE, "-y",
                        "-i", tmp_vid_in,
                        "-filter_script:v", filter_script,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                        "-c:a", "copy", "-movflags", "+faststart",
                        burned_out])

                    # Cleanup temp files
                    for f_ in [tmp_vid_in, filter_script] + text_files:
                        try: os.unlink(f_)
                        except: pass

                    if rc == 0:
                        output_file = burned_out
                        jobs[result_id]["log"].append("Captions burned into video!")
                    else:
                        jobs[result_id]["log"].append(f"Caption burning failed: {err[-300:]}")
                except Exception as ce:
                    jobs[result_id]["log"].append(f"Caption error: {ce}")

            # Save transcript as txt file for download
            txt_file = OUTPUT_DIR / f"{result_id}_transcript.txt"
            with open(str(txt_file), 'w', encoding='utf-8') as tf:
                tf.write(text)

            jobs[result_id]["status"]   = "done"
            jobs[result_id]["progress"] = 100
            jobs[result_id]["result"]   = output_file if burn_captions else str(txt_file)
            jobs[result_id]["stats"]    = {"text": text, "language": lang, "duration": round(dur,2), "words": len(text.split()),
                                           "burned": burn_captions, "detected_language": lang,
                                           "txt_result": str(txt_file),
                                           "video_result": output_file if burn_captions else None}
            jobs[result_id]["log"].append(f"Done! {len(text.split())} words transcribed.")

            shutil.rmtree(tmpdir, ignore_errors=True)
            p.unlink(missing_ok=True)
        except Exception as e:
            jobs[result_id]["status"] = "error"
            jobs[result_id]["error"]  = str(e)

    threading.Thread(target=do_transcribe, daemon=True).start()
    return jsonify({"job_id": result_id})

@app.route("/api/my-transcriptions")
@login_required
def api_my_transcriptions():
    user = get_current_user()
    with get_db() as db:
        rows = db.execute(
            'SELECT * FROM transcriptions WHERE user_id=? ORDER BY created_at DESC LIMIT 20',
            (user["id"],)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    with get_db() as db:
        history = db.execute(
            'SELECT * FROM video_history WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
            (user['id'],)
        ).fetchall()
    history = [dict(h) for h in history]
    plan = PLANS.get(user['plan'], PLANS['free'])
    return render_template_string(DASHBOARD_HTML,
        user=user, history=history, plan=plan, plans=PLANS,
        stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route("/api/history")
@login_required
def api_history():
    user = get_current_user()
    with get_db() as db:
        rows = db.execute(
            'SELECT * FROM video_history WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
            (user['id'],)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/history/<hid>", methods=["DELETE"])
@login_required
def delete_history(hid):
    user = get_current_user()
    with get_db() as db:
        db.execute('DELETE FROM video_history WHERE id=? AND user_id=?', (hid, user['id']))
    return jsonify({"success": True})

@app.route("/api/history/<hid>/rename", methods=["PATCH"])
@login_required
def rename_history(hid):
    user = get_current_user()
    data = request.get_json()
    new_name = (data.get("filename") or "").strip()
    if not new_name:
        return jsonify({"error": "Name cannot be empty"}), 400
    if len(new_name) > 200:
        return jsonify({"error": "Name too long"}), 400
    safe_name = secure_filename(new_name) or new_name
    with get_db() as db:
        db.execute('UPDATE video_history SET filename=? WHERE id=? AND user_id=?',
                   (safe_name, hid, user['id']))
    return jsonify({"success": True, "filename": safe_name})# ─── HTML/CSS/JS frontend ─────────────────────────────────────────────────────


SHARE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ link.title or link.filename or 'Shared Video' }} — Snipforge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;900&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f8f7f5;--bg2:#ffffff;--bg3:#f0eeeb;
  --border:#e5e2dc;--border2:#d4d0c8;
  --text:#1a1916;--muted:#8a8780;
  --accent:#e8420a;--accent2:#f07030;
  --green:#1a7a3c;--green-bg:#edf7f1;
  --blue:#1a5fa8;--blue-bg:#edf3fb;
  --cond:'Barlow Condensed',sans-serif;
  --body:'Barlow',sans-serif;
  --mono:'JetBrains Mono',monospace;
  --radius:10px;--radius-lg:14px;
  --shadow:0 1px 3px rgba(0,0,0,.06);
  --shadow-md:0 4px 12px rgba(0,0,0,.08);
}
html,body{background:var(--bg);color:var(--text);font-family:var(--body);height:100vh;overflow:hidden;-webkit-font-smoothing:antialiased}
.topbar{display:flex;align-items:center;padding:0 24px;height:56px;background:var(--bg2);border-bottom:1px solid var(--border);box-shadow:0 1px 3px rgba(0,0,0,.04)}
.logo{font-family:var(--cond);font-weight:900;font-size:1.5rem;letter-spacing:.06em;color:var(--accent);text-decoration:none;text-transform:uppercase}
.wrap{max-width:760px;margin:0 auto;padding:48px 24px}
.video-card{background:var(--bg2);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin-bottom:20px;box-shadow:var(--shadow-md)}
.video-player{width:100%;aspect-ratio:16/9;background:#000;display:block}
.video-info{padding:20px 24px}
.video-title{font-family:var(--cond);font-size:1.5rem;font-weight:800;margin-bottom:6px}
.video-meta{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.meta-item{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:var(--muted);display:flex;align-items:center;gap:5px}
.dl-btn{display:inline-flex;align-items:center;gap:8px;padding:11px 24px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:none;border-radius:10px;font-family:var(--cond);font-size:.95rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;text-decoration:none;cursor:pointer;transition:all .15s}
.dl-btn:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(255,77,0,.3)}
.share-bar{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;display:flex;align-items:center;gap:12px}
.share-url{flex:1;font-family:'JetBrains Mono',monospace;font-size:.75rem;color:var(--muted);background:var(--bg3);padding:8px 12px;border-radius:6px;border:1px solid var(--border);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copy-btn{padding:8px 16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:var(--cond);font-size:.82rem;font-weight:700;cursor:pointer;transition:all .15s;white-space:nowrap}
.copy-btn:hover{border-color:var(--accent);color:var(--accent)}
.expired{text-align:center;padding:80px 20px}
.expired-icon{font-size:3rem;margin-bottom:12px}
.snipforge-promo{text-align:center;padding:32px 20px;color:var(--muted);font-size:.88rem}
.snipforge-promo a{color:var(--accent);text-decoration:none;font-weight:600}
</style>
<script type="text/javascript">
window.$crisp=[];
window.CRISP_WEBSITE_ID="f33aa82a-1a91-4972-8278-7e2c714cfad6";
(function(){
  d=document;s=d.createElement("script");
  s.src="https://client.crisp.chat/l.js";
  s.async=1;
  d.getElementsByTagName("head")[0].appendChild(s);
})();
</script>
</head>
<body>
<header class="topbar">
  <a href="/" class="logo">Snipforge</a>
</header>
<div class="wrap">
  {% if file_exists %}
  <div class="video-card">
    <video class="video-player" controls autoplay muted>
      <source src="/api/share/{{ share_id }}/download" type="video/mp4">
    </video>
    <div class="video-info">
      <div class="video-title">{{ link.title or link.filename or 'Shared Video' }}</div>
      <div class="video-meta">
        <span class="meta-item">👁 {{ link.views or 0 }} views</span>
        <span class="meta-item">📅 Shared {{ link.created_at[:10] if link.created_at else '' }}</span>
        {% if link.expires_at %}
        <span class="meta-item">⏳ Expires {{ link.expires_at[:10] }}</span>
        {% endif %}
      </div>
      <a href="/api/share/{{ share_id }}/download" class="dl-btn">⬇ Download Video</a>
    </div>
  </div>
  <div class="share-bar">
    <span class="share-url" id="share-url">{{ request.url }}</span>
    <button class="copy-btn" onclick="copyUrl()">Copy Link</button>
  </div>
  {% else %}
  <div class="expired">
    <div class="expired-icon">⏰</div>
    <div style="font-family:var(--cond);font-size:1.8rem;font-weight:900;margin-bottom:8px">File No Longer Available</div>
    <div style="color:var(--muted)">This video has expired or been removed. Files are kept for 1 hour after processing.</div>
  </div>
  {% endif %}
  <div class="snipforge-promo">
    Processed with <a href="/">Snipforge</a> — Video Toolkit · Cut · Forge · Deliver
  </div>
</div>
<script>
function copyUrl(){
  navigator.clipboard.writeText(document.getElementById('share-url').textContent);
  const btn=document.querySelector('.copy-btn');
  btn.textContent='Copied!'; btn.style.color='var(--green)';
  setTimeout(()=>{btn.textContent='Copy Link';btn.style.color='';},2000);
}
</script>
</body>
</html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snipforge — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;900&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f8f7f5;--bg2:#ffffff;--bg3:#f0eeeb;
  --border:#e5e2dc;--border2:#d4d0c8;
  --text:#1a1916;--muted:#8a8780;
  --accent:#e8420a;--accent2:#f07030;
  --green:#1a7a3c;--green-bg:#edf7f1;
  --blue:#1a5fa8;--blue-bg:#edf3fb;
  --cond:'Barlow Condensed',sans-serif;
  --body:'Barlow',sans-serif;
  --mono:'JetBrains Mono',monospace;
  --radius:10px;--radius-lg:14px;
  --shadow:0 1px 3px rgba(0,0,0,.06);
  --shadow-md:0 4px 12px rgba(0,0,0,.08);
}
html,body{background:var(--bg);color:var(--text);font-family:var(--body);min-height:100vh;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:var(--bg2)}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}

/* topbar */
.topbar{display:flex;align-items:center;gap:12px;padding:0 28px;height:56px;background:var(--bg2);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
.logo{font-family:var(--cond);font-weight:900;font-size:1.5rem;letter-spacing:.06em;color:var(--accent);text-decoration:none;text-transform:uppercase}
.topbar-spacer{flex:1}
.nav-link{font-family:var(--mono);font-size:.7rem;padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--muted);border:1px solid transparent;transition:all .15s}
.nav-link:hover{color:var(--text);border-color:var(--border)}
.nav-link.active{color:var(--text);border-color:var(--border);background:var(--bg3)}
.nav-link.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.nav-link.primary:hover{background:#cc3d00}

/* layout */
.wrap{max-width:1100px;margin:0 auto;padding:32px 24px}

/* stats row */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
.stat-card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:18px 20px;box-shadow:var(--shadow)}
.stat-label{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.stat-value{font-family:var(--cond);font-size:2rem;font-weight:800;line-height:1}
.stat-value.green{color:var(--green)}.stat-value.orange{color:var(--accent)}
.stat-value.orange{color:var(--accent)}
.stat-sub{font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-top:4px}

/* plan card */
.plan-strip{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin-bottom:28px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;box-shadow:var(--shadow)}
.plan-strip-name{font-family:var(--cond);font-size:1.1rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
.plan-strip-badge{font-family:var(--mono);font-size:.65rem;padding:3px 10px;border-radius:4px;letter-spacing:.08em}
.badge-free{background:var(--bg3);color:var(--muted);border:1px solid var(--border)}
.badge-pro{background:rgba(232,66,10,.08);color:var(--accent);border:1px solid rgba(232,66,10,.2)}
.badge-team{background:#f3effe;color:#6b3fa0;border:1px solid #d4b8f5}
.usage-wrap{flex:1;min-width:160px}
.usage-label{font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-bottom:5px}
.usage-bar{height:5px;background:var(--border);border-radius:5px;overflow:hidden}
.usage-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:5px;transition:width .4s}
.strip-spacer{flex:1}
.upgrade-btn{padding:8px 18px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:none;border-radius:8px;font-family:var(--cond);font-size:.82rem;font-weight:700;letter-spacing:.06em;cursor:pointer;text-decoration:none;white-space:nowrap}

/* section header */
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.section-title{font-family:var(--cond);font-size:1.3rem;font-weight:800;letter-spacing:.04em;text-transform:uppercase}
.section-count{font-family:var(--mono);font-size:.7rem;color:var(--muted)}

/* history table */
.history-table{width:100%;border-collapse:collapse}
.history-table th{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
.history-table td{padding:12px;border-bottom:1px solid var(--border);font-size:.88rem;vertical-align:middle}
.history-table tr:last-child td{border-bottom:none}
.history-table tr:hover td{background:var(--bg3)}
.op-badge{display:inline-block;font-family:var(--mono);font-size:.6rem;padding:3px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
.op-shorten{background:rgba(232,66,10,.1);color:var(--accent)}
.op-trim{background:var(--blue-bg);color:var(--blue)}
.op-compress{background:#f3effe;color:#6b3fa0}
.op-convert{background:var(--green-bg);color:var(--green)}
.op-merge{background:rgba(255,140,0,.1);color:var(--accent2)}
.op-speed{background:rgba(255,77,0,.08);color:#ff7043}
.op-other{background:var(--bg3);color:var(--muted)}.op-merge{background:#fff8ec;color:#a05c00}.op-speed{background:#fff0ec;color:var(--accent)}
.dur-arrow{color:var(--muted);margin:0 4px;font-size:.75rem}
.saved-pct{font-family:var(--mono);font-size:.7rem;color:var(--green)}
.time-ago{font-family:var(--mono);font-size:.68rem;color:var(--muted)}
.dl-link{font-family:var(--mono);font-size:.68rem;padding:4px 10px;background:var(--green-bg);color:var(--green);border:1px solid #b8dfc8;border-radius:6px;text-decoration:none;transition:all .15s;white-space:nowrap}
.dl-link:hover{background:#daf0e6}
.del-btn{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.85rem;padding:4px 8px;border-radius:4px;transition:all .15s}
.del-btn:hover{color:#ff4444;background:rgba(255,68,68,.1)}
.empty-state{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-icon{font-size:3rem;margin-bottom:12px}
.empty-title{font-family:var(--cond);font-size:1.2rem;font-weight:700;color:var(--text);margin-bottom:6px}
.empty-sub{font-size:.88rem}
.card-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}

/* quick actions */
.quick-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:28px}
.quick-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center;text-decoration:none;color:var(--text);transition:all .15s;cursor:pointer}
.quick-card:hover{border-color:var(--accent);background:rgba(232,66,10,.03);transform:translateY(-2px);box-shadow:var(--shadow-md)}
.quick-icon{font-size:1.6rem;display:block;margin-bottom:6px}
.quick-label{font-family:var(--cond);font-size:.82rem;font-weight:700;letter-spacing:.04em}

@media(max-width:768px){
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .quick-grid{grid-template-columns:repeat(2,1fr)}
  .history-table th:nth-child(3),.history-table td:nth-child(3),
  .history-table th:nth-child(4),.history-table td:nth-child(4){display:none}
}
</style>
<script type="text/javascript">
window.$crisp=[];
window.CRISP_WEBSITE_ID="f33aa82a-1a91-4972-8278-7e2c714cfad6";
(function(){
  d=document;s=d.createElement("script");
  s.src="https://client.crisp.chat/l.js";
  s.async=1;
  d.getElementsByTagName("head")[0].appendChild(s);
})();
</script>
</head>
<body>
<header class="topbar">
  <a href="/" class="logo">Snipforge</a>
  <div class="topbar-spacer"></div>
  <a href="/"          class="nav-link">Editor</a>
  <a href="/dashboard" class="nav-link active">Dashboard</a>
  <a href="/pricing"   class="nav-link">Pricing</a>
  <a href="/account"   class="nav-link">Account</a>
  <a href="/logout"    class="nav-link">Logout</a>
</header>

<div class="wrap">

  <!-- Plan strip -->
  <div class="plan-strip">
    <div>
      <div class="plan-strip-name">{{ user.name or user.email }}</div>
      <div style="margin-top:4px">
        <span class="plan-strip-badge badge-{{ user.plan }}">{{ user.plan.upper() }}</span>
      </div>
    </div>
    <div class="usage-wrap">
      <div class="usage-label">
        Videos this month: {{ user.videos_this_month or 0 }} / {{ plan.videos_per_month if plan.videos_per_month < 999 else '∞' }}
      </div>
      {% if plan.videos_per_month < 999 %}
      <div class="usage-bar">
        <div class="usage-fill" style="width:{{ [(user.videos_this_month or 0) / plan.videos_per_month * 100, 100]|min }}%"></div>
      </div>
      {% endif %}
    </div>
    <div class="strip-spacer"></div>
    {% if user.plan == 'free' %}
    <a href="/pricing" class="upgrade-btn">Upgrade to Pro →</a>
    {% endif %}
  </div>

  <!-- Stats -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-label">Total Videos</div>
      <div class="stat-value orange">{{ history|length }}</div>
      <div class="stat-sub">all time</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Time Saved</div>
      <div class="stat-value green">
        {% set total_saved = namespace(v=0) %}
        {% for h in history %}{% set total_saved.v = total_saved.v + ((h.orig_dur or 0) - (h.new_dur or 0)) %}{% endfor %}
        {% set mins = (total_saved.v / 60)|int %}
        {% if mins > 0 %}{{ mins }}m{% else %}0m{% endif %}
      </div>
      <div class="stat-sub">across all videos</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Storage Saved</div>
      <div class="stat-value">
        {% set total_mb = namespace(v=0) %}
        {% for h in history %}{% set total_mb.v = total_mb.v + ((h.orig_size_mb or 0) - (h.new_size_mb or 0)) %}{% endfor %}
        {{ [total_mb.v, 0]|max|round(1) }}<span style="font-size:1rem;font-weight:400;color:var(--muted)">MB</span>
      </div>
      <div class="stat-sub">compressed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">This Month</div>
      <div class="stat-value">{{ user.videos_this_month or 0 }}</div>
      <div class="stat-sub">videos processed</div>
    </div>
  </div>

  <!-- Quick actions -->
  <div class="section-header"><div class="section-title">Quick Actions</div></div>
  <div class="quick-grid" style="margin-bottom:28px">
    <a href="/?tool=shorten" class="quick-card"><span class="quick-icon">✂️</span><span class="quick-label">AI Shorten</span></a>
    <a href="/?tool=trim"    class="quick-card"><span class="quick-icon">🔪</span><span class="quick-label">Trim</span></a>
    <a href="/?tool=compress" class="quick-card"><span class="quick-icon">📦</span><span class="quick-label">Compress</span></a>
    <a href="/?tool=convert" class="quick-card"><span class="quick-icon">🔄</span><span class="quick-label">Convert</span></a>
  </div>

  <!-- History -->
  <div class="section-header">
    <div class="section-title">Video History</div>
    <div class="section-count">{{ history|length }} videos</div>
  </div>

  <div class="card-wrap">
    {% if history %}
    <table class="history-table">
      <thead>
        <tr>
          <th>File</th>
          <th>Operation</th>
          <th>Duration</th>
          <th>Size</th>
          <th>Date</th>
          <th>Download</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="history-body">
        {% for h in history %}
        <tr id="row-{{ h.id }}">
          <td style="max-width:220px">
            <span class="fname-display" id="fname-{{ h.id }}" title="Click to rename" onclick="startRename('{{ h.id }}')">{{ h.filename or 'Unknown' }}</span>
            <span class="fname-edit" id="fedit-{{ h.id }}" style="display:none">
              <input class="fname-input" id="finput-{{ h.id }}" value="{{ h.filename or '' }}" onkeydown="handleRenameKey(event,'{{ h.id }}')" />
              <button class="fname-save" onclick="saveRename('{{ h.id }}')">✓</button>
              <button class="fname-cancel" onclick="cancelRename('{{ h.id }}')">✕</button>
            </span>
          </td>
          <td>
            <span class="op-badge op-{{ h.operation or 'other' }}">{{ (h.operation or 'process').replace('_',' ') }}</span>
          </td>
          <td>
            {% if h.orig_dur %}
              <span style="font-family:var(--mono);font-size:.75rem">{{ '%d:%02d'|format((h.orig_dur/60)|int, h.orig_dur%60|int) }}</span>
              {% if h.new_dur and h.new_dur != h.orig_dur %}
                <span class="dur-arrow">→</span>
                <span style="font-family:var(--mono);font-size:.75rem">{{ '%d:%02d'|format((h.new_dur/60)|int, h.new_dur%60|int) }}</span>
                {% set saved = h.orig_dur - h.new_dur %}
                {% if saved > 0 %}
                  <span class="saved-pct">(-{{ (saved/h.orig_dur*100)|round|int }}%)</span>
                {% endif %}
              {% endif %}
            {% else %}—{% endif %}
          </td>
          <td>
            {% if h.orig_size_mb %}
              <span style="font-family:var(--mono);font-size:.75rem">{{ h.orig_size_mb }}MB</span>
              {% if h.new_size_mb and h.new_size_mb != h.orig_size_mb %}
                <span class="dur-arrow">→</span>
                <span style="font-family:var(--mono);font-size:.75rem">{{ h.new_size_mb }}MB</span>
              {% endif %}
            {% else %}—{% endif %}
          </td>
          <td><span class="time-ago" data-ts="{{ h.created_at }}">{{ h.created_at[:10] if h.created_at else '—' }}</span></td>
          <td>
            {% if h.job_id %}
              <a href="/api/download/{{ h.job_id }}" class="dl-link">⬇ Download</a>
            {% else %}—{% endif %}
          </td>
          <td>
            <button class="del-btn" onclick="deleteHistory('{{ h.id }}')" title="Remove from history">✕</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">
      <div class="empty-icon">🎬</div>
      <div class="empty-title">No videos yet</div>
      <div class="empty-sub">Process your first video and it'll appear here</div>
      <a href="/" style="display:inline-block;margin-top:16px;padding:10px 24px;background:var(--accent);color:#fff;border-radius:8px;text-decoration:none;font-family:var(--cond);font-size:.9rem;font-weight:700">Go to Editor →</a>
    </div>
    {% endif %}
  </div>

</div>

<script>
// Time ago
document.querySelectorAll('.time-ago[data-ts]').forEach(el=>{
  const ts=el.dataset.ts; if(!ts) return;
  const d=new Date(ts+'Z'); const now=new Date();
  const diff=Math.floor((now-d)/1000);
  if(diff<60) el.textContent='just now';
  else if(diff<3600) el.textContent=Math.floor(diff/60)+'m ago';
  else if(diff<86400) el.textContent=Math.floor(diff/3600)+'h ago';
  else el.textContent=Math.floor(diff/86400)+'d ago';
});

function startRename(id){
  document.getElementById('fname-'+id).style.display='none';
  const edit=document.getElementById('fedit-'+id);
  edit.style.display='inline-flex';
  const inp=document.getElementById('finput-'+id);
  inp.focus(); inp.select();
}
function cancelRename(id){
  document.getElementById('fname-'+id).style.display='';
  document.getElementById('fedit-'+id).style.display='none';
}
function handleRenameKey(e,id){
  if(e.key==='Enter'){e.preventDefault();saveRename(id);}
  if(e.key==='Escape'){cancelRename(id);}
}
async function saveRename(id){
  const inp=document.getElementById('finput-'+id);
  const name=inp.value.trim();
  if(!name){inp.focus();return;}
  const r=await fetch('/api/history/'+id+'/rename',{
    method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filename:name})
  });
  const d=await r.json();
  if(d.success){
    const display=document.getElementById('fname-'+id);
    display.textContent=d.filename;
    display.title='Click to rename';
    cancelRename(id);
  } else {
    alert(d.error||'Rename failed');
  }
}
async function deleteHistory(id){
  if(!confirm('Remove from history?')) return;
  const r=await fetch('/api/history/'+id,{method:'DELETE'});
  const d=await r.json();
  if(d.success){
    const row=document.getElementById('row-'+id);
    if(row) row.style.animation='fadeOut .3s forwards';
    setTimeout(()=>{ if(row) row.remove(); },300);
  }
}
</script>
<style>
.fname-display{
  display:inline-block;max-width:190px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;cursor:pointer;border-radius:4px;padding:2px 4px;
  transition:background .15s;vertical-align:middle}
.fname-display:hover{background:var(--bg3);color:var(--accent)}
.fname-display:hover::after{content:' ✎';font-size:.7rem;opacity:.6}
.fname-edit{display:inline-flex;align-items:center;gap:4px;width:100%}
.fname-input{flex:1;min-width:0;font-size:.82rem;font-family:var(--mono);
  padding:3px 7px;border:1px solid var(--accent);border-radius:5px;
  background:var(--bg2);color:var(--text);outline:none}
.fname-save,.fname-cancel{flex-shrink:0;width:24px;height:24px;border:none;
  border-radius:4px;cursor:pointer;font-size:.8rem;line-height:1;
  display:inline-flex;align-items:center;justify-content:center}
.fname-save{background:var(--green-bg);color:var(--green)}
.fname-save:hover{background:var(--green);color:#fff}
.fname-cancel{background:var(--bg3);color:var(--muted)}
.fname-cancel:hover{background:#fee;color:#c00}
@keyframes fadeOut{to{opacity:0;transform:translateX(20px)}}
</style>
</body>
</html>"""


AUTH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snipforge — {% if page == 'login' %}Login{% elif page == 'register' %}Register{% elif page == 'pricing' %}Pricing{% elif page == 'account' %}Account{% else %}Welcome{% endif %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;900&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f8f7f5;--bg2:#ffffff;--bg3:#f0eeeb;
  --border:#e5e2dc;--border2:#d4d0c8;
  --text:#1a1916;--muted:#8a8780;
  --accent:#e8420a;--accent2:#f07030;
  --green:#1a7a3c;--green-bg:#edf7f1;
  --blue:#1a5fa8;--blue-bg:#edf3fb;
  --cond:'Barlow Condensed',sans-serif;
  --body:'Barlow',sans-serif;
  --mono:'JetBrains Mono',monospace;
  --radius:10px;--radius-lg:14px;
  --shadow:0 1px 3px rgba(0,0,0,.06);
  --shadow-md:0 4px 12px rgba(0,0,0,.08);
}
html,body{background:var(--bg);color:var(--text);font-family:var(--body);min-height:100vh;-webkit-font-smoothing:antialiased}
.topbar{display:flex;align-items:center;padding:0 24px;height:56px;background:var(--bg2);border-bottom:1px solid var(--border);box-shadow:0 1px 3px rgba(0,0,0,.04)}
.logo{font-family:var(--cond);font-weight:900;font-size:1.5rem;letter-spacing:.06em;color:var(--accent);text-decoration:none;text-transform:uppercase}
.topbar-spacer{flex:1}
.topbar-link{font-family:var(--mono);font-size:.72rem;color:var(--muted);text-decoration:none;padding:5px 12px;border:1px solid var(--border);border-radius:7px;transition:all .15s;margin-left:8px;background:var(--bg3)}
.topbar-link:hover{color:var(--text);border-color:var(--border2);background:var(--bg2)}
.topbar-link.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.topbar-link.primary:hover{background:#d03a08}

/* ── auth forms ── */
.auth-wrap{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 56px);padding:40px 20px}
.auth-card{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:40px;width:100%;max-width:440px;box-shadow:0 4px 24px rgba(0,0,0,.06)}
.auth-title{font-family:var(--cond);font-size:2rem;font-weight:900;letter-spacing:.02em;margin-bottom:6px;color:var(--text)}
.auth-sub{color:var(--muted);font-size:.88rem;margin-bottom:28px}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:16px}
.field label{font-family:var(--mono);font-size:.65rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}
.field input{background:var(--bg3);border:1.5px solid var(--border);border-radius:8px;padding:10px 14px;color:var(--text);font-family:var(--body);font-size:.95rem;outline:none;transition:border-color .15s;width:100%}
.field input:focus{border-color:var(--accent)}
.submit-btn{width:100%;padding:13px;background:var(--accent);color:#fff;border:none;border-radius:10px;font-family:var(--cond);font-size:1rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:all .15s;margin-top:8px}
.submit-btn:hover{background:#d03a08;transform:translateY(-1px);box-shadow:0 4px 14px rgba(232,66,10,.25)}
.submit-btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.divider{display:flex;align-items:center;gap:12px;margin:20px 0;color:var(--muted);font-size:.8rem}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:var(--border)}
.google-btn{width:100%;padding:11px;background:var(--bg2);color:var(--text);border:1.5px solid var(--border);border-radius:10px;font-family:var(--body);font-size:.9rem;font-weight:500;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:10px;text-decoration:none;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.google-btn:hover{border-color:var(--accent);background:rgba(232,66,10,.03)}
.auth-footer{text-align:center;margin-top:20px;font-size:.85rem;color:var(--muted)}
.auth-footer a{color:var(--accent);text-decoration:none}
.err-box{background:#fef2f0;border:1px solid #f5c5bb;border-radius:8px;padding:10px 14px;font-size:.85rem;color:#c0392b;margin-bottom:14px;display:none}
.err-box.show{display:block}
.ok-box{background:var(--green-bg);border:1px solid #b8dfc8;border-radius:8px;padding:10px 14px;font-size:.85rem;color:var(--green);margin-bottom:14px;display:none}
.ok-box.show{display:block}

/* ── pricing ── */
.pricing-wrap{max-width:960px;margin:0 auto;padding:60px 24px;text-align:center}
.pricing-title{font-family:var(--cond);font-size:3rem;font-weight:900;text-align:center;margin-bottom:8px}
.pricing-sub{text-align:center;color:var(--muted);font-size:1rem;margin-bottom:48px}
.plans-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.plan-card{background:var(--bg2);border:1.5px solid var(--border);border-radius:16px;padding:28px;position:relative;transition:all .2s;box-shadow:var(--shadow)}
.plan-card.featured{border-color:var(--accent);border-width:2px;background:rgba(232,66,10,.02)}
.plan-badge{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-family:var(--mono);font-size:.6rem;padding:3px 12px;border-radius:20px;letter-spacing:.1em;text-transform:uppercase}
.plan-name{font-family:var(--cond);font-size:1.3rem;font-weight:800;margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em}
.plan-price{font-family:var(--cond);font-size:3rem;font-weight:900;line-height:1;margin-bottom:4px}
.plan-price span{font-size:1rem;font-weight:400;color:var(--muted)}
.plan-period{font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-bottom:20px}
.plan-features{list-style:none;margin-bottom:24px;display:flex;flex-direction:column;gap:8px}
.plan-features li{font-size:.88rem;display:flex;align-items:center;gap:8px}
.plan-features li::before{content:'✓';color:var(--green);font-weight:700;font-size:.8rem;flex-shrink:0;background:var(--green-bg);width:18px;height:18px;display:flex;align-items:center;justify-content:center;border-radius:50%}
.plan-features li.no::before{content:'✗';color:var(--muted)}
.plan-features li.no{color:var(--muted)}
.plan-btn{width:100%;padding:12px;border:none;border-radius:10px;font-family:var(--cond);font-size:.95rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;transition:all .15s}
.plan-btn.free{background:var(--bg3);color:var(--text);border:1px solid var(--border)}
.plan-btn.free:hover{border-color:var(--accent)}
.plan-btn.paid{background:var(--accent);color:#fff}
.plan-btn.paid:hover{background:#d03a08;transform:translateY(-1px);box-shadow:0 4px 14px rgba(232,66,10,.2)}
.plan-btn.current{background:var(--bg3);color:var(--green);border:1px solid rgba(0,230,118,.3);cursor:default}

/* ── account ── */
.account-wrap{max-width:640px;margin:0 auto;padding:48px 24px}
.account-title{font-family:var(--cond);font-size:2rem;font-weight:900;margin-bottom:28px}
.account-card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:16px;box-shadow:var(--shadow)}
.account-card h3{font-family:var(--cond);font-size:.9rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:16px}
.account-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.account-row:last-child{border-bottom:none}
.account-label{font-size:.88rem;color:var(--muted)}
.account-value{font-family:var(--mono);font-size:.85rem;color:var(--text)}
.account-value.green{color:var(--green);font-weight:600}
.usage-bar{height:6px;background:var(--border);border-radius:6px;overflow:hidden;margin-top:8px}
.usage-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:6px;transition:width .3s}
.danger-btn{padding:9px 18px;background:#fef2f2;color:#c0392b;border:1px solid #f5c5c5;border-radius:8px;font-family:var(--mono);font-size:.72rem;cursor:pointer;transition:all .15s}
.danger-btn:hover{background:#fde8e8}
.upgrade-btn{padding:9px 18px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:none;border-radius:8px;font-family:var(--cond);font-size:.82rem;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}
.avatar{width:48px;height:48px;border-radius:50%;background:var(--bg3);border:2px solid var(--border);object-fit:cover}
.avatar-placeholder{width:48px;height:48px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-family:var(--cond);font-size:1.2rem;font-weight:700;color:#fff}

/* success page */
.success-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:calc(100vh - 56px);text-align:center;padding:40px}
.success-icon{font-size:4rem;margin-bottom:16px}
.success-title{font-family:var(--cond);font-size:2.5rem;font-weight:900;margin-bottom:8px}
.success-sub{color:var(--muted);margin-bottom:28px}

@media(max-width:640px){.plans-grid{grid-template-columns:1fr}}
</style>
<script type="text/javascript">
window.$crisp=[];
window.CRISP_WEBSITE_ID="f33aa82a-1a91-4972-8278-7e2c714cfad6";
(function(){
  d=document;s=d.createElement("script");
  s.src="https://client.crisp.chat/l.js";
  s.async=1;
  d.getElementsByTagName("head")[0].appendChild(s);
})();
</script>
</head>
<body>
<header class="topbar">
  <a href="/" class="logo">Snipforge</a>
  <div class="topbar-spacer"></div>
  {% if page in ['login','register','pricing'] %}
    {% if page == 'login' %}
      <a href="/register" class="topbar-link">Register</a>
      <a href="/pricing"  class="topbar-link primary">Pricing</a>
    {% else %}
      <a href="/login"   class="topbar-link">Login</a>
      <a href="/pricing" class="topbar-link primary">Pricing</a>
    {% endif %}
  {% else %}
    <a href="/"        class="topbar-link">App</a>
    <a href="/account" class="topbar-link">Account</a>
    <a href="/logout"  class="topbar-link">Logout</a>
  {% endif %}
</header>

{% if page == 'login' %}
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-title">Sign in</div>
    <div class="auth-sub">New here? <a href="/register" style="color:var(--accent);font-weight:600">Create a free account →</a></div>
    <div class="err-box" id="err"></div>
    <a href="/auth/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
      Continue with Google
    </a>
    <div class="divider">or</div>
    <div class="field"><label>Email</label><input type="email" id="email" placeholder="you@example.com" autocomplete="email"></div>
    <div class="field"><label>Password</label><input type="password" id="password" placeholder="••••••••" autocomplete="current-password"></div>
    <button class="submit-btn" onclick="doLogin()">Sign In</button>
    <div class="auth-footer">Don't have an account? <a href="/register">Register free</a></div>
  </div>
</div>
<script>
async function doLogin(){
  const btn=document.querySelector('.submit-btn');
  btn.disabled=true; btn.textContent='Signing in…';
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:document.getElementById('email').value,
      password:document.getElementById('password').value})});
  const d=await r.json();
  if(d.success){window.location=d.redirect||'/';}
  else{const e=document.getElementById('err');e.textContent=d.error;e.classList.add('show');btn.disabled=false;btn.textContent='Sign In';}
}
document.addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
</script>

{% elif page == 'register' %}
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-title">Create account</div>
    <div class="auth-sub">Start with 3 free videos per month</div>
    <div class="err-box" id="err"></div>
    <a href="/auth/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
      Continue with Google
    </a>
    <div class="divider">or</div>
    <div class="field"><label>Full Name</label><input type="text" id="name" placeholder="Jane Smith" autocomplete="name"></div>
    <div class="field"><label>Email</label><input type="email" id="email" placeholder="you@example.com" autocomplete="email"></div>
    <div class="field"><label>Password</label><input type="password" id="password" placeholder="Min 8 characters" autocomplete="new-password"></div>
    <button class="submit-btn" onclick="doRegister()">Create Account</button>
    <div class="auth-footer">Already have an account? <a href="/login">Sign in</a></div>
  </div>
</div>
<script>
async function doRegister(){
  const btn=document.querySelector('.submit-btn');
  btn.disabled=true; btn.textContent='Creating account…';
  const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:document.getElementById('name').value,
      email:document.getElementById('email').value,
      password:document.getElementById('password').value})});
  const d=await r.json();
  if(d.success){window.location=d.redirect||'/';}
  else{const e=document.getElementById('err');e.textContent=d.error;e.classList.add('show');btn.disabled=false;btn.textContent='Create Account';}
}
</script>

{% elif page == 'pricing' %}
<div class="pricing-wrap">
  <div class="pricing-title">Simple Pricing</div>
  <div class="pricing-sub">Start free. Upgrade when you need more.</div>
  <div style="display:flex;align-items:center;justify-content:center;gap:24px;margin-bottom:24px;flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.72rem;color:var(--muted)">
      <span style="color:var(--green);font-weight:700">✓</span> Secure checkout via Stripe
    </div>
    <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.72rem;color:var(--muted)">
      <span style="color:var(--green);font-weight:700">✓</span> Cancel anytime
    </div>
    <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.72rem;color:var(--muted)">
      <span style="color:var(--green);font-weight:700">✓</span> Works in your browser
    </div>
  </div>

  <!-- Billing toggle -->
  <div style="display:flex;align-items:center;justify-content:center;gap:12px;margin-bottom:36px">
    <span id="lbl-monthly" style="font-family:var(--mono);font-size:.8rem;color:var(--text)">Monthly</span>
    <div id="billing-toggle" onclick="toggleBilling()" style="position:relative;width:52px;height:28px;background:var(--accent);border-radius:28px;cursor:pointer;transition:background .2s">
      <div id="billing-knob" style="position:absolute;top:4px;left:4px;width:20px;height:20px;background:#fff;border-radius:50%;transition:transform .2s"></div>
    </div>
    <span id="lbl-yearly" style="font-family:var(--mono);font-size:.8rem;color:var(--muted)">Yearly <span style="background:rgba(0,230,118,.15);color:var(--green);padding:2px 8px;border-radius:4px;font-size:.65rem">SAVE 33%</span></span>
  </div>

  <div class="plans-grid">
    <div class="plan-card">
      <div class="plan-name">Free</div>
      <div class="plan-price">$0</div>
      <div class="plan-period">forever</div>
      <ul class="plan-features">
        <li>3 videos per month</li>
        <li>Max 5 min per video</li>
        <li>All edit tools</li>
        <li class="no">Watermark on output</li>
        <li class="no">Priority processing</li>
      </ul>
      <button class="plan-btn free" onclick="window.location='/register'">Get Started Free</button>
    </div>
    <div class="plan-card featured">
      <div class="plan-badge">Most Popular</div>
      <div class="plan-name">Pro</div>
      <div class="plan-price" id="pro-price">$8<span>/mo</span></div>
      <div class="plan-period" id="pro-period">billed monthly</div>
      <ul class="plan-features">
        <li>Unlimited videos</li>
        <li>Any duration</li>
        <li>All edit tools</li>
        <li>No watermark</li>
        <li>Priority processing</li>
      </ul>
      <button class="plan-btn paid" onclick="checkout('pro')">Get Pro</button>
    </div>
    <div class="plan-card">
      <div class="plan-name">Team</div>
      <div class="plan-price" id="team-price">$20<span>/mo</span></div>
      <div class="plan-period" id="team-period">up to 5 seats · billed monthly</div>
      <ul class="plan-features">
        <li>Everything in Pro</li>
        <li>5 team seats</li>
        <li>Shared workspace</li>
        <li>No watermark</li>
        <li>Priority processing</li>
      </ul>
      <button class="plan-btn paid" onclick="checkout('team')">Get Team</button>
    </div>
  </div>
</div>
<script>
let _billing='monthly';
function toggleBilling(){
  _billing=_billing==='monthly'?'yearly':'monthly';
  const yearly=_billing==='yearly';
  document.getElementById('billing-knob').style.transform=yearly?'translateX(24px)':'translateX(0)';
  document.getElementById('lbl-monthly').style.color=yearly?'var(--muted)':'var(--text)';
  document.getElementById('lbl-yearly').style.color=yearly?'var(--text)':'var(--muted)';
  document.getElementById('pro-price').innerHTML=yearly?'$64<span>/yr</span>':'$8<span>/mo</span>';
  document.getElementById('pro-period').textContent=yearly?'saves $32/year':'billed monthly';
  document.getElementById('team-price').innerHTML=yearly?'$160<span>/yr</span>':'$20<span>/mo</span>';
  document.getElementById('team-period').textContent=yearly?'saves $80/year · 5 seats':'up to 5 seats · billed monthly';
}
async function checkout(plan){
  const r=await fetch('/api/create-checkout',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({plan,billing:_billing})});
  const d=await r.json();
  if(d.url) window.location=d.url;
  else if(d.error==='login_required'||d.error==='Login required') window.location='/register';
  else alert(d.error||'Something went wrong');
}
</script>

{% elif page == 'success' %}
<div class="success-wrap">
  <div class="success-icon">🎉</div>
  <div class="success-title">You're all set!</div>
  <div class="success-sub">Your subscription is active. Enjoy unlimited Snipforge!</div>
  <a href="/" class="submit-btn" style="display:inline-block;text-decoration:none;padding:13px 32px;width:auto">Start Editing →</a>
</div>

{% elif page == 'account' %}
<div class="account-wrap">
  <div class="account-title">Account</div>
  <div class="account-card">
    <h3>Profile</h3>
    <div class="account-row">
      <span class="account-label">Avatar</span>
      {% if user and user.avatar %}
        <img src="{{ user.avatar }}" class="avatar" alt="avatar">
      {% else %}
        <div class="avatar-placeholder">{{ (user.name or 'U')[0].upper() }}</div>
      {% endif %}
    </div>
    <div class="account-row">
      <span class="account-label">Name</span>
      <span class="account-value">{{ user.name if user else '—' }}</span>
    </div>
    <div class="account-row">
      <span class="account-label">Email</span>
      <span class="account-value">{{ user.email if user else '—' }}</span>
    </div>
  </div>
  <div class="account-card">
    <h3>Plan & Usage</h3>
    <div class="account-row">
      <span class="account-label">Current Plan</span>
      <span class="account-value green">{{ user.plan.upper() if user else 'FREE' }}</span>
    </div>
    {% if user %}
    {% set plan = plans.get(user.plan, plans.free) %}
    <div class="account-row">
      <span class="account-label">Videos this month</span>
      <span class="account-value">{{ user.videos_this_month or 0 }} / {{ plan.videos_per_month if plan.videos_per_month < 999 else '∞' }}</span>
    </div>
    {% if plan.videos_per_month < 999 %}
    <div class="usage-bar">
      <div class="usage-fill" style="width:{{ [(user.videos_this_month or 0) / plan.videos_per_month * 100, 100] | min }}%"></div>
    </div>
    {% endif %}
    {% endif %}
    <div class="account-row" style="margin-top:12px;border-top:none">
      {% if user and user.plan == 'free' %}
        <span class="account-label">Upgrade for unlimited</span>
        <a href="/pricing" class="upgrade-btn">Upgrade →</a>
      {% elif user and user.plan in ['pro','team'] %}
        <span class="account-label">Cancel subscription</span>
        <button class="danger-btn" onclick="cancelSub()">Cancel Plan</button>
      {% endif %}
    </div>
  </div>
  <div class="account-card">
    <h3>Session</h3>
    <div class="account-row">
      <span class="account-label">Sign out of Snipforge</span>
      <a href="/logout" class="danger-btn" style="text-decoration:none">Logout</a>
    </div>
  </div>
</div>
<script>
async function cancelSub(){
  if(!confirm('Cancel your subscription? You keep access until end of billing period.')) return;
  const r=await fetch('/api/cancel-subscription',{method:'POST'});
  const d=await r.json();
  if(d.success) alert('Subscription cancelled. You keep access until end of billing period.');
  else alert(d.error||'Failed');
}
</script>
{% endif %}
</body>
</html>"""


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snipforge — Video Toolkit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;800;900&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f8f7f5;
  --bg2:#ffffff;
  --bg3:#f0eeeb;
  --border:#e5e2dc;
  --border2:#d4d0c8;
  --text:#1a1916;
  --muted:#8a8780;
  --accent:#e8420a;
  --accent2:#f07030;
  --green:#1a7a3c;
  --green-bg:#edf7f1;
  --blue:#1a5fa8;
  --blue-bg:#edf3fb;
  --purple:#6b3fa0;
  --cond:'Barlow Condensed',sans-serif;
  --body:'Barlow',sans-serif;
  --mono:'JetBrains Mono',monospace;
  --radius:10px;
  --radius-lg:14px;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
  --shadow-md:0 4px 12px rgba(0,0,0,.08),0 2px 4px rgba(0,0,0,.04);
}
html,body{background:var(--bg);color:var(--text);font-family:var(--body);min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
.sidebar::-webkit-scrollbar-track{background:var(--bg2)}

.app{display:grid;grid-template-columns:220px 1fr;grid-template-rows:56px 1fr;height:100vh;overflow:hidden}

/* topbar */
.topbar{
  grid-column:1/-1;
  display:flex;align-items:center;gap:8px;
  padding:0 20px;
  background:var(--bg2);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:100;
  box-shadow:var(--shadow);
}
.logo-wrap{
  display:flex;align-items:center;
  width:220px;padding:0 20px 0 16px;
  border-right:1px solid var(--border);
  height:100%;flex-shrink:0;
  gap:10px;
}
.logo{
  font-family:var(--cond);font-weight:900;font-size:1.25rem;
  letter-spacing:.04em;color:var(--accent);text-transform:uppercase;
  white-space:nowrap;
}
.logo-dot{
  width:6px;height:6px;background:var(--accent);border-radius:50%;
  display:inline-block;margin-left:3px;margin-bottom:3px;
}
.topbar-spacer{flex:1}
.topbar-badge{
  font-family:var(--mono);font-size:.58rem;padding:2px 7px;
  border-radius:4px;background:var(--bg3);
  color:var(--muted);border:1px solid var(--border);
  letter-spacing:.08em;text-transform:uppercase;
}
.topbar-nav-link{
  font-family:var(--mono);font-size:.68rem;padding:5px 12px;
  border-radius:var(--radius);text-decoration:none;
  color:var(--muted);border:1px solid transparent;
  transition:all .15s;white-space:nowrap;
}
.topbar-nav-link:hover{color:var(--text);background:var(--bg3);border-color:var(--border)}

/* sidebar */
.sidebar{
  background:var(--bg2);
  border-right:1px solid var(--border);
  padding:12px 0 80px;
  display:flex;flex-direction:column;gap:1px;
  overflow-y:auto;
  overflow-x:hidden;
  height:calc(100vh - 56px);
  position:sticky;
  top:56px;
  scrollbar-width:none;
}
.sidebar::-webkit-scrollbar{display:none}
.nav-section{
  font-family:var(--mono);font-size:.56rem;letter-spacing:.16em;
  color:var(--border2);text-transform:uppercase;
  padding:18px 20px 5px;
  background:var(--bg2);
}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:8px 14px;margin:0 6px;
  cursor:pointer;border-radius:var(--radius);
  transition:all .12s;font-size:.84rem;
  color:var(--muted);font-weight:500;
  line-height:1.3;
}
.nav-item:hover{background:var(--bg3);color:var(--text)}
.nav-item.active{
  background:rgba(232,66,10,.07);
  color:var(--accent);font-weight:600;
  box-shadow:inset 3px 0 0 var(--accent);
}
.nav-icon{width:18px;height:18px;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:var(--muted)}.nav-icon svg{width:16px;height:16px;flex-shrink:0;stroke-width:1.8}.nav-item.active .nav-icon{color:var(--accent)}.nav-item:hover .nav-icon{color:var(--text)}
.nav-badge{
  margin-left:auto;font-family:var(--mono);font-size:.55rem;
  padding:2px 6px;border-radius:4px;
}
.nav-short{display:none}
.nav-full{display:inline}
.nav-badge.new{background:var(--green-bg);color:var(--green)}
.nav-badge.hot{background:rgba(232,66,10,.08);color:var(--accent)}

/* main */
.main{background:var(--bg);padding:28px 32px 100px;overflow-y:auto;height:calc(100vh - 56px)}
@media(max-width:768px){
  .main{padding-bottom:160px !important}
  .panel{padding-bottom:20px}
}
.panel{display:none}.panel.active{display:block}

.panel-header{margin-bottom:24px}
.panel-title{
  font-family:var(--cond);font-size:1.5rem;font-weight:800;
  letter-spacing:.02em;color:var(--text);
  display:flex;align-items:center;gap:10px;
}
.panel-title-icon{font-size:1.1rem;opacity:.85}
.panel-title-icon{font-size:1.2rem}
.panel-sub{color:var(--muted);font-size:.88rem;margin-top:3px}

/* upload zone */
.upload-zone{
  border:2px dashed var(--border2);
  border-radius:var(--radius-lg);
  padding:32px 24px;text-align:center;
  cursor:pointer;position:relative;
  transition:all .18s;
  background:var(--bg2);
  margin-bottom:16px;
}
.upload-zone:hover{border-color:var(--accent);background:rgba(232,66,10,.02)}
.upload-zone.over{
  border-color:var(--accent);border-style:solid;
  background:rgba(232,66,10,.04);
  transform:scale(1.01);
  box-shadow:0 0 0 4px rgba(232,66,10,.08);
}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upload-zone-icon{font-size:2rem;margin-bottom:8px;display:block}
.upload-zone h3{font-family:var(--cond);font-size:1rem;font-weight:700;letter-spacing:.02em;margin-bottom:4px;color:var(--text)}
.upload-zone p{font-family:var(--mono);font-size:.68rem;color:var(--muted)}
.upload-zone .size-hint{font-family:var(--mono);font-size:.6rem;color:var(--border2);margin-top:6px;display:block}

/* file card */
.file-card{
  background:var(--green-bg);
  border:1px solid #b8dfc8;
  border-radius:var(--radius-lg);
  padding:14px 16px;margin-bottom:16px;display:none;
}
.file-card.show{display:block}
.file-card-top{display:flex;align-items:center;gap:12px}
.file-thumb{
  width:72px;height:44px;background:#000;
  border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-size:1.2rem;flex-shrink:0;overflow:hidden;
}
.file-thumb video{width:100%;height:100%;object-fit:cover;border-radius:8px}
.file-meta{flex:1;min-width:0}
.file-name{font-weight:600;font-size:.85rem;margin-bottom:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.file-stats{display:flex;gap:12px;flex-wrap:wrap}
.file-stat{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.file-stat span{color:var(--green);font-weight:500}
.file-change{
  font-family:var(--mono);font-size:.62rem;
  padding:5px 10px;background:var(--bg2);
  border:1px solid var(--border);border-radius:6px;
  cursor:pointer;color:var(--muted);
  transition:all .12s;flex-shrink:0;
}
.file-change:hover{color:var(--text);border-color:var(--border2)}

/* settings card */
.settings-card{
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:var(--radius-lg);
  padding:18px 20px;margin-bottom:14px;
  box-shadow:var(--shadow);
}
.settings-card h4{
  font-family:var(--mono);font-size:.62rem;font-weight:500;
  letter-spacing:.12em;text-transform:uppercase;
  margin-bottom:14px;color:var(--muted);
  padding-bottom:10px;border-bottom:1px solid var(--border);
}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.field-row.single{grid-template-columns:1fr}
.field-row.triple{grid-template-columns:1fr 1fr 1fr}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;color:var(--muted);text-transform:uppercase}
.field input[type=range]{accent-color:var(--accent);cursor:pointer;width:100%}
.field-val{font-family:var(--mono);font-size:.78rem;color:var(--accent);font-weight:500}
.field select,.field input[type=number],.field input[type=text]{
  background:var(--bg3);
  border:1px solid var(--border);
  border-radius:8px;padding:8px 11px;
  color:var(--text);font-family:var(--mono);font-size:.78rem;
  outline:none;transition:border-color .15s;
}
.field select:focus,.field input:focus{border-color:var(--accent)}

/* speed grid */
.speed-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:14px}
.speed-btn{
  background:var(--bg3);border:1.5px solid var(--border);
  border-radius:9px;padding:10px 4px;cursor:pointer;
  text-align:center;transition:all .12s;
}
.speed-btn:hover{border-color:var(--accent)}
.speed-btn.active{
  border-color:var(--accent);
  background:rgba(232,66,10,.06);
}
.speed-val{font-family:var(--cond);font-size:.9rem;font-weight:700;display:block;color:var(--text)}
.speed-desc{font-family:var(--mono);font-size:.56rem;color:var(--muted);display:block;margin-top:2px}

/* toggle */
.toggle-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:11px 0;border-top:1px solid var(--border);margin-top:2px;
}
.toggle-title{font-size:.88rem;font-weight:500;color:var(--text)}
.toggle-sub{font-family:var(--mono);font-size:.62rem;color:var(--muted);margin-top:2px}
.pill{
  position:relative;width:38px;height:20px;
  background:var(--border2);border-radius:20px;
  cursor:pointer;transition:background .2s;flex-shrink:0;
}
.pill.on{background:var(--accent)}
.pill::after{
  content:'';position:absolute;top:3px;left:3px;
  width:14px;height:14px;background:#fff;
  border-radius:50%;transition:transform .2s;
  box-shadow:0 1px 2px rgba(0,0,0,.15);
}
.pill.on::after{transform:translateX(18px)}

/* preset grid */
.preset-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.preset-grid.two{grid-template-columns:repeat(2,1fr)}
.preset-grid.four{grid-template-columns:repeat(4,1fr)}
.preset-btn{
  background:var(--bg3);border:1.5px solid var(--border);
  border-radius:9px;padding:12px 6px;cursor:pointer;
  text-align:center;transition:all .12s;
}
.preset-btn:hover{border-color:var(--accent)}
.preset-btn.active{border-color:var(--accent);background:rgba(232,66,10,.06);color:var(--accent)}
.preset-btn.blue:hover,.preset-btn.blue.active{border-color:var(--blue);background:var(--blue-bg);color:var(--blue)}
.preset-btn.green:hover,.preset-btn.green.active{border-color:var(--green);background:var(--green-bg);color:var(--green)}
.preset-val{font-family:var(--cond);font-size:.9rem;font-weight:800;display:block;text-transform:uppercase}
.preset-desc{font-family:var(--mono);font-size:.56rem;color:var(--muted);display:block;margin-top:3px}

/* segments */
.segments-list{display:flex;flex-direction:column;gap:7px;margin-bottom:10px}
.segment-row{
  display:flex;align-items:center;gap:8px;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:8px 12px;
}
.seg-num{font-family:var(--mono);font-size:.65rem;color:var(--muted);width:18px}
.seg-inputs{display:flex;gap:8px;flex:1;align-items:center}
.seg-inputs input{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:5px;padding:4px 8px;color:var(--text);
  font-family:var(--mono);font-size:.72rem;width:75px;outline:none;
}
.seg-inputs input:focus{border-color:var(--accent)}
.seg-label{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.seg-del{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.85rem;padding:2px 5px;border-radius:4px;transition:all .12s}
.seg-del:hover{color:#c0392b;background:rgba(192,57,43,.08)}
.add-seg-btn{
  background:none;border:1.5px dashed var(--border2);
  border-radius:8px;padding:7px;color:var(--muted);
  cursor:pointer;font-family:var(--mono);font-size:.7rem;
  width:100%;transition:all .12s;
}
.add-seg-btn:hover{border-color:var(--accent);color:var(--accent)}

/* merge list */
.merge-list{display:flex;flex-direction:column;gap:7px;margin-bottom:10px}
.merge-item{
  display:flex;align-items:center;gap:10px;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:8px 12px;
}
.merge-item-name{font-size:.85rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.merge-item-size{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.merge-remove{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.85rem;transition:color .12s}
.merge-remove:hover{color:#c0392b}
.add-merge-btn{
  background:none;border:2px dashed var(--border2);
  border-radius:8px;padding:12px;color:var(--muted);
  cursor:pointer;font-family:var(--mono);font-size:.72rem;
  width:100%;text-align:center;transition:all .12s;position:relative;
}
.add-merge-btn input{position:absolute;inset:0;opacity:0;cursor:pointer}
.add-merge-btn:hover{border-color:var(--accent);color:var(--accent)}

/* run button */
.run-btn{
  width:100%;padding:14px;
  background:var(--accent);color:#fff;border:none;
  border-radius:var(--radius-lg);
  font-family:var(--cond);font-size:1rem;font-weight:800;
  letter-spacing:.1em;text-transform:uppercase;
  cursor:pointer;transition:all .15s;
  box-shadow:0 2px 10px rgba(232,66,10,.2);
  position:relative;overflow:hidden;
}
.run-btn::after{content:'↵ Enter';position:absolute;right:14px;top:50%;transform:translateY(-50%);font-size:.6rem;opacity:.5;font-family:var(--mono);font-weight:400;letter-spacing:.05em}
.run-btn:hover:not(:disabled){
  background:#d03a08;
  transform:translateY(-1px);
  box-shadow:0 5px 18px rgba(232,66,10,.3);
}
.run-btn:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}
.run-btn:disabled::after{display:none}
.run-btn.working{background:var(--bg3);color:var(--muted);box-shadow:none}
.run-btn.working::after{display:none}

/* progress */
.progress-box{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius-lg);padding:16px;
  margin-top:14px;display:none;box-shadow:var(--shadow);
}
.progress-box.show{display:block}
.progress-track{height:4px;background:var(--border);border-radius:4px;overflow:hidden;margin-bottom:10px}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .4s ease;border-radius:4px}
.log{
  font-family:var(--mono);font-size:.68rem;color:var(--muted);
  height:90px;overflow-y:auto;line-height:1.9;
}
.log .ok{color:var(--green);display:block}
.log .err{color:#c0392b;display:block}
.log .info{display:block}

/* result */
.result-box{
  background:var(--bg2);border:1px solid #b8dfc8;
  border-radius:var(--radius-lg);padding:20px;
  margin-top:14px;display:none;box-shadow:var(--shadow);
  animation:slideIn .2s ease;
}
.result-box.show{display:block}
@keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.result-video{
  width:100%;border-radius:10px;background:#000;
  border:1px solid var(--border);display:block;margin-bottom:12px;
}
.result-stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.rstat{
  background:var(--green-bg);border:1px solid #c8e8d4;
  border-radius:9px;padding:10px 14px;text-align:center;flex:1;min-width:80px;
}
.rstat-val{
  font-family:var(--cond);font-size:1.1rem;font-weight:800;
  color:var(--green);
}
.rstat-lbl{
  font-family:var(--mono);font-size:.55rem;color:var(--green);opacity:.7;
  text-transform:uppercase;letter-spacing:.1em;margin-top:3px;
}
/* Recent files */
.recent-files-list{margin-top:8px;display:none}
.recent-label{font-family:var(--mono);font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.recent-file-item{display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:7px;cursor:pointer;transition:all .12s;margin-bottom:4px}
.recent-file-item:hover{border-color:var(--accent);background:rgba(232,66,10,.03)}
.recent-name{font-size:.8rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.recent-meta{font-family:var(--mono);font-size:.62rem;color:var(--muted)}

.dl-btn{
  display:block;width:100%;padding:11px;
  background:var(--green-bg);color:var(--green);
  border:1px solid #b8dfc8;border-radius:9px;
  font-family:var(--cond);font-size:.88rem;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;
  text-decoration:none;text-align:center;transition:all .15s;
}
.dl-btn:hover{background:#daf0e6}

/* screen recorder */
.rec-area{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--radius-lg);overflow:hidden;
  margin-bottom:16px;box-shadow:var(--shadow);
}
.rec-preview{width:100%;aspect-ratio:16/9;background:#111;display:block;object-fit:contain}
.rec-controls{
  display:flex;align-items:center;gap:10px;
  padding:12px 16px;border-top:1px solid var(--border);flex-wrap:wrap;
}
.rec-btn{
  display:flex;align-items:center;gap:6px;
  padding:8px 16px;border:none;border-radius:8px;
  font-family:var(--cond);font-size:.82rem;font-weight:700;
  letter-spacing:.04em;text-transform:uppercase;cursor:pointer;transition:all .12s;
}
.rec-btn.start{background:var(--accent);color:#fff}
.rec-btn.start:hover{background:#d03a08}
.rec-btn.stop{background:#c0392b;color:#fff}
.rec-btn.pause{background:var(--bg3);color:var(--text);border:1px solid var(--border)}
.rec-btn:disabled{opacity:.4;cursor:not-allowed}
.rec-timer{font-family:var(--mono);font-size:.95rem;font-weight:500;color:var(--text);min-width:56px}
.rec-dot{width:7px;height:7px;border-radius:50%;background:#c0392b;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.rec-type-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.rec-type-btn{
  background:var(--bg3);border:1.5px solid var(--border);
  border-radius:var(--radius-lg);padding:16px 8px;
  cursor:pointer;text-align:center;transition:all .12s;
}
.rec-type-btn:hover{border-color:var(--accent)}
.rec-type-btn.active{border-color:var(--accent);background:rgba(232,66,10,.05)}
.rec-type-icon{font-size:1.5rem;display:block;margin-bottom:6px}
.rec-type-label{font-family:var(--cond);font-size:.82rem;font-weight:700;display:block;color:var(--text)}
.rec-type-sub{font-family:var(--mono);font-size:.6rem;color:var(--muted);display:block;margin-top:3px}
.rec-saved{
  background:var(--green-bg);border:1px solid #b8dfc8;
  border-radius:var(--radius-lg);padding:16px;margin-top:14px;display:none;
}
.rec-saved.show{display:block}

@media(max-width:768px){
  /* Layout */
  .app{grid-template-columns:1fr;grid-template-rows:56px 1fr;max-height:none;overflow:visible}
  .main{padding:16px 14px 120px;height:auto;overflow-y:visible}

  /* Topbar */
  .logo-wrap{width:auto;border-right:none;padding:0 12px}
  .logo{font-size:1.1rem}
  .topbar{padding:0 10px 0 0}

  /* Sidebar becomes bottom tab bar */
  .sidebar{
    display:flex;flex-direction:row;
    position:fixed;bottom:0;left:0;right:0;
    height:56px;width:100%;
    border-right:none;border-top:1px solid var(--border);
    overflow-x:auto;overflow-y:hidden;
    padding:0;gap:0;
    z-index:200;
    scrollbar-width:none;
    background:var(--bg2);
    box-shadow:0 -2px 8px rgba(0,0,0,.06);
  }
  .sidebar::-webkit-scrollbar{display:none}
  .nav-section{display:none}
  .nav-item{
    flex-direction:column;align-items:center;justify-content:center;
    gap:2px;padding:5px 10px;margin:0;
    border-radius:0;border-left:none;
    min-width:58px;height:56px;
    font-size:.56rem;letter-spacing:.01em;
    border-top:2px solid transparent;
    white-space:nowrap;font-weight:500;
  }
  .nav-item.active{
    border-top-color:var(--accent);
    background:rgba(232,66,10,.05);
  }
  .nav-icon{width:22px;height:22px;margin-bottom:2px}
  .nav-icon svg{width:16px;height:16px}
  .nav-badge{display:none}

  /* Main content */
  .panel-title{font-size:1.3rem}
  .field-row,.field-row.triple{grid-template-columns:1fr}
  .preset-grid.four{grid-template-columns:repeat(2,1fr)}
  .speed-grid{grid-template-columns:repeat(3,1fr) !important}
  .settings-card{padding:12px}
  .settings-card h4{margin-bottom:10px}

  /* Upload zone */
  .upload-zone{padding:24px 16px}
  .upload-zone-icon{font-size:1.6rem}

  /* Result */
  .result-stats{grid-template-columns:repeat(2,1fr)}

  /* Hide tagline */
  .logo-sub{display:none}

  /* Topbar compress */
  .topbar-nav-link{display:none}
  #user-badge a[href="/account"]{display:none}
  #mob-menu-btn{display:flex !important}

  /* Nav labels */
  .nav-short{display:inline}
  .nav-full{display:none}
  .nav-badge{display:none}

  /* Upgrade banner */
  .upgrade-bar-text{font-size:.72rem}
  .upgrade-bar-text strong{display:inline}
}

@media(max-width:480px){
  .speed-grid{grid-template-columns:repeat(2,1fr)}
  .preset-grid{grid-template-columns:repeat(2,1fr)}
  .nav-item{min-width:54px;padding:6px 8px;font-size:.5rem}
}
</style>
<script type="text/javascript">
window.$crisp=[];
window.CRISP_WEBSITE_ID="f33aa82a-1a91-4972-8278-7e2c714cfad6";
(function(){
  d=document;s=d.createElement("script");
  s.src="https://client.crisp.chat/l.js";
  s.async=1;
  d.getElementsByTagName("head")[0].appendChild(s);
})();
</script>
</head>
<body>
<div class="app">

<header class="topbar">
  <div class="logo">Snipforge</div>
  <span class="logo-sub">CUT · FORGE · DELIVER</span>
  <div class="topbar-spacer"></div>
  <a href="/dashboard" style="font-family:var(--mono);font-size:.7rem;padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--muted);border:1px solid var(--border);transition:all .15s" onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Dashboard</a>
  <div id="user-badge" style="display:flex;align-items:center;gap:10px"></div>
  <div style="font-family:var(--mono);font-size:.55rem;padding:2px 6px;border-radius:3px;background:var(--bg3);color:var(--muted);border:1px solid var(--border);letter-spacing:.06em">BETA</div>
</header>

<nav class="sidebar">
  <div class="nav-section">Record</div>
  <div class="nav-item" data-panel="record" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2.5" fill="currentColor" stroke="none"/></svg></span>
    <span class="nav-full">Screen Record</span><span class="nav-short">Record</span>
    <span class="nav-badge new">NEW</span>
  </div>

  <div class="nav-section">Edit</div>
  <div class="nav-item active" data-panel="shorten" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><circle cx="4" cy="12" r="2"/><circle cx="4" cy="4" r="2"/><path d="M4 10V6M4 6L12 2M4 6L12 10"/></svg></span>
    <span class="nav-full">AI Shorten</span><span class="nav-short">Shorten</span>
    <span class="nav-badge new">AI</span>
  </div>
  <div class="nav-item" data-panel="trim" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><rect x="1" y="6" width="6" height="4" rx="1"/><path d="M7 8h8M12 5l3 3-3 3"/></svg></span>
    <span class="nav-full">Trim</span><span class="nav-short">Trim</span>
  </div>
  <div class="nav-item" data-panel="multitrim" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M1 5h14M1 11h14M6 2v12M10 2v12"/></svg></span>
    <span class="nav-full">Multi-Trim</span><span class="nav-short">Multi</span>
  </div>
  <div class="nav-item" data-panel="speed" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M9 1.5L4 8.5h4l-1 6 5-7.5H8l1-5.5z"/></svg></span>
    <span class="nav-full">Speed</span><span class="nav-short">Speed</span>
  </div>

  <div class="nav-section">Transform</div>
  <div class="nav-item" data-panel="rotate" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M13.5 6.5A5.5 5.5 0 103 8M3 4v4h4"/></svg></span>
    <span class="nav-full">Rotate / Flip</span><span class="nav-short">Rotate</span>
  </div>
  <div class="nav-item" data-panel="crop" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><rect x="1" y="4" width="9" height="7" rx="1"/><rect x="6" y="2" width="9" height="7" rx="1" opacity=".5"/></svg></span>
    <span class="nav-full">Resize for Social</span><span class="nav-short">Resize</span>
  </div>
  <div class="nav-item" data-panel="watermark" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><rect x="1.5" y="4" width="13" height="8" rx="1.5"/><path d="M4.5 9h4M4.5 11h2.5"/></svg></span>
    <span class="nav-full">Watermark</span><span class="nav-short">Wmark</span>
  </div>

  <div class="nav-section">Files</div>
  <div class="nav-item" data-panel="merge" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><rect x="1" y="3" width="5" height="10" rx="1"/><rect x="10" y="3" width="5" height="10" rx="1"/><path d="M6 8h4"/></svg></span>
    <span class="nav-full">Merge</span><span class="nav-short">Merge</span>
  </div>
  <div class="nav-item" data-panel="convert" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M3 5l3-3 3 3M6 2v9"/><path d="M13 11l-3 3-3-3M10 14V5"/></svg></span>
    <span class="nav-full">Convert</span><span class="nav-short">Convert</span>
  </div>
  <div class="nav-item" data-panel="compress" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M8 2v9M5 8l3 3 3-3"/><path d="M2 13h12"/></svg></span>
    <span class="nav-full">Compress</span><span class="nav-short">Compress</span>
  </div>

  <div class="nav-section">Audio</div>
  <div class="nav-item" data-panel="volume" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M3 6H1v4h2l4 3V3L3 6z"/><path d="M11 5a3 3 0 010 6"/></svg></span>
    <span class="nav-full">Volume</span><span class="nav-short">Volume</span>
  </div>
  <div class="nav-item" data-panel="denoise" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M3 6H1v4h2l4 3V3L3 6z"/><path d="M11 5l4 6M15 5l-4 6"/></svg></span>
    <span class="nav-full">Noise Removal</span><span class="nav-short">Denoise</span>
    <span class="nav-badge new">NEW</span>
  </div>
  <div class="nav-item" data-panel="audio" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="10" r="2"/><path d="M7 12V4l7-2v8"/></svg></span>
    <span class="nav-full">Extract Audio</span><span class="nav-short">Audio</span>
  </div>
  <div class="nav-item" data-panel="mute" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M3 6H1v4h2l4 3V3L3 6z"/><path d="M12 6l3 3M15 6l-3 3"/></svg></span>
    <span class="nav-full">Mute</span><span class="nav-short">Mute</span>
  </div>

  <div class="nav-section">AI</div>
  <div class="nav-item" data-panel="transcribe" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M5 6h6M5 9h4M5 12h2"/></svg></span>
    <span class="nav-full">Transcribe</span><span class="nav-short">Transcribe</span>
    <span class="nav-badge new">AI</span>
  </div>

  <div class="nav-section">Share</div>
  <div class="nav-item" data-panel="shares" onclick="switchPanel(this)">
    <span class="nav-icon"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><circle cx="13" cy="3.5" r="2"/><circle cx="13" cy="12.5" r="2"/><circle cx="3" cy="8" r="2"/><path d="M5 7l6-2.5M5 9l6 2.5"/></svg></span>
    <span class="nav-full">Shared Links</span><span class="nav-short">Shares</span>
  </div>
</nav>
<main class="main">

<!-- ── SCREEN RECORDER ── -->
<div class="panel" id="panel-record">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">⏺️</span>Screen Recorder</div><div class="panel-sub">Record your screen, webcam, or both — right in the browser</div></div>

  <div class="settings-card">
    <h4>Recording Mode</h4>
    <div class="rec-type-grid">
      <div class="rec-type-btn active" id="rtype-screen" onclick="selectRecType('screen')">
        <span class="rec-type-icon">🖥️</span>
        <span class="rec-type-label">Screen</span>
        <span class="rec-type-sub">Full screen or window</span>
      </div>
      <div class="rec-type-btn" id="rtype-webcam" onclick="selectRecType('webcam')">
        <span class="rec-type-icon">📷</span>
        <span class="rec-type-label">Webcam</span>
        <span class="rec-type-sub">Camera only</span>
      </div>
      <div class="rec-type-btn" id="rtype-both" onclick="selectRecType('both')">
        <span class="rec-type-icon">🎬</span>
        <span class="rec-type-label">Both</span>
        <span class="rec-type-sub">Screen + webcam PiP</span>
      </div>
    </div>
    <div class="field-row">
      <div class="field">
        <label>Audio Source</label>
        <select id="rec-audio-src">
          <option value="mic">Microphone</option>
          <option value="system">System Audio</option>
          <option value="both">Mic + System</option>
          <option value="none">No Audio</option>
        </select>
      </div>
      <div class="field">
        <label>Quality</label>
        <select id="rec-quality">
          <option value="high">High (1080p)</option>
          <option value="medium" selected>Medium (720p)</option>
          <option value="low">Low (480p)</option>
        </select>
      </div>
    </div>
  </div>

  <div class="rec-area">
    <video id="rec-preview" class="rec-preview" autoplay muted playsinline></video>
    <div class="rec-controls">
      <button class="rec-btn start" id="rec-start-btn" onclick="startRecording()">⏺ Start Recording</button>
      <button class="rec-btn stop" id="rec-stop-btn" onclick="stopRecording()" disabled>⏹ Stop</button>
      <button class="rec-btn pause" id="rec-pause-btn" onclick="pauseRecording()" disabled>⏸ Pause</button>
      <span class="rec-timer" id="rec-timer">00:00</span>
      <span class="rec-dot" id="rec-dot" style="display:none"></span>
    </div>
  </div>

  <div class="rec-saved" id="rec-saved">
    <div style="font-family:var(--cond);font-size:1rem;font-weight:700;margin-bottom:10px;color:var(--green)">✅ Recording saved!</div>
    <video id="rec-result-video" controls style="width:100%;border-radius:8px;background:#000;margin-bottom:10px"></video>
    <a id="rec-dl-btn" class="dl-btn" href="#" download="snipforge-recording.webm">⬇ Download Recording</a>
    <div style="margin-top:10px;font-family:var(--mono);font-size:.68rem;color:var(--muted)">
      💡 Tip: Use AI Shorten to remove silences from your recording
    </div>
  </div>
</div>

<!-- ── AI SHORTEN ── -->
<div class="panel active" id="panel-shorten">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">✂️</span>AI Shorten</div><div class="panel-sub">Remove silences, filler words and speed up your video automatically</div></div>
  <div class="upload-zone" id="sz-dropzone"><input type="file" id="sz-file" accept="video/*">
    <div class="upload-zone-icon">🎬</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p><span class="size-hint">Max 500MB · Drag & drop or click</span>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="sz-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="sz-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="sz-fname">—</div>
        <div class="file-stats">
          <div class="file-stat">Duration <span id="sz-dur">—</span></div>
          <div class="file-stat">Size <span id="sz-size">—</span></div>
          <div class="file-stat">Resolution <span id="sz-res">—</span></div>
        </div>
      </div>
      <button class="file-change" onclick="resetUpload('sz')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Speed</h4>
    <div class="speed-grid" id="sz-speed-grid">
      <div class="speed-btn" data-v="1.0" onclick="selectSpeed('sz',this)"><span class="speed-val">1×</span><span class="speed-desc">Normal</span></div>
      <div class="speed-btn active" data-v="1.25" onclick="selectSpeed('sz',this)"><span class="speed-val">1.25×</span><span class="speed-desc">Slight</span></div>
      <div class="speed-btn" data-v="1.5" onclick="selectSpeed('sz',this)"><span class="speed-val">1.5×</span><span class="speed-desc">Brisk</span></div>
      <div class="speed-btn" data-v="1.75" onclick="selectSpeed('sz',this)"><span class="speed-val">1.75×</span><span class="speed-desc">Fast</span></div>
      <div class="speed-btn" data-v="2.0" onclick="selectSpeed('sz',this)"><span class="speed-val">2×</span><span class="speed-desc">Double</span></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Custom Speed</label>
        <input type="range" id="sz-speed" min="0.5" max="3.0" step="0.05" value="1.25">
        <div class="field-val" id="sz-speed-val">1.25×</div>
      </div>
      <div class="field"><label>Silence Threshold (dB)</label>
        <input type="range" id="sz-thresh" min="-60" max="-20" step="1" value="-40">
        <div class="field-val" id="sz-thresh-val">-40 dB</div>
      </div>
    </div>
    <div class="field-row">
      <div class="field"><label>Min Silence (ms)</label>
        <input type="range" id="sz-minsilence" min="100" max="1500" step="50" value="300">
        <div class="field-val" id="sz-minsilence-val">300 ms</div>
      </div>
      <div class="field"><label>Padding (ms)</label>
        <input type="range" id="sz-pad" min="0" max="300" step="10" value="80">
        <div class="field-val" id="sz-pad-val">80 ms</div>
      </div>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-title">Remove silences & filler sounds</div><div class="toggle-sub">Cuts uh / um / ah and dead air</div></div>
      <div class="pill on" id="sz-silence-pill" onclick="togglePill(this)"></div>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-title">Speed up video</div><div class="toggle-sub">Apply speed multiplier above</div></div>
      <div class="pill on" id="sz-speed-pill" onclick="togglePill(this)"></div>
    </div>
  </div>
  <button class="run-btn" id="sz-run" disabled onclick="runShorten()">Upload a video first</button>
  <div class="progress-box" id="sz-progress"><div class="progress-track"><div class="progress-fill" id="sz-pfill"></div></div><div class="log" id="sz-log"></div></div>
  <div class="result-box" id="sz-result"><video class="result-video" id="sz-rvideo" controls></video><div class="result-stats" id="sz-rstats"></div><a class="dl-btn" id="sz-dl" href="#">⬇ Download Shortened Video</a></div>
</div>

<!-- ── TRIM ── -->
<div class="panel" id="panel-trim">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔪</span>Trim</div><div class="panel-sub">Cut start and end of your video</div></div>
  <div class="upload-zone" id="tr-dropzone"><input type="file" id="tr-file" accept="video/*">
    <div class="upload-zone-icon">🔪</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="tr-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="tr-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="tr-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="tr-dur">—</span></div><div class="file-stat">Size <span id="tr-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('tr')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Trim Points</h4>
    <div class="field-row">
      <div class="field"><label>Start time (seconds)</label><input type="number" id="tr-start" value="0" min="0" step="0.1"></div>
      <div class="field"><label>End time (seconds)</label><input type="number" id="tr-end" value="0" min="0" step="0.1"></div>
    </div>
  </div>
  <button class="run-btn" id="tr-run" disabled onclick="runTrim()">Upload a video first</button>
  <div class="progress-box" id="tr-progress"><div class="progress-track"><div class="progress-fill" id="tr-pfill"></div></div><div class="log" id="tr-log"></div></div>
  <div class="result-box" id="tr-result"><video class="result-video" id="tr-rvideo" controls></video><div class="result-stats" id="tr-rstats"></div><a class="dl-btn" id="tr-dl" href="#">⬇ Download Trimmed Video</a></div>
</div>

<!-- ── MULTI-TRIM ── -->
<div class="panel" id="panel-multitrim">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🎯</span>Multi-Trim</div><div class="panel-sub">Keep multiple sections and stitch them together</div></div>
  <div class="upload-zone" id="mt-dropzone"><input type="file" id="mt-file" accept="video/*">
    <div class="upload-zone-icon">🎯</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="mt-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="mt-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="mt-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="mt-dur">—</span></div><div class="file-stat">Size <span id="mt-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('mt')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Segments to Keep</h4>
    <div class="segments-list" id="mt-segments"></div>
    <button class="add-seg-btn" onclick="addSegment()">+ Add Segment</button>
  </div>
  <button class="run-btn" id="mt-run" disabled onclick="runMultiTrim()">Upload a video first</button>
  <div class="progress-box" id="mt-progress"><div class="progress-track"><div class="progress-fill" id="mt-pfill"></div></div><div class="log" id="mt-log"></div></div>
  <div class="result-box" id="mt-result"><video class="result-video" id="mt-rvideo" controls></video><div class="result-stats" id="mt-rstats"></div><a class="dl-btn" id="mt-dl" href="#">⬇ Download Stitched Video</a></div>
</div>

<!-- ── SPEED ── -->
<div class="panel" id="panel-speed">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">⚡</span>Speed Control</div><div class="panel-sub">Speed up or slow down your video</div></div>
  <div class="upload-zone" id="sp-dropzone"><input type="file" id="sp-file" accept="video/*">
    <div class="upload-zone-icon">⚡</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="sp-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="sp-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="sp-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="sp-dur">—</span></div><div class="file-stat">Size <span id="sp-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('sp')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Speed</h4>
    <div class="speed-grid" id="sp-speed-grid">
      <div class="speed-btn" data-v="0.5" onclick="selectSpeed('sp',this)"><span class="speed-val">0.5×</span><span class="speed-desc">Slow Mo</span></div>
      <div class="speed-btn" data-v="0.75" onclick="selectSpeed('sp',this)"><span class="speed-val">0.75×</span><span class="speed-desc">Slower</span></div>
      <div class="speed-btn active" data-v="1.5" onclick="selectSpeed('sp',this)"><span class="speed-val">1.5×</span><span class="speed-desc">Fast</span></div>
      <div class="speed-btn" data-v="2.0" onclick="selectSpeed('sp',this)"><span class="speed-val">2×</span><span class="speed-desc">Double</span></div>
      <div class="speed-btn" data-v="3.0" onclick="selectSpeed('sp',this)"><span class="speed-val">3×</span><span class="speed-desc">Triple</span></div>
    </div>
    <div class="field field-row single"><label>Custom Speed</label>
      <input type="range" id="sp-speed" min="0.25" max="4.0" step="0.05" value="1.5">
      <div class="field-val" id="sp-speed-val">1.50×</div>
    </div>
  </div>
  <div id="sp-estimate" style="display:none;font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-bottom:8px;padding:8px 12px;background:var(--bg3);border-radius:8px;text-align:center"></div>
  <button class="run-btn" id="sp-run" disabled onclick="runSpeed()">Upload a video first</button>
  <div class="progress-box" id="sp-progress"><div class="progress-track"><div class="progress-fill" id="sp-pfill"></div></div><div class="log" id="sp-log"></div></div>
  <div class="result-box" id="sp-result"><video class="result-video" id="sp-rvideo" controls></video><div class="result-stats" id="sp-rstats"></div><a class="dl-btn" id="sp-dl" href="#">⬇ Download Video</a></div>
</div>

<!-- ── ROTATE ── -->
<div class="panel" id="panel-rotate">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔄</span>Rotate / Flip</div><div class="panel-sub">Fix orientation or mirror your video</div></div>
  <div class="upload-zone" id="ro-dropzone"><input type="file" id="ro-file" accept="video/*">
    <div class="upload-zone-icon">🔄</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="ro-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="ro-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="ro-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="ro-dur">—</span></div><div class="file-stat">Size <span id="ro-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('ro')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Transform</h4>
    <div class="preset-grid">
      <div class="preset-btn active" data-v="90" onclick="selectPreset('ro-transform',this)"><span class="preset-val">↻ 90°</span><span class="preset-desc">Clockwise</span></div>
      <div class="preset-btn" data-v="180" onclick="selectPreset('ro-transform',this)"><span class="preset-val">↻ 180°</span><span class="preset-desc">Upside down</span></div>
      <div class="preset-btn" data-v="270" onclick="selectPreset('ro-transform',this)"><span class="preset-val">↺ 90°</span><span class="preset-desc">Counter-CW</span></div>
      <div class="preset-btn" data-v="hflip" onclick="selectPreset('ro-transform',this)"><span class="preset-val">↔ Flip</span><span class="preset-desc">Horizontal</span></div>
      <div class="preset-btn" data-v="vflip" onclick="selectPreset('ro-transform',this)"><span class="preset-val">↕ Flip</span><span class="preset-desc">Vertical</span></div>
    </div>
    <input type="hidden" id="ro-transform" value="90">
  </div>
  <button class="run-btn" id="ro-run" disabled onclick="runRotate()">Upload a video first</button>
  <div class="progress-box" id="ro-progress"><div class="progress-track"><div class="progress-fill" id="ro-pfill"></div></div><div class="log" id="ro-log"></div></div>
  <div class="result-box" id="ro-result"><video class="result-video" id="ro-rvideo" controls></video><div class="result-stats" id="ro-rstats"></div><a class="dl-btn" id="ro-dl" href="#">⬇ Download Video</a></div>
</div>

<!-- ── RESIZE FOR SOCIAL ── -->
<div class="panel" id="panel-crop">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">📐</span>Resize for Social</div><div class="panel-sub">Fit your video to YouTube, TikTok, Instagram and more</div></div>
  <div class="upload-zone" id="cr-dropzone"><input type="file" id="cr-file" accept="video/*">
    <div class="upload-zone-icon">📐</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="cr-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="cr-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="cr-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="cr-dur">—</span></div><div class="file-stat">Size <span id="cr-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('cr')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Platform</h4>
    <div class="preset-grid">
      <div class="preset-btn blue active" data-v="youtube" onclick="selectPreset('cr-platform',this)"><span class="preset-val">▶ YT</span><span class="preset-desc">1920×1080 · 16:9</span></div>
      <div class="preset-btn blue" data-v="instagram" onclick="selectPreset('cr-platform',this)"><span class="preset-val">📸 IG</span><span class="preset-desc">1080×1080 · 1:1</span></div>
      <div class="preset-btn blue" data-v="tiktok" onclick="selectPreset('cr-platform',this)"><span class="preset-val">♪ TT</span><span class="preset-desc">1080×1920 · 9:16</span></div>
      <div class="preset-btn blue" data-v="linkedin" onclick="selectPreset('cr-platform',this)"><span class="preset-val">in LI</span><span class="preset-desc">1280×720 · 16:9</span></div>
      <div class="preset-btn blue" data-v="twitter" onclick="selectPreset('cr-platform',this)"><span class="preset-val">𝕏 TW</span><span class="preset-desc">1280×720 · 16:9</span></div>
      <div class="preset-btn blue" data-v="square" onclick="selectPreset('cr-platform',this)"><span class="preset-val">⬛ SQ</span><span class="preset-desc">1080×1080 · 1:1</span></div>
    </div>
    <input type="hidden" id="cr-platform" value="youtube">
  </div>
  <button class="run-btn" id="cr-run" disabled onclick="runCrop()">Upload a video first</button>
  <div class="progress-box" id="cr-progress"><div class="progress-track"><div class="progress-fill" id="cr-pfill"></div></div><div class="log" id="cr-log"></div></div>
  <div class="result-box" id="cr-result"><video class="result-video" id="cr-rvideo" controls></video><div class="result-stats" id="cr-rstats"></div><a class="dl-btn" id="cr-dl" href="#">⬇ Download Video</a></div>
</div>

<!-- ── WATERMARK ── -->
<div class="panel" id="panel-watermark">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🏷️</span>Watermark</div><div class="panel-sub">Add your brand name or text to your video</div></div>
  <div class="upload-zone" id="wm-dropzone"><input type="file" id="wm-file" accept="video/*">
    <div class="upload-zone-icon">🏷️</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="wm-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="wm-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="wm-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="wm-dur">—</span></div><div class="file-stat">Size <span id="wm-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('wm')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Watermark Text</h4>
    <div class="field field-row single">
      <label>Text (bottom-right corner)</label>
      <input type="text" id="wm-text" placeholder="e.g. Snipforge · @yourname · confidential" maxlength="50">
    </div>
  </div>
  <button class="run-btn" id="wm-run" disabled onclick="runWatermark()">Upload a video first</button>
  <div class="progress-box" id="wm-progress"><div class="progress-track"><div class="progress-fill" id="wm-pfill"></div></div><div class="log" id="wm-log"></div></div>
  <div class="result-box" id="wm-result"><video class="result-video" id="wm-rvideo" controls></video><div class="result-stats" id="wm-rstats"></div><a class="dl-btn" id="wm-dl" href="#">⬇ Download Watermarked Video</a></div>
</div>

<!-- ── MERGE ── -->
<div class="panel" id="panel-merge">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔗</span>Merge Videos</div><div class="panel-sub">Combine multiple videos into one seamless file</div></div>
  <div class="settings-card">
    <h4>Videos to Merge</h4>
    <div class="merge-list" id="merge-list"></div>
    <div class="add-merge-btn"><input type="file" id="merge-file-input" accept="video/*" onchange="addMergeFile(this)">+ Add Video</div>
  </div>
  <button class="run-btn" id="mg-run" disabled onclick="runMerge()">Add at least 2 videos</button>
  <div class="progress-box" id="mg-progress"><div class="progress-track"><div class="progress-fill" id="mg-pfill"></div></div><div class="log" id="mg-log"></div></div>
  <div class="result-box" id="mg-result"><video class="result-video" id="mg-rvideo" controls></video><div class="result-stats" id="mg-rstats"></div><a class="dl-btn" id="mg-dl" href="#">⬇ Download Merged Video</a></div>
</div>

<!-- ── CONVERT ── -->
<div class="panel" id="panel-convert">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔄</span>Convert</div><div class="panel-sub">Convert your video to MP4, MOV, WebM, GIF, MP3 or WAV</div></div>
  <div class="upload-zone" id="cv-dropzone"><input type="file" id="cv-file" accept="video/*,audio/*">
    <div class="upload-zone-icon">🔄</div><h3>Drop your file here</h3><p>Video or audio file</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="cv-filecard">
    <div class="file-card-top">
      <div class="file-thumb" style="font-size:1.6rem;display:flex;align-items:center;justify-content:center">🎞️</div>
      <div class="file-meta"><div class="file-name" id="cv-fname">—</div>
        <div class="file-stats"><div class="file-stat">Size <span id="cv-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('cv')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Output Format</h4>
    <div class="preset-grid" id="cv-fmt-grid">
      <div class="preset-btn active" data-v="mp4" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">MP4</span><span class="preset-desc">Most compatible</span></div>
      <div class="preset-btn" data-v="mov" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">MOV</span><span class="preset-desc">Apple / FCP</span></div>
      <div class="preset-btn" data-v="webm" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">WebM</span><span class="preset-desc">Web / Chrome</span></div>
      <div class="preset-btn" data-v="gif" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">GIF</span><span class="preset-desc">Animated</span></div>
      <div class="preset-btn" data-v="mp3" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">MP3</span><span class="preset-desc">Audio only</span></div>
      <div class="preset-btn" data-v="wav" onclick="selectPreset('cv-fmt',this)"><span class="preset-val">WAV</span><span class="preset-desc">Lossless audio</span></div>
    </div>
    <input type="hidden" id="cv-fmt" value="mp4">
  </div>
  <button class="run-btn" id="cv-run" disabled onclick="runConvert()">Upload a file first</button>
  <div class="progress-box" id="cv-progress"><div class="progress-track"><div class="progress-fill" id="cv-pfill"></div></div><div class="log" id="cv-log"></div></div>
  <div class="result-box" id="cv-result"><div class="result-stats" id="cv-rstats"></div><a class="dl-btn" id="cv-dl" href="#">⬇ Download Converted File</a></div>
</div>

<!-- ── COMPRESS ── -->
<div class="panel" id="panel-compress">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">📦</span>Compress</div><div class="panel-sub">Reduce file size while keeping quality</div></div>
  <div class="upload-zone" id="cm-dropzone"><input type="file" id="cm-file" accept="video/*">
    <div class="upload-zone-icon">📦</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="cm-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="cm-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="cm-fname">—</div>
        <div class="file-stats"><div class="file-stat">Size <span id="cm-size">—</span></div><div class="file-stat">Duration <span id="cm-dur">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('cm')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Compression Level</h4>
    <div class="preset-grid">
      <div class="preset-btn" data-v="low" onclick="selectPreset('cm-quality',this)"><span class="preset-val">Light</span><span class="preset-desc">~30% smaller</span></div>
      <div class="preset-btn active" data-v="medium" onclick="selectPreset('cm-quality',this)"><span class="preset-val">Medium</span><span class="preset-desc">~50% smaller</span></div>
      <div class="preset-btn" data-v="high" onclick="selectPreset('cm-quality',this)"><span class="preset-val">Heavy</span><span class="preset-desc">~70% smaller</span></div>
    </div>
    <input type="hidden" id="cm-quality" value="medium">
  </div>
  <button class="run-btn" id="cm-run" disabled onclick="runCompress()">Upload a video first</button>
  <div class="progress-box" id="cm-progress"><div class="progress-track"><div class="progress-fill" id="cm-pfill"></div></div><div class="log" id="cm-log"></div></div>
  <div class="result-box" id="cm-result"><video class="result-video" id="cm-rvideo" controls></video><div class="result-stats" id="cm-rstats"></div><a class="dl-btn" id="cm-dl" href="#">⬇ Download Compressed Video</a></div>
</div>

<!-- ── VOLUME ── -->
<div class="panel" id="panel-volume">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔊</span>Volume Control</div><div class="panel-sub">Boost or reduce the audio volume of your video</div></div>
  <div class="upload-zone" id="vl-dropzone"><input type="file" id="vl-file" accept="video/*">
    <div class="upload-zone-icon">🔊</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="vl-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="vl-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="vl-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="vl-dur">—</span></div><div class="file-stat">Size <span id="vl-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('vl')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Volume Level</h4>
    <div class="preset-grid">
      <div class="preset-btn green" data-v="0.25" onclick="selectPreset('vl-vol',this)"><span class="preset-val">25%</span><span class="preset-desc">Very quiet</span></div>
      <div class="preset-btn green" data-v="0.5" onclick="selectPreset('vl-vol',this)"><span class="preset-val">50%</span><span class="preset-desc">Half</span></div>
      <div class="preset-btn green active" data-v="1.5" onclick="selectPreset('vl-vol',this)"><span class="preset-val">150%</span><span class="preset-desc">Boost</span></div>
      <div class="preset-btn green" data-v="2.0" onclick="selectPreset('vl-vol',this)"><span class="preset-val">200%</span><span class="preset-desc">Double</span></div>
      <div class="preset-btn green" data-v="3.0" onclick="selectPreset('vl-vol',this)"><span class="preset-val">300%</span><span class="preset-desc">Triple</span></div>
    </div>
    <input type="hidden" id="vl-vol" value="1.5">
    <div class="field field-row single" style="margin-top:12px"><label>Custom Volume</label>
      <input type="range" id="vl-volume-slider" min="0.1" max="4.0" step="0.1" value="1.5">
      <div class="field-val" id="vl-volume-val">1.5× (150%)</div>
    </div>
  </div>
  <button class="run-btn" id="vl-run" disabled onclick="runVolume()">Upload a video first</button>
  <div class="progress-box" id="vl-progress"><div class="progress-track"><div class="progress-fill" id="vl-pfill"></div></div><div class="log" id="vl-log"></div></div>
  <div class="result-box" id="vl-result"><video class="result-video" id="vl-rvideo" controls></video><div class="result-stats" id="vl-rstats"></div><a class="dl-btn" id="vl-dl" href="#">⬇ Download Video</a></div>
</div>

<!-- ── EXTRACT AUDIO ── -->
<div class="panel" id="panel-audio">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🎵</span>Extract Audio</div><div class="panel-sub">Pull the audio track out as an MP3 file</div></div>
  <div class="upload-zone" id="au-dropzone"><input type="file" id="au-file" accept="video/*">
    <div class="upload-zone-icon">🎵</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="au-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="au-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="au-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="au-dur">—</span></div><div class="file-stat">Size <span id="au-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('au')">Change</button>
    </div>
  </div>
  <button class="run-btn" id="au-run" disabled onclick="runAudio()">Upload a video first</button>
  <div class="progress-box" id="au-progress"><div class="progress-track"><div class="progress-fill" id="au-pfill"></div></div><div class="log" id="au-log"></div></div>
  <div class="result-box" id="au-result"><div class="result-stats" id="au-rstats"></div><a class="dl-btn" id="au-dl" href="#">⬇ Download MP3</a></div>
</div>

<!-- ── MUTE ── -->
<div class="panel" id="panel-mute">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔇</span>Mute Audio</div><div class="panel-sub">Remove the audio track from your video completely</div></div>
  <div class="upload-zone" id="mu-dropzone"><input type="file" id="mu-file" accept="video/*">
    <div class="upload-zone-icon">🔇</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="mu-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="mu-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="mu-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="mu-dur">—</span></div><div class="file-stat">Size <span id="mu-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('mu')">Change</button>
    </div>
  </div>
  <button class="run-btn" id="mu-run" disabled onclick="runMute()">Upload a video first</button>
  <div class="progress-box" id="mu-progress"><div class="progress-track"><div class="progress-fill" id="mu-pfill"></div></div><div class="log" id="mu-log"></div></div>
  <div class="result-box" id="mu-result"><video class="result-video" id="mu-rvideo" controls></video><div class="result-stats" id="mu-rstats"></div><a class="dl-btn" id="mu-dl" href="#">⬇ Download Muted Video</a></div>
</div>


<!-- ── NOISE REMOVAL ── -->
<div class="panel" id="panel-denoise">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">🔕</span>Noise Removal</div><div class="panel-sub">Remove background noise, hiss and hum from your audio</div></div>
  <div class="upload-zone" id="dn-dropzone"><input type="file" id="dn-file" accept="video/*,audio/*">
    <div class="upload-zone-icon">🔕</div><h3>Drop your video here</h3><p>MP4 · MOV · WebM · AVI · MP3 · WAV</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="dn-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="dn-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="dn-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="dn-dur">—</span></div><div class="file-stat">Size <span id="dn-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('dn')">Change</button>
    </div>
  </div>
  <div class="settings-card">
    <h4>Noise Reduction Strength</h4>
    <div class="preset-grid">
      <div class="preset-btn" data-v="light" onclick="selectPreset('dn-strength',this)"><span class="preset-val">🌿 Light</span><span class="preset-desc">Subtle, preserves tone</span></div>
      <div class="preset-btn active" data-v="medium" onclick="selectPreset('dn-strength',this)"><span class="preset-val">⚡ Medium</span><span class="preset-desc">Recommended</span></div>
      <div class="preset-btn" data-v="heavy" onclick="selectPreset('dn-strength',this)"><span class="preset-val">💪 Heavy</span><span class="preset-desc">Max noise removal</span></div>
    </div>
    <input type="hidden" id="dn-strength" value="medium">
  </div>
  <button class="run-btn" id="dn-run" disabled onclick="runDenoise()">Upload a video first</button>
  <div class="progress-box" id="dn-progress"><div class="progress-track"><div class="progress-fill" id="dn-pfill"></div></div><div class="log" id="dn-log"></div></div>
  <div class="result-box" id="dn-result"><video class="result-video" id="dn-rvideo" controls></video><div class="result-stats" id="dn-rstats"></div><a class="dl-btn" id="dn-dl" href="#">⬇ Download Clean Audio</a></div>
</div>

<!-- ── TRANSCRIBE ── -->
<div class="panel" id="panel-transcribe">
  <div class="panel-header"><div class="panel-title"><span class="panel-title-icon">📝</span>AI Transcribe</div><div class="panel-sub">Convert speech to text — any language</div></div>
  <div class="upload-zone" id="tc-dropzone"><input type="file" id="tc-file" accept="video/*,audio/*">
    <div class="upload-zone-icon">📝</div><h3>Drop your video or audio here</h3><p>MP4 · MOV · MP3 · WAV</p>
  </div>
  <div class="recent-files-list"></div>
  <div class="file-card" id="tc-filecard">
    <div class="file-card-top">
      <div class="file-thumb"><video id="tc-thumb" muted></video></div>
      <div class="file-meta"><div class="file-name" id="tc-fname">—</div>
        <div class="file-stats"><div class="file-stat">Duration <span id="tc-dur">—</span></div><div class="file-stat">Size <span id="tc-size">—</span></div></div>
      </div>
      <button class="file-change" onclick="resetUpload('tc')">Change</button>
    </div>
  </div>
  <div style="margin-bottom:12px">
    <div style="font-family:var(--mono);font-size:.6rem;letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin-bottom:6px;display:flex;align-items:center;gap:8px"><span>Language</span><span id="tc-lang-detected" style="display:none;font-size:.62rem;color:var(--green);padding:2px 8px;background:var(--green-bg);border-radius:4px;font-weight:600"></span></div>
    <select id="tc-language" style="width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg3);color:var(--text);font-size:.88rem;font-family:var(--sans)">
      <option value="auto">🌐 Auto-detect</option>
      <option value="en">🇺🇸 English</option>
      <option value="es">🇪🇸 Spanish</option>
      <option value="fr">🇫🇷 French</option>
      <option value="de">🇩🇪 German</option>
      <option value="it">🇮🇹 Italian</option>
      <option value="pt">🇧🇷 Portuguese</option>
      <option value="nl">🇳🇱 Dutch</option>
      <option value="ru">🇷🇺 Russian</option>
      <option value="ja">🇯🇵 Japanese</option>
      <option value="ko">🇰🇷 Korean</option>
      <option value="zh">🇨🇳 Chinese</option>
      <option value="ar">🇸🇦 Arabic</option>
      <option value="hi">🇮🇳 Hindi</option>
      <option value="tr">🇹🇷 Turkish</option>
      <option value="vi">🇻🇳 Vietnamese</option>
      <option value="th">🇹🇭 Thai</option>
      <option value="id">🇮🇩 Indonesian</option>
      <option value="sv">🇸🇪 Swedish</option>
      <option value="uk">🇺🇦 Ukrainian</option>
    </select>
  </div>
  <div style="margin-bottom:12px">
    <div style="font-family:var(--mono);font-size:.6rem;letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin-bottom:6px">Translate to (optional)</div>
    <select id="tc-translate" style="width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg3);color:var(--text);font-size:.88rem;font-family:var(--sans)">
      <option value="none">— No translation —</option>
      <option value="English">🇺🇸 English</option>
      <option value="Spanish">🇪🇸 Spanish</option>
      <option value="French">🇫🇷 French</option>
      <option value="German">🇩🇪 German</option>
      <option value="Italian">🇮🇹 Italian</option>
      <option value="Portuguese">🇧🇷 Portuguese</option>
      <option value="Dutch">🇳🇱 Dutch</option>
      <option value="Russian">🇷🇺 Russian</option>
      <option value="Japanese">🇯🇵 Japanese</option>
      <option value="Korean">🇰🇷 Korean</option>
      <option value="Chinese">🇨🇳 Chinese (Simplified)</option>
      <option value="Arabic">🇸🇦 Arabic</option>
      <option value="Hindi">🇮🇳 Hindi</option>
      <option value="Turkish">🇹🇷 Turkish</option>
      <option value="Vietnamese">🇻🇳 Vietnamese</option>
      <option value="Thai">🇹🇭 Thai</option>
      <option value="Indonesian">🇮🇩 Indonesian</option>
      <option value="Swedish">🇸🇪 Swedish</option>
      <option value="Ukrainian">🇺🇦 Ukrainian</option>
      <option value="Polish">🇵🇱 Polish</option>
    </select>
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;margin-bottom:12px">
    <div>
      <div style="font-size:.9rem;font-weight:600;color:var(--text)">Burn captions into video</div>
      <div style="font-size:.75rem;color:var(--muted);margin-top:2px">Embed subtitles directly into the video file</div>
    </div>
    <label class="toggle"><input type="checkbox" id="tc-burn-captions"><span class="toggle-slider"></span></label>
  </div>
  <button class="run-btn" id="tc-run" disabled onclick="runTranscribe()">Upload a video first</button>
  <div class="progress-box" id="tc-progress"><div class="progress-track"><div class="progress-fill" id="tc-pfill"></div></div><div class="log" id="tc-log"></div></div>
  <div class="result-box" id="tc-result">
    <div class="result-stats" id="tc-rstats"></div>
    <div id="tc-text-wrap" style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-family:var(--mono);font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em">Transcript</span>
        <button onclick="copyTranscript()" style="font-family:var(--mono);font-size:.65rem;padding:4px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer">Copy</button>
      </div>
      <div id="tc-text" style="font-size:.9rem;line-height:1.7;color:var(--text);white-space:pre-wrap;max-height:300px;overflow-y:auto"></div>
    </div>
    <a class="dl-btn" id="tc-dl-txt" href="#" download="transcript.txt">⬇ Download Transcript (.txt)</a>
    <a class="dl-btn" id="tc-dl-video" href="#" download="captioned.mp4" style="display:none;margin-top:10px;background:linear-gradient(135deg,#1a7a3c,#2aa55a);color:#fff">⬇ Download Captioned Video (.mp4)</a>
  </div>
</div>

<!-- ── SHARED LINKS ── -->
<div class="panel" id="panel-shares">
  <div class="panel-header">
    <div class="panel-title"><span class="panel-title-icon">🔗</span>Shared Links</div>
    <div class="panel-sub">Videos you've shared — anyone with the link can watch and download</div>
  </div>

  <div id="shares-loading" style="text-align:center;padding:40px;color:var(--muted);font-family:var(--mono);font-size:.8rem">
    Loading your shared links…
  </div>

  <div id="shares-empty" style="display:none;text-align:center;padding:60px 20px">
    <div style="font-size:2.5rem;margin-bottom:12px">🔗</div>
    <div style="font-family:var(--cond);font-size:1.2rem;font-weight:700;margin-bottom:6px">No shared links yet</div>
    <div style="color:var(--muted);font-size:.88rem">Process a video and click "Copy Share Link" to create one</div>
  </div>

  <div id="shares-list" style="display:none;flex-direction:column;gap:10px"></div>
</div>


</main>
</div>

<script>
// ── state ──
const state = {};

// ── nav ──
function switchPanel(el) {
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('panel-'+el.dataset.panel).classList.add('active');
  if(el.dataset.panel==='shares') loadShares();
}

async function loadShares(){
  const loading=document.getElementById('shares-loading');
  const empty=document.getElementById('shares-empty');
  const list=document.getElementById('shares-list');
  loading.style.display='block'; empty.style.display='none'; list.style.display='none';
  const r=await fetch('/api/my-shares');
  const shares=await r.json();
  loading.style.display='none';
  if(!shares.length){ empty.style.display='block'; return; }
  list.style.display='flex';
  list.innerHTML=shares.map(s=>{
    const url=window.location.origin+'/share/'+s.id;
    const exp=s.expires_at?s.expires_at.slice(0,10):'never';
    const created=s.created_at?s.created_at.slice(0,10):'—';
    return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.filename||'video.mp4'}</div>
          <div style="font-family:var(--mono);font-size:.65rem;color:var(--muted);margin-top:2px">
            Created ${created} · Expires ${exp} · ${s.views||0} views
          </div>
        </div>
        <button onclick="deleteShare('${s.id}',this)" style="font-family:var(--mono);font-size:.62rem;padding:4px 10px;background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer">Delete</button>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <div style="flex:1;font-family:var(--mono);font-size:.7rem;color:var(--muted);background:var(--bg3);padding:7px 10px;border-radius:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${url}</div>
        <button onclick="navigator.clipboard.writeText('${url}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
          style="font-family:var(--mono);font-size:.68rem;padding:7px 14px;background:var(--accent);color:#fff;border:none;border-radius:6px;cursor:pointer;white-space:nowrap">Copy</button>
        <a href="${url}" target="_blank" style="font-family:var(--mono);font-size:.68rem;padding:7px 14px;background:var(--bg3);color:var(--text);border:1px solid var(--border);border-radius:6px;text-decoration:none;white-space:nowrap">Preview</a>
      </div>
    </div>`;
  }).join('');
}

async function deleteShare(id, btn){
  if(!confirm('Delete this shared link?')) return;
  await fetch('/api/share/'+id,{method:'DELETE'});
  loadShares();
}

// ── upload ──
function setupUpload(prefix) {
  const input = document.getElementById(prefix+'-file');
  const dz    = document.getElementById(prefix+'-dropzone');
  if (!input || !dz) return;
  input.addEventListener('change', e => handleFile(prefix, e.target.files[0]));
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('over'); handleFile(prefix, e.dataTransfer.files[0]); });
}

async function handleFile(prefix, file) {
  if (!file) return;
  const dz   = document.getElementById(prefix+'-dropzone');
  const card = document.getElementById(prefix+'-filecard');
  const run  = document.getElementById(prefix+'-run');
  dz.style.display = 'none';
  card.classList.add('show');
  run.disabled = true;
  run.textContent = 'Uploading…';
  const thumb = document.getElementById(prefix+'-thumb');
  if (thumb) { thumb.src = URL.createObjectURL(file); }
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/info', { method:'POST', body:fd });
  const d = await r.json();
  if (d.error) { log(prefix, d.error, 'err'); run.textContent='Upload failed'; return; }
  state[prefix] = d;
  if (document.getElementById(prefix+'-fname')) document.getElementById(prefix+'-fname').textContent = d.filename;
  if (document.getElementById(prefix+'-dur')) document.getElementById(prefix+'-dur').textContent = fmtTime(d.duration);
  if (document.getElementById(prefix+'-size')) document.getElementById(prefix+'-size').textContent = d.size_mb+' MB';
  if (document.getElementById(prefix+'-res') && d.width) document.getElementById(prefix+'-res').textContent = d.width+'×'+d.height;
  if (prefix==='tr') document.getElementById('tr-end').value = d.duration.toFixed(1);
  if (prefix==='mt' && document.getElementById('mt-segments').children.length===0) addSegment();
  run.disabled = false;
  run.textContent = getRunLabel(prefix);
  // For transcribe panel: detect language immediately after upload
  if (prefix === 'tc' && d.file_id) {
    const badge = document.getElementById('tc-lang-detected');
    if (badge) { badge.textContent = '⏳ Detecting…'; badge.style.display = 'inline'; }
    fetch('/api/detect-language', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file_id: d.file_id})
    }).then(r => r.json()).then(ld => {
      const langNames = {en:'English',es:'Spanish',fr:'French',de:'German',it:'Italian',
        pt:'Portuguese',nl:'Dutch',ru:'Russian',ja:'Japanese',ko:'Korean',zh:'Chinese',
        ar:'Arabic',hi:'Hindi',tr:'Turkish',vi:'Vietnamese',th:'Thai',id:'Indonesian',
        sv:'Swedish',uk:'Ukrainian'};
      const lang = ld.language || '';
      if (lang) {
        // Update dropdown
        const langSel = document.getElementById('tc-language');
        if (langSel) {
          const opt = langSel.querySelector(`option[value="${lang}"]`);
          if (opt) {
            langSel.value = lang;
          } else {
            const prev = langSel.querySelector('option[data-detected]');
            if (prev) prev.remove();
            const newOpt = document.createElement('option');
            newOpt.value = lang; newOpt.setAttribute('data-detected','1');
            newOpt.textContent = '✓ ' + (langNames[lang] || lang.charAt(0).toUpperCase()+lang.slice(1)) + ' (detected)';
            langSel.insertBefore(newOpt, langSel.firstChild);
            langSel.value = lang;
          }
        }
        // Show badge
        const langDisplay = langNames[lang] || (lang.charAt(0).toUpperCase()+lang.slice(1));
        if (badge) { badge.textContent = '✓ ' + langDisplay + ' detected'; badge.style.display = 'inline'; }
      } else {
        if (badge) { badge.textContent = ''; badge.style.display = 'none'; }
      }
    }).catch(() => {
      if (badge) { badge.textContent = ''; badge.style.display = 'none'; }
    });
  }
}

function getRunLabel(p) {
  const m = {sz:'✂ AI Shorten Video',tr:'Trim Video',mt:'Stitch Segments',sp:'Change Speed',
             ro:'Rotate / Flip',cr:'Resize Video',wm:'Add Watermark',
             vl:'Adjust Volume',cm:'Compress Video',cv:'Convert Format',au:'Extract Audio',
             mu:'Mute Video',dn:'Remove Noise',tc:'Transcribe Speech'};
  return m[p] || 'Process';
}

function resetUpload(prefix) {
  const dz = document.getElementById(prefix+'-dropzone');
  const card = document.getElementById(prefix+'-filecard');
  const run = document.getElementById(prefix+'-run');
  if (dz) dz.style.display = '';
  if (card) card.classList.remove('show');
  if (run) { run.disabled=true; run.textContent='Upload a video first'; }
  const pb = document.getElementById(prefix+'-progress');
  const rb = document.getElementById(prefix+'-result');
  if (pb) pb.classList.remove('show');
  if (rb) rb.classList.remove('show');
  if (prefix === 'tc') {
    const badge = document.getElementById('tc-lang-detected');
    if (badge) { badge.textContent = ''; badge.style.display = 'none'; }
    const langSel = document.getElementById('tc-language');
    if (langSel) {
      const detected = langSel.querySelector('option[data-detected]');
      if (detected) detected.remove();
      langSel.value = 'auto';
    }
  }
  delete state[prefix];
}

// ── sliders ──
function bindSlider(id, valId, fmt) {
  const el=document.getElementById(id), vl=document.getElementById(valId);
  if (!el||!vl) return;
  el.addEventListener('input', ()=>{
    vl.textContent=fmt(el.value);
    const prefix=id.replace('-speed','').replace('-volume-slider','');
    if (id.endsWith('-speed')) syncSpeedBtns(prefix, parseFloat(el.value));
  });
}
bindSlider('sz-speed','sz-speed-val',v=>parseFloat(v).toFixed(2)+'×');
bindSlider('sz-thresh','sz-thresh-val',v=>v+' dB');
bindSlider('sz-minsilence','sz-minsilence-val',v=>v+' ms');
bindSlider('sz-pad','sz-pad-val',v=>v+' ms');
bindSlider('sp-speed','sp-speed-val',v=>parseFloat(v).toFixed(2)+'×');
bindSlider('vl-volume-slider','vl-volume-val',v=>`${parseFloat(v).toFixed(1)}× (${Math.round(v*100)}%)`);

document.getElementById('vl-volume-slider').addEventListener('input', function() {
  document.getElementById('vl-vol').value = this.value;
  document.querySelectorAll('#panel-volume .preset-btn').forEach(b=>b.classList.toggle('active', parseFloat(b.dataset.v)===parseFloat(this.value)));
});

function syncSpeedBtns(prefix, val) {
  const grid=document.getElementById(prefix+'-speed-grid');
  if (!grid) return;
  grid.querySelectorAll('.speed-btn').forEach(b=>b.classList.toggle('active', parseFloat(b.dataset.v)===val));
}

function selectSpeed(prefix, btn) {
  document.querySelectorAll('#'+prefix+'-speed-grid .speed-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const v=btn.dataset.v;
  const s=document.getElementById(prefix+'-speed');
  if(s){s.value=v;document.getElementById(prefix+'-speed-val').textContent=parseFloat(v).toFixed(2)+'×';}
}

function selectPreset(hiddenId, btn) {
  const parent=btn.parentElement;
  parent.querySelectorAll('.preset-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const hid=document.getElementById(hiddenId);
  if(hid) hid.value=btn.dataset.v;
}

function togglePill(el){el.classList.toggle('on')}

// ── segments ──
let segCount=0;
function addSegment(start='',end=''){
  segCount++;
  const list=document.getElementById('mt-segments');
  const n=list.children.length+1;
  const row=document.createElement('div');
  row.className='segment-row';
  row.innerHTML=`<span class="seg-num">${n}</span>
    <div class="seg-inputs">
      <input type="number" placeholder="Start (s)" value="${start}" min="0" step="0.1" class="seg-start">
      <span class="seg-label">→</span>
      <input type="number" placeholder="End (s)" value="${end}" min="0" step="0.1" class="seg-end">
      <span class="seg-label">sec</span>
    </div>
    <button class="seg-del" onclick="this.parentElement.remove();renumberSegs()">✕</button>`;
  list.appendChild(row);
}
function renumberSegs(){document.querySelectorAll('.segment-row').forEach((r,i)=>r.querySelector('.seg-num').textContent=i+1)}

// ── merge ──
async function addMergeFile(input){
  const file=input.files[0]; if(!file) return;
  const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/info',{method:'POST',body:fd});
  const d=await r.json();
  if(d.error){alert(d.error);return;}
  const list=document.getElementById('merge-list');
  const item=document.createElement('div'); item.className='merge-item'; item.dataset.id=d.file_id;
  item.innerHTML=`<span style="font-size:1.1rem">🎬</span>
    <span class="merge-item-name">${d.filename}</span>
    <span class="merge-item-size">${d.size_mb} MB</span>
    <button class="merge-remove" onclick="this.parentElement.remove();updateMergeBtn()">✕</button>`;
  list.appendChild(item);
  updateMergeBtn();
  input.value='';
}
function updateMergeBtn(){
  const n=document.querySelectorAll('.merge-item').length;
  const btn=document.getElementById('mg-run');
  btn.disabled=n<2;
  btn.textContent=n<2?`Add at least ${2-n} more video${n===1?'':'s'}`:`🔗 Merge ${n} Videos`;
}

// ── helpers ──
function fmtTime(s){return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0')}

function log(prefix, msg, type=''){
  const box=document.getElementById(prefix+'-log');
  if(!box) return;
  const span=document.createElement('span');
  span.className=type||'info'; span.textContent=msg;
  box.appendChild(span); box.scrollTop=box.scrollHeight;
}

function setProgress(prefix, pct){
  const f=document.getElementById(prefix+'-pfill');
  if(f) f.style.width=pct+'%';
}

function showStats(prefix, stats){
  const el=document.getElementById(prefix+'-rstats'); if(!el) return;
  const all=[];
  if(stats){
    if(stats.original) all.push({v:fmtTime(stats.original),l:'Original'});
    if(stats.new) all.push({v:fmtTime(stats.new),l:'New Duration'});
    if(stats.saved) all.push({v:'-'+fmtTime(stats.saved)+' ('+stats.pct+'%)',l:'Saved',c:'var(--green)'});
    if(stats.original_mb) all.push({v:stats.original_mb+' MB',l:'Original Size'});
    if(stats.new_mb) all.push({v:stats.new_mb+' MB',l:'New Size'});
    if(stats.saved_pct) all.push({v:'-'+stats.saved_pct+'%',l:'Saved',c:'var(--green)'});
    if(stats.format) all.push({v:stats.format.toUpperCase(),l:'Format'});
    if(stats.clips) all.push({v:stats.clips,l:'Clips Merged'});
    if(stats.preset) all.push({v:stats.preset,l:'Platform'});
    if(stats.angle) all.push({v:stats.angle+'°',l:'Rotated'});
    if(stats.volume) all.push({v:stats.volume+'×',l:'Volume'});
  }
  el.innerHTML=all.map(s=>`<div class="rstat"><div class="rstat-val" style="${s.c?'color:'+s.c:''}">${s.v}</div><div class="rstat-lbl">${s.l}</div></div>`).join('');
}

async function pollJob(prefix, jobId, videoId, dlId, dlName){
  let idx=0;
  const iv=setInterval(async()=>{
    const r=await fetch(`/api/status/${jobId}?from=${idx}`);
    const d=await r.json();
    if(d.log){d.log.forEach(l=>log(prefix,l,l.startsWith('Done')?'ok':l.startsWith('Error')?'err':'info'));idx+=d.log.length;}
    if(d.progress!=null) setProgress(prefix,d.progress);
    if(d.status==='done'){
      clearInterval(iv);
      const dlUrl = '/api/download/'+jobId;
      if(videoId){const v=document.getElementById(videoId);if(v)v.src=dlUrl;}
      const dl=document.getElementById(dlId);
      if(dl){dl.href=dlUrl;if(dlName)dl.download=dlName;}
      document.getElementById(prefix+'-result').classList.add('show');
      showStats(prefix,d.stats);
      const run=document.getElementById(prefix+'-run');
      if(run){run.disabled=false;run.textContent=getRunLabel(prefix);run.classList.remove('working');}
    }
    if(d.status==='error'){
      clearInterval(iv);
      log(prefix,'Error: '+d.error,'err');
      const run=document.getElementById(prefix+'-run');
      if(run){run.disabled=false;run.textContent='Retry';run.classList.remove('working');}
    }
  },1200);
}

async function startJob(prefix, body){
  const s=state[prefix]; if(!s) return null;
  const run=document.getElementById(prefix+'-run');
  run.disabled=true; run.textContent='Processing…'; run.classList.add('working');
  document.getElementById(prefix+'-progress').classList.add('show');
  document.getElementById(prefix+'-result').classList.remove('show');
  document.getElementById(prefix+'-log').innerHTML='';
  setProgress(prefix,0);
  const r=await fetch('/api/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:s.file_id,...body})});
  const d=await r.json();
  if(d.error){log(prefix,d.error,'err');run.disabled=false;run.textContent=getRunLabel(prefix);run.classList.remove('working');return null;}
  return d.job_id;
}

// ── run functions ──
async function runShorten(){
  const s=state['sz'];if(!s)return;
  const jid=await startJob('sz',{op:'shorten',
    threshold:parseFloat(document.getElementById('sz-thresh').value),
    min_silence:parseInt(document.getElementById('sz-minsilence').value),
    pad:parseInt(document.getElementById('sz-pad').value),
    speed:parseFloat(document.getElementById('sz-speed').value),
    do_speed:document.getElementById('sz-speed-pill').classList.contains('on'),
    out_ext:'mp4'});
  if(jid) pollJob('sz',jid,'sz-rvideo','sz-dl',s.filename.replace(/[.][^.]+$/,'')+'_shortened.mp4');
}

async function runTrim(){
  const s=state['tr'];if(!s)return;
  const jid=await startJob('tr',{op:'trim',
    start:parseFloat(document.getElementById('tr-start').value)||0,
    end:parseFloat(document.getElementById('tr-end').value)||s.duration,
    out_ext:'mp4'});
  if(jid) pollJob('tr',jid,'tr-rvideo','tr-dl',s.filename.replace(/[.][^.]+$/,'')+'_trimmed.mp4');
}

async function runMultiTrim(){
  const s=state['mt'];if(!s)return;
  const segs=[...document.querySelectorAll('.segment-row')].map(r=>[
    parseFloat(r.querySelector('.seg-start').value)||0,
    parseFloat(r.querySelector('.seg-end').value)||0]);
  const jid=await startJob('mt',{op:'multi_trim',segments:segs,out_ext:'mp4'});
  if(jid) pollJob('mt',jid,'mt-rvideo','mt-dl',s.filename.replace(/[.][^.]+$/,'')+'_stitched.mp4');
}

async function runSpeed(){
  const s=state['sp'];if(!s)return;
  const jid=await startJob('sp',{op:'speed',speed:parseFloat(document.getElementById('sp-speed').value),out_ext:'mp4'});
  if(jid) pollJob('sp',jid,'sp-rvideo','sp-dl',s.filename.replace(/[.][^.]+$/,'')+'_speed.mp4');
}

async function runReverse(){
  const s=state['rv'];if(!s)return;
  const jid=await startJob('rv',{op:'reverse',out_ext:'mp4'});
  if(jid) pollJob('rv',jid,'rv-rvideo','rv-dl',s.filename.replace(/[.][^.]+$/,'')+'_reversed.mp4');
}

async function runRotate(){
  const s=state['ro'];if(!s)return;
  const angle=document.getElementById('ro-transform').value;
  const jid=await startJob('ro',{op:'rotate',angle,out_ext:'mp4'});
  if(jid) pollJob('ro',jid,'ro-rvideo','ro-dl',s.filename.replace(/[.][^.]+$/,'')+'_rotated.mp4');
}

async function runCrop(){
  const s=state['cr'];if(!s)return;
  const preset=document.getElementById('cr-platform').value;
  const jid=await startJob('cr',{op:'crop',preset,out_ext:'mp4'});
  if(jid) pollJob('cr',jid,'cr-rvideo','cr-dl',s.filename.replace(/[.][^.]+$/,'')+'_'+preset+'.mp4');
}

async function runWatermark(){
  const s=state['wm'];if(!s)return;
  const text=document.getElementById('wm-text').value||'Snipforge';
  const jid=await startJob('wm',{op:'watermark',text,out_ext:'mp4'});
  if(jid) pollJob('wm',jid,'wm-rvideo','wm-dl',s.filename.replace(/[.][^.]+$/,'')+'_watermarked.mp4');
}

async function runVolume(){
  const s=state['vl'];if(!s)return;
  const volume=parseFloat(document.getElementById('vl-vol').value)||1.5;
  const jid=await startJob('vl',{op:'volume',volume,out_ext:'mp4'});
  if(jid) pollJob('vl',jid,'vl-rvideo','vl-dl',s.filename.replace(/[.][^.]+$/,'')+'_volume.mp4');
}

async function runMerge(){
  const items=[...document.querySelectorAll('.merge-item')]; if(items.length<2)return;
  const file_ids=items.map(i=>i.dataset.id);
  const run=document.getElementById('mg-run');
  run.disabled=true;run.textContent='Merging…';run.classList.add('working');
  document.getElementById('mg-progress').classList.add('show');
  document.getElementById('mg-result').classList.remove('show');
  document.getElementById('mg-log').innerHTML=''; setProgress('mg',0);
  const r=await fetch('/api/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_ids})});
  const d=await r.json();
  if(d.error){log('mg',d.error,'err');run.disabled=false;run.textContent=getRunLabel('mg');run.classList.remove('working');return;}
  pollJob('mg',d.job_id,'mg-rvideo','mg-dl','merged.mp4');
}

async function runConvert(){
  const s=state['cv'];if(!s)return;
  const fmt=document.getElementById('cv-fmt').value;
  const jid=await startJob('cv',{op:'convert',fmt,out_ext:fmt});
  if(jid) pollJob('cv',jid,null,'cv-dl',s.filename.replace(/[.][^.]+$/,'')+'.'+fmt);
}

async function runCompress(){
  const s=state['cm'];if(!s)return;
  const quality=document.getElementById('cm-quality').value;
  const jid=await startJob('cm',{op:'compress',quality,out_ext:'mp4'});
  if(jid) pollJob('cm',jid,'cm-rvideo','cm-dl',s.filename.replace(/[.][^.]+$/,'')+'_compressed.mp4');
}

async function runAudio(){
  const s=state['au'];if(!s)return;
  const jid=await startJob('au',{op:'extract_audio',out_ext:'mp3'});
  if(jid) pollJob('au',jid,null,'au-dl',s.filename.replace(/[.][^.]+$/,'')+'.mp3');
}

async function runMute(){
  const s=state['mu'];if(!s)return;
  const jid=await startJob('mu',{op:'mute',out_ext:'mp4'});
  if(jid) pollJob('mu',jid,'mu-rvideo','mu-dl',s.filename.replace(/[.][^.]+$/,'')+'_muted.mp4');
}

// ── SCREEN RECORDER ──
let mediaRecorder=null, recChunks=[], recStream=null, recTimer=null, recSecs=0, recType='screen';

function selectRecType(type){
  recType=type;
  document.querySelectorAll('.rec-type-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('rtype-'+type).classList.add('active');
}

async function startRecording(){
  try{
    const audioSrc=document.getElementById('rec-audio-src').value;
    const quality=document.getElementById('rec-quality').value;
    const constraints={audio:audioSrc!=='none'};
    const vRes={high:{width:1920,height:1080},medium:{width:1280,height:720},low:{width:854,height:480}}[quality];

    let stream;
    if(recType==='screen'){
      stream=await navigator.mediaDevices.getDisplayMedia({video:vRes,audio:audioSrc==='system'||audioSrc==='both'});
      if(audioSrc==='mic'||audioSrc==='both'){
        const mic=await navigator.mediaDevices.getUserMedia({audio:true});
        mic.getAudioTracks().forEach(t=>stream.addTrack(t));
      }
    } else if(recType==='webcam'){
      stream=await navigator.mediaDevices.getUserMedia({video:vRes,audio:audioSrc!=='none'});
    } else {
      // both - screen + webcam PiP
      const screen=await navigator.mediaDevices.getDisplayMedia({video:vRes,audio:audioSrc==='system'||audioSrc==='both'});
      const cam=await navigator.mediaDevices.getUserMedia({video:{width:320,height:240},audio:audioSrc==='mic'||audioSrc==='both'});
      // Combine on canvas
      const canvas=document.createElement('canvas');
      canvas.width=vRes.width||1280; canvas.height=vRes.height||720;
      const ctx=canvas.getContext('2d');
      const screenVid=document.createElement('video'); screenVid.srcObject=screen; screenVid.play();
      const camVid=document.createElement('video'); camVid.srcObject=cam; camVid.play();
      function drawFrame(){
        ctx.drawImage(screenVid,0,0,canvas.width,canvas.height);
        ctx.drawImage(camVid,canvas.width-330,canvas.height-250,320,240);
        requestAnimationFrame(drawFrame);
      }
      drawFrame();
      stream=canvas.captureStream(30);
      cam.getAudioTracks().forEach(t=>stream.addTrack(t));
      screen.getAudioTracks().forEach(t=>stream.addTrack(t));
    }

    recStream=stream;
    recChunks=[];
    const preview=document.getElementById('rec-preview');
    preview.srcObject=stream;

    mediaRecorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp9,opus'});
    mediaRecorder.ondataavailable=e=>{ if(e.data.size>0) recChunks.push(e.data); };
    mediaRecorder.onstop=saveRecording;
    mediaRecorder.start(1000);

    document.getElementById('rec-start-btn').disabled=true;
    document.getElementById('rec-stop-btn').disabled=false;
    document.getElementById('rec-pause-btn').disabled=false;
    document.getElementById('rec-dot').style.display='inline-block';
    document.getElementById('rec-saved').classList.remove('show');

    recSecs=0;
    recTimer=setInterval(()=>{
      recSecs++;
      const m=String(Math.floor(recSecs/60)).padStart(2,'0');
      const s=String(recSecs%60).padStart(2,'0');
      document.getElementById('rec-timer').textContent=m+':'+s;
    },1000);

    stream.getVideoTracks()[0].addEventListener('ended',()=>stopRecording());
  } catch(e){
    alert('Could not start recording: '+e.message+'\n\nMake sure you allow screen/camera access.');
  }
}

function stopRecording(){
  if(mediaRecorder&&mediaRecorder.state!=='inactive') mediaRecorder.stop();
  if(recStream) recStream.getTracks().forEach(t=>t.stop());
  if(recTimer) clearInterval(recTimer);
  document.getElementById('rec-start-btn').disabled=false;
  document.getElementById('rec-stop-btn').disabled=true;
  document.getElementById('rec-pause-btn').disabled=true;
  document.getElementById('rec-dot').style.display='none';
  document.getElementById('rec-preview').srcObject=null;
}

function pauseRecording(){
  if(!mediaRecorder) return;
  if(mediaRecorder.state==='recording'){
    mediaRecorder.pause();
    document.getElementById('rec-pause-btn').textContent='▶ Resume';
    clearInterval(recTimer);
  } else if(mediaRecorder.state==='paused'){
    mediaRecorder.resume();
    document.getElementById('rec-pause-btn').textContent='⏸ Pause';
    recTimer=setInterval(()=>{
      recSecs++;
      const m=String(Math.floor(recSecs/60)).padStart(2,'0');
      const s=String(recSecs%60).padStart(2,'0');
      document.getElementById('rec-timer').textContent=m+':'+s;
    },1000);
  }
}

function saveRecording(){
  const blob=new Blob(recChunks,{type:'video/webm'});
  const url=URL.createObjectURL(blob);
  const saved=document.getElementById('rec-saved');
  const rv=document.getElementById('rec-result-video');
  const dl=document.getElementById('rec-dl-btn');
  rv.src=url;
  dl.href=url;
  dl.download='snipforge-recording-'+Date.now()+'.webm';
  saved.classList.add('show');
}

// ── init ──
fetch('/api/token').then(r=>r.json()).then(d=>{ window._snipToken = d.token || ''; });
function getCookie(name){const v=document.cookie.match('(^|;)\\s*'+name+'\\s*=\\s*([^;]+)');return v?v.pop():'';}

// Load user info
fetch('/api/me').then(r=>r.json()).then(u=>{
  if(u.error){window.location='/login';return;}
  window._user=u;
  const badge=document.getElementById('user-badge');
  const planColors={'free':'#8a8780','pro':'#e8420a','team':'#6b3fa0'};
  const planLabel={'free':'Free','pro':'Pro','team':'Team'};
  const pc=planColors[u.plan]||planColors.free;
  badge.innerHTML=`
    <span style="font-family:var(--mono);font-size:.72rem;color:var(--muted);display:flex;align-items:center;gap:5px">
      ${u.name.split(' ')[0]}
      <span style="font-size:.52rem;padding:1px 5px;border-radius:3px;background:var(--bg3);color:${pc};border:1px solid var(--border);text-transform:uppercase;letter-spacing:.05em;vertical-align:middle">${planLabel[u.plan]||u.plan}</span>
    </span>
    <a href="/account" style="font-family:var(--mono);font-size:.65rem;padding:4px 10px;border:1px solid var(--border);border-radius:7px;color:var(--muted);text-decoration:none;background:var(--bg3)">Account</a>
  `;
  if(u.plan==='free'){
    const bar=document.createElement('div');
    const isMobile = window.innerWidth <= 768;
    bar.style.cssText = isMobile
      ? 'position:fixed;bottom:56px;left:0;right:0;background:var(--bg2);border-top:1px solid var(--border);padding:8px 14px;display:flex;align-items:center;gap:10px;z-index:150;font-size:.78rem;box-shadow:0 -1px 0 var(--border)'
      : 'position:fixed;bottom:0;left:220px;right:0;background:var(--bg2);border-top:1px solid var(--border);padding:9px 24px;display:flex;align-items:center;gap:16px;z-index:50;font-size:.83rem;box-shadow:0 -1px 0 var(--border)';
    const mob = window.innerWidth <= 768;
    bar.innerHTML=`
      <span style="color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;font-size:${mob?'.72rem':'.83rem'}">
        ${mob ? `<strong style="color:var(--text)">${u.videos_used}/${u.videos_limit}</strong> videos used` : `Free plan: <strong style="color:var(--text)">${u.videos_used}/${u.videos_limit}</strong> videos this month`}
      </span>
      <a href="/pricing" style="flex-shrink:0;padding:${mob?'5px 12px':'6px 16px'};background:var(--accent);color:#fff;border-radius:7px;text-decoration:none;font-family:var(--cond);font-size:${mob?'.72rem':'.82rem'};font-weight:700;letter-spacing:.06em;white-space:nowrap">Upgrade →</a>
      <span onclick="this.parentElement.remove()" style="flex-shrink:0;cursor:pointer;color:var(--muted);font-size:1rem;padding:2px 6px">×</span>`;
    document.body.appendChild(bar);
  }
}).catch(()=>window.location='/login');

// Mobile menu toggle
function toggleMobMenu(){
  const menu = document.getElementById('mob-menu');
  menu.style.display = menu.style.display==='block' ? 'none' : 'block';
}
document.addEventListener('click', e=>{
  const menu = document.getElementById('mob-menu');
  const btn = document.getElementById('mob-menu-btn');
  if(menu && btn && !menu.contains(e.target) && !btn.contains(e.target)){
    menu.style.display='none';
  }
});

// Keyboard shortcut: Enter to run when file loaded
document.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    const panel = document.querySelector('.panel.active');
    if(!panel) return;
    const prefix = panel.id.replace('panel-','');
    const runBtn = document.getElementById(prefix+'-run');
    if(runBtn && !runBtn.disabled && document.activeElement.tagName !== 'INPUT') {
      runBtn.click();
    }
  }
});

// Handle login required error from API
const _origFetch = window.fetch;
window.fetch = function(...args){
  return _origFetch(...args).then(async r=>{
    if(r.status===401){
      const clone=r.clone();
      const d=await clone.json().catch(()=>({}));
      if(d.error==='login_required') window.location='/login';
    }
    return r;
  });
};

// ── Denoise ──
async function runDenoise(){
  const s=state['dn']; if(!s) return;
  const strength=document.getElementById('dn-strength').value;
  const jid=await startJob('dn',{op:'denoise',strength,out_ext:'mp4',
    filename:s.filename,size_mb:s.size_mb});
  if(jid) pollJob('dn',jid,'dn-rvideo','dn-dl',s.filename.replace(/[.][^.]+$/,'')+'_clean.mp4');
}

// ── Transcribe ──
async function runTranscribe(){
  const s=state['tc']; if(!s) return;
  const run=document.getElementById('tc-run');
  run.disabled=true; run.textContent='Transcribing…'; run.classList.add('working');
  document.getElementById('tc-progress').classList.add('show');
  document.getElementById('tc-result').classList.remove('show');
  document.getElementById('tc-log').innerHTML='';
  setProgress('tc',0);

  // Re-upload file for transcription endpoint
  const fileInput=document.getElementById('tc-file');
  if(!fileInput.files[0]){run.disabled=false;run.textContent=getRunLabel('tc');return;}
  const lang = document.getElementById('tc-language')?.value || 'auto';
  const translate = document.getElementById('tc-translate')?.value || 'none';
  const burn = document.getElementById('tc-burn-captions')?.checked || false;
  const fd=new FormData(); 
  fd.append('file',fileInput.files[0]);
  fd.append('language', lang);
  fd.append('translate_to', translate);
  fd.append('burn_captions', burn ? 'true' : 'false');
  const r=await fetch('/api/transcribe',{method:'POST',body:fd});
  const d=await r.json();
  if(d.error){log('tc',d.error,'err');run.disabled=false;run.textContent=getRunLabel('tc');run.classList.remove('working');return;}
  log('tc','Transcription started…');

  let idx=0;
  const iv=setInterval(async()=>{
    const r2=await fetch('/api/status/'+d.job_id+'?from='+idx);
    const d2=await r2.json();
    if(d2.log){d2.log.forEach(l=>log('tc',l,l.startsWith('Done')?'ok':l.startsWith('Error')?'err':'info'));idx+=d2.log.length;}
    if(d2.progress!=null) setProgress('tc',d2.progress);
    if(d2.status==='done'){
      clearInterval(iv);
      const stats=d2.stats||{};
      const text=stats.text||'';
      document.getElementById('tc-text').textContent=text;
      // Create download link for txt
      const blob=new Blob([text],{type:'text/plain'});
      const url=URL.createObjectURL(blob);
      const dl=document.getElementById('tc-dl-txt');
      dl.href=url;
      dl.download=(s.filename||'transcript').replace(/[.][^.]+$/,'')+'.txt';
      // Captioned video download (shown only when burn_captions succeeded)
      const dlVideo=document.getElementById('tc-dl-video');
      if(stats.burned && stats.video_result){
        dlVideo.href='/api/download/'+d.job_id;
        dlVideo.download=(s.filename||'video').replace(/[.][^.]+$/,'')+'_captioned.mp4';
        dlVideo.style.display='inline-flex';
      } else {
        dlVideo.style.display='none';
      }
      // Stats
      const el=document.getElementById('tc-rstats');
      el.innerHTML=`
        <div class="rstat"><div class="rstat-val">${stats.words||0}</div><div class="rstat-lbl">Words</div></div>
        <div class="rstat"><div class="rstat-val">${(stats.language||'').toUpperCase()||'—'}</div><div class="rstat-lbl">Language</div></div>
        <div class="rstat"><div class="rstat-val">${fmtTime(stats.duration||0)}</div><div class="rstat-lbl">Duration</div></div>
      `;
      // Auto-populate language dropdown with detected language
      const detectedLang = stats.detected_language || '';
      if (detectedLang) {
        const langSel = document.getElementById('tc-language');
        if (langSel) {
          // Try to match detected language code to dropdown option
          const opt = langSel.querySelector(`option[value="${detectedLang}"]`);
          if (opt) {
            langSel.value = detectedLang;
          } else {
            // Add a dynamic option showing the detected language name
            const existing = langSel.querySelector('option[data-detected]');
            if (existing) existing.remove();
            const newOpt = document.createElement('option');
            newOpt.value = detectedLang;
            newOpt.setAttribute('data-detected','1');
            newOpt.textContent = '✓ ' + detectedLang.charAt(0).toUpperCase() + detectedLang.slice(1) + ' (detected)';
            langSel.insertBefore(newOpt, langSel.firstChild);
            langSel.value = detectedLang;
          }
        }
        // Show detected language badge in the pre-existing slot
        const langNames = {en:'English',es:'Spanish',fr:'French',de:'German',it:'Italian',
          pt:'Portuguese',nl:'Dutch',ru:'Russian',ja:'Japanese',ko:'Korean',zh:'Chinese',
          ar:'Arabic',hi:'Hindi',tr:'Turkish',vi:'Vietnamese',th:'Thai',id:'Indonesian',
          sv:'Swedish',uk:'Ukrainian'};
        const langDisplay = langNames[detectedLang] || (detectedLang.charAt(0).toUpperCase()+detectedLang.slice(1));
        const badge = document.getElementById('tc-lang-detected');
        if (badge) { badge.textContent = '✓ ' + langDisplay + ' detected'; badge.style.display = 'inline'; }
      }
      document.getElementById('tc-result').classList.add('show');
      run.disabled=false; run.textContent=getRunLabel('tc'); run.classList.remove('working');
    }
    if(d2.status==='error'){
      clearInterval(iv);
      log('tc','Error: '+d2.error,'err');
      run.disabled=false; run.textContent=getRunLabel('tc'); run.classList.remove('working');
    }
  },1500);
}

function copyTranscript(){
  const text=document.getElementById('tc-text').textContent;
  navigator.clipboard.writeText(text);
  const btn=event.target; btn.textContent='Copied!';
  setTimeout(()=>btn.textContent='Copy',2000);
}

// ── Share link ──
async function shareVideo(jobId, filename){
  const r=await fetch('/api/share',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job_id:jobId,filename,title:filename,expires_days:7})});
  const d=await r.json();
  if(d.url){
    navigator.clipboard.writeText(d.url);
    alert('Share link copied!\n\n'+d.url+'\n\nExpires in 7 days.');
  } else {
    alert(d.error||'Could not create share link');
  }
}

// Add share button to result boxes after job completes
function addShareBtn(prefix, jobId, filename){
  const resultBox=document.getElementById(prefix+'-result');
  if(!resultBox) return;
  const existing=resultBox.querySelector('.share-btn');
  if(existing) existing.remove();
  const btn=document.createElement('button');
  btn.className='share-btn';
  btn.style.cssText='width:100%;padding:11px;background:rgba(0,180,255,.08);color:#00b4ff;border:1px solid rgba(0,180,255,.2);border-radius:8px;font-family:var(--cond);font-size:.88rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;margin-top:8px;transition:all .15s';
  btn.textContent='🔗 Copy Share Link';
  btn.onmouseover=()=>btn.style.background='rgba(0,180,255,.15)';
  btn.onmouseout=()=>btn.style.background='rgba(0,180,255,.08)';
  btn.onclick=()=>shareVideo(jobId, filename);
  resultBox.appendChild(btn);
}

// Override pollJob to add share button on completion
const _origPollJob = pollJob;
window.pollJob = function(prefix, jobId, videoId, dlId, dlName){
  let idx=0;
  const iv=setInterval(async()=>{
    const r=await fetch('/api/status/'+jobId+'?from='+idx);
    const d=await r.json();
    if(d.log){d.log.forEach(l=>log(prefix,l,l.startsWith('Done')?'ok':l.startsWith('Error')?'err':'info'));idx+=d.log.length;}
    if(d.progress!=null) setProgress(prefix,d.progress);
    if(d.status==='done'){
      clearInterval(iv);
      const dlUrl='/api/download/'+jobId;
      if(videoId){const v=document.getElementById(videoId);if(v)v.src=dlUrl;}
      const dl=document.getElementById(dlId);
      if(dl){dl.href=dlUrl;if(dlName)dl.download=dlName;}
      document.getElementById(prefix+'-result').classList.add('show');
      showStats(prefix,d.stats);
      const run=document.getElementById(prefix+'-run');
      if(run){run.disabled=false;run.textContent=getRunLabel(prefix);run.classList.remove('working');}
      // Add share button
      const s=state[prefix];
      if(s) addShareBtn(prefix, jobId, s.filename||dlName||'video.mp4');
    }
    if(d.status==='error'){
      clearInterval(iv);
      log(prefix,'Error: '+d.error,'err');
      const run=document.getElementById(prefix+'-run');
      if(run){run.disabled=false;run.textContent=getRunLabel(prefix);run.classList.remove('working');}
    }
  },1200);
};

['sz','tr','mt','sp','ro','cr','wm','vl','cm','cv','au','mu','dn','tc'].forEach(p=>setupUpload(p));

// Auto-open panel from URL param e.g. /?tool=compress
const _urlTool = new URLSearchParams(window.location.search).get('tool');
if(_urlTool){
  const navItem = document.querySelector(`.nav-item[data-panel="${_urlTool}"]`);
  if(navItem) switchPanel(navItem);
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Snipforge Video Toolkit")
    parser.add_argument("--port",   type=int, default=5000)
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--server", action="store_true", help="Start web server")
    parser.add_argument("--workers",type=int, default=4, help="Number of worker threads (Waitress)")
    args = parser.parse_args()

    print(f"\n✂️  Snipforge — Video Toolkit")
    print(f"   Open: http://localhost:{args.port}")

    # Try production servers first, fall back to Flask dev server
    try:
        from waitress import serve
        print(f"   Server: Waitress (production) · {args.workers} workers")
        print(f"   Ready!\n")
        serve(app, host=args.host, port=args.port, threads=args.workers)
    except ImportError:
        try:
            import gunicorn
            print(f"   Server: Gunicorn (production)")
            print(f"   Ready!\n")
            import subprocess
            subprocess.run([
                "gunicorn", "-w", str(args.workers),
                "-b", f"{args.host}:{args.port}",
                "--timeout", "300",
                "snipforge:app"
            ])
        except ImportError:
            print(f"   Server: Flask dev (install waitress for production)")
            print(f"   Run:  pip install waitress")
            print(f"   Ready!\n")
            app.run(host=args.host, port=args.port, debug=False, threaded=True)
