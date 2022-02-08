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
  region = (
    length(regexall("/regions/([^/]*)", var.subnetwork)) > 0
    ? flatten(regexall("/regions/([^/]*)", var.subnetwork))[0]
    : var.region
  )

  etc_dir = abspath("${path.module}/../../../etc")
}

##################
# LOCALS: CONFIG #
##################

locals {
  slurm_cluster_id = (
    var.slurm_cluster_id == null || var.slurm_cluster_id == ""
    ? random_uuid.slurm_cluster_id.result
    : var.slurm_cluster_id
  )

  munge_key = (
    var.munge_key == null
    ? random_id.munge_key.b64_std
    : var.munge_key
  )

  jwt_key = (
    var.jwt_key == null
    ? random_id.jwt_key.b64_std
    : var.jwt_key
  )

  partitions   = { for p in var.partitions[*].partition : p.partition_name => p }
  compute_list = flatten(var.partitions[*].compute_list)
}

####################
# LOCALS: METADATA #
####################

locals {
  metadata_config = {
    cluster_name = var.cluster_name
    project      = var.project_id

    cloudsql  = var.cloudsql
    munge_key = local.munge_key
    jwt_key   = local.jwt_key

    pubsub_topic_id = google_pubsub_topic.this.name

    slurm_cluster_id = local.slurm_cluster_id

    network_storage       = var.network_storage
    login_network_storage = var.login_network_storage

    cloud_parameters = {
      ResumeRate     = lookup(var.cloud_parameters, "ResumeRate", 0)
      SuspendRate    = lookup(var.cloud_parameters, "SuspendRate", 0)
      ResumeTimeout  = lookup(var.cloud_parameters, "ResumeTimeout", 300)
      SuspendTimeout = lookup(var.cloud_parameters, "SuspendTimeout", 300)
    }
    partitions = local.partitions
  }
}

################
# LOCALS: CONF #
################

locals {
  slurmdbd_conf_tpl = (
    var.slurmdbd_conf_tpl == null
    ? abspath("${local.etc_dir}/slurmdbd.conf.tpl")
    : abspath(var.slurmdbd_conf_tpl)
  )

  slurm_conf_tpl = (
    var.slurm_conf_tpl == null
    ? abspath("${local.etc_dir}/slurm.conf.tpl")
    : abspath(var.slurm_conf_tpl)
  )

  cgroup_conf_tpl = (
    var.cgroup_conf_tpl == null
    ? abspath("${local.etc_dir}/cgroup.conf.tpl")
    : abspath(var.cgroup_conf_tpl)
  )
}

##############
# DATA: CONF #
##############

data "local_file" "slurmdbd_conf_tpl" {
  filename = local.slurmdbd_conf_tpl
}

data "local_file" "slurm_conf_tpl" {
  filename = local.slurm_conf_tpl
}

data "local_file" "cgroup_conf_tpl" {
  filename = local.cgroup_conf_tpl
}

##########
# RANDOM #
##########

resource "random_uuid" "slurm_cluster_id" {
}

resource "random_id" "munge_key" {
  byte_length = 256
}

resource "random_id" "jwt_key" {
  byte_length = 256
}

############
# INSTANCE #
############

module "slurm_controller_instance" {
  source = "../_slurm_instance"

  access_config       = var.access_config
  add_hostname_suffix = false
  cluster_name        = var.cluster_name
  hostname            = "${var.cluster_name}-controller"
  instance_template   = var.instance_template
  network             = var.network
  project_id          = var.project_id
  region              = local.region
  slurm_cluster_id    = local.slurm_cluster_id
  slurm_instance_type = "controller"
  static_ips          = var.static_ips
  subnetwork_project  = var.subnetwork_project
  subnetwork          = var.subnetwork
  zone                = var.zone

  depends_on = [
    google_compute_project_metadata_item.controller_d,
    # Ensure nodes are destroyed before controller is
    module.cleanup,
  ]
}

####################
# METADATA: CONFIG #
####################

resource "google_compute_project_metadata_item" "config" {
  project = var.project_id

  key   = "${var.cluster_name}-slurm-config"
  value = jsonencode(local.metadata_config)
}

resource "google_compute_project_metadata_item" "slurm_conf" {
  project = var.project_id

  key   = "${var.cluster_name}-slurm-tpl-slurm-conf"
  value = data.local_file.slurm_conf_tpl.content
}

resource "google_compute_project_metadata_item" "cgroup_conf" {
  project = var.project_id

  key   = "${var.cluster_name}-slurm-tpl-cgroup-conf"
  value = data.local_file.cgroup_conf_tpl.content
}

resource "google_compute_project_metadata_item" "slurmdbd_conf" {
  project = var.project_id

  key   = "${var.cluster_name}-slurm-tpl-slurmdbd-conf"
  value = data.local_file.slurmdbd_conf_tpl.content
}

###################
# METADATA: DEVEL #
###################

