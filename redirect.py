"""Redirect only root's loopback management requests; never customer traffic."""
import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0,str(pathlib.Path(__file__).parent/'generated'))
import grpc
from app.stats.command import command_pb2 as stats
from transport import MARK


def rule(core_port, gateway_port, comment='farstar-inbound-multiplier'):
    return ['OUTPUT','-d','127.0.0.1/32','-p','tcp','--dport',str(core_port),
        '-m','owner','--uid-owner','0','-m','mark','!','--mark',hex(MARK)+'/0xffffffff',
        '-m','comment','--comment',comment,'-j','REDIRECT','--to-ports',str(gateway_port)]


def command(action, arguments):
    return subprocess.run(['iptables','-w','5','-t','nat',action]+arguments,
        capture_output=True,text=True,timeout=10)


def ensure(arguments):
    if command('-C',arguments).returncode:
        result=command('-I',arguments)
        if result.returncode:raise RuntimeError('Could not install the scoped management redirect')


def remove(arguments):
    while command('-C',arguments).returncode==0:
        result=command('-D',arguments)
        if result.returncode:raise RuntimeError('Could not remove the scoped management redirect')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['ensure','status'])
    parser.add_argument('config')
    args=parser.parse_args()
    cfg=json.loads(pathlib.Path(args.config).read_text())
    with grpc.insecure_channel('127.0.0.1:'+str(cfg['gateway_port'])) as channel:
        result=stats.SysStatsResponse.FromString(channel.unary_unary(
            '/xray.app.stats.command.StatsService/GetSysStats')(
            stats.SysStatsRequest().SerializeToString(),timeout=5))
        if not result.Uptime:time.sleep(.2)
    arguments=rule(cfg['core_api_port'],cfg['gateway_port'])
    if args.action=='ensure':ensure(arguments)
    print('MANAGEMENT_REDIRECT_PRESENT',command('-C',arguments).returncode==0)


if __name__=='__main__':main()
