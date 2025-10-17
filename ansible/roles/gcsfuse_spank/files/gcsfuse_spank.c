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
 * to `srun`.
 *
 * The plugin operates in two main contexts:
 * 1. Local (srun): It captures the `--gcsfuse-mount` arguments, resolves
 *    any relative paths to absolute paths based on the user's submission
 *    directory, and passes them to the remote end via an environment variable.
 * 2. Remote (slurmd): On the compute node, it reads the environment variable,
 *    forks a `gcsfuse` process for each requested mount, waits for the mount
 *    to become ready, and then allows the user's job to run. At the end of
 *    the job step, it cleans up by unmounting all filesystems.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <slurm/spank.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <stdbool.h>

/**
 * @brief Checks if a given path is a mountpoint.
 *
 * This function determines if a path is a mountpoint by comparing the
 * device ID of the path with the device ID of its parent directory.
 * If the device IDs are different, it indicates that the path is the
 * entry point for a different filesystem. The root directory ("/") is
 * handled as a special case and is always considered a mountpoint.
 *
 * Note: This method is POSIX-compliant. However, it may not detect
 * all types of mounts, such as bind mounts on Linux where the device
 * ID might not change. For most common use cases, this approach is
 * reliable.
 *
 * @param path The filesystem path to check.
 * @return `true` if the path is a mountpoint, `false` otherwise.
 */
bool is_mountpoint(const char *path) {
    struct stat current_stat;

    // First, get the stat of the provided path.
    // If it fails, the path likely doesn't exist or is inaccessible.
    if (stat(path, &current_stat) != 0) {
        //perror("stat failed for path");
        return false;
    }

    // A mountpoint must be a directory.
    if (!S_ISDIR(current_stat.st_mode)) {
        return false;
    }

    // The root directory "/" is always a mountpoint.
    if (strcmp(path, "/") == 0) {
        return true;
    }

    // To check if it's a mountpoint, we compare its device ID with its parent's.
    // We construct the parent path by appending "/..".
    // e.g., for "/home/user", the parent path becomes "/home/user/..".
    size_t parent_path_len = strlen(path) + 4; // for "/..\0"
    char *parent_path = malloc(parent_path_len);
    if (parent_path == NULL) {
        perror("Failed to allocate memory for parent path");
        return false; // Cannot proceed without memory
    }
    snprintf(parent_path, parent_path_len, "%s/..", path);

    struct stat parent_stat;
    // Get the stat of the parent directory.
    if (stat(parent_path, &parent_stat) != 0) {
        //perror("stat failed for parent path");
        free(parent_path);
        return false;
    }

    free(parent_path);

    // If the device ID of the path is different from its parent's,
    // it's a mountpoint.
    if (current_stat.st_dev != parent_stat.st_dev) {
        return true;
    }

    // Additionally, if the inode number is the same, it implies we are at the
    // root of a filesystem (like in the case of "/"). While the root case is
    // handled above, this check can also identify it.
    if (current_stat.st_ino == parent_stat.st_ino) {
        return true;
    }

    return false;
}


/**
 * @brief Resolves relative paths in a semicolon-delimited mount string.
 *
 * This function parses a string of mount specifications, separated by
 * semicolons. Each specification can be in the format:
 * [bucket:]path[:"options"]
 *
 * It identifies the 'path' component. If the path is relative (does not
 * start with '/'), it is prepended with the current working directory. The
 * final resolved string is reassembled.
 *
 * Example:
 * Input: "bucket1:./gcs:\"-o ro\";/abs/path;rel_path"
 * CWD: "/home/user"
 * Output: "bucket1:/home/user/gcs:\"-o ro\";/abs/path;/home/user/rel_path"
 *
 * @param mounts_str The semicolon-delimited string of mounts.
 * @return A new dynamically allocated string with all paths resolved.
 * The caller is responsible for freeing this memory.
 * Returns NULL on error (e.g., memory allocation failure).
 */
