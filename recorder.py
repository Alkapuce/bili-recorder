import os
import json
import asyncio
import subprocess
import time
import configparser
import shutil
from datetime import datetime
from bilibili_api import live
import traceback
import logging
import glob
import tempfile

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y%m%d_%H%M%S'
)
logger = logging.getLogger(__name__)

# 降低第三方库噪音日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("bilibili_api").setLevel(logging.WARNING)

STATUS_FILE = "status.json"
CONFIG_ENV_VAR = "BILI_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config.ini"
_NVENC_PROBE_CACHE = {}


def get_config_path():
    return os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)


def read_config(config_path=None):
    config = configparser.ConfigParser()
    target_path = config_path or get_config_path()
    loaded_files = config.read(target_path, encoding='utf-8')
    if not loaded_files:
        raise FileNotFoundError(
            f"配置文件不存在: {target_path}。请先复制 config.example.ini 为 config.ini，"
            f"或通过环境变量 {CONFIG_ENV_VAR} 指定配置文件路径。"
        )
    return config


def get_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_status_atomic(status_info):
    """原子写入状态文件，避免半截文件。"""
    tmp_file = f"{STATUS_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(status_info, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATUS_FILE)


def update_status(room_id, is_live=False, is_recording=False, room_title="N/A", last_error="", current_file=""):
    status_info = {
        "updated_at": get_timestamp(),
        "heartbeat_ts": time.time(),
        "room_id": room_id,
        "is_live": is_live,
        "is_recording": is_recording,
        "room_title": room_title if is_live else "N/A",
        "current_file": current_file,
        "last_error": last_error
    }
    try:
        write_status_atomic(status_info)
    except Exception as e:
        logger.warning(f"写入状态文件失败: {e}")


def sanitize_title(title):
    """保留中文，过滤非法文件名字符。"""
    illegal_chars = '<>:"/\\|?*\n\r\t'
    cleaned = ''.join(c for c in title if c not in illegal_chars).strip()
    return cleaned or "untitled"


def get_free_space_gb(path):
    total, used, free = shutil.disk_usage(path)
    return free / (1024 ** 3)


def check_disk_space_or_warn(save_dir, min_free_gb):
    """磁盘阈值保护：低于阈值时不启动新一轮录制。"""
    if min_free_gb <= 0:
        return True

    try:
        free_gb = get_free_space_gb(save_dir)
        if free_gb < min_free_gb:
            logger.error(f"磁盘剩余空间不足: {free_gb:.2f}GB < 阈值 {min_free_gb:.2f}GB，跳过本轮录制")
            return False
        return True
    except Exception as e:
        logger.warning(f"检查磁盘空间失败，默认继续录制: {e}")
        return True


def get_dynamic_room_id(config, current_room_id):
    """支持运行中切换房间：每轮读取 config.ini 的 RoomId。"""
    try:
        new_room_id = int(config['General']['RoomId'])
        if new_room_id != current_room_id:
            logger.info(f"检测到配置变更：监听房间从 {current_room_id} 切换为 {new_room_id}")
        return new_room_id
    except Exception as e:
        logger.warning(f"读取 RoomId 失败，保持原值 {current_room_id}: {e}")
        return current_room_id


def validate_startup_config(config):
    if 'General' not in config:
        raise ValueError("配置文件缺少 [General] 段")

    room_id_raw = config['General'].get('RoomId', '').strip()
    try:
        room_id = int(room_id_raw)
    except ValueError as exc:
        raise ValueError("RoomId 必须是正整数，请填写真实的 B 站直播间号") from exc

    if room_id <= 0:
        raise ValueError("RoomId 必须大于 0，请填写真实的 B 站直播间号")

    save_dir = config['General'].get('SaveDir', '').strip()
    if not save_dir:
        raise ValueError("SaveDir 不能为空")

    return room_id


