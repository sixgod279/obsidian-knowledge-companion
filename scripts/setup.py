#!/usr/bin/env python3
"""Create machine-local configuration and initialize a Vault without overwriting user data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("OBSIDIAN_COMPANION_CONFIG") or str(Path.home() / ".config" / "obsidian-knowledge-companion" / "config.json"))
    parser.add_argument("--vault", help="Initial Vault path; existing config is never overwritten")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    config_path = Path(args.config).expanduser().resolve()
    try:
        defaults = json.loads((root / "assets" / "config.example.json").read_text(encoding="utf-8"))
        if config_path.exists():
            if args.vault:
                raise ValueError("已有配置不会覆盖；请先查看 show-config，再明确编辑现有配置中的 vault")
            created = False
        else:
            defaults["vault"] = str(Path(args.vault or os.environ.get("OBSIDIAN_VAULT") or defaults["vault"]).expanduser().resolve())
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with config_path.open("x", encoding="utf-8") as handle:
                json.dump(defaults, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            created = True
        command = [sys.executable, str(root / "scripts" / "vault_tool.py"), "--config", str(config_path)]
        results = {}
        for operation in ("show-config", "init", "health", "check-archive"):
            run = subprocess.run(command + [operation], capture_output=True, text=True, encoding="utf-8")
            if run.returncode:
                raise RuntimeError(f"{operation} failed: {run.stdout} {run.stderr}")
            results[operation] = json.loads(run.stdout)
            if operation == "health" and not results[operation].get("ok"):
                raise RuntimeError("存在未完成事务；已保留配置和资料，请按 health 报告恢复后继续")
        print(json.dumps({"ok": True, "config": str(config_path), "config_created": created,
                          "checks": results}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
