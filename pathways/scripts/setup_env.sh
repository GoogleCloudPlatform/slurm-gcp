#!/bin/bash
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
# One-time environment setup for Pathways + MaxText on Cluster Director Slurm.
# Pulls the Pathways container images, converts them into Enroot (.sqsh)
# archives under /home/pathways on the shared /home Filestore, builds a shared
# Python 3.12 virtual environment with JAX, pathwaysutils, and MaxText, sets
# shared permissions, and symlinks ~/pathways -> /home/pathways.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-/home/pathways}"
MAXTEXT_DIR="${MAXTEXT_DIR:-${WORK_DIR}/maxtext}"

# JAX version for the client, and the Pathways build to pair it with. Pathways
# image tags are <build>-jax_<version>, and each build is tagged once for every
# JAX version it's compatible with, so a missing tag means an unsupported pair.
JAX_VERSION="${JAX_VERSION:-0.11.1}"
PATHWAYS_BUILD="${PATHWAYS_BUILD:-20260901}"
PATHWAYS_REPO="us-docker.pkg.dev/cloud-tpu-v2-images/pathways"
SERVER_IMAGE="${SERVER_IMAGE:-${PATHWAYS_REPO}/server:${PATHWAYS_BUILD}-jax_${JAX_VERSION}}"
PROXY_IMAGE="${PROXY_IMAGE:-${PATHWAYS_REPO}/proxy_server:${PATHWAYS_BUILD}-jax_${JAX_VERSION}}"

SERVER_SQSH="${WORK_DIR}/server.sqsh"
PROXY_SQSH="${WORK_DIR}/proxy.sqsh"
BIN_DIR="${WORK_DIR}/bin"
VENV_DIR="${WORK_DIR}/venv"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${WORK_DIR}/.python}"

sudo -n mkdir -p "${WORK_DIR}" 2>/dev/null || mkdir -p "${WORK_DIR}"
sudo -n chown "$(id -u):$(id -g)" "${WORK_DIR}" 2>/dev/null || true
chmod a+rwx "${WORK_DIR}" 2>/dev/null || sudo -n chmod a+rwx "${WORK_DIR}" 2>/dev/null || true
mkdir -p "${BIN_DIR}" "${UV_PYTHON_INSTALL_DIR}" "${HOME}/.enroot"
mkdir -p "/tmp/enroot-$(id -u)/"{tmp,run,runtime,cache,data}
# Same Enroot directories as start_pathways.sh. The system default temp directory,
# /tmp/<uid>/tmp, may not exist, and Enroot doesn't create it, so "enroot import" fails.
export ENROOT_CONFIG_PATH="${HOME}/.enroot"
export ENROOT_RUNTIME_PATH="/tmp/enroot-$(id -u)/runtime"
export ENROOT_CACHE_PATH="/tmp/enroot-$(id -u)/cache"
export ENROOT_DATA_PATH="/tmp/enroot-$(id -u)/data"
export ENROOT_TEMP_PATH="/tmp/enroot-$(id -u)/tmp"

