#!/bin/bash
#SBATCH --job-name=pathways-service
#SBATCH --output=pathways-service-%j.out
#SBATCH --error=pathways-service-%j.err
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=80G
#SBATCH --no-requeue
#
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Starts the standing Pathways runtime service on Slurm:
#   1. Pathways Resource Manager (port 29001) and IFRT Proxy Server (port 29000),
#      on the machine this script runs on
#   2. TPU worker slices, as an array job with one task per slice
# Once MIN_SLICES slices are running, it writes ~/pathways/cluster.env so client
# workloads such as run_maxtext.sh can connect. It runs until you stop it.

set -euo pipefail
ulimit -l unlimited 2>/dev/null || true

# Slurm can start this script with a minimal environment.
export USER="${USER:-${SLURM_JOB_USER:-$(id -un)}}"
export HOME="${HOME:-/home/${USER}}"

# ---------------------------------------------------------------------------
# Settings (override any of these as environment variables when you submit)
# ---------------------------------------------------------------------------
SLICE_SHAPE="${SLICE_SHAPE:-v6e:2x4}"   # TPU type and topology of each slice
MIN_SLICES="${MIN_SLICES:-1}"       # slices that must run before clients can connect
MAX_SLICES="${MAX_SLICES:-2}"       # slices requested in total
PARTITION="${PARTITION:-}"          # TPU partition; empty means the default partition
GCS_BUCKET="${GCS_BUCKET:-gs://slurm_${USER}_pathways}"
GCS_PATH="${GCS_PATH:-${GCS_BUCKET%/}/pathways-scratch/shared}"
RM_PORT="${RM_PORT:-29001}"
PROXY_PORT="${PROXY_PORT:-29000}"

WORK_DIR="${WORK_DIR:-/home/${USER}/pathways}"   # set up by setup_env.sh
SERVER_SQSH="${SERVER_SQSH:-${WORK_DIR}/server.sqsh}"
PROXY_SQSH="${PROXY_SQSH:-${WORK_DIR}/proxy.sqsh}"
BIN_DIR="${BIN_DIR:-${WORK_DIR}/bin}"
CLUSTER_ENV_FILE="${CLUSTER_ENV_FILE:-${WORK_DIR}/cluster.env}"

# run_worker_slice.sh: taken from the submit directory (or this script's
# directory when run with bash), else from WORK_DIR.
SCRIPT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
if [[ -z "${WORKER_SCRIPT:-}" ]]; then
  WORKER_SCRIPT="${SCRIPT_DIR}/run_worker_slice.sh"
  [[ -f "${WORKER_SCRIPT}" ]] || WORKER_SCRIPT="${WORK_DIR}/run_worker_slice.sh"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# wait_for_port HOST PORT NAME: waits up to about 6 minutes for a listener.
wait_for_port() {
  local host="$1" port="$2" name="$3"
  for _ in {1..120}; do
    if timeout 1 bash -c "</dev/tcp/${host}/${port}" 2>/dev/null; then
      log "${name} is listening on ${host}:${port}."
      return 0
    fi
    sleep 2
  done
  die "Timed out waiting for ${name} on ${host}:${port}; see ${LOG_DIR}"
}

# make_container NAME IMAGE: creates a fresh Enroot container from a .sqsh image.
make_container() {
  enroot remove -f "$1" 2>/dev/null || true
  enroot create --name "$1" "$2"
}

# Every container gets /tmp, plus the wrapper scripts from BIN_DIR as /pathways.
ENROOT_START=(enroot start --rw --mount /tmp:/tmp --mount "${BIN_DIR}:/pathways")

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP   # run once, even when a signal is followed by EXIT
  echo "=== Stopping Pathways service (job ${JOB_ID}) ==="
  # Tells the recovery loop and the worker slices to stop.
  touch "${RUN_DIR}/shutdown" 2>/dev/null || true
  if [[ -n "${ARRAY_JOB_ID}" ]]; then
    scancel "${ARRAY_JOB_ID}" 2>/dev/null || true
  fi
  # shellcheck disable=SC2086  # unquoted on purpose: PIDs that were never set drop out
  kill -TERM ${RM_PID} ${PROXY_PID} ${RECOVERY_PID} 2>/dev/null || true
  wait 2>/dev/null || true
  rm -f "${CLUSTER_ENV_FILES[@]}" 2>/dev/null || true
  enroot remove -f "pw_rm_${JOB_ID}" "pw_proxy_${JOB_ID}" 2>/dev/null || true
  exit "${exit_code}"
}

