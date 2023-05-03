import importlib
import pkgutil
import logging
import inspect

# Only perform discovery at init
discovered_plugins = {
    name.lstrip("."): importlib.import_module(name=name, package="slurm_gcp_plugins")
    for finder, name, ispkg in pkgutil.iter_modules(path=__path__, prefix=".")
    if name.lstrip(".") != "utils"
}

logging.info(
    (
        "slurm_gcp_plugins found:"
        + ", ".join(
            [
                "slurm_gcp_plugins" + plugin
                for plugin in sorted(discovered_plugins.keys())
            ]
        )
    )
)


def get_plugins():
    return discovered_plugins


def get_plugins_function(function_name):
    plugins = get_plugins()

    return {
        plugin: function
        for plugin in sorted(plugins.keys())
        for name, function in inspect.getmembers(plugins[plugin], inspect.isfunction)
        if name == function_name
    }


def run_plugins_for_function(plugin_function_name, pos_args, keyword_args):
    if "lkp" not in keyword_args:
        logging.error(
            (
                f"Plugin callback {plugin_function_name} called"
                + 'without a "lkp" argument neeed to get obtain deployment'
                + "information"
            )
        )
        return

    if not keyword_args["lkp"].cfg:
        logging.error(
            (
                f"Plugin callback {plugin_function_name} called"
                + 'with "lkp.cfg" unpopulated. lkp.cfg is needed'
                + "to argument neeed to get obtain deployment"
                + "information"
            )
        )
        return

    cfg = keyword_args["lkp"].cfg
    if cfg.enable_slurm_gcp_plugins:
        for plugin, function in get_plugins_function(plugin_function_name).items():
            if plugin in cfg.enable_slurm_gcp_plugins:
                logging.debug(f"Running {function} from plugin {plugin}")
                try:
                    function(*pos_args, **keyword_args)
                except BaseException as e:
                    logging.error(
                        f"Plugin callback {plugin}:{function} caused an exception: {e}"
                    )
            else:
                logging.debug(
                    f"Not running {function} from non-enabled plugin {plugin}"
                )


# Implement this function to add fields to the cached VM instance lookup
def register_instance_information_fields(*pos_args, **keyword_args):
    run_plugins_for_function(
        plugin_function_name="register_instance_information_fields",
        pos_args=pos_args,
        keyword_args=keyword_args,
    )


# Called just after VM instances have been created and are up
def post_prolog_resume_nodes(*pos_args, **keyword_args):
    run_plugins_for_function(
        plugin_function_name="post_prolog_resume_nodes",
        pos_args=pos_args,
        keyword_args=keyword_args,
    )


# Called just before VM instances are deleted should be still up
# (NOTE: if a node has failed it might not be up or unresponsive)
def pre_epilog_suspend_nodes(*pos_args, **keyword_args):
    run_plugins_for_function(
        plugin_function_name="pre_epilog_suspend_nodes",
        pos_args=pos_args,
        keyword_args=keyword_args,
    )


# Called just before VM instances are created are created with
# bulkInsert- this function can be implemented to inspect and/or
# modify the insertion request.
def pre_instance_bulk_insert(*pos_args, **keyword_args):
    run_plugins_for_function(
        plugin_function_name="pre_instance_bulk_insert",
        pos_args=pos_args,
        keyword_args=keyword_args,
    )


# Called just before placement groups are created - this function can
# be implemented to inspect and/or modify the insertion request.
def pre_placement_group_insert(*pos_args, **keyword_args):
    run_plugins_for_function(
        plugin_function_name="pre_placement_group_insert",
        pos_args=pos_args,
        keyword_args=keyword_args,
    )


__all__ = [
    "post_prolog_resume_nodes",
    "pre_epilog_suspend_nodes",
    "register_instance_information_fields",
    "pre_instance_bulk_insert",
    "pre_placement_group_insert",
]
