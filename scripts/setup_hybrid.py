#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
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


import argparse
import logging
from pathlib import Path
import setup
import util

log = logging.getLogger()


def main(args):
    log.info("Generating new cloud.conf for slurm.conf")
    setup.gen_cloud_conf(util.lkp)

    log.info("Generating new cloud_gres.conf for gres.conf")
    setup.gen_cloud_gres_conf(util.lkp)

    log.info("Generating new cloud_topology.conf for topology.conf")
    setup.gen_topology_conf(util.lkp)

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    filename = Path(__file__).name
    logfile = Path(filename).with_suffix(".log")
    args = util.init_logs_and_parse(log_path=logfile)

    main(args)
