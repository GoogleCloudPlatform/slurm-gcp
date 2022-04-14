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

provider "google" {
  project = var.project_id
}

data "google_compute_subnetwork" "default" {
  name   = "default"
  region = var.region
}

module "slurm_controller_template" {
  source = "../../../modules/slurm_instance_template"

  project_id = var.project_id
  subnetwork = data.google_compute_subnetwork.default.self_link

  slurm_cluster_name = var.slurm_cluster_name
  slurm_cluster_id   = module.slurm_controller_instance.slurm_cluster_id
}

module "slurm_controller_instance" {
  source = "../../../modules/slurm_controller_instance"

  instance_template  = module.slurm_controller_template.self_link
  subnetwork         = data.google_compute_subnetwork.default.self_link
  project_id         = var.project_id
  slurm_cluster_name = var.slurm_cluster_name
}
