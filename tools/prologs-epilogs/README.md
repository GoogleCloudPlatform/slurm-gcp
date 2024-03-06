# Prologs and Epilogs

The following scripts function simultaneously as both prolog and epilog scripts
when used with the [external epilog and prolog][epe] feature of the
slurm_controller_instance module.

A typical approach is to stage these files to `/opt/apps/adm/slurm/scripts/` on
the controller and then use symbolic links pointing from the directories that
are iterated over by the `slurm_mux` external epilog and prolog feature.

For example, if the following symbolic links are created:

```
/opt/apps/adm/slurm/prolog_slurmd.d/start-rxdm.prolog_slurmd -> ../scripts/receive-data-path-manager
/opt/apps/adm/slurm/epilog_slurmd.d/stop-rxdm.epilog_slurmd -> ../scripts/receive-data-path-manager
```

Then the Receive Data Path Manager (RxDM) will be started before every user's
job and stopped upon job exit, whether successful or failed. They can otherwise
be run on a partition by partition basis, if they are placed in
partition-specific directories. In the example below, the partition is named
"a3":

```
/opt/apps/adm/slurm/partition-a3-prolog_slurmd.d/start-rxdm.prolog_slurmd -> ../scripts/receive-data-path-manager
/opt/apps/adm/slurm/partition-a3-epilog_slurmd.d/stop-rxdm.epilog_slurmd -> ../scripts/receive-data-path-manager
```

[epe]: ../../terraform/slurm_cluster/modules/slurm_controller_instance/README_TF.md#input_enable_external_prolog_epilog
