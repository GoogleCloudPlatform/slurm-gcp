/*
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
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
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <slurm/spank.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <syslog.h>
#include <unistd.h>

/* --- Configuration Constants --- */
#define GCSFUSE_BIN "/usr/bin/gcsfuse"
#define MOUNT_WAIT_RETRIES 60
#define MOUNT_WAIT_SLEEP_US 500000

SPANK_PLUGIN(gcsfuse_mount, 1);

typedef struct {
  char* bucket;
  char* mount_point;
  char* flags;
} mount_spec_t;

// Global tracking for cleanup
static char** mount_points = NULL;
static pid_t* gcsfuse_pids = NULL;
static int mount_point_count = 0;

static void free_mount_spec(mount_spec_t* spec) {
  if (spec->bucket) free(spec->bucket);
  if (spec->mount_point) free(spec->mount_point);
  if (spec->flags) free(spec->flags);
}

/**
 * @brief Parses a mount string into a struct.
 *
 * Heuristics for token parsing (colon-delimited):
 * 1. If the first part contains a '/', it's treated as a local path (All
 * Buckets mode).
 * 2. If the first part is empty (e.g. ":mount"), it's Explicit All Buckets
 * mode.
 * 3. Otherwise, the first part is the GCS bucket name.
 */
static int parse_mount_spec(const char* token, mount_spec_t* spec) {
  if (!token) return -1;

  spec->bucket = NULL;
  spec->mount_point = NULL;
  spec->flags = NULL;

  char* my_token = strdup(token);
  if (!my_token) return -1;

  char* first_colon = strchr(my_token, ':');

  if (first_colon) {
    *first_colon = '\0';
    const char* part1 = my_token;
    char* part2 = first_colon + 1;

    if (strchr(part1, '/') != NULL) {
      // Case A: "/path/to/mount:flags"
      // Part 1 is a path. Bucket is NULL (All Buckets).
      spec->mount_point = strdup(part1);
      spec->flags = strdup(part2);
    } else if (part1[0] == '\0') {
      // Case B: ":mount"
      spec->bucket = strdup("");

      char* second_colon = strchr(part2, ':');
      if (second_colon) {
        *second_colon = '\0';
        spec->mount_point = strdup(part2);
        spec->flags = strdup(second_colon + 1);
      } else {
        spec->mount_point = strdup(part2);
      }
    } else {
      // Case C: "bucket:mount"
      spec->bucket = strdup(part1);

      char* second_colon = strchr(part2, ':');
      if (second_colon) {
        *second_colon = '\0';
        spec->mount_point = strdup(part2);
        spec->flags = strdup(second_colon + 1);
      } else {
        spec->mount_point = strdup(part2);
      }
    }
  } else {
    // Case D: "mount" (No colon) -> All Buckets
    spec->mount_point = strdup(my_token);
  }

  bool has_slash = (strchr(my_token, '/') != NULL);
  free(my_token);

  if (!spec->mount_point || (first_colon && !spec->flags && has_slash)) {
    // Basic check for strdup failures on required parts
    free_mount_spec(spec);
    return -1;
  }

  return 0;
}

static int handle_gcsfuse_mount(int val, const char* optarg, int remote);

static struct spank_option gcsfuse_mount_option = {
    "gcsfuse-mount",
    "BUCKET_NAME:MOUNT_POINT[:FLAGS]",
    "Mount a GCS bucket using gcsfuse",
    1,
    0,
    (spank_opt_cb_f)handle_gcsfuse_mount};

static bool is_mountpoint_logic(const char* path) {
  struct stat current_stat;
  if (stat(path, &current_stat) != 0) {
    /*
     * If transport endpoint is not connected, it's likely a hung FUSE mount.
     * We treat this as a mountpoint so we can attempt to unmount/remount it.
     */
    if (errno == ENOTCONN) return true;
    return false;
  }

  if (!S_ISDIR(current_stat.st_mode)) return false;
  if (strcmp(path, "/") == 0) return true;

  /*
   * Standard mountpoint detection: check if the device ID (st_dev) or
   * inode number (st_ino) of the directory differs from its parent.
   */
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

  return (current_stat.st_dev != parent_stat.st_dev) ||
         (current_stat.st_ino == parent_stat.st_ino);
}

