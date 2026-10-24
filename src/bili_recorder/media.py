"""Media validation, complete-session merge and AV1 postprocessing."""
import asyncio
import configparser
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import traceback

from .processes import run_command
from .naming import build_merged_file_path, sanitize_title

logger = logging.getLogger(__name__)
_NVENC_PROBE_CACHE = {}


def ffprobe_has_video(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nk=1:nw=1",
        path
    ]
    ret = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return ret.returncode == 0 and bool(ret.stdout.strip())


def ffprobe_duration_ok(path, min_seconds=1.0):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1",
        path
    ]
    ret = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if ret.returncode != 0:
        return False
    try:
        duration = float(ret.stdout.strip())
        return duration >= min_seconds
    except Exception:
        return False


def ffprobe_media_info(path):
    ret = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,sample_rate,channels",
        "-of", "json", path
    ], capture_output=True, text=True, check=False)
    if ret.returncode != 0:
        raise ValueError(f"无法读取媒体信息: {path}")
    data = json.loads(ret.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        # Interrupted FLVs often have no duration header; recover it from packet timestamps.
        packets = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "packet=pts_time,duration_time",
            "-of", "csv=p=0", path
        ], capture_output=True, text=True, check=True)
        starts, ends = [], []
        for line in packets.stdout.splitlines():
            fields = line.split(",")
            try:
                start = float(fields[0])
                length = float(fields[1]) if len(fields) > 1 and fields[1] != "N/A" else 0
            except ValueError:
                continue
            if math.isfinite(start) and math.isfinite(length):
                starts.append(start)
                ends.append(start + length)
        duration = max(ends) - min(starts) if starts else 0
    audio = [
        (s.get("codec_name"), s.get("sample_rate"), s.get("channels"))
        for s in data.get("streams", []) if s.get("codec_type") == "audio"
    ]
    return duration, audio


def ffprobe_video_info(path):
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=codec_name,width,height,pix_fmt,r_frame_rate", "-of", "json", path
    ], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["streams"][0]


def validate_transcoded_file(path, min_seconds=1.0, source_files=None):
    if not os.path.exists(path):
        return False, "output_missing"
    if os.path.getsize(path) <= 0:
        return False, "output_zero_size"
    if not ffprobe_has_video(path):
        return False, "output_no_video_stream"
    if not ffprobe_duration_ok(path, min_seconds=min_seconds):
        return False, "output_duration_too_short"
    if source_files:
        try:
            duration, audio = ffprobe_media_info(path)
            sources = [ffprobe_media_info(p) for p in source_files]
            if any(not math.isfinite(seconds) or seconds <= 0 for seconds, _ in sources):
                return False, "source_duration_unknown"
            expected = sum(seconds for seconds, _ in sources)
            if abs(duration - expected) > max(2, expected * 0.001):
                return False, "output_duration_mismatch"
            if any(audio != source_audio for _, source_audio in sources):
                return False, "output_audio_mismatch"
        except (ValueError, OSError) as e:
            logger.warning(f"媒体完整性校验失败: {e}")
            return False, "media_probe_failed"
    return True, "ok"


def ffmpeg_supports_encoder(encoder):
    cmd = ["ffmpeg", "-hide_banner", "-encoders"]
    ret = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return ret.returncode == 0 and encoder in ret.stdout


def probe_ffmpeg_encoder_usable(encoder):
    cached = _NVENC_PROBE_CACHE.get(encoder)
    if cached is not None:
        return cached

    if not ffmpeg_supports_encoder(encoder):
        result = (False, "encoder_not_built")
        _NVENC_PROBE_CACHE[encoder] = result
        return result

    fd, probe_path = tempfile.mkstemp(prefix=f"{encoder}_probe_", suffix=".mkv")
    os.close(fd)
    try:
        probe_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1:duration=1",
            "-frames:v", "1",
            "-an",
            "-c:v", encoder
        ]

        if "nvenc" in encoder:
            probe_cmd.extend(["-preset", "p4", "-rc", "vbr", "-cq", "30", "-b:v", "0"])

        probe_cmd.append(probe_path)

        ret = subprocess.run(probe_cmd, capture_output=True, text=True, check=False)
        wrote_output = os.path.exists(probe_path) and os.path.getsize(probe_path) > 0
        if ret.returncode == 0 and wrote_output:
            result = (True, "ok")
        else:
            stderr = (ret.stderr or "").strip().splitlines()
            reason = "output_missing" if not wrote_output else f"ffmpeg_exit_{ret.returncode}"
            for line in stderr:
                lowered = line.lower()
                if any(key in lowered for key in ["unsupported device", "no capable devices found", "cannot load libcuda", "error while opening encoder"]):
                    reason = line
                    break
            else:
                if stderr:
                    reason = stderr[-1]
            result = (False, reason)
    finally:
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except Exception:
            pass

    _NVENC_PROBE_CACHE[encoder] = result
    return result


