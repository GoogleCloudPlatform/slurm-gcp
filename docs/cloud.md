# Cloud Cluster Guide

[FAQ](./faq.md) | [Troubleshooting](./troubleshooting.md) |
[Glossary](./glossary.md)

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=1 -->

- [Cloud Cluster Guide](#cloud-cluster-guide)
  - [Overview](#overview)
  - [GCP Marketplace](#gcp-marketplace)
  - [Terraform](#terraform)
    - [Quickstart Examples](#quickstart-examples)

<!-- mdformat-toc end -->

## Overview

This guide focuses on setting up a cloud [Slurm cluster](./glossary.md#slurm).
With cloud, there are decisions that need to be made and certain considerations
taken into account. This guide will cover them and their recommended solutions.

There are two deployment methods for cloud cluster management:

- [Terraform](#terraform)

## Terraform

This deployment method leverages [Terraform](./glossary.md#terraform) to deploy
and manage cluster infrastructure. While this method can be more complex, it is
a robust option. Cluster toolkit provides modules that enables you to create a Slurm cluster with ease.

See the [Cluster toolkit](https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md) for
details.

If you are unfamiliar with [terraform](./glossary.md#terraform), then please
checkout out the [documentation](https://www.terraform.io/docs) and
[starter guide](https://learn.hashicorp.com/collections/terraform/gcp-get-started)
to get you familiar.

### Quickstart Examples

See the [toolkit_quickstart][quickstart] for an extensible and robust
example. It can be configured to handle the creation of all supporting resources
(e.g. network, service accounts) or leave that to you. Slurm can be configured
with partitions and nodesets as desired.

> **NOTE:** For deploying with
> [Cluster toolkit](https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md),
> use the command `gclusster deploy <your_config>.yaml`,
> which manages the underlying infrastructure deployment
> instead of the standard Terraform workflow involving
> `terraform init`, `terraform validate`, and `terraform apply`.
> Please refer to the [Create the cluster deployment folder][deployment].

Alternatively, see
[HPC Blueprints](https://cloud.google.com/hpc-toolkit/docs/setup/hpc-blueprint)
for
[HPC Toolkit](https://cloud.google.com/blog/products/compute/new-google-cloud-hpc-toolkit)
examples.

<!-- Links -->

[quickstart]: https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md#quickstart
[deployment]: https://cloud.google.com/cluster-toolkit/docs/quickstarts/slurm-cluster#create_the_cluster_deployment_folder
