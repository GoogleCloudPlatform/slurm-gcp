#!/usr/bin/env python3

# Copyright 2019 SchedMD LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import httplib2
import logging
import logging.config
import os
import re
import shelve
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache, reduce, partialmethod
from itertools import chain, compress, islice
from pathlib import Path
from time import sleep, time

import google.auth
from google.oauth2 import service_account
import googleapiclient.discovery
import google_auth_httplib2
from googleapiclient.http import set_user_agent

from requests import get as get_url
from requests.exceptions import RequestException

import yaml
from addict import Dict as NSDict


USER_AGENT = "Slurm_GCP_Scripts/1.5 (GPN:SchedMD)"
ENV_CONFIG_YAML = os.getenv("SLURM_CONFIG_YAML")
if ENV_CONFIG_YAML:
    CONFIG_FILE = Path(ENV_CONFIG_YAML)
else:
    CONFIG_FILE = Path(__file__).with_name("config.yaml")
API_REQ_LIMIT = 2000

log = logging.getLogger(__name__)
def_creds, project = google.auth.default()
Path.mkdirp = partialmethod(Path.mkdir, parents=True, exist_ok=True)

# readily available compute api handle
compute = None
# slurm-gcp config object, could be None if not available
cfg = None
# caching Lookup object
lkp = None

# load all directories as Paths into a dict-like namespace
dirs = NSDict(
    {
        n: Path(p)
        for n, p in dict.items(
            {
                "home": "/home",
                "apps": "/opt/apps",
                "slurm": "/slurm",
                "scripts": "/slurm/scripts",
                "custom_scripts": "/slurm/custom_scripts",
                "munge": "/etc/munge",
                "secdisk": "/mnt/disks/sec",
                "log": "/var/log/slurm",
            }
        )
    }
)

slurmdirs = NSDict(
    {
        n: Path(p)
        for n, p in dict.items(
            {
                "prefix": "/usr/local",
                "etc": "/usr/local/etc/slurm",
                "state": "/var/spool/slurm",
            }
        )
    }
)


def publish_message(project_id, topic_id, message) -> None:
    """Publishes message to a Pub/Sub topic."""
    from google.cloud import pubsub_v1
    from google import api_core

    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_id)

    retry_handler = api_core.retry.Retry(
        predicate=api_core.retry.if_exception_type(
            api_core.exceptions.Aborted,
            api_core.exceptions.DeadlineExceeded,
            api_core.exceptions.InternalServerError,
            api_core.exceptions.ResourceExhausted,
            api_core.exceptions.ServiceUnavailable,
            api_core.exceptions.Unknown,
            api_core.exceptions.Cancelled,
        ),
    )

    message_bytes = message.encode("utf-8")
    future = publisher.publish(topic_path, message_bytes, retry=retry_handler)
    result = future.exception()
    if result is not None:
        raise result

    print(f"Published message to '{topic_path}'.")


def access_secret_version(project_id, secret_id, version_id="latest"):
    """
    Access the payload for the given secret version if one exists. The version
    can be a version number as a string (e.g. "5") or an alias (e.g. "latest").
    """
    from google.cloud import secretmanager
    from google.api_core import exceptions

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    try:
        response = client.access_secret_version(request={"name": name})
        log.debug(f"Secret '{name}' was found.")
        payload = response.payload.data.decode("UTF-8")
    except exceptions.NotFound:
        log.debug(f"Secret '{name}' was not found!")
        payload = None

    return payload


def parse_self_link(self_link: str):
    """Parse a selfLink url, extracting all useful values
    https://.../v1/projects/<project>/regions/<region>/...
    {'project': <project>, 'region': <region>, ...}
    can also extract zone, instance (name), image, etc
    """
    link_patt = re.compile(r"(?P<key>[^\/\s]+)s\/(?P<value>[^\s\/]+)")
    return NSDict(link_patt.findall(self_link))


def subscription_list(project_id=None, page_size=None, slurm_cluster_id=None):
    """List pub/sub subscription"""
    from google.cloud import pubsub_v1

    if project_id is None:
        project_id = project
    if slurm_cluster_id is None:
        slurm_cluster_id = lkp.cfg.slurm_cluster_id

    subscriber = pubsub_v1.SubscriberClient()

    subscriptions = []
    # get first page
    page = subscriber.list_subscriptions(
        request={
            "project": f"projects/{project_id}",
            "page_size": page_size,
        }
    )
    subscriptions.extend(page.subscriptions)
    # walk the pages
    while page.next_page_token:
        page = subscriber.list_subscriptions(
            request={
                "project": f"projects/{project_id}",
                "page_token": page.next_page_token,
                "page_size": page_size,
            }
        )
        subscriptions.extend(page.subscriptions)
    # manual filter by label
    subscriptions = [
        s for s in subscriptions if s.labels.get("slurm_cluster_id") == slurm_cluster_id
    ]

    return subscriptions