# ---------------------------------------------------------------------------
# Checks: fail now, with a clear message, rather than halfway through
# ---------------------------------------------------------------------------
# SLICE_SHAPE is <TPU type>:<topology>, such as v6e:2x4. A bare topology means v6e.
# The worker settings assume 4 chips (2x2) per VM, as on v6e.
SLICE_TPU_TYPE=v6e
if [[ "${SLICE_SHAPE}" == *:* ]]; then SLICE_TPU_TYPE="${SLICE_SHAPE%%:*}"; fi
SLICE_TOPOLOGY="${SLICE_SHAPE#*:}"
[[ "${SLICE_TPU_TYPE}" =~ ^v[0-9]+[a-z]*$ && "${SLICE_TOPOLOGY}" =~ ^[0-9]+x[0-9]+$ ]] \
  || die "SLICE_SHAPE must look like v6e:2x4 (got '${SLICE_SHAPE}')"
[[ "${MIN_SLICES}" =~ ^[0-9]+$ && "${MAX_SLICES}" =~ ^[0-9]+$ ]] \
  && (( MIN_SLICES >= 1 && MAX_SLICES >= MIN_SLICES )) \
  || die "Expected 1 <= MIN_SLICES <= MAX_SLICES (got MIN_SLICES=${MIN_SLICES}, MAX_SLICES=${MAX_SLICES})"
for f in "${SERVER_SQSH}" "${PROXY_SQSH}" "${BIN_DIR}/server" "${BIN_DIR}/proxy_server" "${WORKER_SCRIPT}"; do
  [[ -e "${f}" ]] || die "Missing ${f}. Run setup_env.sh first."
done

# ---------------------------------------------------------------------------
# Derived values and run directories
# ---------------------------------------------------------------------------
# Setting --num_elastic_instances to MAX_SLICES keeps the proxy's minimum slice
# count at 0 so slice loss returns PlacementDataLoss instead of terminating the
# proxy and active client sessions; clients enforce MIN_SLICES.
NUM_ELASTIC_SLICES="${MAX_SLICES}"
IFS='x' read -r TOPO_X TOPO_Y <<< "${SLICE_TOPOLOGY}"
CHIPS_PER_SLICE=$(( TOPO_X * TOPO_Y ))

JOB_ID="${SLURM_JOB_ID:-login-$(date +%s)}"   # login-<time> when not run as a job
RUN_DIR="${WORK_DIR}/service_${JOB_ID}"
LOG_DIR="${RUN_DIR}/logs"
RM_NODE="$(hostname -s)"
RM_IP="$(getent ahostsv4 "${RM_NODE}" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
[[ -n "${RM_IP}" ]] || die "Couldn't resolve the IP address of ${RM_NODE}"

# run_maxtext.sh reads cluster_<ClusterName>.env first, then cluster.env.
CLUSTER_NAME="$(scontrol show config 2>/dev/null | awk '/^ClusterName/ {print $3}' || echo default)"
CLUSTER_ENV_FILES=("${WORK_DIR}/cluster_${CLUSTER_NAME}.env" "${CLUSTER_ENV_FILE}")
# Remove a previous run's files so clients never connect to a dead service.
rm -f "${CLUSTER_ENV_FILES[@]}" 2>/dev/null || true

PARTITION_FLAGS=()
if [[ -n "${PARTITION}" ]]; then PARTITION_FLAGS=(-p "${PARTITION}"); fi
PROXY_FLAGS=()
if (( NUM_ELASTIC_SLICES > 0 )); then PROXY_FLAGS=("--num_elastic_instances=${NUM_ELASTIC_SLICES}"); fi

# Enroot keeps its runtime state on this node's local /tmp.
export ENROOT_CONFIG_PATH="${HOME}/.enroot"
export ENROOT_RUNTIME_PATH="/tmp/enroot-$(id -u)/runtime"
export ENROOT_CACHE_PATH="/tmp/enroot-$(id -u)/cache"
export ENROOT_DATA_PATH="/tmp/enroot-$(id -u)/data"
export ENROOT_TEMP_PATH="/tmp/enroot-$(id -u)/tmp"
mkdir -p "${LOG_DIR}" "${ENROOT_CONFIG_PATH}" "${ENROOT_RUNTIME_PATH}" \
  "${ENROOT_CACHE_PATH}" "${ENROOT_DATA_PATH}" "${ENROOT_TEMP_PATH}"

ARRAY_JOB_ID="" RM_PID="" PROXY_PID="" RECOVERY_PID=""
trap cleanup EXIT INT TERM HUP   # HUP: closing the terminal also stops it cleanly

