import argparse
import asyncio
from .compile import compile_fabric
from .replay import replay

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command',required=True)
build = sub.add_parser('build')
play = sub.add_parser('replay')
for command in [build,play]:
    command.add_argument('--input',required=True)
    command.add_argument('--output',required=True)
build.add_argument('--as-of',type=int,required=True)
play.add_argument('--workload',required=True)
args = parser.parse_args()
if args.command == 'build':
    compile_fabric(args.input,args.output,args.as_of)
else:
    asyncio.run(replay(args.input,args.workload,args.output))
