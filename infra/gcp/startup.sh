#!/bin/bash
# First-boot provisioning for a NEW, dedicated verifier VM only.
set -euo pipefail
exec > >(tee -a /var/log/asic-bootstrap.log) 2>&1
test "$(uname -m)" = x86_64
metadata() { curl --fail --silent --show-error -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
ASIC_BUCKET=$(metadata asic-bucket)
ASIC_RELEASE_SHA=$(metadata asic-release-sha)
ASIC_RELEASE_OBJECT=$(metadata asic-release-object)
export ASIC_BUCKET ASIC_RELEASE_SHA ASIC_RELEASE_OBJECT
if [ -f /opt/asic-verifier/DEPLOY_READY ]; then
    test "$(cat /opt/asic-verifier/DEPLOY_READY)" = "$ASIC_RELEASE_SHA"
    exit 0
fi

# Never format an existing filesystem. This device is the newly created dedicated disk.
DEVICE=/dev/disk/by-id/google-asic-data
test -b "$DEVICE"
if ! blkid "$DEVICE" >/dev/null; then mkfs.ext4 -m 1 "$DEVICE"; fi
mkdir -p /data
if ! mountpoint -q /data; then mount "$DEVICE" /data; fi
if ! grep -q 'google-asic-data' /etc/fstab; then
    echo '/dev/disk/by-id/google-asic-data /data ext4 defaults 0 2' >> /etc/fstab
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io git python3-venv postgresql postgresql-client curl
systemctl stop docker.service docker.socket containerd.service
mkdir -p /data/docker /data/runs /data/postgres
# Docker 29 stores image snapshots in containerd separately from Docker's data-root.
# Keep that cache on the retained SSD too. Never overwrite an existing cache.
if [ ! -L /var/lib/containerd ]; then
    if [ -e /data/containerd ]; then
        echo 'Refusing to overwrite an existing containerd cache; inspect retained disk recovery.' >&2
        exit 1
    fi
    if [ -d /var/lib/containerd ]; then mv /var/lib/containerd /data/containerd; else mkdir /data/containerd; fi
    ln -s /data/containerd /var/lib/containerd
fi
printf '%s\n' '{"data-root":"/data/docker","log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}' > /etc/docker/daemon.json
systemctl enable --now containerd docker
for account in asicapi asicworker; do
    if ! id "$account" >/dev/null 2>&1; then useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$account"; fi
done
usermod -aG docker asicworker
chown asicworker:asicworker /data/runs
chmod 755 /data/runs
python3 -m venv /opt/asic-service-env
/opt/asic-service-env/bin/pip install 'google-cloud-storage==3.4.1'
/opt/asic-service-env/bin/python - <<'PY'
import hashlib, os, tarfile
from pathlib import Path
from google.cloud import storage
p=Path('/var/tmp/asic-release.tar.gz')
storage.Client().bucket(os.environ['ASIC_BUCKET']).blob(os.environ['ASIC_RELEASE_OBJECT']).download_to_filename(str(p))
assert hashlib.sha256(p.read_bytes()).hexdigest() == os.environ['ASIC_RELEASE_SHA'], 'Release digest mismatch'
root=Path('/opt/asic-verifier')
root.mkdir(exist_ok=True)
with tarfile.open(p) as archive:
    for item in archive.getmembers():
        path=Path(item.name)
        assert not path.is_absolute() and '..' not in path.parts and item.isfile(), 'Invalid trusted release entry'
    archive.extractall(root)
PY
cd /opt/asic-verifier
/opt/asic-service-env/bin/pip install -r service/requirements.txt
chmod 755 verify setup.sh
./setup.sh --physical
chown -R root:root /opt/asic-verifier
chmod -R a+rX /opt/asic-verifier
# Only these root-owned, checksum-verified repositories are trusted by the worker.
git config --system --replace-all safe.directory /opt/asic-verifier/.tools/physical/tt '^/opt/asic-verifier/\.tools/physical/tt$'
git config --system --replace-all safe.directory /opt/asic-verifier/.tools/physical/pdk '^/opt/asic-verifier/\.tools/physical/pdk$'

# A separate PostgreSQL cluster lives entirely on the retained data disk.
PG_BIN=$(dirname "$(find /usr/lib/postgresql -name initdb -type f | sort -V | tail -1)")
systemctl disable --now postgresql || true
chown postgres:postgres /data/postgres
chmod 700 /data/postgres
if [ ! -f /data/postgres/PG_VERSION ]; then
    runuser -u postgres -- "$PG_BIN/initdb" -D /data/postgres --auth-local=peer --auth-host=reject
fi
cat > /etc/systemd/system/asic-db.service <<EOF
[Unit]
Description=Isolated ASIC queue PostgreSQL
RequiresMountsFor=/data
After=network.target
[Service]
User=postgres
RuntimeDirectory=postgresql
RuntimeDirectoryMode=0755
ExecStart=$PG_BIN/postgres -D /data/postgres -k /run/postgresql -p 5433 -c listen_addresses=
Restart=on-failure
TimeoutStopSec=120
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now asic-db
for i in $(seq 1 30); do
    if runuser -u postgres -- pg_isready -h /run/postgresql -p 5433; then break; fi
    sleep 1
done
runuser -u postgres -- psql -h /run/postgresql -p 5433 -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='asicworker') THEN CREATE ROLE asicworker LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='asicapi') THEN CREATE ROLE asicapi LOGIN; END IF;
END $$;
SELECT 'CREATE DATABASE asic OWNER asicworker' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='asic')\gexec
SQL
runuser -u asicworker -- /opt/asic-service-env/bin/python -c 'from service.core import Store; Store("dbname=asic host=/run/postgresql port=5433").initialize()'
runuser -u postgres -- psql -h /run/postgresql -p 5433 -d asic -v ON_ERROR_STOP=1 -c 'GRANT SELECT,INSERT,UPDATE ON asic_jobs TO asicapi'
cat > /etc/asic-service.env <<EOF
ASIC_DATABASE_URL=dbname=asic host=/run/postgresql port=5433
ASIC_BUCKET=$ASIC_BUCKET
ASIC_RUNS=/data/runs
PYTHONDONTWRITEBYTECODE=1
EOF
chmod 644 /etc/asic-service.env
cat > /etc/systemd/system/asic-api.service <<'EOF'
[Unit]
Description=Private ASIC verifier operator API
After=asic-db.service
Requires=asic-db.service
[Service]
User=asicapi
WorkingDirectory=/opt/asic-verifier
EnvironmentFile=/etc/asic-service.env
ExecStart=/opt/asic-service-env/bin/python -m service.api
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/asic-worker@.service <<'EOF'
[Unit]
Description=ASIC verifier %i worker
After=asic-db.service docker.service
Requires=asic-db.service docker.service
RequiresMountsFor=/data
[Service]
User=asicworker
SupplementaryGroups=docker
WorkingDirectory=/opt/asic-verifier
EnvironmentFile=/etc/asic-service.env
ExecStart=/opt/asic-service-env/bin/python -m service.worker --mode %i
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=30
MemoryMax=4G
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now asic-api asic-worker@fast asic-worker@full
curl --fail --retry 30 --retry-connrefused --retry-delay 1 --max-time 5 -H 'X-ASIC-Client: 1' http://127.0.0.1:8123/health
printf '%s\n' "$ASIC_RELEASE_SHA" > /opt/asic-verifier/DEPLOY_READY
echo 'ASIC_BOOTSTRAP_READY'
