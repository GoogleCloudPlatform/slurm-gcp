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

variable "slurm_cluster_name" {
  type        = string
  description = "Cluster name, used for resource naming."
  default     = "advanced"
}

variable "enable_devel" {
  type        = bool
  description = "Enables development process for faster iterations. NOTE: *NOT* intended for production use."
  default     = false
}

variable "region" {
  type        = string
  description = "The default region to place resources in."
}

############
# FIREWALL #
############

variable "firewall_network_name" {
  type        = string
  description = "Name of the network this set of firewall rules applies to."
  default     = "default"
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
  description = "List of scripts to be ran on controller VM startup."
  type = list(object({
    filename = string
    content  = string
  }))
  default = []
}

variable "compute_d" {
  description = "List of scripts to be ran on compute VM startup."
  type = list(object({
    filename = string
    content  = string
  }))
  default = []
}

variable "prolog_d" {
  description = <<EOD
List of scripts to be used for Prolog. Programs for the slurmd to execute
whenever it is asked to run a job step from a new job allocation.
See https://slurm.schedmd.com/slurm.conf.html#OPT_Prolog.
EOD
  type = list(object({
    filename = string
    content  = string
  }))
  default = []
}

variable "epilog_d" {
  description = <<EOD
List of scripts to be used for Epilog. Programs for the slurmd to execute
on every node when a user's job completes.
See https://slurm.schedmd.com/slurm.conf.html#OPT_Epilog.
EOD
  type = list(object({
    filename = string
    content  = string
  }))
  default = []
}

##############
# CONTROLLER #
##############

variable "controller_instance_config" {
  description = <<EOD
Creates a controller instance with given configuration.
EOD
  type = object({
    access_config = list(object({
      nat_ip       = string
      network_tier = string
    }))
    additional_disks = list(object({
      disk_name    = string
      device_name  = string
      disk_size_gb = number
      disk_type    = string
      disk_labels  = map(string)
      auto_delete  = bool
      boot         = bool
    }))
    can_ip_forward         = bool
    disable_smt            = bool
    disk_auto_delete       = bool
    disk_labels            = map(string)
    disk_size_gb           = number
    disk_type              = string
    enable_confidential_vm = bool
    enable_oslogin         = bool
    enable_shielded_vm     = bool
    gpu = object({
      count = number
      type  = string
    })
    instance_template   = string
    labels              = map(string)
    machine_type        = string
    metadata            = map(string)
    min_cpu_platform    = string
    network_ip          = string
    on_host_maintenance = string
    preemptible         = bool
    region              = string
    service_account = object({
      email  = string
      scopes = list(string)
    })
    shielded_instance_config = object({
      enable_integrity_monitoring = bool
      enable_secure_boot          = bool
      enable_vtpm                 = bool
    })
    source_image_family  = string
    source_image_project = string
    source_image         = string
    static_ip            = string
    subnetwork_project   = string
    subnetwork           = string
    tags                 = list(string)
    zone                 = string
  })
  default = {
    access_config            = null
    additional_disks         = null
    can_ip_forward           = null
    disable_smt              = null
    disk_auto_delete         = null
    disk_labels              = null
    disk_size_gb             = null
    disk_type                = null
    enable_confidential_vm   = null
    enable_oslogin           = null
    enable_shielded_vm       = null
    gpu                      = null
    instance_template        = null
    labels                   = null
    machine_type             = null
    metadata                 = null
    min_cpu_platform         = null
    network_ip               = null
    network                  = null
    on_host_maintenance      = null
    preemptible              = null
    region                   = null
    service_account          = null
    shielded_instance_config = null
    source_image_family      = null
    source_image_project     = null
    source_image             = null
    static_ip                = null
    subnetwork_project       = null
    subnetwork               = null
    tags                     = null
    zone                     = null
  }
}

#########
# LOGIN #
#########

variable "login_node_groups" {
  description = "List of slurm login instance definitions."
  type = list(object({
    access_config = list(object({
      nat_ip       = string
      network_tier = string
    }))
    additional_disks = list(object({
      disk_name    = string
      device_name  = string
      disk_size_gb = number
      disk_type    = string
      disk_labels  = map(string)
      auto_delete  = bool
      boot         = bool
    }))
    can_ip_forward         = bool
    disable_smt            = bool
    disk_auto_delete       = bool
    disk_labels            = map(string)
    disk_size_gb           = number
    disk_type              = string
    enable_confidential_vm = bool
    enable_oslogin         = bool
    enable_shielded_vm     = bool
    gpu = object({
      count = number
      type  = string
    })
    group_name          = string
    instance_template   = string
    labels              = map(string)
    machine_type        = string
    metadata            = map(string)
    min_cpu_platform    = string
    network_ips         = list(string)
    num_instances       = number
    on_host_maintenance = string
    preemptible         = bool
    region              = string
    service_account = object({
      email  = string
      scopes = list(string)
    })
    shielded_instance_config = object({
      enable_integrity_monitoring = bool
      enable_secure_boot          = bool
      enable_vtpm                 = bool
    })
    source_image_family  = string
    source_image_project = string
    source_image         = string
    static_ips           = list(string)
    subnetwork_project   = string
    subnetwork           = string
    tags                 = list(string)
    zone                 = string
  }))
  default = []
}

##############
# PARTITIONS #
##############

variable "partitions" {
  description = "Cluster partition configuration as a list."
  type = list(object({
    enable_job_exclusive    = bool
    enable_placement_groups = bool
    compute_node_groups = list(object({
      count_static  = number
      count_dynamic = number
      group_name    = string
      node_conf     = map(string)
      additional_disks = list(object({
        disk_name    = string
        device_name  = string
        disk_size_gb = number
        disk_type    = string
        disk_labels  = map(string)
        auto_delete  = bool
        boot         = bool
      }))
      can_ip_forward         = bool
      disable_smt            = bool
      disk_auto_delete       = bool
      disk_labels            = map(string)
      disk_size_gb           = number
      disk_type              = string
      enable_confidential_vm = bool
      enable_oslogin         = bool
      enable_shielded_vm     = bool
      gpu = object({
        count = number
        type  = string
      })
      instance_template   = string
      labels              = map(string)
      machine_type        = string
      metadata            = map(string)
      min_cpu_platform    = string
      on_host_maintenance = string
      preemptible         = bool
      service_account = object({
        email  = string
        scopes = list(string)
      })
      shielded_instance_config = object({
        enable_integrity_monitoring = bool
        enable_secure_boot          = bool
        enable_vtpm                 = bool
      })
      source_image_family  = string
      source_image_project = string
      source_image         = string
      tags                 = list(string)
    }))
    network_storage = list(object({
      local_mount   = string
      fs_type       = string
      server_ip     = string
      remote_mount  = string
      mount_options = string
    }))
    partition_name = string
    partition_conf = map(string)
    partition_d = list(object({
      filename = string
      content  = string
    }))
    region             = string
    subnetwork_project = string
    subnetwork         = string
    zone_policy_allow  = list(string)
    zone_policy_deny   = list(string)
  }))
  default = []
}
