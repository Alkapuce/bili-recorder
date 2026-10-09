import os
import json
import asyncio
import subprocess
import time
import configparser
import shutil
from datetime import datetime
from bilibili_api import live
from bilibili_api.exceptions import ResponseCodeException
import traceback
import logging
import glob
import tempfile
import httpx
import math
from session_store import SessionStore

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


async def refresh_heartbeat_periodically():
    """主事件循环存活时刷新心跳，保留录制状态及错误信息。"""
    while True:
        await asyncio.sleep(30)
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                status_info = json.load(f)
            status_info["heartbeat_ts"] = time.time()
            status_info["updated_at"] = get_timestamp()
            write_status_atomic(status_info)
        except Exception as e:
            logger.warning(f"刷新心跳失败: {e}")


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
    """获取直播间真实流地址（带重试）。bilibili-api 优先，-352 时 curl 兜底。返回 (url, error_reason)。"""
    last_exc = None
    curl_only = False

    for attempt in range(max_retries):
        if curl_only:
            url, fb_err = await _curl_get_play_url(room_id)
            if url:
                return url, ""
            logger.warning(f"curl 兜底获取流地址失败 (尝试 {attempt + 1}/{max_retries}): {fb_err}")
            last_exc = Exception(fb_err)
            if attempt < max_retries - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            continue

        try:
            room = live.LiveRoom(room_id)
            info = await room.get_room_play_url()
            play_url = info['durl'][0]['url']
            return play_url, ""
        except Exception as e:
            last_exc = e
            logger.warning(f"bilibili-api 获取流地址失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
            if _is_352_error(e):
                logger.info("bilibili-api 遇 -352 → 立即 curl 兜底获取流地址")
                curl_only = True
                url, fb_err = await _curl_get_play_url(room_id)
                if url:
                    logger.info("curl 兜底获取流地址成功")
                    return url, ""
                logger.warning(f"curl 兜底获取流地址也失败: {fb_err}，后续重试全部走 curl")
                last_exc = Exception(fb_err)

            backoff = 5 * (2 ** attempt) if (_is_352_error(e) or curl_only) else (2 ** attempt)
            if attempt < max_retries - 1:
                await asyncio.sleep(backoff)

    if curl_only:
        logger.error("get_live_url 最终失败：bilibili-api + curl 兜底均失败 → 视为风控")
        return None, "live_url_352"

    if last_exc and _is_352_error(last_exc):
        logger.error("获取直播地址最终失败：B站风控(-352)，进入熔断等待")
        return None, "live_url_352"
    if last_exc and is_dns_resolution_error(last_exc):
        logger.error("获取直播地址最终失败：DNS 解析临时失败，可能是网络波动")
    else:
        logger.error(f"获取直播地址最终失败，完整堆栈:\n{traceback.format_exc()}")

    return None, to_error_reason(last_exc or Exception("unknown"), "live_url")


def _is_352_error(exc):
    """检测是否为 B站 -352 风控错误。"""
    return isinstance(exc, ResponseCodeException) and exc.code == -352


# ── curl fallback（room/v1 接口，对未登录更宽容） ──────────────

_CURL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}

_curl_client = None

def _get_curl_client():
    global _curl_client
    if _curl_client is None:
        _curl_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    return _curl_client


async def _curl_check_live(room_id):
    """curl 兜底：通过 room/v1/Room/get_info 检查开播状态。"""
    try:
        client = _get_curl_client()
        resp = await client.get(
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": str(room_id)},
            headers=_CURL_HEADERS,
        )
        data = resp.json()
        if data.get("code") != 0:
            return False, "", f"curl_fallback_code_{data.get('code')}"
        room_info = data.get("data") or {}
        is_live = room_info.get("live_status") == 1
        title = room_info.get("title", "")
        return is_live, title, ""
    except Exception as e:
        return False, "", f"curl_fallback_{type(e).__name__}"


async def _curl_get_play_url(room_id):
    """curl 兜底：通过 room/v1/Room/playUrl 获取流地址。"""
    try:
        client = _get_curl_client()
        resp = await client.get(
            "https://api.live.bilibili.com/room/v1/Room/playUrl",
            params={"cid": str(room_id), "qn": "10000", "platform": "web"},
            headers=_CURL_HEADERS,
        )
        data = resp.json()
        if data.get("code") != 0:
            return None, f"curl_fallback_code_{data.get('code')}"
        durl = (data.get("data") or {}).get("durl") or []
        if not durl or not durl[0].get("url"):
            return None, "curl_fallback_empty_durl"
        return durl[0]["url"], ""
    except Exception as e:
        return None, f"curl_fallback_{type(e).__name__}"


