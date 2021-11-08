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

provider "google" {
  project = var.project_id
}

resource "random_uuid" "cluster_id" {
}

data "google_compute_network" "default" {
  name = "default"
}

module "slurm_instance_template" {
  source = "../../../modules/slurm_instance_template"

  project_id = var.project_id
  network    = data.google_compute_network.default.self_link

  cluster_id = random_uuid.cluster_id.result
}
