/*
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file gcsfuse_spank.c
 * @brief A Slurm SPANK plugin to mount GCS buckets using gcsfuse.
 *
 * This plugin allows users to specify GCS buckets to be mounted for the
 * duration of their Slurm job step. It provides a `--gcsfuse-mount` option
 * to `srun`, `sbatch`, and `salloc`.
 *
 * The plugin operates in three main contexts:
 * 1. Local/Allocator (srun/sbatch/salloc): Captures `--gcsfuse-mount`, resolves
 *    paths, and sets `GCSFUSE_MOUNTS` environment variable for the job.
 * 2. Job Script (prolog/epilog): Runs on the compute node. Prolog mounts the
 * buckets defined in `GCSFUSE_MOUNTS` (from job env). Epilog unmounts them.
 * 3. Remote (slurmd/user_init): Runs before each job step. Checks if already
 * mounted. If not (e.g. srun with new options), mounts them.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <slurm/spank.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

SPANK_PLUGIN(gcsfuse, 1);

// Global array to store the mount points for cleanup in user_init/exit context.
// Note: This is NOT shared with prolog/epilog context.
static char** mount_points = NULL;
static int mount_point_count = 0;

// Forward declaration for the callback.
static int handle_gcsfuse_mount(int val, const char* optarg, int remote);

// Option definition.
static struct spank_option gcsfuse_mount_option = {
    "gcsfuse-mount",
    "BUCKET_NAME:MOUNT_POINT[:FLAGS]",
    "Mount a GCS bucket using gcsfuse",
    1,
    0,
    (spank_opt_cb_f)handle_gcsfuse_mount};

/**
 * @brief Checks if a given path is a mountpoint.
 */
static bool is_mountpoint(const char* path) {
  struct stat current_stat;
  if (stat(path, &current_stat) != 0) return false;
  if (!S_ISDIR(current_stat.st_mode)) return false;
  if (strcmp(path, "/") == 0) return true;

  size_t parent_path_len = strlen(path) + 4;
  char* parent_path = malloc(parent_path_len);
  if (!parent_path) return false;
  snprintf(parent_path, parent_path_len, "%s/..", path);

  struct stat parent_stat;
  if (stat(parent_path, &parent_stat) != 0) {
    free(parent_path);
    return false;
  }
  free(parent_path);

  if (current_stat.st_dev != parent_stat.st_dev) return true;
  if (current_stat.st_ino == parent_stat.st_ino) return true;

  return false;
}

/**
 * @brief Resolves relative paths in a semicolon-delimited mount string.
 */