char* resolve_relative_mounts(const char *mounts_str) {
    if (mounts_str == NULL || mounts_str[0] == '\0') {
        return strdup("");
    }

    char *cwd = getcwd(NULL, 0);
    if (cwd == NULL) {
        perror("getcwd failed");
        return NULL;
    }

    char *mounts_copy = strdup(mounts_str);
    if (mounts_copy == NULL) {
        perror("strdup for mounts_copy failed");
        free(cwd);
        return NULL;
    }

    char *result = strdup("");
    if (result == NULL) {
        perror("strdup for result failed");
        free(cwd);
        free(mounts_copy);
        return NULL;
    }

    char *saveptr;
    char *token = strtok_r(mounts_copy, ";", &saveptr);

    while (token != NULL) {
        char *bucket = NULL;
        char *mount_path = NULL;
        const char *mount_options = ""; // Default to empty string

        char *first_colon = strchr(token, ':');
        char *path_part;

        if (first_colon) {
            // Format is bucket:path... or path:options...
            // Check if the part before the colon contains a '/', if not it's a bucket
            char *slash_before_colon = memchr(token, '/', first_colon - token);
            if (slash_before_colon == NULL) {
                 bucket = strndup(token, first_colon - token);
                 path_part = first_colon + 1;
            } else {
                 path_part = token;
            }
        } else {
            path_part = token;
        }

        char *second_colon = strchr(path_part, ':');
        if (second_colon) {
            mount_path = strndup(path_part, second_colon - path_part);
            mount_options = second_colon; // Includes the leading ':'
        } else {
            mount_path = strdup(path_part);
        }

        char *absolute_mount_path = NULL;
        if (mount_path && mount_path[0] != '/') {
            const char *path_ptr = mount_path;
            if (path_ptr[0] == '.' && path_ptr[1] == '/') {
                path_ptr += 2;
            }
            if (asprintf(&absolute_mount_path, "%s/%s", cwd, path_ptr) < 0) {
                 absolute_mount_path = NULL;
            }
        } else {
            absolute_mount_path = mount_path ? strdup(mount_path) : strdup("");
        }

        char *final_token_part = NULL;
        if (bucket) {
            if (asprintf(&final_token_part, "%s:%s%s", bucket, absolute_mount_path, mount_options) < 0) {
                final_token_part = NULL;
            }
        } else {
            if (asprintf(&final_token_part, "%s%s", absolute_mount_path, mount_options) < 0) {
                 final_token_part = NULL;
            }
        }

        free(bucket);
        free(mount_path);
        free(absolute_mount_path);

        if (final_token_part) {
            char *new_result;
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
             perror("Failed to build result string");
             break;
        }

        token = strtok_r(NULL, ";", &saveptr);
    }

    free(mounts_copy);
    free(cwd);
    return result;
}

SPANK_PLUGIN(gcsfuse, 1);

// Global array to store the mount points for cleanup.
static char **mount_points = NULL;
static int mount_point_count = 0;

// Forward declaration for the callback.
static int handle_gcsfuse_mount(int val, const char *optarg, int remote);

// Option definition.
static struct spank_option gcsfuse_mount_option = {
    "gcsfuse-mount",
    "BUCKET_NAME:MOUNT_POINT[:FLAGS]",
    "Mount a GCS bucket using gcsfuse",
    1, 0,
    (spank_opt_cb_f)handle_gcsfuse_mount
};

/**
 * @brief SPANK plugin initialization function.
 *
 * This function is called by Slurm when the plugin is loaded. It runs in
 * both the local (srun) and remote (slurmd) contexts.
 *
 * In the local context, it simply registers the `--gcsfuse-mount` option.
 * In the remote context, it is a no-op, as the mounting logic is handled
 * in `slurm_spank_user_init`.
 *
 * @param sp The SPANK handle.
 * @param ac The argument count.
 * @param av The argument vector.
 * @return 0 on success, -1 on failure.
 */
int slurm_spank_init(spank_t sp, int ac, char **av) {
    if (spank_context() == S_CTX_LOCAL) {
        return spank_option_register(sp, &gcsfuse_mount_option);
    }
    return 0;
}

/**
 * @brief SPANK plugin exit function.
 *
 * This function is called by Slurm just before the job step finishes. It
 * runs in the remote (slurmd) context.
 *
 * Its primary responsibility is to clean up by unmounting all the gcsfuse
 * filesystems that were mounted for the job step. It iterates through the
 * global `mount_points` array and calls `fusermount -u` for each one.
 *
 * @param sp The SPANK handle.
 * @param ac The argument count.
 * @param av The argument vector.
 * @return 0 on success.
 */
int slurm_spank_exit(spank_t sp, int ac, char **av) {
    // slurm_info("gcsfuse-mount: Entering slurm_spank_exit. Context: %i", spank_context());
    if (spank_context() != S_CTX_REMOTE) {
        return 0;
    }
    // slurm_info("gcsfuse-mount: Starting to unmount. Mount point count: %i", mount_point_count);

    for (int i = 0; i < mount_point_count; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            // slurm_info("fusermount: attempting to unmount %s", mount_points[i]);
            execlp("fusermount", "fusermount", "-u", mount_points[i], NULL);
            slurm_error("gcsfuse-mount: execlp failed for fusermount: %m");
            exit(1);
        } else if (pid > 0) {
            waitpid(pid, NULL, 0);
        }
        free(mount_points[i]);
    }
    if (mount_points) {
        free(mount_points);
        mount_points = NULL;
        mount_point_count = 0;
    }
    // slurm_info("gcsfuse-mount: Ending unmount");
    return 0;
}