async def check_live_status_with_retry(room_id, max_retries=3):
    """检查是否开播（带重试）。bilibili-api 优先，-352 时 curl 兜底。返回 (is_live, title, error_reason)。"""
    last_exc = None
    curl_only = False  # 切换后全部重试走 curl

    for attempt in range(max_retries):
        if curl_only:
            is_live, title, fb_err = await _curl_check_live(room_id)
            if not fb_err:
                logger.info(f"curl 兜底成功 (live={is_live}, title={title})")
                return is_live, title, ""
            logger.warning(f"curl 兜底失败 (尝试 {attempt + 1}/{max_retries}): {fb_err}")
            last_exc = Exception(fb_err)
            if attempt < max_retries - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            continue

        try:
            room = live.LiveRoom(room_id)
            info = await room.get_room_info()
            is_live = info['room_info']['live_status'] == 1
            title = info['room_info']['title']
            return is_live, title, ""
        except Exception as e:
            last_exc = e
            logger.warning(f"bilibili-api 状态检查失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")

            if _is_352_error(e):
                logger.info("bilibili-api 遇 -352 → 立即 curl 兜底")
                curl_only = True
                is_live, title, fb_err = await _curl_check_live(room_id)
                if not fb_err:
                    logger.info(f"curl 兜底成功 (live={is_live}, title={title})")
                    return is_live, title, ""
                logger.warning(f"curl 兜底也失败: {fb_err}，后续重试全部走 curl")
                last_exc = Exception(fb_err)

            backoff = 5 * (2 ** attempt) if (_is_352_error(e) or curl_only) else (2 ** attempt)
            if attempt < max_retries - 1:
                await asyncio.sleep(backoff)

    if curl_only:
        logger.error("check_live_status 最终失败：bilibili-api + curl 兜底均失败 → 视为风控")
        return False, "", "check_live_status_352"

    if last_exc and _is_352_error(last_exc):
        logger.error("检查直播状态最终失败：B站风控(-352)，进入熔断等待")
        return False, "", "check_live_status_352"
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
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
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

    return subprocess.run(final_cmd, check=False).returncode


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
        ret = subprocess.run(cmd, check=False).returncode
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
        source, reason = await asyncio.to_thread(
            merge_recording_parts_if_needed, parts, config, session_tag, session_title
        )
        if reason == "incompatible_video_streams":
            return await asyncio.to_thread(transcode_mixed_session, parts, config, session_tag, session_title)
        if source:
            ok, reason = await asyncio.to_thread(transcode_after_recording_if_enabled, source, config)
            if ok:
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
            merge_config = configparser.ConfigParser()
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


