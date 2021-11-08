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

output "cluster_id" {
  description = "Slurm cluster ID."
  value       = module.slurm_controller_instance.cluster_id
}

output "cluster_name" {
  description = "Slurm cluster name."
  value       = module.slurm_controller_instance.cluster_name
}

output "template_map" {
  description = "Slurm compute isntance template map."
  value       = local.template_map
}

output "partitions" {
  description = "Configured Slurm partitions."
  value       = local.partitions
}
