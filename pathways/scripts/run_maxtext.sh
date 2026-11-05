#!/bin/bash
#SBATCH --job-name=maxtext-llama31
#SBATCH --output=maxtext-llama31-%j.out
#SBATCH --error=maxtext-llama31-%j.err
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=160G
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
# Runs Elastic MaxText (Llama 3.1 8B) as a separate client workload against the
# standing Pathways runtime service started by start_pathways.sh.
#
# By default, runs the full 32-layer Llama 3.1 8B model with synthetic data,
# Flash Attention, and activation offloading. Set REDUCED_LAYERS=1 for a lighter
# 8-layer configuration when testing on smaller slices (such as 2x2).
#
# You can submit it right after start_pathways.sh: it waits until the service
# is READY (up to SERVICE_WAIT_MINUTES, default 60).
#
# Usage:
#   # Full 32-layer Llama 3.1 8B (recommended for v6e=2x4 or v6e=4x4 slices):
#   sbatch run_maxtext.sh
#
#   # Reduced 8-layer option for quick smoke testing:
#   REDUCED_LAYERS=1 sbatch run_maxtext.sh
#
#   # On a cluster without a cpu partition, run it on the login node instead:
#   bash run_maxtext.sh

set -euo pipefail

export USER="${USER:-${SLURM_JOB_USER:-$(id -un)}}"
export HOME="${HOME:-/home/${USER}}"
WORK_DIR="${WORK_DIR:-/home/${USER}/pathways}"
MAXTEXT_DIR="${MAXTEXT_DIR:-${WORK_DIR}/maxtext}"
VENV_PYTHON="${VENV_PYTHON:-${WORK_DIR}/venv/bin/python}"
CLUSTER_NAME="$(scontrol show config 2>/dev/null | awk '/^ClusterName/ {print $3}' || echo default)"

# Wait until the Pathways service is READY: its discovery file exists and its
# IFRT proxy accepts connections. A file left behind by a service that died
# fails the proxy check, so it is never used.
SERVICE_WAIT_MINUTES="${SERVICE_WAIT_MINUTES:-60}"
deadline=$(( SECONDS + SERVICE_WAIT_MINUTES * 60 ))
announced=0
while true; do
  env_file="${CLUSTER_ENV_FILE:-}"
  if [[ -z "${env_file}" ]]; then
    env_file="${WORK_DIR}/cluster_${CLUSTER_NAME}.env"
    [[ -f "${env_file}" ]] || env_file="${WORK_DIR}/cluster.env"
  fi
  # shellcheck disable=SC1090
  if [[ -f "${env_file}" ]] \
      && (source "${env_file}" && timeout 2 bash -c "</dev/tcp/${RM_IP}/${PROXY_PORT}") 2>/dev/null; then
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "ERROR: No running Pathways service after ${SERVICE_WAIT_MINUTES} minutes. Start it with:" >&2
    echo "  sbatch start_pathways.sh" >&2
    exit 1
  fi
  if (( ! announced )); then
    echo "Waiting for the Pathways service to be READY (start it with: sbatch start_pathways.sh)..."
    announced=1
  fi
  sleep 10
done
CLUSTER_ENV_FILE="${env_file}"

# Load JAX_BACKEND_TARGET, SLICE_SHAPE, MIN_SLICES, MAX_SLICES, GCS_BUCKET
# shellcheck disable=SC1090
source "${CLUSTER_ENV_FILE}"

STEPS="${STEPS:-30}"
CHECKPOINT_PERIOD="${CHECKPOINT_PERIOD:-5}"
REDUCED_LAYERS="${REDUCED_LAYERS:-0}"
RUN_NAME="${RUN_NAME:-llama3_1_8b_${SLURM_JOB_ID:-$(date +%s)}}"
BASE_OUTPUT_DIR="${BASE_OUTPUT_DIR:-${GCS_BUCKET%/}/maxtext-runs}"

MODEL_FLAGS=(
  "model_name=llama3.1-8b"
  "weight_dtype=bfloat16"
  "use_iota_embed=True"
  "dataset_type=synthetic"
  "ici_fsdp_parallelism=-1"
  "remat_policy=custom"
  "decoder_layer_input=offload"
  "out_proj=offload"
  "query_proj=offload"
  "key_proj=offload"
  "value_proj=offload"
  "attention=flash"
  "sa_block_q=1024"
  "sa_block_kv=1024"
  "sa_block_kv_compute=1024"
  "sa_block_q_dkv=1024"
  "sa_block_kv_dkv=1024"
  "sa_block_kv_dkv_compute=1024"
  "sa_block_q_dq=1024"
  "sa_block_kv_dq=1024"
  "sa_use_fused_bwd_kernel=True"
)

if [[ "${REDUCED_LAYERS}" == "1" ]]; then
  LAYER_DESC="8 layers (REDUCED_LAYERS=1)"
  MODEL_FLAGS+=(
    "override_model_config=True"
    "base_num_decoder_layers=8"
    "per_device_batch_size=${PER_DEVICE_BATCH_SIZE:-1}"
    "max_target_length=${MAX_TARGET_LENGTH:-2048}"
  )
else
  LAYER_DESC="Full 32 layers"
  MODEL_FLAGS+=(
    "per_device_batch_size=${PER_DEVICE_BATCH_SIZE:-3}"
    "max_target_length=${MAX_TARGET_LENGTH:-8192}"
  )
fi

echo "=================================================================="
echo "Launching Elastic MaxText Client Workload (Llama 3.1 8B)"
echo "  Pathways Proxy     : ${JAX_BACKEND_TARGET}"
echo "  TPU Slice Shape    : ${SLICE_TPU_TYPE:-v6e}=${SLICE_TOPOLOGY:-${SLICE_SHAPE}}"
echo "  Elastic Slice Range: MIN_SLICES=${MIN_SLICES} .. MAX_SLICES=${MAX_SLICES}"
echo "  Model Config       : llama3.1-8b (${LAYER_DESC})"
echo "  Steps / Ckpt Period: steps=${STEPS}, checkpoint_period=${CHECKPOINT_PERIOD}"
echo "  Output Directory   : ${BASE_OUTPUT_DIR}/${RUN_NAME}"
echo "=================================================================="

cd "${MAXTEXT_DIR}"
export PYTHONPATH="${MAXTEXT_DIR}/src:${MAXTEXT_DIR}:${PYTHONPATH:-}"
export ENABLE_PATHWAYS_PERSISTENCE="${ENABLE_PATHWAYS_PERSISTENCE:-1}"

exec "${VENV_PYTHON}" -u -m maxtext.trainers.pre_train.train \
  src/maxtext/configs/base.yml \
  run_name="${RUN_NAME}" \
  base_output_directory="${BASE_OUTPUT_DIR}" \
  enable_single_controller=True \
  elastic_enabled=True \
  elastic_min_slice_count="${MIN_SLICES}" \
  enable_checkpointing=True \
  async_checkpointing=False \
  save_checkpoint_on_start="${SAVE_CHECKPOINT_ON_START:-False}" \
  checkpoint_storage_concurrent_gb="${CHECKPOINT_CONCURRENT_GB:-8}" \
  checkpoint_period="${CHECKPOINT_PERIOD}" \
  steps="${STEPS}" \
  log_period=1 \
  "${MODEL_FLAGS[@]}"