async def process_pending_sessions(store, wake):
    """Retry durable jobs serially; sources survive until the final output is published."""
    while True:
        wake.clear()
        for session in store.load():
            if session["state"] == "recording" or session.get("retry_at", 0) > time.time():
                continue
            config = store.config(session)
            keep_source = config["PostProcess"].getboolean("KeepSource", fallback=False)
            config["PostProcess"]["KeepSource"] = "true"
            try:
                if session["state"] != "complete":
                    if session["parts"] and not await process_recording_session(
                            session["parts"], config, session["tag"], session["title"]):
                        session["retry_at"] = time.time() + 300
                        store.save(session)
                        continue
                    session["state"] = "complete"
                    store.save(session)
                if not keep_source and config["PostProcess"].getboolean("TranscodeEnabled", fallback=True):
                    sources = session["parts"] + [build_merged_file_path(config, session["tag"], session["title"])]
                    for source in sources:
                        if os.path.exists(source):
                            os.remove(source)
                store.remove(session)
            except Exception:
                logger.exception(f"场次后处理未完成，下轮重试: {session['tag']}")
                session["retry_at"] = time.time() + 300
                store.save(session)
        try:
            await asyncio.wait_for(wake.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


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

    upload_ledger = {}
    upload_enabled = config.getboolean('Upload', 'Enabled', fallback=False)
    if upload_enabled:
        from quark_upload import upload_pending
        ledger_path = os.path.join(save_dir, '.quark-upload', 'manifest.json')
        try:
            if os.path.exists(ledger_path):
                with open(ledger_path, encoding='utf-8') as f:
                    upload_ledger = json.load(f)
        except (OSError, ValueError):
            logger.warning("上传记录不可读，本轮跳过清理")
            return

    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(save_dir, f"*.{ext}")) )

    # Raw parts and partial outputs are recovery inputs, never retention-cleanup candidates.
    files = [f for f in files if os.path.isfile(f) and f.endswith('.transcoded.mp4')]
    if upload_enabled:
        from pathlib import Path
        files = [f for f in files if not f.endswith('.transcoded.mp4')
                 or not upload_pending(Path(f), upload_ledger)]
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
    files = [f for f in files if os.path.isfile(f) and f.endswith('.transcoded.mp4')]
    if upload_enabled:
        files = [f for f in files if not f.endswith('.transcoded.mp4')
                 or not upload_pending(Path(f), upload_ledger)]

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

    update_status(room_id)
    heartbeat_task = asyncio.create_task(refresh_heartbeat_periodically())

    last_pulse = 0
    consecutive_352 = 0        # 连续 -352 风控计数
    max_352_backoff = 600      # 熔断最大等待秒数 (10分钟)
    store = SessionStore(config['General']['SaveDir'])
    wake = asyncio.Event()
    pending_task = asyncio.create_task(process_pending_sessions(store, wake))
    session_tag = None
    active_session = None
    for session in store.load():
        if session["state"] != "recording":
            continue
        if session["room_id"] != room_id:
            store.finish(session)
            continue
        if active_session is not None:
            raise ValueError("同一房间存在多个未结束场次，需要核对后恢复")
        active_session = session
        session_tag, session_title = session["tag"], session["title"]
        session_config = store.config(session)
        part_index = len(session['parts']) + 1
        logger.info(f"恢复未结束场次: {session_tag}, 已登记 {len(session['parts'])} 个分段")
    while True:
        # 每轮读取配置，支持运行中切房间/改参数
        if pending_task.done():
            pending_task.result()
        config = read_config(config_path)
        new_room_id = get_dynamic_room_id(config, room_id)
        if new_room_id != room_id and session_tag is not None:
            store.finish(active_session)
            wake.set()
            session_tag = None
        room_id = new_room_id
        interval = int(config['General'].get('CheckInterval', '60'))

        # -352 风控熔断：连续触发时指数退避拉长间隔
        if consecutive_352 > 0:
            backoff = min(interval * (2 ** consecutive_352), max_352_backoff)
            logger.info(f"B站风控熔断中 (连续 {consecutive_352} 次)，等待 {backoff}s...")
            await asyncio.sleep(backoff)
            consecutive_352 = 0  # 重置，等下一轮检查验证是否恢复

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

        # 状态查询失败不能当成下播；成功退出的 FFmpeg 也可能只是断流。
        if session_tag is not None and not is_live and not live_status_error:
            store.finish(active_session)
            wake.set()
            session_tag = None

        # -352 风控计数
        if "_352" in live_status_error:
            consecutive_352 += 1
        else:
            consecutive_352 = 0  # API 恢复正常，重置熔断计数

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

            if session_tag is None:
                if not store.load():
                    cleanup_recordings_if_needed(config)
                session_tag = get_timestamp()
                session_title = title
                session_config = config
                part_index = 1
                active_session = store.create(config, room_id, session_tag, session_title)
            config = session_config

            attempt = 0
            while True:
                current_file = build_output_file_path(config, session_title, session_tag=session_tag, part_index=part_index)
                active_session['parts'].append(current_file)
                store.save(active_session)
                update_status(room_id, is_live=True, is_recording=True, room_title=session_title, current_file=current_file)
                success, reason, record_file = await asyncio.to_thread(
                    record_stream, url, session_title, config, file_path=current_file
                )

                part_index += 1
                if success:
                    break

                attempt += 1

                if attempt > max_recover_retries:
                    logger.error(f"录制失败且超过最大自恢复次数({max_recover_retries})，本轮放弃，原因: {reason}")
                    update_status(room_id, is_live=True, is_recording=False, room_title=session_title, last_error=reason)
                    break

                logger.warning(f"录制异常，{recover_delay}s 后尝试自恢复 ({attempt}/{max_recover_retries})，原因: {reason}")
                await asyncio.sleep(recover_delay)

                is_live_retry, title_retry, retry_live_status_error = await check_live_status_with_retry(room_id)
                if not is_live_retry:
                    logger.info("自恢复前复查：主播已下播或状态检查失败，本轮结束")
                    if not retry_live_status_error:
                        store.finish(active_session)
                        wake.set()
                        session_tag = None
                    update_status(
                        room_id,
                        is_live=False,
                        is_recording=False,
                        room_title="N/A",
                        last_error=retry_live_status_error
                    )
                    break

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

            logger.info("本段录制结束，继续检查同场直播状态...")
            update_status(room_id, is_live=False, is_recording=False, room_title="N/A")

        await asyncio.sleep(min(interval, max(1, recover_delay)) if session_tag else interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n录制器已手动停止。")
    except Exception as e:
        logger.error(f"启动失败: {e}")
        raise SystemExit(1)
