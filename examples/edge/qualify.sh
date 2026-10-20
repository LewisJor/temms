#!/usr/bin/env bash
# Run two fresh, constrained native Linux containers and independently verify evidence.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 IMAGE ASSETS NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
image=$1
assets=$(cd -- "$2" && pwd -P)
mkdir -- "$3"
output=$(cd -- "$3" && pwd -P)
[[ $(uname -s) == Linux && $(id -u) -ne 0 ]] || {
  echo "Run this qualification as a non-root user on native Linux." >&2
  exit 2
}
case $(uname -m) in
  x86_64) architecture=amd64 ;;
  aarch64|arm64) architecture=arm64 ;;
  *) echo "Supported native architectures: x86_64 and aarch64." >&2; exit 2 ;;
esac
docker image inspect "$image" > "$output/image.json"
docker version --format '{{json .Server}}' > "$output/docker-server.json"
docker info --format '{{.OSType}} {{.Architecture}} {{.CgroupVersion}}' > "$output/docker-host.txt"
uname -srm > "$output/host.txt"
[[ $(docker image inspect --format '{{.Architecture}}' "$image") == "$architecture" ]]
read -r engine_os engine_arch cgroup_version < "$output/docker-host.txt"
[[ $engine_os == linux && $cgroup_version == 2 ]]
case "$architecture:$engine_arch" in
  amd64:x86_64|amd64:amd64|arm64:aarch64|arm64:arm64) ;;
  *) echo "Docker engine and host architecture differ; emulation is not qualified." >&2; exit 2 ;;
esac

scratch=$(mktemp -d)
prefix="temms-edge-$(id -u)-$$-${scratch##*/}"
cleanup() {
  for cidfile in "$scratch"/*.cid; do
    [[ -s $cidfile ]] || continue
    local cid label
    cid=$(cat "$cidfile")
    label=${cidfile##*/}
    if [[ $(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null) == true ]]; then
      docker kill "$cid" >/dev/null 2>&1 || true
      docker wait "$cid" >/dev/null 2>&1 || true
    fi
    if docker inspect "$cid" > "$scratch/$label.inspect" 2>/dev/null; then
      cp "$scratch/$label.inspect" "$output/${label%.cid}-container.json"
    fi
    docker rm --force "$cid" >/dev/null 2>&1 || true
  done
  rm -rf -- "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

launch() {
  local label=$1 access=$2
  shift 2
  local mount="type=bind,source=$output,target=/evidence"
  [[ $access != ro ]] || mount+=",readonly"
  local status=0
  echo "Starting $label in a fresh constrained container."
  timeout --signal=TERM --kill-after=10s 240s \
    docker run --name "$prefix-$label" --cidfile "$scratch/$label.cid" \
      --user "$(id -u):$(id -g)" --read-only --network none --cgroupns private \
      --cap-drop ALL --security-opt no-new-privileges \
      --cpu-period 100000 --cpu-quota 100000 \
      --memory 512m --memory-swap 512m --pids-limit 64 \
      --tmpfs /tmp:rw,nosuid,nodev,size=128m \
      --mount "type=bind,source=$assets,target=/assets,readonly" \
      --mount "$mount" "$image" "$@" \
      2>&1 | tee "$output/$label.log" || status=$?
  if [[ ! -s $scratch/$label.cid ]]; then
    echo "Container creation failed ($status): $label" >&2
    return 1
  fi
  local cid
  cid=$(cat "$scratch/$label.cid")
  if [[ $(docker inspect --format '{{.State.Running}}' "$cid") == true ]]; then
    docker kill "$cid" >/dev/null || true
    docker wait "$cid" >/dev/null || true
  fi
  docker inspect "$cid" > "$output/$label-container.json" || return 1
  python - "$output/$label-container.json" "$status" "$access" <<'PY'
import json
import sys
from pathlib import Path

container = json.loads(Path(sys.argv[1]).read_text())[0]
state, config = container['State'], container['HostConfig']
assert int(sys.argv[2]) == 0, f'launch exit: {sys.argv[2]}'
assert state['ExitCode'] == 0 and not state['OOMKilled'] and not state['Running'], state
assert config['CpuPeriod'] == config['CpuQuota'] == 100000, config
assert config['Memory'] == config['MemorySwap'] == 536870912, config
assert config['NetworkMode'] == 'none' and config['ReadonlyRootfs'], config
assert config['CgroupnsMode'] == 'private' and config['PidsLimit'] == 64, config
assert config['CapDrop'] == ['ALL'] and 'no-new-privileges' in config['SecurityOpt'], config
assert container['Config']['User'].split(':')[0] != '0', container['Config']['User']
mounts = {mount['Destination']: mount for mount in container['Mounts']}
assert mounts['/assets']['RW'] is False, mounts
assert mounts['/evidence']['RW'] is (sys.argv[3] != 'ro'), mounts
print(f"Verified container exit, no OOM, limits and mounts: {container['Name']}")
PY
}

failed=0
for repetition in 1 2; do
  if ! launch "run-$repetition" rw python -m examples.edge.run \
      --assets /assets --output "/evidence/run-$repetition"; then
    failed=1
  fi
done
if ! launch verification ro python -m examples.edge.verify /evidence; then
  failed=1
fi
exit "$failed"
