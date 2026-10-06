#!/usr/bin/env python3
"""Host-side Compose backup, verification, isolated restore rehearsal and health checks.
Run from the frontend Compose directory. Uses Python's standard library only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid


def run(args, *, input=None, output=None):
    return subprocess.run(args, input=input, stdout=output or subprocess.PIPE, check=True).stdout


def compose(*args, **kwargs):
    return run(['docker', 'compose', *args], **kwargs)


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    expected = {'database.dump', 'media.tgz', 'accounting.json'}
    if manifest.get('version') != 1 or set(manifest['files']) != expected:
        raise ValueError('Unsupported or incomplete backup manifest')
    for name in expected:
        file = directory / name
        if file.is_symlink() or not file.is_file() or checksum(file) != manifest['files'][name]:
            raise ValueError(f'Backup verification failed: {name}')
    with tarfile.open(directory / 'media.tgz') as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'media' or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe media archive member')
    return manifest


def service_states():
    # Compose emits either JSON lines or an array depending on its version.
    raw = compose('ps', '--all', '--format', 'json').decode().strip()
    states = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines()]
    return {state['Service']: state for state in states}


def backup(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    staging = Path(tempfile.mkdtemp(prefix='.incomplete-', dir=root))
    services = []
    stopped = False
    try:
        compose('config', '--quiet')
        states = service_states()
        running = [name for name, value in states.items() if value.get('State') == 'running']
        if any(states.get(name, {}).get('Health') != 'healthy' for name in ('db', 'backend')):
            raise ValueError('Database and backend must be healthy and running before backup')
        images = {}
        for service in ('db', 'backend', 'frontend'):
            ids = compose('ps', '-q', service).decode().splitlines()
            images[service] = run(['docker', 'inspect', '--format', '{{.Image}}', ids[0]]).decode().strip() if ids else None
        services = [name for name in ('frontend', 'backend') if name in running]
        # Quiesce app writes, then use one-off non-server backend commands.
        compose('stop', *services)
        stopped = True
        with (staging / 'accounting.json').open('wb') as out:
            compose('run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', 'backend', 'manage.py', 'release_snapshot', output=out)
        with (staging / 'database.dump').open('wb') as out:
            compose('exec', '-T', 'db', 'sh', '-c', 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc', output=out)
        with (staging / 'media.tgz').open('wb') as out:
            compose('run', '--rm', '--no-deps', '-T', '--entrypoint', 'tar', 'backend', '-C', '/app', '-czf', '-', 'media', output=out)
        manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'images': images,
                    'files': {name: checksum(staging / name) for name in ('accounting.json', 'database.dump', 'media.tgz')}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        verify(staging)
        destination = root / stamp
        staging.rename(destination)
        print(f'Verified backup: {destination}')
    finally:
        if stopped:
            compose('start', *services)
        # Incomplete directories remain clearly marked for diagnosis; never count as backups.


def rehearse(directory, backend_image=None):
    directory = Path(directory).resolve()
    manifest = verify(directory)
    name = 'ibfs-restore-' + uuid.uuid4().hex[:12]
    media_volume = name + '-media'
    password = uuid.uuid4().hex
    image = manifest['images']['backend']
    if not image:
        raise ValueError('Backup does not record a backend image')
    created_db = created_volume = False
    try:
        run(['docker', 'volume', 'create', media_volume]); created_volume = True
        run(['docker', 'run', '--detach', '--rm', '--name', name,
             '--env', f'POSTGRES_PASSWORD={password}', '--env', 'POSTGRES_DB=ibfs_restore',
             '--tmpfs', '/var/lib/postgresql/data:rw', manifest['images']['db']]); created_db = True
        for _ in range(60):
            # PostgreSQL's image briefly starts a socket-only initialization
            # server before restarting. TCP readiness waits for the final server.
            ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'ibfs_restore'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise ValueError('Restore database did not start')
        with (directory / 'database.dump').open('rb') as source:
            subprocess.run(['docker', 'exec', '-i', name, 'pg_restore', '--exit-on-error', '--no-owner', '--no-privileges', '-U', 'postgres', '-d', 'ibfs_restore'], stdin=source, check=True)
        with (directory / 'media.tgz').open('rb') as source:
            subprocess.run(['docker', 'run', '--rm', '-i', '--user', '0', '--entrypoint', 'sh', '-v', f'{media_volume}:/app/media', image,
                            '-c', 'tar -C /app -xzf - && chown -R 1000:1000 /app/media'], stdin=source, check=True)
        command = ['docker', 'run', '--rm', '--network', f'container:{name}', '--entrypoint', 'python',
                   '-v', f'{media_volume}:/app/media', '--env', 'DEBUG=True', '--env', 'DB_HOST=127.0.0.1',
                   '--env', 'DB_NAME=ibfs_restore', '--env', 'DB_USER=postgres', '--env', f'DB_PASSWORD={password}', image, 'manage.py']
        before = json.loads(run([*command, 'release_snapshot']))
        run([*command, 'migrate', '--noinput'])
        if backend_image:
            command[command.index(image)] = backend_image
            run([*command, 'migrate', '--noinput'])
        after = json.loads(run([*command, 'release_snapshot']))
        expected = json.loads((directory / 'accounting.json').read_text())
        if before != expected:
            raise ValueError('Restored accounting snapshot differs from backup; review before release')
        if before != after:
            raise ValueError('Migrations changed accounting snapshot; review intentional changes before release')
        print('Isolated restore and migration snapshot checks passed. Production volumes were not mounted.')
    finally:
        if created_db:
            run(['docker', 'stop', name])
        if created_volume:
            run(['docker', 'volume', 'rm', media_volume])


def health(root, max_age, max_disk):
    indexed = service_states()
    for name in ('db', 'backend', 'frontend'):
        if indexed.get(name, {}).get('State') != 'running' or indexed[name].get('Health') != 'healthy':
            raise ValueError(f'{name} is not healthy')
    root = Path(root).resolve()
    candidates = sorted(root.glob('*/manifest.json'))
    if not candidates:
        raise ValueError('No completed backups found')
    latest = candidates[-1].parent
    manifest = verify(latest)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(manifest['created_at'])).total_seconds() / 3600
    if age > max_age:
        raise ValueError(f'Last completed backup is {age:.1f} hours old')
    usage = shutil.disk_usage(root)
    if 100 * usage.used / usage.total >= max_disk:
        raise ValueError('Backup disk usage exceeds configured threshold')
    print('Containers, backup checksums/age and backup disk usage passed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('backup'); p.add_argument('--directory', default=os.getenv('BACKUP_DIR', './backups'))
    for name in ('verify', 'rehearse'):
        p = subs.add_parser(name); p.add_argument('directory')
        if name == 'rehearse': p.add_argument('--backend-image', help='Candidate image to test after restoring with the recorded image')
    p = subs.add_parser('health'); p.add_argument('--directory', default=os.getenv('BACKUP_DIR', './backups')); p.add_argument('--max-age-hours', type=int, default=30); p.add_argument('--max-disk-percent', type=int, default=85)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'backup': backup(args.directory)
        elif args.command == 'verify': verify(args.directory); print('Backup checksums and media archive passed.')
        elif args.command == 'rehearse': rehearse(args.directory, args.backend_image)
        else: health(args.directory, args.max_age_hours, args.max_disk_percent)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'IBFS operations failed: {error}\n')


if __name__ == '__main__': main()
