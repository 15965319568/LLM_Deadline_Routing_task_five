import argparse
import asyncio
from .build import build_profiles


def main():
    parser = argparse.ArgumentParser(description='Serving profile reconstruction and continuous CPU replay')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--input', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--as-of', required=True, type=int)
    replay = sub.add_parser('replay')
    replay.add_argument('--input', required=True)
    replay.add_argument('--workload', required=True)
    replay.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'build':
        build_profiles(args.input, args.output, args.as_of)
    else:
        from .replay import replay_workload
        asyncio.run(replay_workload(args.input, args.workload, args.output))


if __name__ == '__main__':
    main()
