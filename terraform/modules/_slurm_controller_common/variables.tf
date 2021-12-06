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
  description = "Project ID."
}

############
# METADATA #
############

variable "metadata_compute" {
  type        = map(string)
  description = "Metadata key/value pairs to make available from within the compute instances."
  default     = null
}

#########
# SLURM #
#########

variable "cluster_name" {
  type        = string
  description = "Cluster name, used resource naming and slurm accounting."
}

variable "slurm_cluster_id" {
  type        = string
  description = "The Cluster ID."
}

variable "enable_devel" {
  type        = bool
  description = "Enables development mode. Not for production use."
  default     = false
}

variable "munge_key" {
  type        = string
  description = "Cluster munge authentication key. If 'null', then a key will be generated instead."
  default     = null

  validation {
    condition = (
      var.munge_key == null
      ? true
      : length(var.munge_key) >= 32 && length(var.munge_key) <= 1024
    )
    error_message = "Munge key must be between 32 and 1024 bytes."
  }
}

variable "jwt_key" {
  type        = string
  description = "Cluster jwt authentication key. If 'null', then a key will be generated instead."
  default     = null
}

variable "compute_d" {
  type        = string
  description = "Path to directory containing user compute provisioning scripts."
  default     = null
}

variable "template_map" {
  type        = map(string)
  description = "Slurm compute templates as a map. Key=slurm_template_name Value=template_self_link"
  default     = {}

  validation {
    condition = alltrue([
      for t, l in var.template_map : can(regex("(^[a-z][-a-z0-9]*$)", t))
    ])
    error_message = "Keys must be a match of regex '(^[a-z][-a-z0-9]*$)'."
  }
}

variable "partitions" {
  description = <<EOD
Cluster partitions as a map.

* subnetwork  : The subnetwork name to create instances in.
* region      : The subnetwork region to create instances in.
* zone_policy : Zone location policy for regional bulkInsert.

* template      : Slurm template key from variable 'compute_template'.
* count_static  : Number of static nodes. These nodes are exempt from SuspendProgram.
* count_dynamic : Number of dynamic nodes. These nodes are subject to SuspendProgram and ResumeProgram.

* server_ip     : Address of the storage server.
* remote_mount  : The location in the remote instance filesystem to mount from.
* local_mount   : The location on the instance filesystem to mount to.
* fs_type       : Filesystem type (e.g. "nfs").
* mount_options : Options to mount with.

* exclusive        : Enables job exclusivity.
* placement_groups : Enables partition placement groups.
* conf             : Slurm partition configurations as a map.
EOD
  type = map(object({
    subnetwork  = string
    region      = string
    zone_policy = map(string)
    nodes = list(object({
      template      = string
      count_static  = number
      count_dynamic = number
    }))
    network_storage = list(object({
      server_ip     = string
      remote_mount  = string
      local_mount   = string
      fs_type       = string
      mount_options = string
    }))
    exclusive        = bool
    placement_groups = bool
    conf             = map(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for k, v in var.partitions : can(regex("(^[a-z][a-z0-9]*$)", k))
    ])
    error_message = "Keys must be a match of regex '(^[a-z][a-z0-9]*$)'."
  }
}
