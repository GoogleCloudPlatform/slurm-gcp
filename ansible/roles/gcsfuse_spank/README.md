# Slurm SPANK plugin for GCSFuse

This project provides a Slurm SPANK plugin that allows users to manage
gcsfuse mounts through srun commands.

## Interface

The desired interface is:
`--gcsfuse-mount=BUCKET_NAME:MOUNT_POINT[:FLAGS]`

This will start gcsfuse on all nodes at the beginning of the step, and will
unmount the bucket at the end.

## Design

The plugin uses the Slurm SPANK plugin infrastructure to mount GCS buckets
at the beginning of a job or job step and unmount them at the end.

The plugin operates in several contexts:

1.  **Local/Allocator (srun/sbatch/salloc) context:** When the user provides the
    `--gcsfuse-mount` option, the `handle_gcsfuse_mount` callback function
    is invoked. This function resolves any relative paths in the mount
    point and then appends the mount specification to the `GCSFUSE_MOUNTS`
    environment variable. For `sbatch` and `salloc`, this variable is
    stored in the job's environment.

2.  **Job Script (prolog/epilog) context:** On the compute node, the
    `slurm_spank_job_prolog` function is called at job start. It reads
    `GCSFUSE_MOUNTS` from the job environment and mounts the buckets.  The
    `slurm_spank_job_epilog` function is called at job end to unmount them. This
    ensures buckets are available for the entire duration of an `sbatch` or
    `salloc` session.

3.  **Remote (slurmd) context:** On the compute node, before each job step,
    `slurm_spank_user_init` is called. It checks the `GCSFUSE_MOUNTS`
    environment variable. If a mount point is not already active (e.g., it
    wasn't mounted by the prolog or it's a new mount requested specifically for
    this `srun`), it performs the mount.

4.  **Conflict Detection:** The plugin includes logic to prevent mounting
    different buckets to the same mount point. If such a conflict is detected
    (e.g., an `srun` attempt to override a job-level mount with a different
    bucket), the command will fail with an error message.

At the end of a job step, `slurm_spank_exit` unmounts any filesystems
specifically mounted for that step.

## Functionality

This plugin allows users to easily mount GCS buckets within their Slurm jobs.
The `--gcsfuse-mount` option can be used multiple times to mount multiple
buckets. It is supported by `srun`, `sbatch`, and `salloc`.

### Examples

1.  **Submit a batch job with a GCS mount:**
    ```bash
    sbatch --gcsfuse-mount=my-bucket:/tmp/my_bucket my_script.sh
    ```

2.  **Mount a bucket and list its contents with srun:**
    ```bash
    srun --gcsfuse-mount=my-bucket:/tmp/my_bucket ls /tmp/my_bucket
    ```

2.  **Mount a bucket with gcsfuse flags (e.g., read-only and implicit directories):**
    ```bash
    srun --gcsfuse-mount=my-bucket:/tmp/my_bucket:"-o ro --implicit-dirs" ls /tmp/my_bucket
    ```

3.  **Mount a specific directory within a bucket:**
    ```bash
    srun --gcsfuse-mount=my-bucket:/tmp/my_dir:"--only-dir my_folder" ls /tmp/my_dir
    ```

4.  **Mount a bucket to a relative path:**
    ```bash
    srun --gcsfuse-mount=my-bucket:./my_bucket ls ./my_bucket
    ```

5.  **Mount multiple buckets simultaneously:**
    ```bash
    srun --gcsfuse-mount=bucket-a:/tmp/a --gcsfuse-mount=bucket-b:./b ls /tmp/a ./b
    ```
