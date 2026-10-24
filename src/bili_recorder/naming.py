"""Stable session paths, independent of process and cloud providers."""
from datetime import datetime
import os


def get_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_title(title):
    """保留中文，过滤非法文件名字符。"""
    illegal_chars = '<>:"/\\|?*\n\r\t'
    cleaned = ''.join(c for c in title if c not in illegal_chars).strip()
    return cleaned or "untitled"


def build_output_file_path(config, title, session_tag=None, part_index=1):
    save_dir = config['General']['SaveDir']
    os.makedirs(save_dir, exist_ok=True)

    timestamp = session_tag or get_timestamp()
    safe_title = sanitize_title(title)
    part_suffix = f"_part{part_index:03d}"
    filename = f"{timestamp}_{safe_title}{part_suffix}.{config['Recording']['Format']}"
    return os.path.join(save_dir, filename)


def build_merged_file_path(config, session_tag, session_title):
    save_dir = config['General']['SaveDir']
    ext = config['Recording']['Format']
    safe_title = sanitize_title(session_title)
    return os.path.join(save_dir, f"{session_tag}_{safe_title}.merged.{ext}")