/**
 * @brief Checks if a mountpoint exists by forking and dropping privileges.
 */
static bool is_mountpoint_as_user(const char* path, uid_t uid, gid_t gid) {
  pid_t pid = fork();
  if (pid == 0) {
    // Child: Drop to user and check

    // Suppress output
    int devnull = open("/dev/null", O_RDWR);
    if (devnull != -1) {
      dup2(devnull, STDOUT_FILENO);
      dup2(devnull, STDERR_FILENO);
      close(devnull);
    }

    if (setresgid(gid, gid, -1) != 0) _exit(1);
    if (setresuid(uid, uid, -1) != 0) _exit(1);

    if (is_mountpoint_logic(path)) {
      _exit(0);
    }

    openlog("gcsfuse-spank-check", LOG_PID, LOG_USER);
    syslog(LOG_ERR,
           "Check failed for %s: Directory exists but is not a mountpoint.",
           path);
    closelog();

    _exit(1);
  } else if (pid > 0) {
    int status;
    waitpid(pid, &status, 0);
    return (WIFEXITED(status) && WEXITSTATUS(status) == 0);
  }
  return false;
}

static bool is_directory_empty(const char* path) {
  DIR* dir = opendir(path);
  if (!dir) return false;

  const struct dirent* d;
  bool empty = true;
  while ((d = readdir(dir)) != NULL) {
    if (strcmp(d->d_name, ".") != 0 && strcmp(d->d_name, "..") != 0) {
      empty = false;
      break;
    }
  }
  closedir(dir);
  return empty;
}

static char* resolve_relative_mounts(const char* mounts_str, const char* cwd) {
  if (mounts_str == NULL || mounts_str[0] == '\0') {
    return strdup("");
  }
  char* alloc_cwd = NULL;
  if (cwd == NULL) {
    char buf[4096];
    if (getcwd(buf, sizeof(buf)) == NULL) {
      perror("getcwd failed");
      return NULL;
    }
    alloc_cwd = strdup(buf);
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
    mount_spec_t spec;
    if (parse_mount_spec(token, &spec) != 0) {
      slurm_error("gcsfuse-mount: Failed to parse mount token: %s", token);
      token = strtok_r(NULL, ";", &saveptr);
      continue;
    }

    char* absolute_mount_path = NULL;
    if (spec.mount_point && spec.mount_point[0] != '/') {
      const char* path_ptr = spec.mount_point;
      if (path_ptr[0] == '.' && path_ptr[1] == '/') path_ptr += 2;
      if (asprintf(&absolute_mount_path, "%s/%s", cwd, path_ptr) < 0)
        absolute_mount_path = NULL;
    } else {
      absolute_mount_path =
          spec.mount_point ? strdup(spec.mount_point) : strdup("");
    }

    char* final_token_part = NULL;
    char* flags_with_colon = NULL;
    if (spec.flags) {
      if (asprintf(&flags_with_colon, ":%s", spec.flags) < 0)
        flags_with_colon = NULL;
    } else {
      flags_with_colon = strdup("");
    }

    if (absolute_mount_path && flags_with_colon) {
      if (spec.bucket) {
        if (asprintf(&final_token_part, "%s:%s%s", spec.bucket,
                     absolute_mount_path, flags_with_colon) < 0) {
          final_token_part = NULL;
        }
      } else {
        if (asprintf(&final_token_part, "%s%s", absolute_mount_path,
                     flags_with_colon) < 0) {
          final_token_part = NULL;
        }
      }
    }

    free_mount_spec(&spec);
    free(absolute_mount_path);
    free(flags_with_colon);

    if (final_token_part) {
      char* new_result;
      if (result[0] == '\0') {
        new_result = strdup(final_token_part);
      } else {
        if (asprintf(&new_result, "%s;%s", result, final_token_part) < 0)
          new_result = NULL;
      }
      free(result);
      result = new_result;
      free(final_token_part);
    }

    if (result == NULL) break;
    token = strtok_r(NULL, ";", &saveptr);
  }

  free(mounts_copy);
  if (alloc_cwd) free(alloc_cwd);
  return result;
}