def is_dns_resolution_error(exc):
    """判断是否为 DNS 解析类网络错误（常见于临时网络抖动）。"""
    msg = str(exc).lower()
    return (
        "temporary failure in name resolution" in msg
        or "name or service not known" in msg
        or "nodename nor servname provided" in msg
    )


def to_error_reason(exc, prefix):
    if is_dns_resolution_error(exc):
        return f"{prefix}_dns_resolution_failed"
    return f"{prefix}_{type(exc).__name__.lower()}"


async def get_live_url_with_retry(room_id, max_retries=3):
    """获取直播间真实流地址（带重试）。返回 (url, error_reason)。"""
    last_exc = None
    for attempt in range(max_retries):
        try:
            room = live.LiveRoom(room_id)
            info = await room.get_room_play_url()
            play_url = info['durl'][0]['url']
            return play_url, ""
        except Exception as e:
            last_exc = e
            logger.warning(f"获取直播地址失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    if last_exc and is_dns_resolution_error(last_exc):
        logger.error("获取直播地址最终失败：DNS 解析临时失败，可能是网络波动")
    else:
        logger.error(f"获取直播地址最终失败，完整堆栈:\n{traceback.format_exc()}")

    return None, to_error_reason(last_exc or Exception("unknown"), "live_url")


async def check_live_status_with_retry(room_id, max_retries=3):
    """检查是否开播（带重试）。返回 (is_live, title, error_reason)。"""
    last_exc = None
    for attempt in range(max_retries):
        try:
            room = live.LiveRoom(room_id)
            info = await room.get_room_info()
            is_live = info['room_info']['live_status'] == 1
            title = info['room_info']['title']
            return is_live, title, ""
        except Exception as e:
            last_exc = e
            logger.warning(f"检查直播状态失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    if last_exc and is_dns_resolution_error(last_exc):
        logger.error("检查直播状态最终失败：DNS 解析临时失败，可能是网络波动")
    else:
        logger.error(f"检查直播状态最终失败，完整错误堆栈:\n{traceback.format_exc()}")

    return False, "", to_error_reason(last_exc or Exception("unknown"), "check_live_status")


def build_output_file_path(config, title, session_tag=None, part_index=1):
    save_dir = config['General']['SaveDir']
    os.makedirs(save_dir, exist_ok=True)

    timestamp = session_tag or get_timestamp()
    safe_title = sanitize_title(title)
    part_suffix = f"_part{part_index:03d}" if part_index > 1 else ""
    filename = f"{timestamp}_{safe_title}{part_suffix}.{config['Recording']['Format']}"
    return os.path.join(save_dir, filename)


def build_merged_file_path(config, session_tag, session_title):
    save_dir = config['General']['SaveDir']
    ext = config['Recording']['Format']
    safe_title = sanitize_title(session_title)
    return os.path.join(save_dir, f"{session_tag}_{safe_title}.merged.{ext}")


def record_stream(url, title, config, file_path=None):
    """执行单次录制，返回 (success, reason, output_file)"""
    if not file_path:
        file_path = build_output_file_path(config, title)

    v_encoder = config['Recording']['VideoEncoder']
    a_encoder = config['Recording']['AudioEncoder']

    max_record_duration = int(config['Recording'].get('MaxRecordDurationSeconds', '0'))
    stall_timeout = int(config['Recording'].get('StallTimeoutSeconds', '300'))
    poll_interval = int(config['Recording'].get('ProcessPollIntervalSeconds', '5'))

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+discardcorrupt",
        "-err_detect", "ignore_err",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "-headers", "Referer: https://live.bilibili.com/\r\n",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
        "-c:v", v_encoder,
        "-c:a", a_encoder
    ]

    if v_encoder != "copy":
        v_quality = config['Recording'].get('VideoQuality', '23')
        if "nvenc" in v_encoder:
            cmd.extend(["-preset", "p4", "-rc", "vbr", "-cq", v_quality])
        elif "libx264" in v_encoder or "libx265" in v_encoder:
            cmd.extend(["-preset", "medium", "-crf", v_quality])

    cmd.append(file_path)

    logger.info(f">>> 启动录制: {title}")

    try:
        process = subprocess.Popen(cmd)
        start_ts = time.time()
        last_size = -1
        last_change_ts = start_ts

        while True:
            ret = process.poll()
            if ret is not None:
                if ret == 0:
                    logger.info(f"录制成功完成: {title}")
                    return True, "completed", file_path
                logger.error(f"录制异常结束 (返回码 {ret}): {title}")
                return False, f"ffmpeg_exit_{ret}", file_path

            now = time.time()

            if max_record_duration > 0 and now - start_ts > max_record_duration:
                logger.error(f"录制超时，准备中止 (>{max_record_duration}s): {title}")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                return False, "record_timeout", file_path

            try:
                size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                if size != last_size:
                    last_size = size
                    last_change_ts = now
                elif stall_timeout > 0 and now - last_change_ts > stall_timeout:
                    logger.error(f"检测到录制文件长时间无增长 (>{stall_timeout}s)，判定卡死并重启录制: {title}")
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return False, "stalled", file_path
            except Exception as e:
                logger.warning(f"检查录制文件大小失败: {e}")

            time.sleep(max(1, poll_interval))

    except Exception as e:
        logger.error(f"录制进程异常: {e}\n{traceback.format_exc()}")
        return False, "record_exception", file_path


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


def validate_transcoded_file(path, min_seconds=1.0):
    if not os.path.exists(path):
        return False, "output_missing"
    if os.path.getsize(path) <= 0:
        return False, "output_zero_size"
    if not ffprobe_has_video(path):
        return False, "output_no_video_stream"
    if not ffprobe_duration_ok(path, min_seconds=min_seconds):
        return False, "output_duration_too_short"
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

    nvenc_primary = config['PostProcess'].get('NvencVideoEncoder', 'hevc_nvenc')
    nvenc_primary_quality = config['PostProcess'].get('NvencQuality', '28')
    nvenc_primary_preset = config['PostProcess'].get('NvencPreset', 'p4')
    nvenc_fallback = config['PostProcess'].get('NvencFallbackVideoEncoder', 'h264_nvenc')
    nvenc_fallback_quality = config['PostProcess'].get('NvencFallbackQuality', '23')
    nvenc_fallback_preset = config['PostProcess'].get('NvencFallbackPreset', 'p4')

    cpu_primary = config['PostProcess'].get('VideoEncoder', 'libx265')
    cpu_primary_quality = config['PostProcess'].get('VideoQuality', '28')
    cpu_primary_preset = config['PostProcess'].get('Preset', 'medium')
    cpu_fallback = config['PostProcess'].get('FallbackVideoEncoder', 'libx264')
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
    return unique_transcode_attempts(attempts)


def build_transcode_cmd(record_file, out_file, encoder, quality, preset, cpu_threads):
    base = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-fflags", "+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", record_file
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

    if encoder in {"hevc_nvenc", "h264_nvenc"}:
        return base + [
            "-c:v", encoder,
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

    preexec_fn = None
    if use_nice:
        def _set_nice():
            os.nice(max(0, min(19, int(nice_level))))
        preexec_fn = _set_nice

    return subprocess.run(final_cmd, check=False, preexec_fn=preexec_fn).returncode


def merge_recording_parts_if_needed(parts, config, session_tag, session_title):
    """默认先合并分段，再转码。"""
    valid_parts = [p for p in parts if os.path.exists(p) and os.path.getsize(p) > 0]
    if not valid_parts:
        return None, "no_valid_parts"

    if len(valid_parts) == 1:
        return valid_parts[0], "single_part"

    merged_file = build_merged_file_path(config, session_tag, session_title)
    list_file = f"{merged_file}.concat.txt"

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in valid_parts:
                f.write(f"file '{os.path.abspath(p)}'\n")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy",
            merged_file
        ]
        ret = subprocess.run(cmd, check=False).returncode
        if ret != 0:
            return None, f"merge_failed_exit_{ret}"

        if not os.path.exists(merged_file) or os.path.getsize(merged_file) <= 0:
            return None, "merge_empty_output"

        # 合并成功后删除原分段，避免双倍占用
        for p in valid_parts:
            try:
                if p != merged_file and os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"删除分段失败: {p}, {e}")

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

    for idx, (encoder, quality, preset) in enumerate(attempts, start=1):
        logger.info(
            f"开始录后转码(尝试 {idx}/{len(attempts)}): {os.path.basename(record_file)} -> {os.path.basename(out_file)} | "
            f"encoder={encoder}, threads={cpu_threads}, nice={use_nice}, ionice={use_ionice}"
        )
        cmd = build_transcode_cmd(record_file, out_file, encoder, quality, preset, cpu_threads)

        try:
            ret = run_low_priority_command(cmd, nice_level=nice_level, use_nice=use_nice, use_ionice=use_ionice)
            if ret != 0:
                logger.error(f"录后转码失败，返回码 {ret}，encoder={encoder}")
                if os.path.exists(out_file):
                    try:
                        os.remove(out_file)
                    except Exception:
                        pass
                continue

            valid, reason = validate_transcoded_file(out_file, min_seconds=min_probe_seconds)
            if not valid:
                logger.error(f"录后转码产物校验失败: reason={reason} encoder={encoder}")
                if os.path.exists(out_file):
                    try:
                        os.remove(out_file)
                    except Exception:
                        pass
                continue

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


