/**
 * Copyright (C) SchedMD LLC.
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
  description = "Project ID of the project that holds the network."
  type        = string
}

variable "slurm_cluster_name" {
  description = "Cluster name, used for resource naming."
  type        = string
}

variable "account_type" {
  description = "Account to create. May be one of: controller; login; or compute."
  type        = string
  default     = "controller"

  validation {
    condition = (
    contains(["controller", "login", "compute"], lower(var.account_type)))
    error_message = "Must be one of: controller; login; compute; or null."
  }
}