module "slurm_metadata_devel" {
  source = "../_slurm_metadata_devel"

  count = var.enable_devel ? 1 : 0

  cluster_name = var.cluster_name
  project_id   = var.project_id
}

#####################
# METADATA: SCRIPTS #
#####################

resource "google_compute_project_metadata_item" "controller_d" {
  project = var.project_id

  for_each = {
    for x in var.controller_d
    : replace(basename(x.filename), "/[^a-zA-Z0-9-_]/", "_") => x
  }

  key   = "${var.cluster_name}-slurm-controller-script-${each.key}"
  value = each.value.content
}

resource "google_compute_project_metadata_item" "compute_d" {
  project = var.project_id

  for_each = {
    for x in var.compute_d
    : replace(basename(x.filename), "/[^a-zA-Z0-9-_]/", "_") => x
  }

  key   = "${var.cluster_name}-slurm-compute-script-${each.key}"
  value = each.value.content
}

##################
# PUBSUB: SCHEMA #
##################

resource "google_pubsub_schema" "this" {
  name       = "${var.cluster_name}-slurm-events"
  type       = "PROTOCOL_BUFFER"
  definition = <<EOD
syntax = "proto3";
message Results {
  string request = 1;
  string timestamp = 2;
}
EOD

  lifecycle {
    create_before_destroy = true
  }
}

#################
# PUBSUB: TOPIC #
#################

resource "google_pubsub_topic" "this" {
  name = "${var.cluster_name}-slurm-events"

  schema_settings {
    schema   = google_pubsub_schema.this.id
    encoding = "JSON"
  }

  labels = {
    slurm_cluster_id = local.slurm_cluster_id
  }

  lifecycle {
    create_before_destroy = true
  }
}

##########
# PUBSUB #
##########

module "slurm_pubsub" {
  source  = "terraform-google-modules/pubsub/google"
  version = "~> 3.0"

  project_id = var.project_id
  topic      = google_pubsub_topic.this.id

  create_topic = false

  pull_subscriptions = flatten([
    [
      {
        name                    = module.slurm_controller_instance.instances_details[0].name
        ack_deadline_seconds    = 120
        enable_message_ordering = true
        maximum_backoff         = "300s"
        minimum_backoff         = "30s"
      },
    ],
    [
      for nodename in local.compute_list
      : {
        name                    = nodename
        ack_deadline_seconds    = 60
        enable_message_ordering = true
        maximum_backoff         = "300s"
        minimum_backoff         = "30s"
      }
    ],
  ])

  subscription_labels = {
    slurm_cluster_id = local.slurm_cluster_id
  }
}

####################
# NOTIFY: RECONFIG #
####################

module "notify_reconfigure" {
  source = "../slurm_notify_cluster"

  topic = google_pubsub_topic.this.name
  type  = "reconfig"

  triggers = {
    compute_list  = join(",", local.compute_list)
    config        = sha256(google_compute_project_metadata_item.config.value)
    cgroup_conf   = sha256(google_compute_project_metadata_item.cgroup_conf.value)
    slurm_conf    = sha256(google_compute_project_metadata_item.slurm_conf.value)
    slurmdbd_conf = sha256(google_compute_project_metadata_item.slurmdbd_conf.value)
  }

  depends_on = [
    # Ensure subscriptions are created
    module.slurm_pubsub,
    # Ensure controller is created
    module.slurm_controller_instance,
  ]
}

#################
# DESTROY NODES #
#################

# Destroy all compute nodes on `terraform destroy`
module "cleanup" {
  source = "../slurm_destroy_nodes"

  slurm_cluster_id = local.slurm_cluster_id
  when_destroy     = true
}

# Destroy all compute nodes when the compute node environment changes
module "delta_critical" {
  source = "../slurm_destroy_nodes"

  slurm_cluster_id = local.slurm_cluster_id

  triggers = merge(
    {
      for x in var.compute_d
      : "compute_d_${replace(basename(x.filename), "/[^a-zA-Z0-9-_]/", "_")}"
      => sha256(x.content)
    },
    {
      controller_id = module.slurm_controller_instance.instances_details[0].instance_id
    },
  )

  depends_on = [
    # Ensure compute_d metadata is updated before destroying nodes
    google_compute_project_metadata_item.compute_d,
  ]
}

# Destroy all removed compute nodes when partitions change
module "delta_compute_list" {
  source = "../slurm_destroy_nodes"

  slurm_cluster_id = local.slurm_cluster_id
  exclude_list     = local.compute_list

  triggers = {
    compute_list = join(",", local.compute_list)
  }

  depends_on = [
    # Prevent race condition
    module.delta_critical,
  ]
}

#############################
# DESTROY RESOURCE POLICIES #
#############################

# Destroy all resource policies on `terraform destroy`
module "cleanup_resource_policies" {
  source = "../slurm_destroy_resource_policies"

  cluster_name = var.cluster_name
  when_destroy = true
}
