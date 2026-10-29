"""Run all bridge self-check groups without starting framework services."""

import os
import sys
from pathlib import Path

from selfcheck import chat, images, maintenance, memory, messages, policy, text
from selfcheck.support import CheckResults, load_plugin

HERE = Path(__file__).resolve().parents[1]
PLUGIN = Path(
    os.environ.get(
        "QQ_BOT_PLUGIN_FILE",
        str(HERE / "AstrBot/data/plugins/workbuddy_bridge/main.py"),
    )
)


def main() -> int:
    """Run groups in their established order and return the aggregate status."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - output encoding support is optional.
        pass
    mod = load_plugin(PLUGIN)
    check = CheckResults()
    policy.check_core(mod, check)
    messages.check_messages(mod, check)
    maintenance.check_attachments(mod, check)
    policy.check_group_rules(mod, check)
    text.check_text(mod, check)
    chat.check_follow(mod, check)
    images.check_generation(mod, check)
    chat.check_sparks(mod, check)
    maintenance.check_cache(mod, check)
    text.check_receipts_search(mod, check)
    images.check_names(mod, check)
    messages.check_image_context(mod, check)
    memory.check_memory(mod, check)
    print()
    print("-" * 56)
    print("ALL PASS" if check.ok else "SOME FAILED")
    return 0 if check.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