// static char *get_job_env_var(spank_t sp, const char *name) {
//   char **env = NULL;
//   if (spank_get_item(sp, S_JOB_ENV, &env) != ESPANK_SUCCESS || !env)
//     return NULL;
//   size_t name_len = strlen(name);
//   for (int i = 0; env[i]; i++) {
//     if (strncmp(env[i], name, name_len) == 0 && env[i][name_len] == '=') {
//       return strdup(env[i] + name_len + 1);
//     }
//   }
//   return NULL;
// }

static pid_t mount_gcsfuse(const char* bucket, const char* mount_point,
                           const char* flags, uid_t uid, gid_t gid) {
  const char* effective_bucket_arg = NULL;

  // --- 1. Determine Bucket Name ---
  // Logic: NULL or Empty String -> All Buckets (NULL arg to gcsfuse)
  //        Anything else -> Explicit Bucket
  if (bucket == NULL || bucket[0] == '\0') {
    effective_bucket_arg = NULL;
  } else {
    effective_bucket_arg = bucket;
  }

  // --- 2. Check if already mounted ---
  if (is_mountpoint_as_user(mount_point, uid, gid)) {
    slurm_spank_log("gcsfuse-mount: %s is already a mountpoint, skipping.",
                    mount_point);
    return 0;
  }

  // --- 3. Fork and Mount ---
  pid_t pid = fork();

  if (pid == 0) {
    // >>> CHILD PROCESS START <<<

    /*
     * A. Drop Privileges
     * We drop to the job user's UID/GID before calling gcsfuse. This ensures:
     * 1. gcsfuse runs with the user's permissions.
     * 2. Any user-provided flags (like --key-file) cannot be used to access
     *    privileged system files that the user themselves cannot read.
     */
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

    // B. Setup Environment
    struct passwd* pw = getpwuid(uid);
    if (pw) setenv("HOME", pw->pw_dir, 1);

    // C. Validate/Create Mount Point
    struct stat st;
    if (lstat(mount_point, &st) == 0) {
      if (!S_ISDIR(st.st_mode)) {
        slurm_error("gcsfuse-mount: Error: %s exists but is not a directory.",
                    mount_point);
        exit(1);
      }
      if (st.st_uid != uid) {
        slurm_error(
            "gcsfuse-mount: Security Error: You do not own the mount point %s.",
            mount_point);
        exit(1);
      }
      if (!is_directory_empty(mount_point)) {
        slurm_error("gcsfuse-mount: Error: Mount point %s is not empty.",
                    mount_point);
        exit(1);
      }
      if (access(mount_point, W_OK) != 0) {
        slurm_error("gcsfuse-mount: Permission denied. Cannot write to %s.",
                    mount_point);
        exit(1);
      }
    } else if (errno == ENOENT) {
      if (mkdir(mount_point, 0755) != 0) {
        slurm_error("gcsfuse-mount: failed to mkdir %s: %m", mount_point);
        exit(1);
      }
    } else {
      slurm_error("gcsfuse-mount: lstat failed on %s. Error: %d", mount_point,
                  errno);
      exit(1);
    }

    /*
     * D. Setup Logging
     * We use a nested fork to run the 'logger' command. The main child's
     * stdout/stderr are piped to logger's stdin. This ensures gcsfuse output
     * is captured in syslog/journald with a recognizable tag, making it
     * much easier to debug mount failures on remote nodes.
     */
    int log_pipe[2];
    if (pipe(log_pipe) == -1) exit(1);

    pid_t logger_pid = fork();
    if (logger_pid == 0) {
      close(log_pipe[1]);
      dup2(log_pipe[0], STDIN_FILENO);
      close(log_pipe[0]);
      int devnull = open("/dev/null", O_WRONLY);
      if (devnull != -1) {
        dup2(devnull, STDOUT_FILENO);
        dup2(devnull, STDERR_FILENO);
        close(devnull);
      }
      execlp("logger", "logger", "-t", "gcsfuse_mount", "-p", "user.info",
             NULL);
      _exit(127);
    }

    close(log_pipe[0]);
    dup2(log_pipe[1], STDOUT_FILENO);
    dup2(log_pipe[1], STDERR_FILENO);
    close(log_pipe[1]);
    int devnull = open("/dev/null", O_RDONLY);
    if (devnull != -1) {
      dup2(devnull, STDIN_FILENO);
      close(devnull);
    }

    char uid_str[20], gid_str[20];
    sprintf(uid_str, "%u", uid);
    sprintf(gid_str, "%u", gid);

    char* gcsfuse_argv[64];
    int j = 0;
    gcsfuse_argv[j++] = GCSFUSE_BIN;
    gcsfuse_argv[j++] = "--foreground";
    gcsfuse_argv[j++] = "-o";
    gcsfuse_argv[j++] = "allow_other";
    gcsfuse_argv[j++] = "--uid";
    gcsfuse_argv[j++] = uid_str;
    gcsfuse_argv[j++] = "--gid";
    gcsfuse_argv[j++] = gid_str;
    gcsfuse_argv[j++] = "--log-format";
    gcsfuse_argv[j++] = "json";

    if (flags) {
      char* flag_str = strdup(flags);
      if (flag_str) {
        char* flag = strtok(flag_str, " ");
        while (flag && j < 60) {
          gcsfuse_argv[j++] = flag;
          flag = strtok(NULL, " ");
        }
        // No need to free(flag_str) before execv, but good practice if execv
        // fails
      }
    }

    if (effective_bucket_arg) {
      gcsfuse_argv[j++] = (char*)effective_bucket_arg;
    }

    gcsfuse_argv[j++] = (char*)mount_point;
    gcsfuse_argv[j] = NULL;

    /* --- DEBUG: Print the full command --- */
    char debug_buffer[8192] = {0};
    int offset = 0;
    for (int k = 0; gcsfuse_argv[k] != NULL; k++) {
      int written =
          snprintf(debug_buffer + offset, sizeof(debug_buffer) - offset, "%s ",
                   gcsfuse_argv[k]);
      if (written > 0 && offset + written < (int)sizeof(debug_buffer)) {
        offset += written;
      } else {
        break;
      }
    }
    dprintf(STDERR_FILENO, "DEBUG: Executing: %s\n", debug_buffer);
    /* ------------------------------------- */

    execv(GCSFUSE_BIN, gcsfuse_argv);
    slurm_error("gcsfuse-mount: execv failed: %m");
    exit(1);

    // >>> CHILD PROCESS END <<<
  } else if (pid < 0) {
    slurm_error("gcsfuse-mount: fork failed: %m");
    return -1;
  }

  // --- 4. Parent Waits for Mount ---
  int found = 0;
  for (int k = 0; k < MOUNT_WAIT_RETRIES; k++) {
    if (is_mountpoint_as_user(mount_point, uid, gid)) {
      found = 1;
      break;
    }

    int status;
    if (waitpid(pid, &status, WNOHANG) != 0) {
      slurm_error(
          "gcsfuse-mount: mount process exited early (check "
          "permissions or syslog)");
      return -1;
    }
    usleep(MOUNT_WAIT_SLEEP_US);
  }

  if (!found) {
    slurm_error("gcsfuse-mount: timed out waiting for %s", mount_point);
    kill(pid, SIGKILL);
    waitpid(pid, NULL, 0);
    return -1;
  }

  return pid;
}

