"""Artifact-backed gateway; profile replacement and admission share one lock."""
import copy
from collections import deque
from pathlib import Path
from vllm_router.deadline.gateway import DeadlineGateway
from vllm_router.deadline.profiles import Surface
from .loader import load_profiles
from .storage import read_json, read_yaml


def effective(records):
    output = copy.deepcopy(records)
    for row in output:
        if not row['usable']:
            row['surface']['service_us'] = [[0 for _ in row['surface']['tokens']] for _ in row['surface']['decodes']]
    return output


class ServingGateway(DeadlineGateway):
    def __init__(self, source_dir, profile_dir, clock=None, transport=None, tokenizers=None):
        self.deployment = read_yaml(Path(source_dir) / 'deployment.yaml')
        records = load_profiles(Path(profile_dir), self.deployment)
        configuration = dict(self.deployment['runtime'], require_support=True, endpoints=effective(records))
        super().__init__(configuration, clock=clock, transport=transport, tokenizers=tokenizers)
        if read_json(Path(profile_dir) / 'profiles.json')['as_of_us'] > self.clock.now_us():
            raise ValueError('Profile uses future evidence')

    async def reload(self, profile_dir):
        directory = Path(profile_dir)
        records = effective(load_profiles(directory, self.deployment))
        if read_json(directory / 'profiles.json')['as_of_us'] > self.clock.now_us():
            raise ValueError('Profile uses future evidence')
        # Parse every target before mutating any live endpoint.
        surfaces = {p['endpoint_id']: Surface(p['surface']) for p in records}
        async with self.router.lock:
            for profile in records:
                eid = profile['endpoint_id']
                self.router.epochs[eid] += 1
                self.router.feedback.factors[eid] = 1.0
                self.router.feedback.samples[eid] = deque(maxlen=self.router.feedback.policy['window_samples'])
                self.router.feedback.seen = {key for key in self.router.feedback.seen if key[0] != eid}
                for row in self.router.capacity.requests.values():
                    if row.endpoint == eid and row.stage == 'prefill':
                        row.work_us = surfaces[eid].estimate(8, 0)
                self.router.profiles[eid] = profile
                self.router.surfaces[eid] = surfaces[eid]
            self.config['endpoints'] = records