def cleanup_recordings_if_needed(config):
    """按保留天数/数量清理旧录制文件，减少长期运行占满磁盘风险。"""
    enabled = config['Maintenance'].get('CleanupEnabled', 'true').lower() == 'true'
    if not enabled:
        return

    save_dir = config['General']['SaveDir']
    keep_days = int(config['Maintenance'].get('KeepDays', '7'))
    keep_count = int(config['Maintenance'].get('KeepCount', '200'))
    ext_csv = config['Maintenance'].get('CleanupExtensions', 'flv,mp4,mkv')
    exts = [e.strip().lstrip('.').lower() for e in ext_csv.split(',') if e.strip()]

    if not os.path.isdir(save_dir):
        return

    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(save_dir, f"*.{ext}")) )

    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    now = time.time()
    deleted_count = 0

    if keep_count > 0 and len(files) > keep_count:
        for p in files[keep_count:]:
            try:
                os.remove(p)
                deleted_count += 1
                logger.info(f"清理旧录制文件(超数量): {os.path.basename(p)}")
            except Exception as e:
                logger.warning(f"清理文件失败: {p}, 错误: {e}")

    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(save_dir, f"*.{ext}")) )
    files = [f for f in files if os.path.isfile(f)]

    if keep_days > 0:
        max_age = keep_days * 86400
        for p in files:
            try:
                age = now - os.path.getmtime(p)
                if age > max_age:
                    os.remove(p)
                    deleted_count += 1
                    logger.info(f"清理旧录制文件(超天数): {os.path.basename(p)}")
            except Exception as e:
                logger.warning(f"清理文件失败: {p}, 错误: {e}")

    if deleted_count > 0:
        logger.info(f"本轮录制文件清理完成，共删除 {deleted_count} 个文件")


