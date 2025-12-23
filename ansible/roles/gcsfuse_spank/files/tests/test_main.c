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

// Use types from spank.h included via gcsfuse_spank.c later,
// but we need to forward declare or include headers if we want to use them
// here. Actually, since we include gcsfuse_spank.c at the bottom, we should
// move the include up or just use void* for now until the include happens.
// Better strategy: Include the headers we need for types FIRST.

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
  if (strcmp(var, mock_env_key) == 0) {
    strncpy(buf, mock_env_val, len);
    return ESPANK_SUCCESS;
  }
  return ESPANK_ERROR;
}

spank_err_t spank_setenv(spank_t sp, const char* var, const char* val,
                         int overwrite) {
  snprintf(mock_env_key, sizeof(mock_env_key), "%s", var);
  snprintf(mock_env_val, sizeof(mock_env_val), "%s", val);
  return ESPANK_SUCCESS;
}

// Stubs for functions we don't need to test logic
spank_context_t spank_context(void) { return S_CTX_LOCAL; }
spank_err_t spank_option_register(spank_t sp, struct spank_option* opt) {
  return ESPANK_SUCCESS;
}
spank_err_t spank_get_item(spank_t sp, spank_item_t item, ...) {
  return ESPANK_SUCCESS;
}

// Bypass the SPANK_PLUGIN macro by defining it as empty
#undef SPANK_PLUGIN
#define SPANK_PLUGIN(name, version)

// --- INCLUDE SOURCE FILE TO TEST STATIC FUNCTIONS ---
// We define a flag to prevent main() from being included from the source
#ifndef TESTING_MODE
#define TESTING_MODE
#endif
#include "../gcsfuse_spank.c"

// --- TESTS ---

void test_resolve_relative_mounts() {
  printf("Running test_resolve_relative_mounts...\n");

  char cwd[1024];
  getcwd(cwd, sizeof(cwd));

  // Case 1: Absolute path
  char* res1 = resolve_relative_mounts("bucket:/abs/path", cwd);
  assert(strcmp(res1, "bucket:/abs/path") == 0);
  free(res1);

  // Case 2: Relative path
  char* res2 = resolve_relative_mounts("bucket:./rel/path", cwd);
  char expected2[2048];
  sprintf(expected2, "bucket:%s/rel/path", cwd);
  assert(strcmp(res2, expected2) == 0);
  free(res2);

  // Case 3: Multiple mounts mixed
  char* res3 = resolve_relative_mounts("b1:./p1;b2:/p2", cwd);
  char expected3[2048];
  sprintf(expected3, "b1:%s/p1;b2:/p2", cwd);
  assert(strcmp(res3, expected3) == 0);
  free(res3);

  // Case 4: With options
  char* res4 = resolve_relative_mounts("bucket:./path:--flag", cwd);
  char expected4[2048];
  sprintf(expected4, "bucket:%s/path:--flag", cwd);
  assert(strcmp(res4, expected4) == 0);
  free(res4);

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

  // Case 4: No Conflict (same bucket, different path)
  assert(check_mount_conflicts(current, "bucket1:/tmp/mount3") == 0);

  printf("PASS\n");
}

int main() {
  test_resolve_relative_mounts();
  test_check_mount_conflicts();
  printf("All tests passed!\n");
  return 0;
}