def unique_transcode_attempts(attempts):
    seen = set()
    result = []
    for encoder, quality, preset in attempts:
        key = (encoder, quality, preset)
        if key in seen:
            continue
        seen.add(key)
        result.append((encoder, quality, preset))
    return result


def resolve_transcode_attempts(config):
    auto_prefer_nvenc = config['PostProcess'].get('AutoPreferNvenc', 'true').lower() == 'true'

    nvenc_primary = config['PostProcess'].get('NvencVideoEncoder', 'av1_nvenc')
    nvenc_primary_quality = config['PostProcess'].get('NvencQuality', '34')
    nvenc_primary_preset = config['PostProcess'].get('NvencPreset', 'p5')
    nvenc_fallback = config['PostProcess'].get('NvencFallbackVideoEncoder', '')
    nvenc_fallback_quality = config['PostProcess'].get('NvencFallbackQuality', '23')
    nvenc_fallback_preset = config['PostProcess'].get('NvencFallbackPreset', 'p4')

    cpu_primary = config['PostProcess'].get('VideoEncoder', 'libsvtav1')
    cpu_primary_quality = config['PostProcess'].get('VideoQuality', '34')
    cpu_primary_preset = config['PostProcess'].get('Preset', '6')
    cpu_fallback = config['PostProcess'].get('FallbackVideoEncoder', '')
    cpu_fallback_quality = config['PostProcess'].get('FallbackVideoQuality', '23')
    cpu_fallback_preset = config['PostProcess'].get('FallbackPreset', 'medium')

    attempts = []

    if auto_prefer_nvenc:
        for encoder, quality, preset in [
            (nvenc_primary, nvenc_primary_quality, nvenc_primary_preset),
            (nvenc_fallback, nvenc_fallback_quality, nvenc_fallback_preset),
        ]:
            if not encoder:
                continue
            usable, reason = probe_ffmpeg_encoder_usable(encoder)
            if usable:
                attempts.append((encoder, quality, preset))
            else:
                logger.info(f"NVENC 编码器不可用，跳过 {encoder}: {reason}")

    attempts.extend([
        (cpu_primary, cpu_primary_quality, cpu_primary_preset),
        (cpu_fallback, cpu_fallback_quality, cpu_fallback_preset),
    ])
    return unique_transcode_attempts([a for a in attempts if a[0]])


def build_transcode_cmd(record_file, out_file, encoder, quality, preset, cpu_threads):
    base = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-fflags", "+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", record_file,
        "-map", "0:v:0", "-map", "0:a?"
    ]
    if out_file.endswith(".mp4"):
        base.extend(["-movflags", "+faststart"])

    if encoder == "libsvtav1":
        return base + [
            "-c:v", encoder, "-preset", preset, "-crf", quality, "-g", "60",
            "-pix_fmt", "yuv420p10le", "-svtav1-params", f"lp={cpu_threads}",
            "-c:a", "copy", out_file
        ]

    if "libx265" in encoder:
        x265_params = f"pools=1:frame-threads={max(1, min(2, cpu_threads))}"
        return base + [
            "-threads", str(cpu_threads),
            "-c:v", "libx265",
            "-preset", preset,
            "-crf", quality,
            "-x265-params", x265_params,
            "-c:a", "copy",
            out_file
        ]

    if "libx264" in encoder:
        return base + [
            "-threads", str(cpu_threads),
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", quality,
            "-c:a", "copy",
            out_file
        ]

    if encoder in {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}:
        return base + [
            "-c:v", encoder,
            "-g", "60",
            "-preset", preset,
            "-rc", "vbr",
            "-cq", quality,
            "-b:v", "0",
            "-c:a", "copy",
            out_file
        ]

    return base + [
        "-c:v", encoder,
        "-c:a", "copy",
        out_file
    ]


def run_low_priority_command(cmd, nice_level=10, use_nice=True, use_ionice=True):
    """低优先级运行命令，减少对同机其他业务影响。"""
    final_cmd = cmd
    if use_ionice and shutil.which("ionice"):
        final_cmd = ["ionice", "-c2", "-n7"] + final_cmd

    if use_nice and shutil.which("nice"):
        final_cmd = ["nice", "-n", str(max(0, min(19, int(nice_level))))] + final_cmd

    return run_command(final_cmd)


