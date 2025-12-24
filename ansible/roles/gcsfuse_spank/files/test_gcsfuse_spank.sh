#!/bin/bash
#SBATCH --job-name=gcsfuse_spank_test
#SBATCH --output=gcsfuse_spank_test.out
#SBATCH --error=gcsfuse_spank_test.err
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:10:00

# NOTE: This test script requires the following GCS buckets to exist
# and be accessible by the service account used by the Slurm compute nodes:
# - your-bucket-a
# - your-bucket-b
# - your-bucket-c
# Please create them and update the BUCKET_ variables below if necessary.

# Bucket names
BUCKET_A="your-bucket-a"
BUCKET_B="your-bucket-b"
BUCKET_C="your-bucket-c"

echo "--- Starting gcsfuse SPANK Plugin Tests ---"

# --- Initialize Buckets for Test 1 ---
echo "Initializing buckets for Test 1..."
echo ID_A > ID_A.txt
echo ID_B > ID_B.txt
gcloud storage cp ID_A.txt gs://${BUCKET_A}/
gcloud storage cp ID_B.txt gs://${BUCKET_B}/
rm ID_A.txt ID_B.txt
echo "Bucket initialization complete."

# --- Test 1: Conflicting Mounts ---
echo -e "\n--- Test 1: Mounting two different buckets to the same location ---"
CONFLICT_MP="/tmp/gcs_conflict_test"
rm -rf "${CONFLICT_MP}"
mkdir -p "${CONFLICT_MP}"

echo "Attempting to mount ${BUCKET_A} and ${BUCKET_B} to ${CONFLICT_MP}"
srun --gcsfuse-mount="${BUCKET_A}:${CONFLICT_MP}" --gcsfuse-mount="${BUCKET_B}:${CONFLICT_MP}" bash -c "
    echo 'Inside srun for Test 1'
    ls -l '${CONFLICT_MP}'
    if [ -f '${CONFLICT_MP}/ID_A.txt' ]; then
        echo 'Found ID_A.txt, ${BUCKET_A} seems to be mounted.'
    elif [ -f '${CONFLICT_MP}/ID_B.txt' ]; then
        echo 'Found ID_B.txt, ${BUCKET_B} seems to be mounted.'
    else
        echo 'ERROR: Neither ID_A.txt nor ID_B.txt found in ${CONFLICT_MP}. Mount may have failed or buckets are not set up correctly.'
    fi
    echo 'Exiting srun for Test 1'
"
echo "srun command finished for Test 1."

if mountpoint -q "${CONFLICT_MP}"; then
    echo "ERROR: ${CONFLICT_MP} is still a mount point after srun (Test 1)."
else
    echo "SUCCESS: ${CONFLICT_MP} is NOT a mount point after srun (Test 1)."
fi
rm -rf "${CONFLICT_MP}"

# --- Test 2: Lingering File Handle with --overlap ---
echo -e "\n--- Test 2: Open file handle with --overlap ---"
LINGER_MP="/tmp/gcs_linger_test"
LINGER_FILE="${LINGER_MP}/linger.txt"
OVERLAP_JOB_ID_FILE="/tmp/overlap_job_id.txt"

rm -rf "${LINGER_MP}" "${OVERLAP_JOB_ID_FILE}"
mkdir -p "${LINGER_MP}"

echo "Starting srun --overlap in background to mount ${BUCKET_C}..."
( srun --overlap --gcsfuse-mount="${BUCKET_C}:${LINGER_MP}" bash -c "
    echo \${SLURM_JOB_ID}.\${SLURM_STEP_ID} > '${OVERLAP_JOB_ID_FILE}'
    echo 'Overlap srun (PID \$$) mounted ${BUCKET_C} on ${LINGER_MP}. Waiting to be cancelled...'
    sleep 3600
" ) &

# Wait for the mount to be ready and JOB ID file to appear
echo "Waiting for mount and Job ID file..."
while [ ! -f "${OVERLAP_JOB_ID_FILE}" ] || ! mountpoint -q "${LINGER_MP}"; do
    sleep 0.5
done
OVERLAP_STEP_ID=$(cat "${OVERLAP_JOB_ID_FILE}")
echo "Overlap srun step ID: ${OVERLAP_STEP_ID} started. Mount point ${LINGER_MP} is ready."

# Keep a file open in the mount point
echo "Opening file handle in ${LINGER_FILE}..."
touch "${LINGER_FILE}"
exec 3<> "${LINGER_FILE}"  # Open file descriptor 3 for read/write
echo "File handle open on FD 3."

# Cancel the srun step, which should trigger spank_exit
echo "Cancelling the overlap srun step: $(cat "${OVERLAP_JOB_ID_FILE}")"
scancel $(cat "${OVERLAP_JOB_ID_FILE}")

echo "Waiting a few seconds for scancel to take effect and unmount to be attempted..."
sleep 5

# Check if the mount point is still there
if mountpoint -q "${LINGER_MP}"; then
    echo "ERROR: ${LINGER_MP} is STILL a mount point after scancel (Test 2). Open file handle likely prevented unmount."
else
    echo "SUCCESS: ${LINGER_MP} is NOT a mount point after scancel (Test 2). Unmount worked."
fi

# Close the file handle
echo "Closing file handle FD 3..."
exec 3<&-

echo "Attempting to lazily unmount ${LINGER_MP} in case of stale mount..."
sudo umount -l "${LINGER_MP}" 2>/dev/null

rm -rf "${LINGER_MP}" "${OVERLAP_JOB_ID_FILE}"

echo -e "\n--- gcsfuse SPANK Plugin Tests Finished ---"
