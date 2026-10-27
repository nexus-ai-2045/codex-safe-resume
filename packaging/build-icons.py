#!/usr/bin/env python3
"""GUIを起動せず、既存の共通描画から配布アイコンを生成する。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_icons import generate_icons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='生成先フォルダー')
    args = parser.parse_args()
    paths = generate_icons(args.output)
    print('共通アイコンを生成しました: ' + ', '.join(path.name for path in paths.values()))


if __name__ == '__main__':
    main()