static char* resolve_relative_mounts(const char* mounts_str, const char* cwd) {
  if (mounts_str == NULL || mounts_str[0] == '\0') {
    return strdup("");
  }

  // If cwd is not provided, use current working directory
  char* alloc_cwd = NULL;
  if (cwd == NULL) {
    alloc_cwd = getcwd(NULL, 0);
    if (alloc_cwd == NULL) {
      perror("getcwd failed");
      return NULL;
    }
    cwd = alloc_cwd;
  }

  char* mounts_copy = strdup(mounts_str);
  if (!mounts_copy) {
    if (alloc_cwd) free(alloc_cwd);
    return NULL;
  }

  char* result = strdup("");
  if (!result) {
    free(mounts_copy);
    if (alloc_cwd) free(alloc_cwd);
    return NULL;
  }

  char* saveptr;
  char* token = strtok_r(mounts_copy, ";", &saveptr);

  while (token != NULL) {
    char* bucket = NULL;
    char* mount_path = NULL;
    const char* mount_options = "";

    char* first_colon = strchr(token, ':');
    char* path_part;

    if (first_colon) {
      const char* slash_before_colon = memchr(token, '/', first_colon - token);
      if (slash_before_colon == NULL) {
        bucket = strndup(token, first_colon - token);
        path_part = first_colon + 1;
      } else {
        path_part = token;
      }
    } else {
      path_part = token;
    }

    char* second_colon = strchr(path_part, ':');
    if (second_colon) {
      mount_path = strndup(path_part, second_colon - path_part);
      mount_options = second_colon;
    } else {
      mount_path = strdup(path_part);
    }

    char* absolute_mount_path = NULL;
    if (mount_path && mount_path[0] != '/') {
      const char* path_ptr = mount_path;
      if (path_ptr[0] == '.' && path_ptr[1] == '/') {
        path_ptr += 2;
      }
      if (asprintf(&absolute_mount_path, "%s/%s", cwd, path_ptr) < 0) {
        absolute_mount_path = NULL;
      }
    } else {
      absolute_mount_path = mount_path ? strdup(mount_path) : strdup("");
    }

    char* final_token_part = NULL;
    if (bucket) {
      if (asprintf(&final_token_part, "%s:%s%s", bucket, absolute_mount_path,
                   mount_options) < 0) {
        final_token_part = NULL;
      }
    } else {
      if (asprintf(&final_token_part, "%s%s", absolute_mount_path,
                   mount_options) < 0) {
        final_token_part = NULL;
      }
    }

    free(bucket);
    free(mount_path);
    free(absolute_mount_path);

    if (final_token_part) {
      char* new_result;
      if (result[0] == '\0') {
        new_result = strdup(final_token_part);
      } else {
        if (asprintf(&new_result, "%s;%s", result, final_token_part) < 0) {
          new_result = NULL;
        }
      }
      free(result);
      result = new_result;
      free(final_token_part);
    }

    if (result == NULL) {
      break;
    }

    token = strtok_r(NULL, ";", &saveptr);
  }

  free(mounts_copy);
  if (alloc_cwd) free(alloc_cwd);
  return result;
}

/**
 * @brief Helper to look up an environment variable in S_JOB_ENV.
 */
static char* get_job_env_var(spank_t sp, const char* name) {
  char** env = NULL;
  if (spank_get_item(sp, S_JOB_ENV, &env) != ESPANK_SUCCESS || !env) {
    return NULL;
  }

  size_t name_len = strlen(name);
  for (int i = 0; env[i]; i++) {
    if (strncmp(env[i], name, name_len) == 0 && env[i][name_len] == '=') {
      return strdup(env[i] + name_len + 1);
    }
  }
  return NULL;
}

/**
 * @brief Mounts a single bucket.
 */
