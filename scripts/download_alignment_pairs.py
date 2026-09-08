#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignment_harness.download import download_manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = download_manifest(args.manifest.resolve(), args.root.resolve())
    print(f"Downloaded or derived {len(result)} pair-side files")