def subscription_create(subscription_id, project_id=None):
    """Create pub/sub subscription"""
    from google.cloud import pubsub_v1
    from google.api_core import exceptions

    if project_id is None:
        project_id = lkp.project
    topic_id = lkp.cfg.pubsub_topic_id

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(project_id, topic_id)
    subscription_path = subscriber.subscription_path(project_id, subscription_id)

    with subscriber:
        request = {
            "name": subscription_path,
            "topic": topic_path,
            "ack_deadline_seconds": 60,
            "labels": {
                "slurm_cluster_id": cfg.slurm_cluster_id,
            },
        }
        try:
            subscription = subscriber.create_subscription(request=request)
            log.info(f"Subscription created: {subscription}")
        except exceptions.AlreadyExists:
            log.info(f"Subscription '{subscription_path}' already exists!")


def subscription_delete(subscription_id, project_id=None):
    """Delete pub/sub subscription"""
    from google.cloud import pubsub_v1
    from google.api_core import exceptions

    if project_id is None:
        project_id = lkp.project

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(project_id, subscription_id)

    with subscriber:
        try:
            subscriber.delete_subscription(request={"subscription": subscription_path})
            log.info(f"Subscription deleted: {subscription_path}.")
        except exceptions.NotFound:
            log.info(f"Subscription '{subscription_path}' not found!")


def execute_with_futures(func, list):
    with ThreadPoolExecutor() as exe:
        futures = []
        for i in list:
            future = exe.submit(func, i)
            futures.append(future)
        for future in futures:
            result = future.exception()
            if result is not None:
                raise result


def split_nodelist(nodelist):
    """split nodelist expression into independent host expressions"""
    # We do this in order to eliminate nodes we don't need to handle prior to
    # expansion
    # split on commas that are not within brackets
    hostlist_patt = re.compile(r",(?![^\[]*\])")
    nodes = hostlist_patt.split(nodelist)
    return nodes


def is_exclusive_node(node):
    partition = lkp.node_partition(node)
    return not lkp.node_is_static(node) and (
        partition.enable_job_exclusive or partition.enable_placement_groups
    )


def compute_service(credentials=None, user_agent=USER_AGENT):
    """Make thread-safe compute service handle
    creates a new Http for each request
    """
    try:
        key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    except KeyError:
        key_path = None
    if key_path is not None:
        credentials = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    elif credentials is None:
        credentials = def_creds

    def build_request(http, *args, **kwargs):
        new_http = httplib2.Http()
        if user_agent is not None:
            new_http = set_user_agent(new_http, user_agent)
        if credentials is not None:
            new_http = google_auth_httplib2.AuthorizedHttp(credentials, http=new_http)
        return googleapiclient.http.HttpRequest(new_http, *args, **kwargs)

    return googleapiclient.discovery.build(
        "compute",
        "v1",
        requestBuilder=build_request,
        credentials=credentials,
    )


compute = compute_service()


def load_config_data(config):
    """load dict-like data into a config object"""
    cfg = NSDict(config)
    if not cfg.slurm_log_dir:
        cfg.slurm_log_dir = dirs.log
    if not cfg.slurm_bin_dir:
        cfg.slurm_bin_dir = slurmdirs.prefix / "bin"
    return cfg


def new_config(config):
    """initialize a new config object
    necessary defaults are handled here
    """
    cfg = load_config_data(config)

    network_storage_iter = filter(
        None,
        (
            *cfg.network_storage,
            *cfg.login_network_storage,
            *chain.from_iterable(p.network_storage for p in cfg.partitions.values()),
        ),
    )
    for netstore in network_storage_iter:
        if netstore.server_ip == "$controller":
            netstore.server_ip = cfg.slurm_cluster_name + "-controller"
    return cfg