cat <<EOF
==================================================================
Starting Standing Pathways Runtime Service
  Service Job ID     : ${JOB_ID} on ${RM_NODE} (${RM_IP})
  TPU Partition      : ${PARTITION:-<default>}
  TPU Slice Shape    : ${SLICE_TPU_TYPE}=${SLICE_TOPOLOGY} (${CHIPS_PER_SLICE} chips/slice)
  Slice Scaling Range: MIN_SLICES=${MIN_SLICES}, MAX_SLICES=${MAX_SLICES} (elastic=${NUM_ELASTIC_SLICES})
  Logs Directory     : ${LOG_DIR}
==================================================================
EOF

# ---------------------------------------------------------------------------
# 1. Resource Manager
# ---------------------------------------------------------------------------
make_container "pw_rm_${JOB_ID}" "${SERVER_SQSH}"
"${ENROOT_START[@]}" "pw_rm_${JOB_ID}" /pathways/server \
    --server_port="${RM_PORT}" \
    --node_type=resource_manager \
    --instance_type="tpu${SLICE_TPU_TYPE}:${SLICE_TOPOLOGY}" \
    --instance_count="${MAX_SLICES}" \
    --gcs_scratch_location="${GCS_PATH}" \
    --enable_gcs_olm=false \
  > "${LOG_DIR}/rm.log" 2>&1 &
RM_PID=$!
wait_for_port "${RM_IP}" "${RM_PORT}" "Pathways Resource Manager"

# ---------------------------------------------------------------------------
# 2. IFRT proxy: a long-running service like the Resource Manager. It stays up
#    while slices come and go, and between client jobs.
# ---------------------------------------------------------------------------
make_container "pw_proxy_${JOB_ID}" "${PROXY_SQSH}"
"${ENROOT_START[@]}" "pw_proxy_${JOB_ID}" /pathways/proxy_server \
    --server_port="${PROXY_PORT}" \
    --resource_manager_address="${RM_IP}:${RM_PORT}" \
    --gcs_scratch_location="${GCS_PATH}" \
    "${PROXY_FLAGS[@]}" \
  > "${LOG_DIR}/proxy.log" 2>&1 &
PROXY_PID=$!
wait_for_port "${RM_IP}" "${PROXY_PORT}" "Pathways IFRT Proxy Server"

# ---------------------------------------------------------------------------
# 3. Discovery file, read by the worker slices now and by clients later
# ---------------------------------------------------------------------------
cat > "${RUN_DIR}/service.env" <<EOF
export RM_NODE="${RM_NODE}"
export RM_IP="${RM_IP}"
export RM_PORT="${RM_PORT}"
export PROXY_PORT="${PROXY_PORT}"
export JAX_PLATFORMS="proxy"
export JAX_BACKEND_TARGET="grpc://${RM_IP}:${PROXY_PORT}"
export ENABLE_PATHWAYS_PERSISTENCE="1"
export SLICE_SHAPE="${SLICE_SHAPE}"
export SLICE_TPU_TYPE="${SLICE_TPU_TYPE}"
export SLICE_TOPOLOGY="${SLICE_TOPOLOGY}"
export MIN_SLICES="${MIN_SLICES}"
export MAX_SLICES="${MAX_SLICES}"
export GCS_BUCKET="${GCS_BUCKET}"
export GCS_PATH="${GCS_PATH}"
export RUN_DIR="${RUN_DIR}"
export LOG_DIR="${LOG_DIR}"
export COORD_JOB_ID="${JOB_ID}"
EOF

# ---------------------------------------------------------------------------
# 4. Worker slices: an array job with one task per slice
# ---------------------------------------------------------------------------
# env -u keeps this job's CPU and memory settings out of the worker job, whose
# srun would otherwise pick them up. --propagate=NONE does the same for ulimits.
ARRAY_JOB_ID="$(env -u SLURM_TRES_PER_TASK -u SLURM_CPUS_PER_TASK \
                    -u SLURM_MEM_PER_NODE -u SLURM_MEM_PER_CPU \
  sbatch --parsable \
    --array="1-${MAX_SLICES}" \
    --requeue \
    --propagate=NONE \
    "${PARTITION_FLAGS[@]}" \
    --layout="${SLICE_TPU_TYPE}=${SLICE_TOPOLOGY}" \
    --output="${LOG_DIR}/slice-%A_%a.out" \
    --error="${LOG_DIR}/slice-%A_%a.err" \
    --export="ALL,SERVICE_ENV=${RUN_DIR}/service.env,WORK_DIR=${WORK_DIR}" \
    "${WORKER_SCRIPT}")"
echo "export WORKER_ARRAY_JOB_ID=\"${ARRAY_JOB_ID}\"" >> "${RUN_DIR}/service.env"
log "Submitted TPU Worker Slice Array Job: ${ARRAY_JOB_ID} (tasks 1..${MAX_SLICES})"

