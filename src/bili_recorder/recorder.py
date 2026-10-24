import os
import json
import asyncio
import subprocess
import time
import shutil
from datetime import datetime
from pathlib import Path
from bilibili_api import live
from bilibili_api.exceptions import ResponseCodeException
import traceback
import logging
import glob
import httpx
from contextlib import nullcontext
from .session_store import SessionStore
from .config import get_config_path, read_config, room_config
from .locking import exclusive
from .processes import STOP_EVENT, stop_process
from .naming import get_timestamp, build_output_file_path, build_merged_file_path
from .media import process_recording_session

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
        "pid": os.getpid(),
        "stopped": False,
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
    for section in ('General', 'Recording', 'PostProcess', 'Maintenance'):
        if section not in config:
            raise ValueError(f"配置文件缺少 [{section}] 段")

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
    if config['General'].getint('CheckInterval', fallback=60) <= 0:
        raise ValueError("CheckInterval 必须大于 0")
    if config['Recording'].get('Format', 'flv') not in {'flv', 'mp4', 'mkv'}:
        raise ValueError("Recording Format 只支持 flv、mp4、mkv")
    if config['PostProcess'].getint('CpuThreads', fallback=2) <= 0:
        raise ValueError("CpuThreads 必须大于 0")
    if config.getboolean('Upload', 'Enabled', fallback=False) and config['PostProcess'].get('OutputFormat', 'mp4') != 'mp4':
        raise ValueError("自动上传目前只支持最终 MP4，请设置 PostProcess OutputFormat=mp4")

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
            if STOP_EVENT.is_set():
                stop_process(process)
                return False, "stopped", file_path
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

            STOP_EVENT.wait(max(1, poll_interval))

    except Exception as e:
        logger.error(f"录制进程异常: {e}\n{traceback.format_exc()}")
        return False, "record_exception", file_path


async def process_pending_sessions(store, wake, lock_path=None):
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
                with exclusive(lock_path) if lock_path else nullcontext():
                    if session["state"] != "complete":
                        if session["parts"] and not await process_recording_session(
                                session["parts"], config, session["tag"], session["title"]):
                            session["retry_at"] = time.time() + 300
                            store.save(session)
                            continue
                        session["state"] = "complete"
                        store.save(session)
                    if not keep_source:
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
    if config.getboolean("Upload", "DeleteLocal", fallback=False):
        return  # Archival verification owns deletion in this mode.
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
        from .quark_upload import upload_pending
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
    from .cloud_sync import completed_files
    completed = completed_files(Path(save_dir))
    files = [f for f in files if Path(f) in completed]
    if upload_enabled:
        files = [f for f in files if not upload_pending(Path(f), upload_ledger)]
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
    files = [f for f in files if Path(f) in completed and os.path.isfile(f)]
    if upload_enabled:
        files = [f for f in files if not upload_pending(Path(f), upload_ledger)]

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


async def main(config_path=None, room_id_override=None):
    logger.info("=== BiliLive Recorder (Linux Server Edition - Stable) ===")

    config_path = config_path or get_config_path()
    config = read_config(config_path)
    if room_id_override is not None:
        config = room_config(config, room_id_override)
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
    pending_task = asyncio.create_task(process_pending_sessions(store, wake, config["PostProcess"].get("LockFile")))
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
        if heartbeat_task.done():
            heartbeat_task.result()
        if pending_task.done():
            pending_task.result()
        config = read_config(config_path)
        if room_id_override is not None:
            config = room_config(config, room_id_override)
        if Path(config['General']['SaveDir']).resolve() != store.directory.parent:
            raise ValueError("运行中修改 SaveDir 需要重新启动房间进程；原场次已保留")
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
                session_tag = f"{datetime.now():%Y%m%d_%H%M%S_%f}_{room_id}"
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