async def main():
    logger.info("=== BiliLive Recorder (Linux Server Edition - Stable) ===")

    config_path = get_config_path()
    config = read_config(config_path)
    room_id = validate_startup_config(config)
    interval = int(config['General']['CheckInterval'])

    logger.info(f"加载配置文件: {config_path}")
    logger.info(f"正在监听房间: {room_id}，检查间隔: {interval}秒")

    last_pulse = 0
    while True:
        # 每轮读取配置，支持运行中切房间/改参数
        config = read_config(config_path)
        room_id = get_dynamic_room_id(config, room_id)
        interval = int(config['General'].get('CheckInterval', '60'))

        max_recover_retries = int(config['Recording'].get('MaxRecoverRetries', '2'))
        recover_delay = int(config['Recording'].get('RecoverDelaySeconds', '15'))
        min_free_space_gb = float(config['General'].get('MinFreeSpaceGB', '0'))

        save_dir = config['General']['SaveDir']
        os.makedirs(save_dir, exist_ok=True)

        current_time = time.time()
        if current_time - last_pulse > 300:
            logger.info(f"[心跳] 录制服务运行中，监控房间: {room_id}")
            last_pulse = current_time

        is_live, title, live_status_error = await check_live_status_with_retry(room_id)
        update_status(
            room_id,
            is_live=is_live,
            is_recording=False,
            room_title=title,
            last_error=live_status_error
        )

        if is_live:
            if not check_disk_space_or_warn(save_dir, min_free_space_gb):
                update_status(room_id, is_live=True, is_recording=False, room_title=title, last_error="low_disk_space")
                await asyncio.sleep(interval)
                continue

            url, live_url_error = await get_live_url_with_retry(room_id)
            if not url:
                update_status(
                    room_id,
                    is_live=True,
                    is_recording=False,
                    room_title=title,
                    last_error=live_url_error or "live_url_unavailable"
                )
                await asyncio.sleep(interval)
                continue

            cleanup_recordings_if_needed(config)

            # 会话标识：同一场直播内断线重试共用同一个 session_tag
            session_tag = get_timestamp()
            session_title = title
            part_index = 1
            recorded_parts = []

            attempt = 0
            while True:
                current_file = build_output_file_path(config, session_title, session_tag=session_tag, part_index=part_index)
                update_status(room_id, is_live=True, is_recording=True, room_title=session_title, current_file=current_file)
                success, reason, record_file = record_stream(url, session_title, config, file_path=current_file)

                if os.path.exists(record_file) and os.path.getsize(record_file) > 0:
                    recorded_parts.append(record_file)

                if success:
                    break

                attempt += 1
                part_index += 1

                if attempt > max_recover_retries:
                    logger.error(f"录制失败且超过最大自恢复次数({max_recover_retries})，本轮放弃，原因: {reason}")
                    update_status(room_id, is_live=True, is_recording=False, room_title=session_title, last_error=reason)
                    break

                logger.warning(f"录制异常，{recover_delay}s 后尝试自恢复 ({attempt}/{max_recover_retries})，原因: {reason}")
                await asyncio.sleep(recover_delay)

                is_live_retry, title_retry, retry_live_status_error = await check_live_status_with_retry(room_id)
                if not is_live_retry:
                    logger.info("自恢复前复查：主播已下播或状态检查失败，本轮结束")
                    update_status(
                        room_id,
                        is_live=False,
                        is_recording=False,
                        room_title="N/A",
                        last_error=retry_live_status_error
                    )
                    break

                session_title = title_retry or session_title
                url, recover_live_url_error = await get_live_url_with_retry(room_id)
                if not url:
                    update_status(
                        room_id,
                        is_live=True,
                        is_recording=False,
                        room_title=session_title,
                        last_error=recover_live_url_error or "recover_live_url_unavailable"
                    )
                    break

            if recorded_parts:
                merged_source, merge_reason = merge_recording_parts_if_needed(recorded_parts, config, session_tag, session_title)
                if not merged_source:
                    update_status(room_id, is_live=False, is_recording=False, room_title="N/A", last_error=merge_reason)
                    logger.error(f"分段合并失败: {merge_reason}")
                else:
                    trans_ok, trans_reason = transcode_after_recording_if_enabled(merged_source, config)
                    if not trans_ok:
                        update_status(room_id, is_live=False, is_recording=False, room_title="N/A", last_error=trans_reason)

            logger.info("录制会话结束，继续监控...")
            update_status(room_id, is_live=False, is_recording=False, room_title="N/A")

        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n录制器已手动停止。")
    except Exception as e:
        logger.error(f"启动失败: {e}")
        raise SystemExit(1)
