"""
main.py — Pipeline orchestrator
Runs all 7 stages: scrape → tag → enrich → research → score → output → track
Designed for both local runs and GitHub Actions CI.
"""

import sys
import os
import time
from pathlib import Path

# Always run relative to this script's directory so relative paths work
os.chdir(Path(__file__).parent)

import db
from output import run as run_output


def main() -> int:
    """
    Run the full pipeline.
    Returns exit code: 0 = success, 1 = fatal error.
    """
    start = time.time()
    print("=" * 64)
    print("  INSIDER TRACKER — Daily Run")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 64)

    # Ensure DB schema is up to date
    db.init_db()

    # ── Stages 1-6: scrape → output (+ HTML write + track update) ────────────
    try:
        j_path, md_path = run_output(config_path="config.yaml")
        if j_path is None:
            print("[main] No picks today — pipeline ran clean, nothing to save.")
        else:
            print(f"[main] Reports: {j_path.name}, {md_path.name}")
    except Exception as e:
        print(f"[main] FATAL pipeline error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.time() - start
    print(f"\n[main] Done in {elapsed:.1f}s")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
