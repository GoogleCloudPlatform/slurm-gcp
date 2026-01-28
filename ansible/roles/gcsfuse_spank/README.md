# Slurm GCSFuse SPANK Plugin

A Slurm SPANK (Slurm Plug-in Architecture for Node Keyset) plugin that enables
users to mount Google Cloud Storage (GCS) buckets as local filesystems using
`gcsfuse` for the duration of a Slurm job or job step.

## Features

- **Consolidated Job Interface**: Manage mounts using the `--gcsfuse-mount` flag
  with `srun`, `sbatch`, and `salloc`.
- **Flexible Mounting Modes**: Supports mounting specific buckets or "All
  Buckets" mode.
- **Privilege Separation**: Forks and drops privileges to the job user before
  executing `gcsfuse`, ensuring security and correct file ownership.
- **Robust Lifecycle Management**:
    - Automatic polling for mount availability.
    - Captures `gcsfuse` logs in `syslog`/`journald` via `logger`.
    - Explicit PID tracking to ensure daemon termination.
    - Fallback to lazy unmount (`umount -l`) for hung or busy mountpoints.
- **Conflict Prevention**: Detects and prevents conflicting mount requests
  (e.g., mounting different buckets to the same path).

## Installation

### Prerequisites
- Slurm development headers (`slurm/spank.h`).
- `gcsfuse` installed on all compute nodes at `/usr/bin/gcsfuse`.
- `fusermount` (provided by the `fuse` or `fuse3` package).

### Building
Build the shared object using the provided `Makefile`. You may need to specify the path to your Slurm headers.

```bash
# Basic build
make SLURM_PATH=/path/to/slurm/include

# Build with AddressSanitizer for debugging
make SANITIZE=1

# Run static analysis
make analyze
```

### Deployment
The plugin **must be installed on all nodes** of the Slurm cluster (controllers,
login nodes, and compute nodes) to function correctly across different job
contexts.

You can use the provided install target to deploy the plugin:

```bash
# Install to default location (/usr/local/lib/slurm)
sudo make install

# Install to a specific Slurm library directory
sudo make install LIBDIR=/usr/lib64/slurm
```

After installation, add the plugin to your `plugstack.conf` file:
```text
optional /usr/lib64/slurm/gcsfuse_spank.so
```

## Usage

The plugin provides a single command-line option: `--gcsfuse-mount`. Multiple
mounts can be specified by separating them with semicolons or by supplying
multiple `--gscfuse-mount` flags.

### Syntax
`--gcsfuse-mount=[BUCKET:]MOUNT_POINT[:FLAGS]`

If the bucket name is omitted, the plugin defaults to **"All Buckets"** mode.
Any `FLAGS` will be passed directly to the `gcsfuse` command.

### Examples

**1. Mount a specific bucket**
```bash
srun --gcsfuse-mount=my-data-bucket:/home/user/data my_app.sh
```

**2. Mount all accessible buckets (Dynamic mode)**
```bash
# Both syntaxes are equivalent
srun --gcsfuse-mount=/home/user/gcs my_app.sh
srun --gcsfuse-mount=:/home/user/gcs my_app.sh
```

**3. Pass custom gcsfuse flags**
```bash
srun --gcsfuse-mount=my-bucket:/mnt/gcs:"--implicit-dirs --only-dir logs" my_app.sh
```

**4. Specify multiple mounts**
```bash
# Both syntaxes are equivalent. sbatch/salloc/srun are all valid.
sbatch --gcsfuse-mount="bucket1:/mnt/b1;bucket2:/mnt/b2" my_job.slurm
sbatch --gcsfuse-mount=bucket1:/mnt/b1 --gcsfuse-mount=bucket2:/mnt/b2 my_job.slurm
```

## Design and Implementation

### Context Handling
- **Local/Allocator Context (`srun`, `sbatch`, `salloc`)**: The plugin captures
  the user's mount requests, resolves any relative paths to absolute paths, and
  stores the configuration in the `GCSFUSE_MOUNTS` environment variable.
- **Remote Context (`slurmd`)**: Before the job step starts, the plugin parses
  `GCSFUSE_MOUNTS`, performs security validations, and invokes `gcsfuse`.

### Security Model
The plugin follows a "Least Privilege" model:
1. It verifies that the job user owns the target `MOUNT_POINT` and that it is an
   empty directory.
2. It forks a child process and uses `setresuid`/`setresgid` to drop all root
   privileges.
3. Only after dropping privileges does it `exec` the `gcsfuse` binary. This
   ensures that a user cannot use `gcsfuse` flags (like `--key-file`) to read
   files they do not already have permission to access on the host.

### Observability
Debugging FUSE mounts on remote compute nodes can be difficult. To solve this,
the plugin pipes all `gcsfuse` output to the system logger (`logger -t
gcsfuse_mount`). Admins and users can check `journalctl` or `/var/log/syslog` on
the compute node to see the exact reason for a mount failure.

### Cleanup
During the `exit` phase of a Slurm step:
1. It attempts a standard `fusermount -u`.
2. It sends `SIGKILL` to the tracked PID of the `gcsfuse` daemon.
3. If the mountpoint is still active (e.g., a hung network), it attempts a lazy
   unmount (`umount -l`).

## Development

The project includes several targets for maintaining code quality:
- `make lint`: Runs `cppcheck` with exhaustive branch analysis.
- `make format`: Formats code according to the `clang-format` project style.
- `make analyze`: Runs the Clang Static Analyzer (`scan-build`).