if [[ "${SCRIPT_DIR}" != "${WORK_DIR}" ]]; then
  cp -f "${SCRIPT_DIR}"/*.sh "${WORK_DIR}/" 2>/dev/null || true
  chmod a+rx "${WORK_DIR}"/*.sh 2>/dev/null || true
fi

echo "=== [1/4] Configuring Artifact Registry authentication ==="
gcloud auth configure-docker us-docker.pkg.dev --quiet

# Wrapper scripts mounted at /pathways/server and /pathways/proxy_server inside
# the Enroot containers. Each runs its image's entrypoint binary. The image
# already sets LD_LIBRARY_PATH, TPU_STDERR_LOG_LEVEL and the gRPC credentials
# override, and Enroot and Pyxis apply the image's environment.
cat << 'EOF' > "${BIN_DIR}/server"
#!/bin/sh
export TPU_SKIP_MDS_QUERY="${TPU_SKIP_MDS_QUERY:-true}"
exec /usr/pathways/run/cloud_pathways_server_sanitized "$@"
EOF

cat << 'EOF' > "${BIN_DIR}/proxy_server"
#!/bin/sh
exec /usr/pathways/run/cloud_proxy_server_sanitized "$@"
EOF

chmod +x "${BIN_DIR}/server" "${BIN_DIR}/proxy_server"

# Builds an Enroot archive from a Pathways image, adding the wrapper script.
# The build fails if the image doesn't have the expected binary. The image name
# is saved next to the archive, so a different image (a new JAX_VERSION or
# PATHWAYS_BUILD) triggers a rebuild. FORCE_REBUILD=1 always rebuilds.
build_sqsh() {
  local image="$1" sqsh="$2" wrapper="$3" binary="$4" tag="$5" ctx
  if [[ -f "${sqsh}" && "$(cat "${sqsh}.image" 2>/dev/null)" == "${image}" \
        && "${FORCE_REBUILD:-0}" != "1" ]]; then
    echo "Reusing existing ${sqsh} (${image})"
    return
  fi
  docker pull "${image}" || {
    echo "ERROR: Couldn't pull ${image}. If the tag doesn't exist, Pathways build" \
         "${PATHWAYS_BUILD} isn't marked compatible with JAX ${JAX_VERSION}." >&2
    exit 1
  }
  ctx="$(mktemp -d)"
  cp "${BIN_DIR}/${wrapper}" "${ctx}/${wrapper}"
  cat << EOF > "${ctx}/Dockerfile"
FROM ${image}
RUN test -x ${binary} && mkdir -p /dev /tmp /pathways
COPY ${wrapper} /pathways/${wrapper}
ENTRYPOINT []
CMD ["/bin/sh"]
EOF
  docker build -t "${tag}" -f "${ctx}/Dockerfile" "${ctx}"
  rm -rf "${ctx}"
  rm -f "${sqsh}"
  enroot import -o "${sqsh}" -- "dockerd://${tag}"
  echo "${image}" > "${sqsh}.image"
}

echo "=== [2/4] Pulling Pathways container images and building Enroot archives ==="
build_sqsh "${SERVER_IMAGE}" "${SERVER_SQSH}" server \
  /usr/pathways/run/cloud_pathways_server_sanitized pathways-server-local:latest
build_sqsh "${PROXY_IMAGE}" "${PROXY_SQSH}" proxy_server \
  /usr/pathways/run/cloud_proxy_server_sanitized pathways-proxy-local:latest

echo "=== [3/4] Setting up Python 3.12 virtual environment (${VENV_DIR}) ==="
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "${HOME}/.local/bin/uv" ]]; then
  python3 -m pip install --user uv
fi
UV_BIN="$(command -v uv || echo "${HOME}/.local/bin/uv")"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${UV_BIN}" venv --python 3.12 "${VENV_DIR}"
fi
# pathwaysutils: the newest PyPI release that supports this JAX version.
"${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" \
  "jax==${JAX_VERSION}" \
  "jaxlib==${JAX_VERSION}" \
  pathwaysutils

echo "=== [4/4] Setting up MaxText (${MAXTEXT_DIR}) ==="
if [[ ! -d "${MAXTEXT_DIR}" ]]; then
  git clone --depth 1 https://github.com/AI-Hypercomputer/maxtext.git "${MAXTEXT_DIR}"
fi
"${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" --no-deps -e "${MAXTEXT_DIR}"
"${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" \
  flax optax orbax-checkpoint grain tensorflow-datasets tiktoken transformers sentencepiece \
  ml-collections absl-py pyyaml rich pillow protobuf google-cloud-storage cloud-tpu-diagnostics

echo "=== Setup Complete ==="
chmod -R a+rX "${WORK_DIR}" 2>/dev/null || sudo -n chmod -R a+rX "${WORK_DIR}" 2>/dev/null || true
chmod a+rwx "${WORK_DIR}" 2>/dev/null || sudo -n chmod a+rwx "${WORK_DIR}" 2>/dev/null || true

if [[ "${HOME}/pathways" != "${WORK_DIR}" ]]; then
  ln -sfn "${WORK_DIR}" "${HOME}/pathways"
fi

ls -lh "${SERVER_SQSH}" "${PROXY_SQSH}"
"${VENV_DIR}/bin/python" -c "import jax, pathwaysutils, maxtext; print('Environment ready: JAX', jax.__version__)"