# ---------------------------------------------------------------------------
# 5. Slice recovery
# ---------------------------------------------------------------------------
# When a TPU node goes down or stops responding (for example, a Spot
# preemption), requeue its worker task and force the node to power down so
# Cluster Director recreates the whole slice. In sinfo states, * means not
# responding, % powering down, ~ powered off and # powering up. ! (power down
# pending) is not treated as a failure: running dynamic nodes show as allocated!.
if [[ -z "${PARTITION}" ]]; then
  # Without -p, sinfo lists every partition; watch only the workers' partition.
  PARTITION="$(squeue -j "${ARRAY_JOB_ID}" -h -o %P 2>/dev/null | head -n1 || true)"
fi
SINFO_ARGS=(-N -h -o "%N %T")
if [[ -n "${PARTITION}" ]]; then SINFO_ARGS+=(-p "${PARTITION}"); fi
(
  declare -A last_cycled=()
  until [[ -f "${RUN_DIR}/shutdown" ]]; do
    now="$(date +%s)"
    while read -r node state; do
      [[ "${node}" == "${RM_NODE}" ]] && continue
      (( now - ${last_cycled[${node}]:-0} < 300 )) && continue   # handled in the last 5 minutes
      last_cycled[${node}]="${now}"
      log "[SliceRecovery] Cycling preempted/down TPU node ${node} (${state}) via POWER_DOWN_FORCE..."
      for task in $(squeue -j "${ARRAY_JOB_ID}" -w "${node}" -h -o %i 2>/dev/null); do
        scontrol requeue "${task}" 2>/dev/null || true
      done
      sudo -n scontrol update NodeName="${node}" State=POWER_DOWN_FORCE Reason=spot_slice_recovery 2>/dev/null \
        || log "[SliceRecovery] WARNING: couldn't power down ${node}; this needs passwordless sudo."
    done < <(sinfo "${SINFO_ARGS[@]}" 2>/dev/null \
               | awk '($2 ~ /^(down|fail)/ || $2 ~ /\*$/) && $2 !~ /[~#%]/')
    sleep 5
  done
) &
RECOVERY_PID=$!

# ---------------------------------------------------------------------------
# 6. Wait until MIN_SLICES slices are running
# ---------------------------------------------------------------------------
log "Waiting for at least MIN_SLICES=${MIN_SLICES} worker slice(s) to start..."
while true; do
  # One state per line (PD, CF, R, ...); empty once every worker task has ended.
  if states="$(squeue -j "${ARRAY_JOB_ID}" -h -o %t 2>/dev/null)"; then
    [[ -n "${states}" ]] \
      || die "Worker job ${ARRAY_JOB_ID} ended before ${MIN_SLICES} slice(s) started; see ${LOG_DIR}/slice-*.err"
    running_slices="$(grep -cx R <<< "${states}" || true)"
    (( running_slices >= MIN_SLICES )) && break
  fi
  sleep 3
done

# ---------------------------------------------------------------------------
# 7. Publish the discovery file for clients
# ---------------------------------------------------------------------------
for f in "${CLUSTER_ENV_FILES[@]}"; do cp -f "${RUN_DIR}/service.env" "${f}"; done

if [[ -n "${SLURM_JOB_ID:-}" ]]; then
  RUN_CMD="sbatch run_maxtext.sh"
  STOP_CMD="scancel ${JOB_ID}"
elif [[ -n "${INVOCATION_ID:-}" ]]; then   # started by systemd (login_startup)
  RUN_CMD="bash run_maxtext.sh"
  STOP_CMD="sudo systemctl stop pathways-coordinator"
else
  RUN_CMD="bash run_maxtext.sh"
  STOP_CMD="kill $$   (or press Ctrl+C in this terminal)"
fi

cat <<EOF
==================================================================
Pathways Runtime Service is READY!
  Discovery file     : ${CLUSTER_ENV_FILE}
  IFRT Proxy Target  : grpc://${RM_IP}:${PROXY_PORT}
  Running TPU Slices : ${running_slices} / ${MAX_SLICES} (min=${MIN_SLICES})

To launch the Elastic MaxText Llama 3.1 8B workload, run:
  ${RUN_CMD}

To stop the Pathways runtime service when finished, run:
  ${STOP_CMD}
==================================================================
EOF

# Run until the service is stopped. If the Resource Manager or the proxy exits,
# stop the whole service so the failure is visible.
while kill -0 "${RM_PID}" 2>/dev/null && kill -0 "${PROXY_PID}" 2>/dev/null; do
  sleep 5
done
die "The Pathways Resource Manager or IFRT proxy exited; see rm.log and proxy.log in ${LOG_DIR}"