static int mount_gcsfuse(const char* bucket, const char* mount_point,
                         const char* flags, uid_t uid, gid_t gid) {
  if (is_mountpoint(mount_point)) {
    slurm_spank_log("gcsfuse-mount: %s is already a mountpoint, skipping.",
                    mount_point);
    return 0;  // Already mounted
  }

  // Create directory.
  // If running as root (prolog), mkdir sets owner to root, so we must chown.
  // If running as user (user_init), mkdir sets owner to user.
  if (mkdir(mount_point, 0755) != 0 && errno != EEXIST) {
    slurm_error("gcsfuse-mount: failed to mkdir %s: %m", mount_point);
    return -1;
  }

  if (geteuid() == 0) {
    if (chown(mount_point, uid, gid) != 0) {
      slurm_error("gcsfuse-mount: failed to chown %s: %m", mount_point);
      // Proceeding anyway, gcsfuse might still work if allow_other is used?
      // But usually it needs access.
    }
  }

  pid_t pid = fork();
  if (pid == 0) {
    // Child process

    // If running as root, switch to user.
    if (geteuid() == 0) {
      if (setresgid(gid, gid, -1) != 0) {
        slurm_error("gcsfuse-mount: setresgid failed: %m");
        exit(1);
      }
      if (setresuid(uid, uid, -1) != 0) {
        slurm_error("gcsfuse-mount: setresuid failed: %m");
        exit(1);
      }
    }

    char uid_str[20], gid_str[20];
    sprintf(uid_str, "%d", uid);
    sprintf(gid_str, "%d", gid);

    char* gcsfuse_argv[64];  // Increased size to prevent overflow
    int j = 0;
    gcsfuse_argv[j++] = "gcsfuse";
    gcsfuse_argv[j++] = "-o";
    gcsfuse_argv[j++] = "allow_other";
    gcsfuse_argv[j++] = "--uid";
    gcsfuse_argv[j++] = uid_str;
    gcsfuse_argv[j++] = "--gid";
    gcsfuse_argv[j++] = gid_str;
    gcsfuse_argv[j++] = "--file-mode=644";
    gcsfuse_argv[j++] = "--dir-mode=755";
    // gcsfuse_argv[j++] = "--foreground"; // Not needed if we wait for
    // mountpoint? Actually gcsfuse forks by default. We want that.

    if (flags) {
      char* flag_str = strdup(flags);
      char* flag = strtok(flag_str, " ");
      while (flag &&
             j < 60) {  // Limit to 60 to leave room for bucket, mp, NULL
        gcsfuse_argv[j++] = flag;
        flag = strtok(NULL, " ");
      }
      // free(flag_str); // Leaked in child, doesn't matter
    }
    gcsfuse_argv[j++] = (char*)bucket;
    gcsfuse_argv[j++] = (char*)mount_point;
    gcsfuse_argv[j] = NULL;

    execvp("gcsfuse", gcsfuse_argv);
    slurm_error("gcsfuse-mount: execvp failed for gcsfuse: %m");
    exit(1);
  } else if (pid < 0) {
    slurm_error("gcsfuse-mount: fork failed: %m");
    return -1;
  }

  // Wait for the mount to become responsive.
  int found = 0;
  for (int k = 0; k < 20; k++) {  // Wait up to 10 seconds
    if (is_mountpoint(mount_point)) {
      found = 1;
      break;
    }
    usleep(500000);  // 500ms
  }
  if (!found) {
    slurm_error("gcsfuse-mount: timed out waiting for mountpoint '%s'",
                mount_point);
    // Maybe try to kill the gcsfuse process if we knew its PID?
    // But gcsfuse forks, so 'pid' above is the parent of the daemon.
    return -1;
  }
  return 0;
}

/**
 * @brief Unmounts a bucket.
 */
static int unmount_gcsfuse(const char* mount_point) {
  pid_t pid = fork();
  if (pid == 0) {
    execlp("fusermount", "fusermount", "-u", mount_point, NULL);
    slurm_error("gcsfuse-mount: execlp failed for fusermount: %m");
    exit(1);
  } else if (pid > 0) {
    int status;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) != 0) {
      slurm_error("gcsfuse-mount: fusermount exited with error for %s",
                  mount_point);
      return -1;
    }
  }
  return 0;
}

/**
 * @brief Parse mounts string and perform action (mount/unmount).
 * mode: 0=mount, 1=unmount
 */
static void process_mounts(const char* mount_env, uid_t uid, gid_t gid,
                           int mode) {
  if (!mount_env) return;

  char* saveptr;
  char* env_copy = strdup(mount_env);
  char* optarg = strtok_r(env_copy, ";", &saveptr);

  while (optarg) {
    char *bucket = NULL, *mount_point = NULL, *flags = NULL;
    char* arg = strdup(optarg);

    bucket = strtok(arg, ":");
    mount_point = strtok(NULL, ":");
    flags = strtok(NULL, "");

    if (bucket && mount_point) {
      if (mode == 0) {
        if (mount_gcsfuse(bucket, mount_point, flags, uid, gid) == 0) {
          // If called from user_init, we might want to track this.
          // But here we are generic.
        }
      } else {
        unmount_gcsfuse(mount_point);
      }
    }
    free(arg);
    optarg = strtok_r(NULL, ";", &saveptr);
  }
  free(env_copy);
}

/**
 * @brief Check for mount conflicts.
 *
 * Checks if any mount point in `new_mounts` uses the same mount point as
 * an entry in `current_mounts` but with a different bucket.
 *
 * @return 0 if no conflict, -1 if conflict found.
 */