def merge_recording_parts_if_needed(parts, config, session_tag, session_title):
    """默认先合并分段，再转码。"""
    valid_parts = [p for p in parts if os.path.exists(p) and os.path.getsize(p) > 0]
    if not valid_parts:
        return None, "no_valid_parts"
    if len(valid_parts) != len(parts):
        return None, "missing_recording_parts"

    if len(valid_parts) == 1:
        return valid_parts[0], "single_part"

    video = [ffprobe_video_info(p) for p in valid_parts]
    if any(info != video[0] for info in video[1:]):
        return None, "incompatible_video_streams"

    merged_file = build_merged_file_path(config, session_tag, session_title)
    list_file = f"{merged_file}.concat.txt"

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in valid_parts:
                escaped = os.path.abspath(p).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
                seconds, _ = ffprobe_media_info(p)
                if seconds <= 0 or not math.isfinite(seconds):
                    return None, "source_duration_unknown"
                f.write(f"duration {seconds:.9f}\n")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
        ]
        if merged_file.endswith('.mp4'):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(merged_file)
        ret = run_command(cmd)
        if ret != 0:
            return None, f"merge_failed_exit_{ret}"

        valid, reason = validate_transcoded_file(merged_file, source_files=valid_parts)
        if not valid:
            return None, f"merge_invalid_{reason}"

        return merged_file, "merged"
    except Exception as e:
        logger.error(f"分段合并异常: {e}\n{traceback.format_exc()}")
        return None, "merge_exception"
    finally:
        try:
            if os.path.exists(list_file):
                os.remove(list_file)
        except Exception:
            pass


def transcode_after_recording_if_enabled(record_file, config):
    """录后离线转码：优先 NVENC，探测失败时自动回退 CPU。"""
    enabled = config['PostProcess'].get('TranscodeEnabled', 'true').lower() == 'true'
    if not enabled:
        return True, "disabled"

    if not os.path.exists(record_file):
        logger.warning(f"录后转码跳过：源文件不存在 {record_file}")
        return False, "source_missing"

    out_ext = config['PostProcess'].get('OutputFormat', 'mkv')
    keep_source = config['PostProcess'].get('KeepSource', 'false').lower() == 'true'
    min_probe_seconds = float(config['PostProcess'].get('MinValidDurationSeconds', '3'))

    cpu_threads = int(config['PostProcess'].get('CpuThreads', '2'))
    use_nice = config['PostProcess'].get('UseNice', 'true').lower() == 'true'
    nice_level = int(config['PostProcess'].get('NiceLevel', '10'))
    use_ionice = config['PostProcess'].get('UseIonice', 'true').lower() == 'true'

    attempts = resolve_transcode_attempts(config)

    base, _ = os.path.splitext(record_file)
    out_file = f"{base}.transcoded.{out_ext}"
    partial_file = f"{base}.transcoded.partial.{out_ext}"

    if os.path.exists(out_file):
        valid, reason = validate_transcoded_file(out_file, min_seconds=min_probe_seconds,
                                                source_files=[record_file])
        if valid:
            return True, "already_transcoded"
        logger.error(f"已有最终文件校验失败，保留现场: {out_file}, {reason}")
        return False, "existing_output_invalid"

    for idx, (encoder, quality, preset) in enumerate(attempts, start=1):
        logger.info(
            f"开始录后转码(尝试 {idx}/{len(attempts)}): {os.path.basename(record_file)} -> {os.path.basename(out_file)} | "
            f"encoder={encoder}, threads={cpu_threads}, nice={use_nice}, ionice={use_ionice}"
        )
        cmd = build_transcode_cmd(record_file, partial_file, encoder, quality, preset, cpu_threads)

        try:
            ret = run_low_priority_command(cmd, nice_level=nice_level, use_nice=use_nice, use_ionice=use_ionice)
            if ret != 0:
                logger.error(f"录后转码失败，返回码 {ret}，encoder={encoder}")
                if os.path.exists(partial_file):
                    try:
                        os.remove(partial_file)
                    except Exception:
                        pass
                continue

            valid, reason = validate_transcoded_file(
                partial_file, min_seconds=min_probe_seconds, source_files=[record_file]
            )
            if not valid:
                logger.error(f"录后转码产物校验失败: reason={reason} encoder={encoder}")
                if os.path.exists(partial_file):
                    try:
                        os.remove(partial_file)
                    except Exception:
                        pass
                continue

            os.replace(partial_file, out_file)
            src_size = os.path.getsize(record_file) if os.path.exists(record_file) else 0
            out_size = os.path.getsize(out_file) if os.path.exists(out_file) else 0
            ratio = (1 - out_size / src_size) * 100 if src_size > 0 else 0
            logger.info(f"录后转码完成并校验通过: encoder={encoder}, 节省 {ratio:.2f}% 空间")

            if not keep_source and os.path.exists(record_file):
                os.remove(record_file)
                logger.info(f"转码产物有效，已删除源文件: {record_file}")

            return True, f"ok_{encoder}"

        except Exception as e:
            logger.error(f"录后转码异常: {e}\n{traceback.format_exc()}")

    logger.error("录后转码所有尝试均失败，已保留源文件")
    return False, "all_transcode_attempts_failed"


