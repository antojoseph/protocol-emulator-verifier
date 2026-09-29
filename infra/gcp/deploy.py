#!/usr/bin/env python3
"""Create an isolated project. No existing project is a valid initial target.

Every GCP command uses explicit account/project flags; the CLI defaults are never changed.
The on-disk receipt permits continuation ONLY in the project this invocation created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def release_bundle(destination):
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    names = {n for n in tracked if n and not n.startswith(('reports/', 'docs/', 'tests/', '.github/'))}
    names.update(p.relative_to(ROOT).as_posix() for folder in ('service', 'infra/gcp')
                 for p in (ROOT / folder).rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    with tarfile.open(destination, 'w:gz') as archive:
        for name in sorted(names):
            path = ROOT / name
            if path.is_symlink():
                raise ValueError('Release source may not contain symlinks')
            archive.add(path, arcname=name, recursive=False)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class Deployment:
    def __init__(self, receipt, state, *, runner=subprocess.run):
        self.receipt, self.state, self.runner = Path(receipt), state, runner

    def save(self):
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=2) + '\n')
        temporary.replace(self.receipt)

    def gcloud(self, *args, check=True):
        command = ['gcloud', *args, '--account=' + self.state['account'],
                   '--project=' + self.state['project'], '--quiet']
        # Log command arguments only; no credential/token commands are used.
        print('+', ' '.join(command), flush=True)
        result = self.runner(command, check=False, capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result

    def validate_target(self):
        data = json.loads(self.gcloud('projects', 'describe', self.state['project'], '--format=json').stdout)
        if str(data['projectNumber']) != self.state.get('number') or data.get('labels', {}).get('asic-isolation') != self.state['nonce']:
            raise ValueError('Refusing a project not owned by this deployment receipt')

    def step(self, name, *args):
        if name in self.state['completed']:
            return
        self.validate_target()
        self.gcloud(*args)
        self.state['completed'].append(name)
        self.save()

    def create(self):
        # Listing must succeed. An auth/permission/network failure is never treated as nonexistence.
        projects = json.loads(self.gcloud('projects', 'list', '--format=json').stdout)
        if any(p['projectId'] == self.state['project'] for p in projects):
            raise ValueError('Existing projects are forbidden; choose a fresh project ID')
        args = ['projects', 'create', self.state['project'], '--name=ASIC Verifier Isolated',
                '--labels=asic-isolation=' + self.state['nonce']]
        if self.state.get('organization'):
            args.append('--organization=' + self.state['organization'])
        self.gcloud(*args)
        data = json.loads(self.gcloud('projects', 'describe', self.state['project'], '--format=json').stdout)
        self.state['number'] = str(data['projectNumber'])
        self.save()

    def provision(self):
        p, zone, region = self.state['project'], self.state['zone'], self.state['region']
        bucket = p + '-evidence'
        service_account = 'asic-worker@' + p + '.iam.gserviceaccount.com'
        self.step('billing', 'billing', 'projects', 'link', p, '--billing-account=' + self.state['billing'])
        self.step('apis', 'services', 'enable', 'compute.googleapis.com', 'iam.googleapis.com',
                  'iap.googleapis.com', 'storage.googleapis.com')
        self.step('network', 'compute', 'networks', 'create', 'asic-only', '--subnet-mode=custom')
        self.step('subnet', 'compute', 'networks', 'subnets', 'create', 'asic-only', '--network=asic-only',
                  '--region=' + region, '--range=10.83.0.0/24')
        self.step('iap-firewall', 'compute', 'firewall-rules', 'create', 'asic-iap-ssh', '--network=asic-only',
                  '--direction=INGRESS', '--action=ALLOW', '--rules=tcp:22',
                  '--source-ranges=35.235.240.0/20', '--target-tags=asic-worker')
        self.step('service-account', 'iam', 'service-accounts', 'create', 'asic-worker', '--display-name=ASIC worker only')
        self.step('bucket', 'storage', 'buckets', 'create', 'gs://' + bucket, '--location=' + region,
                  '--uniform-bucket-level-access', '--public-access-prevention')
        for role in ('roles/storage.objectCreator', 'roles/storage.objectViewer'):
            self.step(role, 'storage', 'buckets', 'add-iam-policy-binding', 'gs://' + bucket,
                      '--member=serviceAccount:' + service_account, '--role=' + role)
        self.step('iap-user', 'projects', 'add-iam-policy-binding', p,
                  '--member=user:' + self.state['account'], '--role=roles/iap.tunnelResourceAccessor')
        self.step('login-user', 'projects', 'add-iam-policy-binding', p,
                  '--member=user:' + self.state['account'], '--role=roles/compute.osAdminLogin')
        self.step('act-as', 'iam', 'service-accounts', 'add-iam-policy-binding', service_account,
                  '--member=user:' + self.state['account'], '--role=roles/iam.serviceAccountUser')
        release = self.receipt.parent / 'release.tar.gz'
        if not self.state.get('release_sha'):
            self.state['release_sha'] = release_bundle(release)
            self.save()
        if not release.is_file() or hashlib.sha256(release.read_bytes()).hexdigest() != self.state['release_sha']:
            raise ValueError('Release archive changed; refusing to deploy different bytes')
        release_object = 'releases/' + self.state['release_sha'] + '.tar.gz'
        self.step('release', 'storage', 'cp', str(release), 'gs://' + bucket + '/' + release_object,
                  '--if-generation-match=0')
        self.step('data-disk', 'compute', 'disks', 'create', 'asic-data', '--zone=' + zone,
                  '--size=200GB', '--type=pd-ssd', '--labels=asic-isolation=' + self.state['nonce'])
        self.step('snapshot-policy', 'compute', 'resource-policies', 'create', 'snapshot-schedule', 'asic-daily',
                  '--region=' + region, '--daily-schedule', '--start-time=04:00',
                  '--max-retention-days=7', '--on-source-disk-delete=keep-auto-snapshots')
        self.step('snapshot-disk', 'compute', 'disks', 'add-resource-policies', 'asic-data', '--zone=' + zone,
                  '--resource-policies=asic-daily')
        self.step('worker', 'compute', 'instances', 'create', 'asic-worker-1', '--zone=' + zone,
                  '--machine-type=' + self.state['machine'], '--image-family=ubuntu-2404-lts-amd64',
                  '--image-project=ubuntu-os-cloud', '--boot-disk-size=30GB', '--boot-disk-type=pd-balanced',
                  '--no-boot-disk-auto-delete', '--disk=name=asic-data,device-name=asic-data,auto-delete=no',
                  '--network=asic-only', '--subnet=asic-only', '--tags=asic-worker',
                  '--service-account=' + service_account, '--scopes=cloud-platform',
                  '--labels=asic-isolation=' + self.state['nonce'], '--shielded-secure-boot',
                  '--metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE,asic-bucket=' + bucket +
                  ',asic-release-sha=' + self.state['release_sha'] + ',asic-release-object=' + release_object,
                  '--metadata-from-file=startup-script=' + str(ROOT / 'infra/gcp/startup.sh'))
        self.state.update(bucket=bucket, instance='asic-worker-1')
        self.save()
        print('Created isolated resources. Bootstrap and full verifier validation are still required.')
        print('Receipt:', self.receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', default='anto@eigenlabs.org')
    parser.add_argument('--project')
    parser.add_argument('--billing-account')
    parser.add_argument('--organization')
    parser.add_argument('--zone', default='us-central1-a')
    parser.add_argument('--machine', default='c3-standard-8')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    if args.resume:
        state = json.loads(args.resume.read_text())
        deployment = Deployment(args.resume, state)
        deployment.validate_target()
    else:
        if not args.project or not re.fullmatch(r'asic-verifier-[a-z0-9-]{6,16}', args.project):
            parser.error('--project must be a NEW asic-verifier-* project ID (max 30 chars)')
        if not args.billing_account:
            parser.error('--billing-account is required')
        receipt = ROOT / '.runs/gcp-deployment' / args.project / 'receipt.json'
        if receipt.exists():
            parser.error('Receipt already exists; inspect it and use --resume')
        state = {'project': args.project, 'account': args.account, 'billing': args.billing_account,
                 'organization': args.organization, 'zone': args.zone, 'region': args.zone.rsplit('-', 1)[0],
                 'machine': args.machine, 'nonce': secrets.token_hex(12), 'completed': []}
        deployment = Deployment(receipt, state)
        deployment.save()
        deployment.create()
    deployment.provision()


if __name__ == '__main__':
    main()
