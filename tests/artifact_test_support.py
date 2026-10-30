"""Offline fixtures shared by artifact isolation and image recovery tests."""

import asyncio
import json
import os
import random
import re
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from bridge_test_support import PLUGIN, load_definitions, load_module

MEME_LIBRARY = load_module(
    PLUGIN.with_name("meme_library.py"), "artifact_test_support_memes"
)
MEME_COLLECTION = load_module(
    PLUGIN.with_name("meme_collection.py"), "artifact_test_support_collection"
)

JIMENG = load_module(PLUGIN.with_name("jimeng.py"), "artifact_jimeng_under_test")
IMAGE_JOBS = load_module(
    PLUGIN.with_name("image_jobs.py"), "artifact_image_jobs_under_test"
)
ATTACHMENTS = load_module(
    PLUGIN.with_name("attachments.py"), "artifact_attachments_under_test"
)


def load_bridge(output_root):
    """Compile only owned methods with in-memory dependencies.

    Args:
        output_root: Temporary output directory for this test.

    Returns:
        The isolated bridge class and its globals.
    """
    names = {
        "_run_approved",
        "_collect_produced",
        "_run_draw",
        "_process_draw_job",
        "_generate_draw_job",
        "_resume_draw_jobs",
        "_draw_recovery_loop",
        "_spawn_background",
        "on_message",
        "_process_commands",
        "_reply_result",
        "_process_approval",
        "_answer_question",
    }
    component = lambda **kwargs: types.SimpleNamespace(**kwargs)
    noop = lambda *args, **kwargs: None
    namespace = {
        "asyncio": asyncio,
        "meme_library": MEME_LIBRARY,
        "meme_collection": MEME_COLLECTION,
        "threading": threading,
        "jimeng": JIMENG,
        "_describe_saved_attachments": ATTACHMENTS.describe,
        "time": time,
        "uuid": uuid,
        "Path": Path,
        "os": os,
        "json": json,
        "random": random,
        "re": re,
        "subprocess": types.SimpleNamespace(
            run=Mock(side_effect=AssertionError("CLI forbidden"))
        ),
        "logger": types.SimpleNamespace(
            info=noop, warning=noop, error=noop, exception=noop
        ),
        "OUTPUT_ROOT": output_root,
        "_safe_dir_name": lambda *args: "same-user",
        "RELAY_OUTPUT_MAX_FILES": 10,
        "RELAY_OUTPUT_MAX_MB": 20,
        "RELAY_BACK_OUTPUT": True,
        "IMAGE_GEN_ENABLED": True,
        "IMAGE_GEN_BIN": "fake-cli.exe",
        "IMAGE_GEN_TIMEOUT": 30,
        "IMAGE_GEN_DIR": str(output_root / "images"),
        "IMAGE_GEN_MODEL": "fake-model",
        "IMAGE_GEN_RATIO": "1:1",
        "IMAGE_GEN_RESOLUTION": "fake-resolution",
        "DRAW_SELFIE_PROMPT": "selfie:",
        "MessageChain": component,
        "Plain": lambda text: component(text=text),
        "Image": component,
        "File": component,
        "AstrMessageEvent": object,
        "_is_paused": lambda: False,
        "_is_empty_reply": lambda text: bool(text) and text.startswith("["),
        "EXPECTED_SELF_ID": "",
        "DM_MASTER_ONLY": True,
        "_is_master": lambda sender: sender == "same-user",
        "POKE_ENABLED": False,
        "GROUP_REQUIRE_EXPLICIT_WAKE": False,
        "SILENT_FOR_OTHERS": False,
        "NAME_WAKE_ENABLED": False,
        "RESET_KEYWORDS": (),
        "CONFIG_TOPIC_WORDS": (),
        "BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS": False,
        "PENDING_TTL_SEC": 300,
        "_hit_words": lambda *args: None,
        "_hit_group_gag": lambda *args: None,
        "_hit_approve": lambda text: text == "同意",
        "_sanitize": lambda text: text,
        "_conv_id": lambda origin, generation: origin.rsplit(":", 1)[-1],
    }
    load_definitions(
        namespace,
        functions={"_question_notice"},
        methods=names,
        undecorated_methods={"on_message"},
    )
    return namespace["Bridge"], namespace


class Event:
    """Represent one source group without loading the messaging framework."""

    def __init__(self, group):
        self.group = group
        self.unified_msg_origin = f"fake:GroupMessage:{group}"
        self.stopped = False

    def get_messages(self):
        return []

    def get_message_str(self):
        return f"/执行 create artifact for {self.group}"

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "same-user"

    def get_sender_name(self):
        return "same-name"

    def get_self_id(self):
        return "fake-bot"

    def is_private_chat(self):
        return False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class ArtifactFixture:
    """Provide temporary storage and offline dependencies for image tests."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bridge_class, self.globals = load_bridge(self.root)
        self.bridge = bridge_class()
        self.bridge._cfg = lambda key, default: default
        self.bridge._cfg_int = lambda key, default: default
        self.bridge._cfg_bool = lambda key, default: default
        self.bridge._stopped = False
        self.bridge._blacklisted = lambda _group: False
        self.bridge._pending = {}
        self.bridge._generation = {}
        self.bridge._can_send_active = lambda: True
        self.bridge._remember_group_message = lambda *args: None
        self.bridge._remember_recent_images = lambda *args: None
        self.bridge._note_group_activity = lambda *args: None
        self.bridge._maybe_draw = lambda *args: ""
        self.bridge._handle_reload_cmd = lambda *args: None
        self.bridge._handle_relay_cmd = AsyncMock(return_value=None)
        self.bridge._handle_roleplay_cmd = lambda *args: None
        self.bridge._maybe_clean_attachments = AsyncMock()
        self.bridge._collect_attachments = AsyncMock(return_value=[])
        self.bridge._send_chain_to_session = AsyncMock(return_value=True)
        self.bridge._send_to_session = AsyncMock(return_value=True)
        self.bridge._draw_busy = set()
        self.bridge._draw_jobs = IMAGE_JOBS.ImageJobs(
            self.root / "jobs",
            output_root=self.root / "images",
            selfie_prefix=self.globals["DRAW_SELFIE_PROMPT"],
        )
        self.bridge._draw_running = set()
        self.bridge._draw_slots = asyncio.Semaphore(2)
        self.bridge._background_tasks = set()
        self.bridge._jimeng = types.SimpleNamespace(
            ensure_session=Mock(return_value="123"),
            generate=Mock(side_effect=AssertionError("Generation must be mocked")),
            rename_session=Mock(return_value=False),
        )