async def process_recording_session(parts, config, session_tag, session_title):
    """串行处理已下播的场次，不阻塞下一场的录制。"""
    try:
        extension = config['PostProcess'].get('OutputFormat', 'mp4')
        final = os.path.join(config['General']['SaveDir'],
                             f"{session_tag}_{sanitize_title(session_title)}.{extension}")
        enabled = config['PostProcess'].getboolean('TranscodeEnabled', fallback=True)
        if os.path.exists(final):
            if Path(final).resolve() in {Path(part).resolve() for part in parts}:
                logger.error(f"最终文件名与原流冲突，保留源文件: {final}")
                return False
            return await asyncio.to_thread(lambda: validate_transcoded_file(final, source_files=parts)[0])
        source, reason = await asyncio.to_thread(
            merge_recording_parts_if_needed, parts, config, session_tag, session_title
        )
        if reason == "incompatible_video_streams":
            ok = await asyncio.to_thread(transcode_mixed_session, parts, config, session_tag, session_title)
            output = os.path.splitext(build_merged_file_path(config, session_tag, session_title))[0] + '.transcoded.mp4'
        elif source and not enabled:
            output = final + '.partial.' + extension
            command = ['ffmpeg', '-v', 'warning', '-y', '-i', source,
                       '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', output]
            ok = await asyncio.to_thread(run_command, command) == 0
            ok = ok and await asyncio.to_thread(lambda: validate_transcoded_file(output, source_files=parts)[0])
        elif source:
            ok, reason = await asyncio.to_thread(transcode_after_recording_if_enabled, source, config)
            output = f"{os.path.splitext(source)[0]}.transcoded.{extension}"
        else:
            ok = False
        if ok:
            os.replace(output, final)
            return True
        logger.error(f"录制会话后处理失败，保留源文件: {session_tag}, {reason}")
    except Exception:
        logger.exception(f"录制会话后处理异常，保留源文件: {session_tag}")
    return False


def transcode_mixed_session(parts, config, tag, title):
    """Normalize changed video formats to AV1 before concat; never re-encode audio."""
    if not config['PostProcess'].getboolean('TranscodeEnabled', fallback=True):
        return False
    output = os.path.splitext(build_merged_file_path(config, tag, title))[0] + '.transcoded.mp4'
    if os.path.exists(output):
        return validate_transcoded_file(output, source_files=parts)[0]
    media = [ffprobe_media_info(p) for p in parts]
    if any(audio != media[0][1] for _, audio in media[1:]):
        logger.error("同场音轨格式发生变化，保留全部分段，避免损失音质")
        return False
    video = [ffprobe_video_info(p) for p in parts]
    width = min(v['width'] for v in video) // 2 * 2
    height = min(v['height'] for v in video) // 2 * 2
    work = os.path.join(config['General']['SaveDir'], '.sessions', f'{tag}.work')
    if os.path.islink(work):
        raise ValueError(f"后处理临时目录不能是符号链接: {work}")
    os.makedirs(work, exist_ok=True)
    normalized = [os.path.join(work, f'part{n:03d}.mp4') for n in range(len(parts))]
    options = config['PostProcess']
    for encoder, quality, preset in resolve_transcode_attempts(config):
        for source, target in zip(parts, normalized):
            command = build_transcode_cmd(source, target, encoder, quality, preset,
                                          options.getint('CpuThreads', 2))
            command[-1:-1] = ['-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                                    f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30']
            result = run_low_priority_command(command, nice_level=options.getint('NiceLevel', 10),
                                             use_nice=options.getboolean('UseNice', True),
                                             use_ionice=options.getboolean('UseIonice', True))
            if result != 0 or not validate_transcoded_file(target, source_files=[source])[0]:
                break
        else:
            merge_config = configparser.ConfigParser(interpolation=None)
            merge_config.read_dict({section: dict(config[section]) for section in config.sections()})
            merge_config['General']['SaveDir'] = work
            merge_config['Recording']['Format'] = 'mp4'
            merged, reason = merge_recording_parts_if_needed(normalized, merge_config, tag, title)
            if merged and validate_transcoded_file(merged, source_files=parts)[0]:
                os.replace(merged, output)
                shutil.rmtree(work)
                logger.info(f"同场不同视频格式已统一为 AV1 并合并: {output}")
                return True
            logger.error(f"AV1 分段合并失败: {reason}")
    return False