def config_from_metadata():
    # get setup config from metadata
    slurm_cluster_name = instance_metadata("attributes/slurm_cluster_name")
    if not slurm_cluster_name:
        return None

    metadata_key = f"{slurm_cluster_name}-slurm-config"
    RETRY_WAIT = 5
    for i in range(8):
        if i:
            log.error(f"config not found in project metadata, retry {i}")
            sleep(RETRY_WAIT)
        config_yaml = project_metadata.__wrapped__(metadata_key)
        if config_yaml is not None:
            break
    else:
        return None
    cfg = new_config(yaml.safe_load(config_yaml))
    return cfg


def load_config_file(path):
    """load config from file"""
    content = None
    try:
        content = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        log.error(f"config file not found: {path}")
        return None
    return load_config_data(content)


def save_config(cfg, path):
    """save given config to file at path"""
    Path(path).write_text(yaml.dump(cfg, Dumper=Dumper))


def config_root_logger(
    caller_logger, level="DEBUG", util_level=None, stdout=True, logfile=None
):
    """configure the root logger, disabling all existing loggers"""
    if not util_level:
        util_level = level
    handlers = list(compress(("stdout_handler", "file_handler"), (stdout, logfile)))

    config = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "standard": {
                "format": "",
            },
            "stamp": {
                "format": "%(asctime)s %(process)s %(thread)s %(name)s %(levelname)s: %(message)s",
            },
        },
        "handlers": {
            "stdout_handler": {
                "level": "DEBUG",
                "formatter": "standard",
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
            },
        },
        "loggers": {
            __name__: {  # enable util.py logging
                "level": util_level,
            },
        },
        "root": {
            "handlers": handlers,
            "level": level,
        },
    }
    if logfile:
        config["handlers"]["file_handler"] = {
            "level": "DEBUG",
            "formatter": "stamp",
            "class": "logging.handlers.WatchedFileHandler",
            "filename": logfile,
        }
    logging.config.dictConfig(config)
    loggers = (
        "resume",
        "suspend",
        "slurmsync",
        "setup",
        caller_logger,
    )
    for logger in map(logging.getLogger, loggers):
        logger.disabled = False


def handle_exception(exc_type, exc_value, exc_trace):
    """log exceptions other than KeyboardInterrupt"""
    # TODO does this work?
    if not issubclass(exc_type, KeyboardInterrupt):
        log.exception("Fatal exception", exc_info=(exc_type, exc_value, exc_trace))
    sys.__excepthook__(exc_type, exc_value, exc_trace)


def run(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
    timeout=None,
    check=True,
    universal_newlines=True,
    **kwargs,
):
    """Wrapper for subprocess.run() with convenient defaults"""
    log.debug(f"run: {cmd}")
    args = cmd if shell else shlex.split(cmd)
    result = subprocess.run(
        args,
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        timeout=timeout,
        check=check,
        universal_newlines=universal_newlines,
        **kwargs,
    )
    return result


def spawn(cmd, quiet=False, shell=False, **kwargs):
    """nonblocking spawn of subprocess"""
    if not quiet:
        log.debug(f"spawn: {cmd}")
    args = cmd if shell else shlex.split(cmd)
    return subprocess.Popen(args, shell=shell, **kwargs)


def chown_slurm(path, mode=None):
    if path.exists():
        if mode:
            path.chmod(mode)
    else:
        path.parent.mkdirp()
        if mode:
            path.touch(mode=mode)
        else:
            path.touch()
    try:
        shutil.chown(path, user="slurm", group="slurm")
    except PermissionError:
        log.error(f"Not authorized to 'chown slurm:slurm {path}'.")


@contextmanager
def cd(path):
    """Change working directory for context"""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def with_static(**kwargs):
    def decorate(func):
        for var, val in kwargs.items():
            setattr(func, var, val)
        return func

    return decorate


def cached_property(f):
    return property(lru_cache()(f))


def seperate(pred, coll):
    """filter into 2 lists based on pred returning True or False
    returns ([False], [True])
    """
    return reduce(lambda acc, el: acc[pred(el)].append(el) or acc, coll, ([], []))


def chunked(iterable, n=API_REQ_LIMIT):
    """group iterator into chunks of max size n"""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            return
        yield chunk


def groupby_unsorted(seq, key):
    indices = defaultdict(list)
    for i, el in enumerate(seq):
        indices[key(el)].append(i)
    for k, idxs in indices.items():
        yield k, (seq[i] for i in idxs)


ROOT_URL = "http://metadata.google.internal/computeMetadata/v1"