static int check_mount_conflicts(const char* current_mounts,
                                 const char* new_mounts) {
  if (!current_mounts || !new_mounts) return 0;

  char* new_copy = strdup(new_mounts);
  char* saveptr_new;
  char* new_token = strtok_r(new_copy, ";", &saveptr_new);

  while (new_token) {
    char *new_bucket = NULL, *new_path = NULL;
    char* new_arg = strdup(new_token);

    new_bucket = strtok(new_arg, ":");
    new_path = strtok(NULL, ":");

    if (new_bucket && new_path) {
      char* cur_copy = strdup(current_mounts);
      char* saveptr_cur;
      char* cur_token = strtok_r(cur_copy, ";", &saveptr_cur);

      while (cur_token) {
        char *cur_bucket = NULL, *cur_path = NULL;
        char* cur_arg = strdup(cur_token);

        cur_bucket = strtok(cur_arg, ":");
        cur_path = strtok(NULL, ":");

        if (cur_bucket && cur_path) {
          if (strcmp(new_path, cur_path) == 0) {
            if (strcmp(new_bucket, cur_bucket) != 0) {
              slurm_error(
                  "gcsfuse-mount: Conflict! Mountpoint '%s' is already "
                  "assigned to bucket '%s'. Cannot mount bucket '%s'.",
                  new_path, cur_bucket, new_bucket);
              free(cur_arg);
              free(cur_copy);
              free(new_arg);
              free(new_copy);
              return -1;
            }
          }
        }
        free(cur_arg);
        cur_token = strtok_r(NULL, ";", &saveptr_cur);
      }
      free(cur_copy);
    }
    free(new_arg);
    new_token = strtok_r(NULL, ";", &saveptr_new);
  }
  free(new_copy);
  return 0;
}

/**
 * @brief SPANK plugin initialization.
 */
int slurm_spank_init(spank_t sp, int ac, char** av) {
  // Register option in LOCAL (srun) and ALLOCATOR (sbatch/salloc) contexts.
  if (spank_context() == S_CTX_LOCAL || spank_context() == S_CTX_ALLOCATOR) {
    return spank_option_register(sp, &gcsfuse_mount_option);
  }
  return 0;
}

/**
 * @brief SPANK callback for the --gcsfuse-mount option.
 */
static int handle_gcsfuse_mount(int val, const char* optarg, int remote) {
  char* new_mounts;
  const char* current_mounts = getenv("GCSFUSE_MOUNTS");
  const char* next_mount = resolve_relative_mounts(optarg, NULL);  // Use cwd

  if (next_mount == NULL) {
    slurm_error("gcsfuse-mount: Failed to resolve mount arguments");
    return -1;
  }

  if (check_mount_conflicts(current_mounts, next_mount) != 0) {
    free((void*)next_mount);  // cast to void* to suppress const warning if any,
                              // though it's allocated
    return -1;
  }

  if (current_mounts && current_mounts[0] != '\0') {
    asprintf(&new_mounts, "%s;%s", current_mounts, next_mount);
  } else {
    asprintf(&new_mounts, "%s", next_mount);
  }

  if (new_mounts == NULL) {
    slurm_error("gcsfuse-mount: asprintf failed");
    free((void*)next_mount);
    return -1;
  }

  // Set the environment variable.
  // In sbatch/salloc (allocator), this modifies the job environment.
  // In srun (local), this modifies the step environment.
  setenv("GCSFUSE_MOUNTS", new_mounts, 1);
  free(new_mounts);
  free((void*)next_mount);

  return 0;
}

/**
 * @brief Job Prolog: Mount buckets on the node.
 */
