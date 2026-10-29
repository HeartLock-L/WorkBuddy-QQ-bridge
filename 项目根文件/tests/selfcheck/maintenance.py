"""Offline maintenance regression scenarios for the QQ bridge."""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path


def check_attachments(mod, check):
    """Run the attachments regression scenarios."""
    try:
        from PIL import Image
    except ImportError:
        check("GIF 读取依赖 Pillow 已安装", False, True)
    else:
        check("GIF 读取依赖 Pillow 已安装", True, True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "source.gif"
            Image.new("RGB", (16, 16), "red").save(
                original,
                save_all=True,
                append_images=[Image.new("RGB", (16, 16), "blue")],
                duration=100,
                loop=0,
            )
            saved, preview, description, _size = mod.attachments._save_one(
                original,
                root / "saved.gif",
                mod.attachments.MAX_ATTACHMENT_BYTES,
                time.monotonic() + 5,
                threading.Event(),
            )
            check("GIF 原文件完整保留", Path(saved).read_bytes(), original.read_bytes())
            check("GIF 时序预览可读取", Path(preview).is_file(), True)
            check("GIF 预览说明包含解析范围", "共解析 2 帧" in description, True)
            check("GIF 预览说明抽样局限", "不是完整播放" in description, True)
    mod = sys.modules[mod._clean_old_temp.__module__]
    print("== 过期附件清理（保留 2 天） ==")
    attachment_work = Path(tempfile.mkdtemp()).resolve()
    original_work, original_attachments = mod.WORK_DIR, mod.ATTACHMENT_ROOT
    mod.WORK_DIR = attachment_work
    tmp = attachment_work / "收到的附件"
    mod.ATTACHMENT_ROOT = tmp
    old_ts = time.time() - 3 * 86400

    (tmp / "甲").mkdir(parents=True, exist_ok=True)
    keep = tmp / "甲" / "new.jpg"
    keep.write_text("x")

    (tmp / "乙").mkdir(parents=True, exist_ok=True)
    gone = tmp / "乙" / "old.jpg"
    gone.write_text("x")
    os.utime(gone, (old_ts, old_ts))

    removed = mod._clean_old_attachments()
    check("删掉过期文件数量", removed, 1)
    check("过期文件已消失", gone.exists(), False)
    check("未过期文件保留", keep.exists(), True)
    check("空目录被回收", (tmp / "乙").exists(), False)
    check("非空目录保留", (tmp / "甲").exists(), True)

    outside = Path(tempfile.mkdtemp()) / "用户产出"
    outside.mkdir(parents=True, exist_ok=True)
    artifact = outside / "成果.txt"
    artifact.write_text("x")
    os.utime(artifact, (old_ts, old_ts))
    mod._clean_old_attachments()
    check("用户产出绝不受影响", artifact.exists(), True)
    mod.WORK_DIR, mod.ATTACHMENT_ROOT = original_work, original_attachments

    print()


def check_cache(mod, check):
    """Run the cache regression scenarios."""
    mod = sys.modules[mod._clean_old_temp.__module__]
    print("== temp 媒体缓存清理：路径守卫（真实目录绝不参与测试）==")
    # 用 tmp 造假 temp：父目录链必须真的是 .../AstrBot/data/temp，
    # 否则守卫生效，测不到东西 —— 所以这里按同样的层级搭出来。
    fake_root = (
        Path(tempfile.mkdtemp(prefix="astrbotsim_")).resolve()
        / "AstrBot"
        / "data"
        / "temp"
    )
    fake_root.mkdir(parents=True)
    try:
        real_root = mod.TEMP_ROOT
        real_work = mod.WORK_DIR
        mod.WORK_DIR = fake_root.parents[2]
        real_keep = mod.TEMP_KEEP_DAYS
        real_cap = mod.TEMP_CLEAN_MAX_FILES

        old_f = fake_root / "media_old.jpg"
        new_f = fake_root / "media_new.jpg"
        sub_d = fake_root / "somedir"
        old_f.write_bytes(b"x" * 4096)
        new_f.write_bytes(b"y" * 8192)
        sub_d.mkdir()
        (sub_d / "inner.txt").write_bytes(b"z" * 1024)
        # 老文件改mtime 到2 天前，新文件保持现在
        old_ts = time.time() - 3 * 86400
        os.utime(old_f, (old_ts, old_ts))

        mod.TEMP_ROOT = fake_root
        mod.TEMP_KEEP_DAYS = 1
        mod.TEMP_CLEAN_MAX_FILES = 2000

        removed, freed = mod._clean_old_temp()
        check("删掉超期文件 1 个", removed, 1)
        check("释放量=4096", freed, 4096)
        check("超期文件已消失", old_f.exists(), False)
        check("当天文件保留", new_f.exists(), True)
        check("子目录不被删", sub_d.is_dir(), True)
        check("_temp_usage 跳过非维护子目录", mod._temp_usage(), 8192)
        check("重复调用无残留（幂等）", mod._clean_old_temp(), (0, 0))

        print("-- 路径守卫：以下每条都必须拒绝 --")
        for label, bad in [
            ("目录名非temp", fake_root.parent / "astrbot"),
            ("父目录不是data", fake_root.parent.parent / "AstrBot"),
            ("路径不存在", Path("F:/ZZZ_no_such/data/temp")),
        ]:
            mod.TEMP_ROOT = bad
            check(f"{label}-> 拒绝", mod._clean_old_temp(), (0, 0))

        mod.TEMP_ROOT = fake_root
        mod.TEMP_KEEP_DAYS = 0
        check("KEEP_DAYS=0 -> 不清", mod._clean_old_temp(), (0, 0))

        mod.TEMP_KEEP_DAYS = 1
        mod.TEMP_CLEAN_MAX_FILES = 2000
        # 单次上限：造 10 个超期文件，cap=3 只能删 3 个
        for i in range(10):
            f = fake_root / f"bulk_{i}.jpg"
            f.write_bytes(b"b" * 128)
            os.utime(f, (old_ts, old_ts))
        mod.TEMP_CLEAN_MAX_FILES = 3
        r_cap, _ = mod._clean_old_temp()
        check("单次上限生效(<=3)", r_cap <= 3, True)

        mod.TEMP_ROOT, mod.TEMP_KEEP_DAYS, mod.TEMP_CLEAN_MAX_FILES = (
            real_root,
            real_keep,
            real_cap,
        )
    finally:
        mod.WORK_DIR = real_work
        shutil.rmtree(fake_root.parents[2], ignore_errors=True)

    print()