/**
 * @brief SPANK user initialization function.
 *
 * This function is called in the remote (slurmd) context on the compute
 * node just before the user's task is executed.
 *
 * It retrieves the mount arguments from the `GCSFUSE_MOUNTS` environment
 * variable (which was set by the callback in the local context), parses
 * them, and then forks and executes the `gcsfuse` command for each mount.
 * It waits for each mount to become ready before proceeding.
 *
 * @param sp The SPANK handle.
 * @param ac The argument count.
 * @param av The argument vector.
 * @return 0 on success, -1 on failure.
 */
int slurm_spank_user_init(spank_t sp, int ac, char **av) {
    uid_t uid;
    gid_t gid;
    char mount_env[4096];

    if (spank_get_item(sp, S_JOB_UID, &uid) != 0 || spank_get_item(sp, S_JOB_GID, &gid) != 0) {
        slurm_error("gcsfuse-mount: could not get job UID/GID");
        return -1;
    }

    if (spank_getenv(sp, "GCSFUSE_MOUNTS", mount_env, sizeof(mount_env)) != ESPANK_SUCCESS) {
        return 0; // No mounts requested.
    }

    char *saveptr;
    char *optarg = strtok_r(mount_env, ";", &saveptr);

    while (optarg) {
        char *bucket, *mount_point, *flags;
        char *arg = strdup(optarg);

        bucket = strtok(arg, ":");
        mount_point = strtok(NULL, ":");
        flags = strtok(NULL, "");

        if (!bucket || !mount_point) {
            slurm_error("gcsfuse-mount: invalid argument format: %s", optarg);
            free(arg);
            optarg = strtok_r(NULL, ";", &saveptr);
            continue;
        }

        mount_points = realloc(mount_points, (mount_point_count + 1) * sizeof(char *));
        mount_points[mount_point_count++] = strdup(mount_point);

        mkdir(mount_point, 0755);

        pid_t pid = fork();
        if (pid == 0) {
            char uid_str[20], gid_str[20];
            sprintf(uid_str, "%d", uid);
            sprintf(gid_str, "%d", gid);

            char *gcsfuse_argv[32];
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

            if (flags) {
                char *flag = strtok(flags, " ");
                while (flag && j < 30) {
                    gcsfuse_argv[j++] = flag;
                    flag = strtok(NULL, " ");
                }
            }
            gcsfuse_argv[j++] = bucket;
            gcsfuse_argv[j++] = mount_point;
            gcsfuse_argv[j] = NULL;

            execvp("gcsfuse", gcsfuse_argv);
            slurm_error("gcsfuse-mount: execvp failed for gcsfuse: %m");
            exit(1);
        } else if (pid < 0) {
            slurm_error("gcsfuse-mount: fork failed: %m");
        }
        free(arg);

        // Wait for the mount to become responsive.
        int found = 0;
        for (int k = 0; k < 20; k++) { // Wait up to 10 seconds
            if (is_mountpoint(mount_point)) {
                found = 1;
                break;
            }
            usleep(500000); // 500ms
        }
        if (!found) {
            slurm_error("gcsfuse-mount: timed out waiting for mountpoint '%s'", mount_point);
        }
        optarg = strtok_r(NULL, ";", &saveptr);
    }
    return 0;
}

/**
 * @brief SPANK callback for the --gcsfuse-mount option.
 *
 * This function is called in the local (srun) context whenever the
 * `--gcsfuse-mount` option is used.
 *
 * It takes the option's argument, resolves any relative paths within it,
 * and appends the resolved string to the `GCSFUSE_MOUNTS` environment
 * variable. This variable is then propagated to the job step on the
 * compute node. Multiple uses of the option will append to the variable,
 * separated by semicolons.
 *
 * @param val The value of the option (not used).
 * @param optarg The argument provided to the option.
 * @param remote Flag indicating if the context is remote (0 for local).
 * @return 0 on success, -1 on failure.
 */
static int handle_gcsfuse_mount(int val, const char *optarg, int remote) {
    char *new_mounts;
    const char *current_mounts = getenv("GCSFUSE_MOUNTS");
    // slurm_info("gcsfuse-mount: current_mounts: %s Adding: %s", current_mounts, optarg);

    const char *next_mount = resolve_relative_mounts(optarg);

    if (current_mounts && current_mounts[0] != '\0') {
        asprintf(&new_mounts, "%s;%s", current_mounts, next_mount);
    } else {
        asprintf(&new_mounts, "%s", next_mount);
    }

    if (new_mounts == NULL) {
        slurm_error("gcsfuse-mount: asprintf failed");
        return -1;
    }

    // Set the environment variable in the current srun process.
    // Slurm will propagate this to the job step's environment.
    setenv("GCSFUSE_MOUNTS", new_mounts, 1);
    // slurm_info("gcsfuse-mount: new_mounts: %s", new_mounts);
    free(new_mounts);

    return 0;
}