def get_metadata(path, root=ROOT_URL):
    """Get metadata relative to metadata/computeMetadata/v1"""
    HEADERS = {"Metadata-Flavor": "Google"}
    url = f"{root}/{path}"
    try:
        resp = get_url(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text
    except RequestException:
        log.error(f"Error while getting metadata from {url}")
        return None


@lru_cache(maxsize=None)
def instance_metadata(path):
    """Get instance metadata"""
    return get_metadata(path, root=f"{ROOT_URL}/instance")


@lru_cache(maxsize=None)
def project_metadata(key):
    """Get project metadata project/attributes/<slurm_cluster_name>-<path>"""
    return get_metadata(key, root=f"{ROOT_URL}/project/attributes")


def nodeset_prefix(node_group, part_name):
    return f"{cfg.slurm_cluster_name}-{part_name}-{node_group.group_name}"


def nodeset_lists(node_group, part_name):
    """Return static and dynamic nodenames given a partition node type
    definition
    """

    def node_range(count, start=0):
        end = start + count - 1
        return f"{start}" if count == 1 else f"[{start}-{end}]", end + 1

    prefix = nodeset_prefix(node_group, part_name)
    static_count = node_group.count_static
    dynamic_count = node_group.count_dynamic
    static_range, end = node_range(static_count) if static_count else (None, 0)
    dynamic_range, _ = node_range(dynamic_count, end) if dynamic_count else (None, 0)

    static_nodelist = f"{prefix}-{static_range}" if static_count else None
    dynamic_nodelist = f"{prefix}-{dynamic_range}" if dynamic_count else None
    return static_nodelist, dynamic_nodelist


def natural_sort(text):
    def atoi(text):
        return int(text) if text.isdigit() else text

    return [atoi(w) for w in re.split(r"(\d+)", text)]


def to_hostlist(nodenames):
    """make hostlist from list of node names"""
    # use tmp file because list could be large
    tmp_file = tempfile.NamedTemporaryFile(mode="w+t", delete=False)
    tmp_file.writelines("\n".join(sorted(nodenames, key=natural_sort)))
    tmp_file.close()
    log.debug("tmp_file = {}".format(tmp_file.name))

    hostlist = run(f"{lkp.scontrol} show hostlist {tmp_file.name}").stdout.rstrip()
    log.debug("hostlist = {}".format(hostlist))
    os.remove(tmp_file.name)
    return hostlist


def to_hostnames(nodelist):
    """make list of hostnames from hostlist expression"""
    if isinstance(nodelist, str):
        hostlist = nodelist
    else:
        hostlist = ",".join(nodelist)
    hostnames = run(f"{lkp.scontrol} show hostnames {hostlist}").stdout.splitlines()
    return hostnames


def retry_exception(exc):
    """return true for exceptions that should always be retried"""
    retry_errors = (
        "Rate Limit Exceeded",
        "Quota Exceeded",
    )
    return any(e in str(exc) for e in retry_errors)


def ensure_execute(request):
    """Handle rate limits and socket time outs"""

    retry = 0
    wait = 1
    max_wait = 60
    while True:
        try:
            return request.execute()

        except googleapiclient.errors.HttpError as e:
            if retry_exception(e):
                retry += 1
                wait = min(wait * 2, max_wait)
                log.error(f"retry:{retry} sleep:{wait} '{e}'")
                sleep(wait)
                continue
            raise

        except socket.timeout as e:
            # socket timed out, try again
            log.debug(e)

        except Exception as e:
            log.error(e, exc_info=True)
            raise

        break


def batch_execute(requests, compute=compute, retry_cb=None):
    """execute list or dict<req_id, request> as batch requests
    retry if retry_cb returns true
    """
    BATCH_LIMIT = 1000
    if not isinstance(requests, dict):
        requests = {str(k): v for k, v in enumerate(requests)}  # rid generated here
    done = {}
    failed = {}
    timestamps = []
    rate_limited = False

    def batch_callback(rid, resp, exc):
        nonlocal rate_limited
        if exc is not None:
            log.error(f"compute request exception {rid}: {exc}")
            if retry_exception(exc):
                rate_limited = True
            else:
                req = requests.pop(rid)
                failed[rid] = (req, exc)
        else:
            # if retry_cb is set, don't move to done until it returns false
            if retry_cb is None or not retry_cb(resp):
                requests.pop(rid)
                done[rid] = resp

    def batch_request(reqs):
        batch = compute.new_batch_http_request(callback=batch_callback)
        for rid, req in reqs:
            batch.add(req, request_id=rid)
        return batch

    while requests:
        if timestamps:
            timestamps = [stamp for stamp in timestamps if stamp > time()]
        if rate_limited and timestamps:
            stamp = next(iter(timestamps))
            sleep(max(stamp - time(), 0))
            rate_limited = False
        # up to API_REQ_LIMIT (2000) requests
        # in chunks of up to BATCH_LIMIT (1000)
        batches = [
            batch_request(chunk)
            for chunk in chunked(islice(requests.items(), API_REQ_LIMIT), BATCH_LIMIT)
        ]
        timestamps.append(time() + 100)
        with ThreadPoolExecutor() as exe:
            futures = []
            for batch in batches:
                future = exe.submit(ensure_execute, batch)
                futures.append(future)
            for future in futures:
                result = future.exception()
                if result is not None:
                    raise result

    return done, failed


def wait_request(operation, project=project, compute=compute):
    """makes the appropriate wait request for a given operation"""
    if "zone" in operation:
        req = compute.zoneOperations().wait(
            project=project,
            zone=operation["zone"].split("/")[-1],
            operation=operation["name"],
        )
    elif "region" in operation:
        req = compute.regionOperations().wait(
            project=project,
            region=operation["region"].split("/")[-1],
            operation=operation["name"],
        )
    else:
        req = compute.globalOperations().wait(
            project=project, operation=operation["name"]
        )
    return req


def wait_for_operations(operations, project=project, compute=compute):
    """wait for all operations"""

    def operation_retry(resp):
        return resp["status"] != "DONE"

    requests = [wait_request(op) for op in operations]
    return batch_execute(requests, retry_cb=operation_retry)


def wait_for_operation(operation, project=project, compute=compute):
    """wait for given operation"""
    print("Waiting for operation to finish...")
    wait_req = wait_request(operation)

    while True:
        result = ensure_execute(wait_req)
        if result["status"] == "DONE":
            print("done.")
            return result


def get_group_operations(operation, project=project, compute=compute):
    """get list of operations associated with group id"""

    group_id = operation["operationGroupId"]
    if "zone" in operation:
        operation = compute.zoneOperations().list(
            project=project,
            zone=operation["zone"].split("/")[-1],
            filter=f"operationGroupId={group_id}",
        )
    elif "region" in operation:
        operation = compute.regionOperations().list(
            project=project,
            region=operation["region"].split("/")[-1],
            filter=f"operationGroupId={group_id}",
        )
    else:
        operation = compute.globalOperations().list(
            project=project, filter=f"operationGroupId={group_id}"
        )

    return ensure_execute(operation)


class Dumper(yaml.SafeDumper):
    """Add representers for pathlib.Path and NSDict for yaml serialization"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_representer(NSDict, self.represent_nsdict)
        self.add_multi_representer(Path, self.represent_path)

    @staticmethod
    def represent_nsdict(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())

    @staticmethod
    def represent_path(dumper, path):
        return dumper.represent_scalar("tag:yaml.org,2002:str", str(path))


class Lookup:
    """Wrapper class for cached data access"""

    regex = (
        r"^(?P<prefix>"
        r"(?P<name>[^\s\-]+)"
        r"-(?P<partition>[^\s\-]+)"
        r"-(?P<group>\S+)"
        r")"
        r"-(?P<node>"
        r"(?P<index>\d+)|"
        r"(?P<range>\[[\d,-]+\])"
        r")$"
    )
    node_desc_regex = re.compile(regex)

    def __init__(self, cfg=None):
        self._cfg = cfg or NSDict()
        self.template_cache_path = Path(__file__).parent / "template_info.cache"

    @property
    def cfg(self):
        return self._cfg

    @property
    def project(self):
        return self.cfg.project or project

    @property
    def control_host(self):
        if self.cfg.slurm_cluster_name:
            return f"{self.cfg.slurm_cluster_name}-controller"
        return None

    @property
    def scontrol(self):
        return Path(self.cfg.slurm_bin_dir if cfg else "") / "scontrol"

    @property
    def template_map(self):
        return self.cfg.template_map

    @cached_property
    def instance_role(self):
        return instance_metadata("attributes/slurm_instance_role")

    @cached_property
    def compute(self):
        # TODO evaluate when we need to use google_app_cred_path
        if self.cfg.google_app_cred_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.cfg.google_app_cred_path
        return compute_service()

    @cached_property
    def hostname(self):
        return socket.gethostname()

    @cached_property
    def zone(self):
        return instance_metadata("zone")

    @property
    def enable_job_exclusive(self):
        return bool(self.cfg.enable_job_exclusive or self.cfg.enable_placement)

    @lru_cache(maxsize=None)
    def _node_desc(self, node_name):
        """Get parts from node name"""
        if not node_name:
            node_name = self.hostname
        m = self.node_desc_regex.match(node_name)
        if not m:
            raise Exception(f"node name {node_name} is not valid")
        return NSDict(m.groupdict())

    def node_prefix(self, node_name=None):
        return self._node_desc(node_name).prefix

    def node_partition_name(self, node_name=None):
        return self._node_desc(node_name).partition

    def node_group_name(self, node_name=None):
        return self._node_desc(node_name).group

    def node_index(self, node_name=None):
        return int(self._node_desc(node_name).index)

    def node_partition(self, node_name=None):
        return self.cfg.partitions[self.node_partition_name(node_name)]

    def node_group(self, node_name=None):
        group_name = self.node_group_name(node_name)
        return self.node_partition(node_name).partition_nodes[group_name]

    def node_template(self, node_name=None):
        return self.node_group(node_name).instance_template

    def node_template_info(self, node_name=None):
        return self.template_info(self.node_template(node_name))

    def node_region(self, node_name=None):
        partition = self.node_partition(node_name)
        return parse_self_link(partition.subnetwork).region

    def node_is_static(self, node_name=None):
        node_group = self.node_group(node_name)
        return self.node_index(node_name) < node_group.count_static

    @lru_cache(maxsize=1)
    def static_nodelist(self):
        return list(
            filter(
                None,
                (
                    nodeset_lists(node, part.partition_name)[0]
                    for part in self.cfg.partitions.values()
                    for node in part.partition_nodes.values()
                ),
            )
        )

    @lru_cache(maxsize=None)
    def slurm_nodes(self):
        StateTuple = namedtuple("StateTuple", "base,flags")

        def make_node_tuple(node_line):
            """turn node,state line to (node, StateTuple(state))"""
            # state flags include: CLOUD, COMPLETING, DRAIN, FAIL, POWERED_DOWN,
            #   POWERING_DOWN
            node, fullstate = node_line.split(",")
            state = fullstate.split("+")
            state_tuple = StateTuple(state[0], set(state[1:]))
            return (node, state_tuple)

        cmd = (
            f"{self.scontrol} show nodes | "
            r"grep -oP '^NodeName=\K(\S+)|State=\K(\S+)' | "
            r"paste -sd',\n'"
        )
        node_lines = run(cmd, shell=True).stdout.rstrip().splitlines()
        nodes = {
            node: state
            for node, state in map(make_node_tuple, node_lines)
            if "CLOUD" in state.flags
        }
        return nodes

    def slurm_node(self, nodename):
        return self.slurm_nodes().get(nodename)

    @lru_cache(maxsize=1)
    def instances(self, project=None, slurm_cluster_name=None):
        slurm_cluster_name = slurm_cluster_name or self.cfg.slurm_cluster_name
        project = project or self.project
        fields = (
            "items.zones.instances(name,zone,status,machineType,metadata),nextPageToken"
        )
        flt = f"name={slurm_cluster_name}-*"
        act = self.compute.instances()
        op = act.aggregatedList(project=project, fields=fields, filter=flt)

        def properties(inst):
            """change instance properties to a preferred format"""
            inst["zone"] = inst["zone"].split("/")[-1]
            inst["machineType"] = inst["machineType"].split("/")[-1]
            # metadata is fetched as a dict of dicts like:
            # {'key': key, 'value': value}, kinda silly
            metadata = {i["key"]: i["value"] for i in inst["metadata"]["items"]}
            inst["role"] = metadata["slurm_instance_role"]
            del inst["metadata"]  # no need to store all the metadata
            return NSDict(inst)

        instances = {}
        while op is not None:
            result = ensure_execute(op)
            instances.update(
                {
                    inst["name"]: properties(inst)
                    for inst in chain.from_iterable(
                        m["instances"] for m in result["items"].values()
                    )
                }
            )
            op = act.aggregatedList_next(op, result)
        return instances

    def instance(self, instance_name, project=None, slurm_cluster_name=None):
        instances = self.instances(
            project=project, slurm_cluster_name=slurm_cluster_name
        )
        return instances.get(instance_name)

    def subscription(self, instance_name, project=None, slurm_cluster_id=None):
        subscriptions = self.subscriptions(
            project=project, slurm_cluster_id=slurm_cluster_id
        )
        subscriptions = [parse_self_link(s.name).subscription for s in subscriptions]
        return instance_name in subscriptions

    @lru_cache(maxsize=1)
    def machine_types(self, project=None):
        project = project or self.project
        field_names = "name,zone,guestCpus,memoryMb,accelerators"
        fields = f"items.zones.machineTypes({field_names}),nextPageToken"

        machines = defaultdict(dict)
        act = self.compute.machineTypes()
        op = act.aggregatedList(project=project, fields=fields)
        while op is not None:
            result = ensure_execute(op)
            machine_iter = chain.from_iterable(
                m["machineTypes"]
                for m in result["items"].values()
                if "machineTypes" in m
            )
            for machine in machine_iter:
                name = machine["name"]
                zone = machine["zone"]
                machines[name][zone] = machine

            op = act.aggregatedList_next(op, result)
        return machines

    def machine_type(self, machine_type, project=None, zone=None):
        """ """
        if zone:
            project = project or self.project
            machine_info = ensure_execute(
                self.compute.machineTypes().get(
                    project=project, zone=zone, machineType=machine_type
                )
            )
        else:
            machines = self.machine_types(project=project)
            machine_info = next(iter(machines[machine_type].values()))
        return NSDict(machine_info)

    def template_machine_conf(self, template_link, project=None, zone=None):

        template = self.template_info(template_link)
        template.machine_info = self.machine_type(template.machineType, zone=zone)
        machine = template.machine_info
        machine_conf = NSDict()
        # TODO how is smt passed?
        # machine['cpus'] = machine['guestCpus'] // (1 if part.image_hyperthreads else 2) or 1
        machine_conf.cpus = machine.guestCpus
        # Because the actual memory on the host will be different than
        # what is configured (e.g. kernel will take it). From
        # experiments, about 16 MB per GB are used (plus about 400 MB
        # buffer for the first couple of GB's. Using 30 MB to be safe.
        gb = machine.memoryMb // 1024
        machine_conf.memory = machine.memoryMb - (400 + (30 * gb))
        return machine_conf

    def _get_template_info(self, template_link, project):
        template_name = template_link.split("/")[-1]

        # split read and write access to minimize write-lock. This might be a
        # bit slower? TODO measure
        if self.template_cache_path.exists():
            with shelve.open(str(self.template_cache_path), flag="r") as cache:
                if template_name in cache:
                    return NSDict(cache[template_name])

        template = ensure_execute(
            self.compute.instanceTemplates().get(
                project=project, instanceTemplate=template_name
            )
        ).get("properties")
        template = NSDict(template)
        # name and link are not in properties, so stick them in
        template.name = template_name
        template.link = template_link
        # TODO delete metadata to reduce memory footprint?
        # del template.metadata

        # translate gpus into an easier-to-read format
        if template.guestAccelerators:
            template.gpu_type = template.guestAccelerators[0].acceleratorType
            template.gpu_count = template.guestAccelerators[0].acceleratorCount
        else:
            template.gpu_type = None
            template.gpu_count = 0

        # keep write access open for minimum time
        with shelve.open(str(self.template_cache_path), writeback=True) as cache:
            cache[template_name] = template.to_dict()

        return template

    @lru_cache(maxsize=None)
    def template_info(self, template_link, project=None):
        project = project or self.project

        # In the event of concurrent write access to the cache, _get_template_info could fail
        while True:
            try:
                return self._get_template_info(template_link, project)
            except OSError:
                sleep(0.1)
                continue

    @lru_cache(maxsize=1)
    def subscriptions(slef, project=None, slurm_cluster_id=None):
        return subscription_list(project_id=project, slurm_cluster_id=slurm_cluster_id)

    def clear_template_info_cache(self):
        with shelve.open(str(self.template_cache_path), writeback=True) as cache:
            cache.clear()
        self.template_info.cache_clear()


# Define late globals
cfg = load_config_file(CONFIG_FILE)
if not cfg:
    log.warning(f"{CONFIG_FILE} not found")
    cfg = config_from_metadata()
    if cfg:
        save_config(cfg, CONFIG_FILE)
    else:
        log.error("config metadata unavailable")

lkp = Lookup(cfg)
