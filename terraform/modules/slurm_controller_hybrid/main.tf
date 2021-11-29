/**
 * Copyright 2021 SchedMD LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

##########
# LOCALS #
##########

locals {
  scripts_dir = abspath("${path.module}/../../../scripts")

  output_dir = (
    var.output_dir == null || var.output_dir == ""
    ? abspath(".")
    : abspath(var.output_dir)
  )
}

##################
# LOCALS: SCRIPT #
##################

locals {
  setup_hybrid = abspath("${local.scripts_dir}/setup_hybrid.py")

  no_comma_params = lookup(var.cloud_parameters, "NoCommaParams", false)

  resume_rate = lookup(var.cloud_parameters, "ResumeRate", 0)

  resume_timeout = lookup(var.cloud_parameters, "ResumeTimeout", 300)

  suspend_rate = lookup(var.cloud_parameters, "SuspendRate", 0)

  suspend_timeout = lookup(var.cloud_parameters, "SuspendTimeout", 300)
}

##################
# LOCALS: CONFIG #
##################

locals {
  cluster_name = module.slurm_controller_common.cluster_name

  slurm_cluster_id = module.slurm_controller_common.slurm_cluster_id

  munge_key = module.slurm_controller_common.munge_key

  jwt_key = module.slurm_controller_common.jwt_key

  serf_keys = module.slurm_controller_common.serf_keys

  template_map = module.slurm_controller_common.template_map

  partitions = module.slurm_controller_common.partitions

  google_app_cred_path = (
    var.google_app_cred_path != null
    ? abspath(var.google_app_cred_path)
    : null
  )

  slurm_scripts_dir = (
    var.slurm_scripts_dir != null
    ? abspath(var.slurm_scripts_dir)
    : local.scripts_dir
  )

  slurm_bin_dir = (
    var.slurm_bin_dir != null
    ? abspath(var.slurm_bin_dir)
    : null
  )

  slurm_log_dir = (
    var.slurm_log_dir != null
    ? abspath(var.slurm_log_dir)
    : null
  )

  config = yamlencode({
    cluster_name = local.cluster_name
    project      = var.project_id
    etc          = local.output_dir
    scripts      = local.scripts_dir

    munge_key = local.munge_key
    jwt_key   = local.jwt_key
    serf_keys = local.serf_keys

    network_storage       = var.network_storage
    login_network_storage = var.login_network_storage

    cloud_parameters = {
      ResumeRate      = local.resume_rate
      ResumeTimeout   = local.resume_timeout
      SuspendRate     = local.suspend_rate
      suspend_timeout = local.suspend_timeout
    }
    template_map = module.slurm_controller_common.template_map
    partitions   = module.slurm_controller_common.partitions

    google_app_cred_path = local.google_app_cred_path
    slurm_scripts_dir    = local.slurm_scripts_dir
    slurm_bin_dir        = local.slurm_bin_dir
    slurm_log_dir        = local.slurm_log_dir
  })
}

################
# DATA: SCRIPT #
################

data "local_file" "setup_hybrid" {
  filename = local.setup_hybrid
}

##########
# CONFIG #
##########

resource "local_file" "config_yaml" {
  filename = abspath("${local.output_dir}/config.yaml")
  content  = local.config

  file_permission = "0644"
}

#########
# SETUP #
#########

resource "null_resource" "setup_hybrid" {
  triggers = {
    scripts_dir = local.scripts_dir
    config_dir  = local.output_dir
    config      = local_file.config_yaml.content
    config_path = local_file.config_yaml.filename
    script_path = data.local_file.setup_hybrid.filename

    no_comma_params = local.no_comma_params
    resume_rate     = local.resume_rate
    resume_timeout  = local.resume_timeout
    suspend_rate    = local.suspend_rate
    suspend_timeout = local.suspend_timeout
  }

  provisioner "local-exec" {
    working_dir = self.triggers.scripts_dir
    environment = {
      SLURM_CONFIG_YAML = self.triggers.config_path
    }
    command = <<EOC
${self.triggers.script_path} \
--ResumeRate=${self.triggers.resume_rate} \
--SuspendRate=${self.triggers.suspend_rate} \
--ResumeTimeout=${self.triggers.resume_timeout} \
--SuspendTimeout=${self.triggers.suspend_timeout} \
${tobool(self.triggers.no_comma_params) == true ? "--no-comma-params" : ""}
EOC
  }
}

##########
# COMMON #
##########

module "slurm_controller_common" {
  source = "../_slurm_controller_common"

  project_id = var.project_id

  slurm_cluster_id = var.slurm_cluster_id
  cluster_name     = var.cluster_name
  munge_key        = var.munge_key
  jwt_key          = var.jwt_key
  serf_keys        = var.serf_keys
  template_map     = var.template_map
  partitions       = var.partitions
  metadata_compute = var.metadata_compute
  compute_d        = var.compute_d
  enable_devel     = var.enable_devel
}
