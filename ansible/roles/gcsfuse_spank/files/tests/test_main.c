#define _GNU_SOURCE
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

// --- MOCKING SLURM/SPANK ENVIRONMENT ---

#include <slurm/spank.h>

// Mock functions
void slurm_info(const char* format, ...) {
  va_list args;
  va_start(args, format);
  printf("[INFO] ");
  vprintf(format, args);
  printf("\n");
  va_end(args);
}

void slurm_error(const char* format, ...) {
  va_list args;
  va_start(args, format);
  printf("[ERROR] ");
  vprintf(format, args);
  printf("\n");
  va_end(args);
}

void slurm_spank_log(const char* format, ...) {
  va_list args;
  va_start(args, format);
  printf("[LOG] ");
  vprintf(format, args);
  printf("\n");
  va_end(args);
}

// Mock spank_getenv (simple kv store for testing)
char mock_env_key[256] = {0};
char mock_env_val[4096] = {0};

spank_err_t spank_getenv(spank_t sp, const char* var, char* buf, int len) {
  (void)sp;
  if (strcmp(var, mock_env_key) == 0) {
    strncpy(buf, mock_env_val, len);
    return ESPANK_SUCCESS;
  }
  return ESPANK_ERROR;
}

spank_err_t spank_setenv(spank_t sp, const char* var, const char* val,
                         int overwrite) {
  (void)sp;
  (void)overwrite;
  snprintf(mock_env_key, sizeof(mock_env_key), "%s", var);
  snprintf(mock_env_val, sizeof(mock_env_val), "%s", val);
  return ESPANK_SUCCESS;
}

// Stubs for functions we don't need to test logic
spank_context_t spank_context(void) { return S_CTX_LOCAL; }
spank_err_t spank_option_register(spank_t sp, struct spank_option* opt) {
  (void)sp;
  (void)opt;
  return ESPANK_SUCCESS;
}
spank_err_t spank_get_item(spank_t sp, spank_item_t item, ...) {
  (void)sp;
  (void)item;
  return ESPANK_SUCCESS;
}

// Bypass the SPANK_PLUGIN macro by defining it as empty
#undef SPANK_PLUGIN
#define SPANK_PLUGIN(name, version)

// --- INCLUDE SOURCE FILE TO TEST STATIC FUNCTIONS ---
#ifndef TESTING_MODE
#define TESTING_MODE
#endif
#include "../gcsfuse_spank.c"

// --- TESTS ---

void test_parse_mount_spec() {
  printf("Running test_parse_mount_spec...\n");
  mount_spec_t spec;

  // Case 1: bucket:mount
  assert(parse_mount_spec("mybucket:/mnt/gcs", &spec) == 0);
  assert(strcmp(spec.bucket, "mybucket") == 0);
  assert(strcmp(spec.mount_point, "/mnt/gcs") == 0);
  assert(spec.flags == NULL);
  free_mount_spec(&spec);

  // Case 2: bucket:mount:flags
  assert(parse_mount_spec("mybucket:/mnt/gcs:--implicit-dirs", &spec) == 0);
  assert(strcmp(spec.bucket, "mybucket") == 0);
  assert(strcmp(spec.mount_point, "/mnt/gcs") == 0);
  assert(strcmp(spec.flags, "--implicit-dirs") == 0);
  free_mount_spec(&spec);

  // Case 3: :mount (All Buckets)
  assert(parse_mount_spec(":/mnt/gcs", &spec) == 0);
  assert(strcmp(spec.bucket, "") == 0);
  assert(strcmp(spec.mount_point, "/mnt/gcs") == 0);
  free_mount_spec(&spec);

  // Case 4: mount (Implicit All Buckets)
  assert(parse_mount_spec("/mnt/gcs", &spec) == 0);
  assert(spec.bucket == NULL);
  assert(strcmp(spec.mount_point, "/mnt/gcs") == 0);
  free_mount_spec(&spec);

  // Case 5: mount:flags (Implicit All Buckets)
  assert(parse_mount_spec("/mnt/gcs:--some-flag", &spec) == 0);
  assert(spec.bucket == NULL);
  assert(strcmp(spec.mount_point, "/mnt/gcs") == 0);
  assert(strcmp(spec.flags, "--some-flag") == 0);
  free_mount_spec(&spec);

  printf("PASS\n");
}

void test_resolve_relative_mounts() {
  printf("Running test_resolve_relative_mounts...\n");

  const char* cwd = "/home/user/project";

  // Case 1: Absolute path
  char* res1 = resolve_relative_mounts("bucket:/abs/path", cwd);
  assert(strcmp(res1, "bucket:/abs/path") == 0);
  free(res1);

  // Case 2: Relative path
  char* res2 = resolve_relative_mounts("bucket:./rel/path", cwd);
  assert(strcmp(res2, "bucket:/home/user/project/rel/path") == 0);
  free(res2);

  // Case 3: Multiple mounts mixed
  char* res3 = resolve_relative_mounts("b1:./p1;b2:/p2", cwd);
  assert(strcmp(res3, "b1:/home/user/project/p1;b2:/p2") == 0);
  free(res3);

  // Case 4: With options
  char* res4 = resolve_relative_mounts("bucket:./path:--flag", cwd);
  assert(strcmp(res4, "bucket:/home/user/project/path:--flag") == 0);
  free(res4);

  // Case 5: All buckets mode (relative)
  char* res5 = resolve_relative_mounts("./mnt/gcs", cwd);
  assert(strcmp(res5, "/home/user/project/mnt/gcs") == 0);
  free(res5);

  printf("PASS\n");
}

void test_check_mount_conflicts() {
  printf("Running test_check_mount_conflicts...\n");

  const char* current = "bucket1:/tmp/mount1;bucket2:/tmp/mount2";

  // Case 1: No conflict (new mount, different path)
  assert(check_mount_conflicts(current, "bucket3:/tmp/mount3") == 0);

  // Case 2: No conflict (same bucket, same path - idempotent)
  assert(check_mount_conflicts(current, "bucket1:/tmp/mount1") == 0);

  // Case 3: Conflict (different bucket, same path)
  assert(check_mount_conflicts(current, "bucket3:/tmp/mount1") == -1);

  // Case 4: Conflict (All buckets vs specific bucket on same path)
  assert(check_mount_conflicts(current, ":/tmp/mount1") == -1);
  assert(check_mount_conflicts(":/tmp/mount1", "bucket1:/tmp/mount1") == -1);

  printf("PASS\n");
}

int main() {
  test_parse_mount_spec();
  test_resolve_relative_mounts();
  test_check_mount_conflicts();
  printf("All tests passed!\n");
  return 0;
}