static int unmount_gcsfuse(const char* mount_point) {
  if (!is_mountpoint_logic(mount_point)) return 0;

  /*
   * We first attempt a standard user-space unmount using fusermount.
   * This is the cleanest way to shut down a FUSE filesystem.
   */
  pid_t pid = fork();
  if (pid == 0) {
    execlp("fusermount", "fusermount", "-u", mount_point, NULL);
    exit(1);
  } else if (pid > 0) {
    waitpid(pid, NULL, 0);
  }

  /*
   * Lazy unmount fallback.
   * If the mount is still busy or hung, we attempt a lazy unmount. Note that
   * this may require elevated privileges depending on system configuration,
   * but it's our last-resort effort to clean up the mountpoint.
   */
  if (is_mountpoint_logic(mount_point)) {
    slurm_info("gcsfuse-mount: lazy unmount %s", mount_point);
    pid_t lpid = fork();
    if (lpid == 0) {
      execlp("umount", "umount", "-l", mount_point, NULL);
      exit(1);
    } else if (lpid > 0)
      waitpid(lpid, NULL, 0);
  }

  return 0;
}

// static int process_mounts(const char *mount_env, uid_t uid, gid_t gid,
//                           int mode) {
//   slurm_error("gcsfuse-mount: Inside process_mounts. %s %d %d %d", mount_env,
//   uid, gid, mode); if (!mount_env)
//     return 0;
//   int rc = 0;
//   char *saveptr;
//   char *env_copy = strdup(mount_env);
//   char *optarg = strtok_r(env_copy, ";", &saveptr);
//
//   while (optarg) {
//     mount_spec_t spec;
//     if (parse_mount_spec(optarg, &spec) == 0) {
//       if (spec.mount_point) {
//         if (mode == 0) {
//           if (mount_gcsfuse(spec.bucket, spec.mount_point, spec.flags, uid,
//                             gid) < 0) {
//             rc = -1;
//           }
//         } else {
//           unmount_gcsfuse(spec.mount_point);
//
//           slurm_info("attempting to clean up %s.", spec.mount_point);
//
// 	  // --- CLEANUP: Attempt to remove directory ---
// 	  // Only remove if it is no longer a mountpoint.
// 	  if (!is_mountpoint_logic(spec.mount_point)) {
// 		  // rmdir is safe: it only removes if empty.
// 		  // If user left files, it fails (errno=ENOTEMPTY or EBUSY) and
// we ignore it. 		  if (rmdir(spec.mount_point) == 0) {
// slurm_info("gcsfuse-mount: removed empty mountpoint %s", spec.mount_point);
// } else {
// 			  // Debug only, failing to remove is fine (user might
// want to keep it) 			  slurm_info("gcsfuse-mount: could not
// remove %s: %m", spec.mount_point);
// 		  }
// 	  } else {
// 		  slurm_info("gcsfuse-mount: %s is still mount point",
// spec.mount_point);
// 	  }
// 	}
//       }
//       free_mount_spec(&spec);
//     }
//     if (rc != 0)
//       break;
//     optarg = strtok_r(NULL, ";", &saveptr);
//   }
//   free(env_copy);
//   return rc;
// }

