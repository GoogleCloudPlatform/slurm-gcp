#!/bin/bash
#SBATCH --job-name=pw-slice
#SBATCH --requeue
#SBATCH --ntasks-per-node=1
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
# Per-slice Pathways TPU worker script (submitted as a Slurm array job 1..MAX_SLICES
# by start_pathways.sh). Each array task manages one TPU slice independently.
# If a Spot slice is preempted, only that array task exits and is requeued by Slurm.


set -euo pipefail
ulimit -l unlimited 2>/dev/null || true
unset SLURM_TRES_PER_TASK SLURM_CPUS_PER_TASK SLURM_MEM_PER_NODE SLURM_MEM_PER_CPU


export USER="${USER:-${SLURM_JOB_USER:-$(id -un)}}"
export HOME="${HOME:-/home/${USER}}"
WORK_DIR="${WORK_DIR:-/home/${USER}/pathways}"
SERVER_SQSH="${SERVER_SQSH:-${WORK_DIR}/server.sqsh}"
BIN_DIR="${BIN_DIR:-${WORK_DIR}/bin}"


# shellcheck disable=SC1090
source "${SERVICE_ENV:?ERROR: SERVICE_ENV must point to service.env}"


SLICE_TASK_ID="${SLURM_ARRAY_TASK_ID:-1}"
SLICE_INDEX=$(( SLICE_TASK_ID - 1 ))
RESTART_COUNT="${SLURM_RESTART_COUNT:-0}"


resolve_ip() {
  local host="$1"
  for _ in {1..15}; do
    local ip
    ip="$(getent ahostsv4 "${host}" 2>/dev/null | awk 'NR==1 {print $1}')"
    if [[ -n "${ip}" ]]; then
      echo "${ip}"
      return 0
    fi
    sleep 2
  done
  return 1
}


mapfile -t SLICE_NODES < <(scontrol show hostnames "${SLURM_JOB_NODELIST}")
NUM_SLICE_NODES="${#SLICE_NODES[@]}"
SLICE_NODELIST_CSV="$(IFS=,; echo "${SLICE_NODES[*]}")"


SLICE_IPS=()
for node in "${SLICE_NODES[@]}"; do
  SLICE_IPS+=("$(resolve_ip "${node}")")
done
SLICE_IPS_CSV="$(IFS=,; echo "${SLICE_IPS[*]}")"


IFS='x' read -r TOPO_X TOPO_Y <<< "${SLICE_TOPOLOGY}"
CHIPS_PER_SLICE=$(( TOPO_X * TOPO_Y ))
TPU_HOST_BOUNDS="$(( TOPO_X / 2 )),$(( TOPO_Y / 2 )),1"
TPU_ACCELERATOR_TYPE="${SLICE_TPU_TYPE}-${CHIPS_PER_SLICE}"


echo "=== Pathways Worker Slice ${SLICE_TASK_ID}/${MAX_SLICES} (attempt ${RESTART_COUNT}) ==="
echo "  Nodes    : ${SLICE_NODELIST_CSV} (${SLICE_IPS_CSV})"
echo "  Topology : ${SLICE_TPU_TYPE}:${SLICE_TOPOLOGY} (${TPU_ACCELERATOR_TYPE}, host_bounds=${TPU_HOST_BOUNDS})"
echo "  RM Target: ${RM_IP}:${RM_PORT}"


# Ensure Enroot runtime directories exist on all hosts in this slice.
srun --propagate=NONE --overlap --gres=none --mem=4G -N "${NUM_SLICE_NODES}" -n "${NUM_SLICE_NODES}" --ntasks-per-node=1 \
  /bin/bash -c 'mkdir -p "${HOME}/.enroot" "/tmp/enroot-$(id -u)/"{tmp,runtime,cache,data} 2>/dev/null || true'


# Run the Pathways worker server inside the Enroot container across all hosts in
# the slice. The inner loop restarts the server cleanly whenever a client
# workload finishes (EXIT_TYPE_QUICK_RESTART) so the standing service stays up
# across multiple client jobs.
set +e
srun \
  --propagate=NONE \
  --overlap \
  --mem=0 \
  --kill-on-bad-exit=1 \
  -N "${NUM_SLICE_NODES}" \
  -n "${NUM_SLICE_NODES}" \
  --ntasks-per-node=1 \
  --container-image="${SERVER_SQSH}" \
  --container-mounts="/dev:/dev,/tmp:/tmp,${BIN_DIR}:/pathways,${RUN_DIR}:${RUN_DIR}" \
  --container-writable \
  /bin/sh -c "
    export TPU_SKIP_MDS_QUERY=true
    export TPU_ACCELERATOR_TYPE='${TPU_ACCELERATOR_TYPE}'
    export TPU_TOPOLOGY='${SLICE_TOPOLOGY}'
    export TPU_CHIPS_PER_HOST_BOUNDS='2,2,1'
    export TPU_HOST_BOUNDS='${TPU_HOST_BOUNDS}'
    export TPU_TOPOLOGY_WRAP='false,false,false'
    export TPU_TOPOLOGY_ALT='false'
    export TPU_WORKER_ID=\"\${SLURM_NODEID}\"
    export TPU_WORKER_HOSTNAMES='${SLICE_IPS_CSV}'
    # Pathways binds multi-slice MegaScale endpoints externally via the Resource
    # Manager (CreateMultiSliceConfig) rather than DiscoverSlices; libtpu only
    # requires MEGASCALE_COORDINATOR_ADDRESS to be non-empty (AppendMegascaleArgs).
    export MEGASCALE_PORT='8080'
    export MEGASCALE_COORDINATOR_ADDRESS='${SLICE_IPS[0]}'
    export MEGASCALE_NUM_SLICES='${MAX_SLICES}'
    export MEGASCALE_SLICE_ID='${SLICE_INDEX}'
    while [ ! -f '${RUN_DIR}/shutdown' ]; do
      /pathways/server \
        --server_port=${RM_PORT} \
        --resource_manager_address=${RM_IP}:${RM_PORT} \
        --gcs_scratch_location=${GCS_PATH} \
        --enable_gcs_olm=false
      if [ -f '${RUN_DIR}/shutdown' ]; then
        exit 0
      fi
      sleep 1
    done
  "
SRUN_RC=$?
set -e


if [[ -f "${RUN_DIR}/shutdown" ]]; then
  exit 0
fi


# If srun exited because a VM in the slice was preempted or became unhealthy,
# power-cycle the slice so Cluster Director recreates the multi-host slice MIG.
# Always exit 1 so Slurm (RequeueExit=1, requeue_delay=5) requeues this slice task.
slice_unhealthy=0
for ip in "${SLICE_IPS[@]}"; do
  if ! ping -c 1 -W 2 "${ip}" >/dev/null 2>&1; then
    slice_unhealthy=1
    break
  fi
done
if [[ -n "$(sinfo -n "${SLICE_NODELIST_CSV}" -h -o "%T" 2>/dev/null | grep -E 'down|fail|unk' || true)" ]]; then
  slice_unhealthy=1
fi
if (( slice_unhealthy == 1 )); then
  sudo -n scontrol update NodeName="${SLICE_NODELIST_CSV}" State=POWER_DOWN_FORCE Reason="slice_task_recovery" 2>/dev/null || true
fi
exit 1
