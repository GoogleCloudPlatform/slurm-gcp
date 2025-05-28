# Cloud Cluster Guide

[FAQ](./faq.md) | [Troubleshooting](./troubleshooting.md) |
[Glossary](./glossary.md)

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=1 -->

- [Cloud Cluster Guide](#cloud-cluster-guide)
  - [Overview](#overview)
  - [Cluster Toolkit](#cluster-toolkit)
  - [Terraform](#terraform)
  - [Quickstart Examples](#quickstart-examples)

<!-- mdformat-toc end -->

## Overview

This guide focuses on setting up a cloud [Slurm cluster](./glossary.md#slurm).
in the cloud. Deploying in a cloud environment involves various decisions and
considerations, which this guide will cover, along with recommended solutions.

There are two primary deployment methods for cloud cluster management:

- [Cluster Toolkit](#cluster-toolkit)
- [Terraform](#terraform) 

## Cluster Toolkit
The Cluster Toolkit provides a set of modules designed to simplify Slurm cluster
creation. While it leverages Terraform for underlying infrastructure deployment
and management, the Cluster Toolkit offers an abstraction layer that streamlines
the operational process, making it a powerful and convenient deployment option.

For detailed information, please refer to the
[Cluster Toolkit documentation](https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md).

> **NOTE:** When deploying with the
> [Cluster toolkit](https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md),
> use the command `gclusster deploy <your_config>.yaml`. This command manages the
> underlying infrastructure deployment automatically, replacing the standard
> Terraform workflow (e.g., `terraform init`, `terraform validat`, `terraform apply`).
> Please refer to [Create the cluster deployment folder][deployment] for more details.

## Terraform

This deployment method directly utilizes [Terraform](./glossary.md#terraform) to deploy
and manage cluster infrastructure.While potentially more complex than using the Cluster
Toolkit, it offers greater flexibility and customization capabilities.

If you are unfamiliar with [terraform](./glossary.md#terraform), we recommend reviewing the
[official Terraform documentation](https://www.terraform.io/docs) and the
[Terraform starter guide](https://learn.hashicorp.com/collections/terraform/gcp-get-started)
to familiarize yourself with its core concepts and operations.

### Quickstart Examples

Refer to [toolkit_quickstart][quickstart] for an extensible and robust
example. This can be configured to handle the creation of all supporting resources
(e.g., network, service accounts) or allow you to manage them manually. Slurm can also
be configured with partitions and nodesets as desired.

Alternatively,you can find 
[HPC Blueprints](https://cloud.google.com/hpc-toolkit/docs/setup/hpc-blueprint)
within
[HPC Toolkit](https://cloud.google.com/blog/products/compute/new-google-cloud-hpc-toolkit)
examples.

<!-- Links -->

[quickstart]: https://github.com/GoogleCloudPlatform/cluster-toolkit/blob/main/README.md#quickstart
[deployment]: https://cloud.google.com/cluster-toolkit/docs/quickstarts/slurm-cluster#create_the_cluster_deployment_folder