static int check_mount_conflicts(const char* current_mounts,
                                 const char* new_mounts) {
  if (!current_mounts || !new_mounts) return 0;

  char* new_copy = strdup(new_mounts);
  char* saveptr_new;
  const char* new_token = strtok_r(new_copy, ";", &saveptr_new);

  while (new_token) {
    mount_spec_t new_spec;
    if (parse_mount_spec(new_token, &new_spec) == 0) {
      if (new_spec.mount_point) {
        char* cur_copy = strdup(current_mounts);
        char* saveptr_cur;
        const char* cur_token = strtok_r(cur_copy, ";", &saveptr_cur);

        while (cur_token) {
          mount_spec_t cur_spec;
          if (parse_mount_spec(cur_token, &cur_spec) == 0) {
            if (cur_spec.mount_point) {
              if (strcmp(new_spec.mount_point, cur_spec.mount_point) == 0) {
                bool bucket_conflict = false;
                const char* b1 = new_spec.bucket ? new_spec.bucket : "";
                const char* b2 = cur_spec.bucket ? cur_spec.bucket : "";

                if (strcmp(b1, b2) != 0) bucket_conflict = true;

                if (bucket_conflict) {
                  slurm_error(
                      "gcsfuse-mount: Conflict! Mountpoint '%s' is already "
                      "assigned to bucket '%s'. Cannot mount bucket '%s'.",
                      new_spec.mount_point, b2[0] ? b2 : "(all)",
                      b1[0] ? b1 : "(all)");
                  free_mount_spec(&cur_spec);
                  free(cur_copy);
                  free_mount_spec(&new_spec);
                  free(new_copy);
                  return -1;
                }
              }
            }
            free_mount_spec(&cur_spec);
          }
          cur_token = strtok_r(NULL, ";", &saveptr_cur);
        }
        free(cur_copy);
      }
      free_mount_spec(&new_spec);
    }
    new_token = strtok_r(NULL, ";", &saveptr_new);
  }
  free(new_copy);
  return 0;
}

int slurm_spank_init(spank_t sp, int ac, char** av) {
  (void)ac;
  (void)av;
  if (spank_context() == S_CTX_LOCAL || spank_context() == S_CTX_ALLOCATOR ||
      spank_context() == S_CTX_REMOTE) {
    return spank_option_register(sp, &gcsfuse_mount_option);
  }
  return 0;
}