int slurm_spank_job_prolog(spank_t sp, int ac, char** av) {
  uid_t uid;
  gid_t gid;

  if (spank_get_item(sp, S_JOB_UID, &uid) != ESPANK_SUCCESS ||
      spank_get_item(sp, S_JOB_GID, &gid) != ESPANK_SUCCESS) {
    return 0;
  }

  // Get mounts from job environment
  char* mount_env = get_job_env_var(sp, "GCSFUSE_MOUNTS");
  if (mount_env) {
    // slurm_spank_log("gcsfuse-mount: prolog mounting: %s", mount_env);
    process_mounts(mount_env, uid, gid, 0);  // Mount
    free(mount_env);
  }
  return 0;
}

/**
 * @brief Job Epilog: Unmount buckets on the node.
 */
int slurm_spank_job_epilog(spank_t sp, int ac, char** av) {
  // We don't need UID/GID to unmount (root can do it).
  // But we need the list of mounts.
  char* mount_env = get_job_env_var(sp, "GCSFUSE_MOUNTS");
  if (mount_env) {
    // slurm_spank_log("gcsfuse-mount: epilog unmounting: %s", mount_env);
    process_mounts(mount_env, 0, 0, 1);  // Unmount
    free(mount_env);
  }
  return 0;
}

/**
 * @brief User Init: Mount buckets for the step (if not already mounted).
 */
int slurm_spank_user_init(spank_t sp, int ac, char** av) {
  uid_t uid;
  gid_t gid;
  char mount_env_buf[4096];

  if (spank_get_item(sp, S_JOB_UID, &uid) != 0 ||
      spank_get_item(sp, S_JOB_GID, &gid) != 0) {
    return -1;
  }

  // Use spank_getenv for remote context
  if (spank_getenv(sp, "GCSFUSE_MOUNTS", mount_env_buf,
                   sizeof(mount_env_buf)) != ESPANK_SUCCESS) {
    return 0;
  }

  const char* mount_env = mount_env_buf;
  char* env_copy = strdup(mount_env);
  char* saveptr;
  char* optarg = strtok_r(env_copy, ";", &saveptr);

  while (optarg) {
    char *bucket = NULL, *mount_point = NULL, *flags = NULL;
    char* arg = strdup(optarg);

    bucket = strtok(arg, ":");
    mount_point = strtok(NULL, ":");
    flags = strtok(NULL, "");

    if (bucket && mount_point) {
      // Check if already mounted (e.g. by prolog)
      if (!is_mountpoint(mount_point)) {
        slurm_info("gcsfuse-mount: user_init mounting %s to %s", bucket,
                   mount_point);
        if (mount_gcsfuse(bucket, mount_point, flags, uid, gid) == 0) {
          // Track this mount point for cleanup in exit
          char** temp_points =
              realloc(mount_points, (mount_point_count + 1) * sizeof(char*));
          if (temp_points) {
            mount_points = temp_points;
            mount_points[mount_point_count++] = strdup(mount_point);
          } else {
            slurm_error("gcsfuse-mount: realloc failed for mount_points");
            // If realloc fails, we can't track it, so we won't unmount it?
            // Serious error. But mounting succeeded.
          }
        }
      } else {
        // slurm_info("gcsfuse-mount: user_init skipping %s (already mounted)",
        // mount_point);
      }
    }
    free(arg);
    optarg = strtok_r(NULL, ";", &saveptr);
  }
  free(env_copy);
  return 0;
}

/**
 * @brief User Exit: Unmount buckets mounted by this step.
 */
int slurm_spank_exit(spank_t sp, int ac, char** av) {
  if (spank_context() != S_CTX_REMOTE) {
    return 0;
  }

  slurm_info("gcsfuse-mount: exit called. Cleaning up %d mounts.",
             mount_point_count);

  for (int i = 0; i < mount_point_count; i++) {
    slurm_info("gcsfuse-mount: unmounting %s", mount_points[i]);
    unmount_gcsfuse(mount_points[i]);
    free(mount_points[i]);
  }
  if (mount_points) {
    free(mount_points);
    mount_points = NULL;
    mount_point_count = 0;
  }
  return 0;
}
