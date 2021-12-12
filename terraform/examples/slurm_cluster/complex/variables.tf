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

###########
# GENERAL #
###########

variable "project_id" {
  type        = string
  description = "Project ID to create resources in."
}

variable "cluster_name" {
  type        = string
  description = "Cluster name, used for resource naming."
  default     = "complex"
}

variable "enable_devel" {
  type        = bool
  description = "Enables development process for faster iterations. NOTE: *NOT* intended for production use."
  default     = false
}

###########
# NETWORK #
###########

variable "region" {
  type        = string
  description = "The region to place resources in."
}

#################
# CONFIGURATION #
#################

variable "cloud_parameters" {
  description = "cloud.conf key/value as a map."
  type        = map(string)
  default     = {}
}

variable "cloudsql" {
  description = <<EOD
Use this database instead of the one on the controller.
* server_ip : Address of the database server.
* user      : The user to access the database as.
* password  : The password, given the user, to access the given database. (sensitive)
* db_name   : The database to access.
EOD
  type = object({
    server_ip = string
    user      = string
    password  = string # sensitive
    db_name   = string
  })
  default   = null
  sensitive = true
}

variable "munge_key" {
  description = "Cluster munge authentication key. If 'null', then a key will be generated instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "jwt_key" {
  description = "Cluster jwt authentication key. If 'null', then a key will be generated instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "network_storage" {
  description = <<EOD
Storage to mounted on all instances.
* server_ip     : Address of the storage server.
* remote_mount  : The location in the remote instance filesystem to mount from.
* local_mount   : The location on the instance filesystem to mount to.
* fs_type       : Filesystem type (e.g. "nfs").
* mount_options : Options to mount with.
EOD
  type = list(object({
    server_ip     = string
    remote_mount  = string
    local_mount   = string
    fs_type       = string
    mount_options = string
  }))
  default = []
}

variable "login_network_storage" {
  description = <<EOD
Storage to mounted on login and controller instances
* server_ip     : Address of the storage server.
* remote_mount  : The location in the remote instance filesystem to mount from.
* local_mount   : The location on the instance filesystem to mount to.
* fs_type       : Filesystem type (e.g. "nfs").
* mount_options : Options to mount with.
EOD
  type = list(object({
    server_ip     = string
    remote_mount  = string
    local_mount   = string
    fs_type       = string
    mount_options = string
  }))
  default = []
}

variable "slurmdbd_conf_tpl" {
  description = "Slurm slurmdbd.conf template file path."
  type        = string
  default     = null
}

variable "slurm_conf_tpl" {
  description = "Slurm slurm.conf template file path."
  type        = string
  default     = null
}

variable "cgroup_conf_tpl" {
  description = "Slurm cgroup.conf template file path."
  type        = string
  default     = null
}

variable "controller_d" {
  description = "Path to directory containing user controller provisioning scripts."
  type        = string
  default     = null
}

variable "compute_d" {
  description = "Path to directory containing user compute provisioning scripts."
  type        = string
  default     = null
}

##############
# CONTROLLER #
##############

variable "controller_service_account" {
  type = object({
    email  = string
    scopes = set(string)
  })
  description = "Service account to attach to the instance. See https://www.terraform.io/docs/providers/google/r/compute_instance_template.html#service_account."
  default     = null
}

variable "controller_template" {
  description = "Slurm controller template."
  type = object({
    ### network ###
    tags = list(string)

    ### instance ###
    machine_type     = string
    min_cpu_platform = string
    gpu = object({
      type  = string
      count = number
    })
    shielded_instance_config = object({
      enable_secure_boot          = bool
      enable_vtpm                 = bool
      enable_integrity_monitoring = bool
    })
    enable_confidential_vm = bool
    enable_shielded_vm     = bool
    disable_smt            = bool
    preemptible            = bool
    labels                 = map(string)

    ### source image ###
    source_image_project = string
    source_image_family  = string
    source_image         = string

    ### disk ###
    disk_type        = string
    disk_size_gb     = number
    disk_labels      = map(string)
    disk_auto_delete = bool
    additional_disks = list(object({
      disk_name    = string
      device_name  = string
      auto_delete  = bool
      boot         = bool
      disk_size_gb = number
      disk_type    = string
      disk_labels  = map(string)
    }))
  })
}

#########
# LOGIN #
#########

variable "login_service_account" {
  type = object({
    email  = string
    scopes = set(string)
  })
  description = "Service account to attach to the instance. See https://www.terraform.io/docs/providers/google/r/compute_instance_template.html#service_account."
  default     = null
}

variable "login" {
  description = "List of slurm login instance definitions."
  type = list(object({
    alias         = string
    num_instances = number

    ### network ###
    tags = list(string)

    ### instance ###
    machine_type     = string
    min_cpu_platform = string
    gpu = object({
      type  = string
      count = number
    })
    shielded_instance_config = object({
      enable_secure_boot          = bool
      enable_vtpm                 = bool
      enable_integrity_monitoring = bool
    })
    enable_confidential_vm = bool
    enable_shielded_vm     = bool
    disable_smt            = bool
    preemptible            = bool
    labels                 = map(string)

    ### source image ###
    source_image_project = string
    source_image_family  = string
    source_image         = string

    ### disk ###
    disk_type        = string
    disk_size_gb     = number
    disk_labels      = map(string)
    disk_auto_delete = bool
    additional_disks = list(object({
      disk_name    = string
      device_name  = string
      auto_delete  = bool
      boot         = bool
      disk_size_gb = number
      disk_type    = string
      disk_labels  = map(string)
    }))
  }))
  default = []
}

###########
# COMPUTE #
###########

variable "compute_service_account" {
  type = object({
    email  = string
    scopes = set(string)
  })
  description = "Service account to attach to the instance. See https://www.terraform.io/docs/providers/google/r/compute_instance_template.html#service_account."
  default = {
    email  = null
    scopes = null
  }
}

variable "compute_templates" {
  description = "List of slurm compute instance templates."
  type = list(object({
    alias = string

    ### network ###
    tags = list(string)

    ### instance ###
    machine_type     = string
    min_cpu_platform = string
    gpu = object({
      type  = string
      count = number
    })
    shielded_instance_config = object({
      enable_secure_boot          = bool
      enable_vtpm                 = bool
      enable_integrity_monitoring = bool
    })
    enable_confidential_vm = bool
    enable_shielded_vm     = bool
    disable_smt            = bool
    preemptible            = bool
    labels                 = map(string)

    ### source image ###
    source_image_project = string
    source_image_family  = string
    source_image         = string

    ### disk ###
    disk_type        = string
    disk_size_gb     = number
    disk_labels      = map(string)
    disk_auto_delete = bool
    additional_disks = list(object({
      disk_name    = string
      device_name  = string
      auto_delete  = bool
      boot         = bool
      disk_size_gb = number
      disk_type    = string
      disk_labels  = map(string)
    }))
  }))
  default = []
}

##############
# PARTITIONS #
##############

variable "partitions" {
  description = "Cluster partition configuration as a list."
  type = list(object({
    partition_name = string
    partition_conf = map(string)
    partition_nodes = list(object({
      node_group_name            = string
      compute_template_alias_ref = string
      count_static               = number
      count_dynamic              = number
    }))
    zone_policy_allow = list(string)
    zone_policy_deny  = list(string)
    network_storage = list(object({
      server_ip     = string
      remote_mount  = string
      local_mount   = string
      fs_type       = string
      mount_options = string
    }))
    enable_job_exclusive    = bool
    enable_placement_groups = bool
  }))
  default = []
}