static int handle_gcsfuse_mount(int val, const char* optarg, int remote) {
  (void)val;
  (void)remote;
  char* new_mounts;
  const char* current_mounts = getenv("GCSFUSE_MOUNTS");
  const char* next_mount = resolve_relative_mounts(optarg, NULL);

  if (next_mount == NULL) return -1;

  if (check_mount_conflicts(current_mounts, next_mount) != 0) {
    free((void*)next_mount);
    return -1;
  }

  if (current_mounts && current_mounts[0] != '\0') {
    if (asprintf(&new_mounts, "%s;%s", current_mounts, next_mount) < 0)
      new_mounts = NULL;
  } else {
    if (asprintf(&new_mounts, "%s", next_mount) < 0) new_mounts = NULL;
  }

  if (new_mounts) {
    setenv("GCSFUSE_MOUNTS", new_mounts, 1);
    free(new_mounts);
  } else {
    /*
     * If memory allocation for the environment variable fails, we abort the
     * mount request to prevent inconsistent job state.
     */
    slurm_error(
        "gcsfuse-mount: memory allocation failed in handle_gcsfuse_mount");
    free((void*)next_mount);
    return -1;
  }
  free((void*)next_mount);
  return 0;
}

int slurm_spank_user_init(spank_t sp, int ac, char** av) {
  (void)ac;
  (void)av;
  uid_t uid;
  gid_t gid;
  char mount_env_buf[4096];

  if (spank_get_item(sp, S_JOB_UID, &uid) != 0 ||
      spank_get_item(sp, S_JOB_GID, &gid) != 0)
    return -1;

  if (spank_getenv(sp, "GCSFUSE_MOUNTS", mount_env_buf,
                   sizeof(mount_env_buf)) != ESPANK_SUCCESS)
    return 0;

  char* env_copy = strdup(mount_env_buf);
  char* saveptr;
  const char* optarg = strtok_r(env_copy, ";", &saveptr);
  int rc = 0;

  while (optarg) {
    mount_spec_t spec;
    if (parse_mount_spec(optarg, &spec) == 0) {
      if (spec.mount_point) {
        if (!is_mountpoint_logic(spec.mount_point)) {
          pid_t pid = mount_gcsfuse(spec.bucket, spec.mount_point, spec.flags,
                                    uid, gid);
          if (pid > 0) {
            /*
             * Track both the mount point path and the PID of the gcsfuse
             * daemon. Tracking the PID is necessary because gcsfuse is started
             * in the foreground but managed by this plugin; we need to
             * explicitly terminate it during cleanup to prevent leaked
             * processes.
             */
            char** next_points =
                realloc(mount_points, (mount_point_count + 1) * sizeof(char*));
            if (!next_points) {
              slurm_error("gcsfuse-mount: Failed to realloc mount_points");
              rc = -1;
            } else {
              mount_points = next_points;
              pid_t* next_pids = realloc(
                  gcsfuse_pids, (mount_point_count + 1) * sizeof(pid_t));
              if (!next_pids) {
                slurm_error("gcsfuse-mount: Failed to realloc gcsfuse_pids");
                rc = -1;
              } else {
                gcsfuse_pids = next_pids;
                mount_points[mount_point_count] = strdup(spec.mount_point);
                gcsfuse_pids[mount_point_count] = pid;
                mount_point_count++;
              }
            }
          } else {
            rc = -1;
          }
        }
      }
      free_mount_spec(&spec);
    }
    if (rc != 0) break;
    optarg = strtok_r(NULL, ";", &saveptr);
  }
  free(env_copy);
  return rc;
}

int slurm_spank_exit(spank_t sp, int ac, char** av) {
  (void)sp;
  (void)ac;
  (void)av;
  if (spank_context() != S_CTX_REMOTE) return 0;
  for (int i = 0; i < mount_point_count; i++) {
    unmount_gcsfuse(mount_points[i]);
    if (gcsfuse_pids[i] > 0) {
      kill(gcsfuse_pids[i], SIGKILL);
      waitpid(gcsfuse_pids[i], NULL, 0);
    }
    free(mount_points[i]);
  }
  if (mount_points) free(mount_points);
  if (gcsfuse_pids) free(gcsfuse_pids);
  mount_point_count = 0;
  return 0;
}
