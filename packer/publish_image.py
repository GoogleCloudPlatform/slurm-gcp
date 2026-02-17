#!/usr/bin/env python3
import argparse
import json
import re
import shlex
import subprocess as sp
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Self

import yaml


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_multi_representer(Path, lambda dumper, data: dumper.represent_str(str(data)))


def dump(obj):
    return yaml.dump(obj, Dumper=Dumper)


@dataclass
class ArgsNamespace(argparse.Namespace):
    config: Path = Path("publish_builds.yaml")
    select: int | None = None
    copy: bool | None = None
    source_project: str | None = None
    publish_project: str | None = None
    source_image_family: str | None = None
    source_image: str | None = None
    slurm_gcp_version: str | None = None
    os_tag: str | None = None
    prefix: str | None = None
    license: str | None = None

    def asdict(self):
        return {
            field.name: val
            for field in fields(self)
            if (val := getattr(self, field.name)) is not None
        }

    def update(self, namespace: Self):
        source = namespace.asdict()
        for field in fields(self):
            val = source.get(field.name)
            if val is not None:
                setattr(self, field.name, val)

    def resolve(self):
        if self.slurm_gcp_version is not None:
            self.slurm_gcp_version = self.slurm_gcp_version.replace(".", "-")
        if self.source_image_family is not None:
            self.source_image_family = self.source_image_family.format(**self.__dict__)
        if self.source_image is not None:
            self.source_image = self.source_image.format(**self.__dict__)
        if self.license:
            self.license = self.license.format(default_license=default_license)


default_license = (
    "projects/schedmd-slurm-public/global/licenses/schedmd-slurm-gcp-free-plan"
)


def parse_self_link(self_link: str):
    """Parse a selfLink url, extracting all useful values
    https://.../v1/projects/<project>/regions/<region>/...
    {'project': <project>, 'region': <region>, ...}
    can also extract zone, instance (name), image, etc
    """
    link_patt = re.compile(r"(?P<key>[^\/\s]+)s\/(?P<value>[^\s\/]+)")
    return dict(link_patt.findall(self_link))


def lookup_image(args):
    if args.source_image is not None:
        proc = sp.run(
            [
                "gcloud",
                "compute",
                "images",
                "describe",
                args.source_image,
                "--project",
                args.source_project,
                "--format",
                "json",
            ],
            stdout=sp.PIPE,
            text=True,
            check=True,
        )
    elif args.source_image_family is not None:
        proc = sp.run(
            [
                "gcloud",
                "compute",
                "images",
                "describe-from-family",
                args.source_image_family,
                "--project",
                args.source_project,
                "--format",
                "json",
            ],
            stdout=sp.PIPE,
            text=True,
            check=True,
        )
    else:
        raise Exception("No source image specified")
    # print(proc.stdout)
    info = json.loads(proc.stdout)
    return info


def confirm(default="n"):
    while True:
        print("Confirm y/n")
        resp = input().lower()
        if resp == "":
            resp = default
        if resp == "y":
            return True
        elif resp == "n":
            return False


def main(args: ArgsNamespace):
    config = yaml.safe_load(args.config.read_text())
    build = ArgsNamespace(**config["default"])
    if args.select is None:
        print("no build selected")
        return 1
    build.update(ArgsNamespace(**config["builds"][args.select]))
    build.update(args)

    build.resolve()
    print(dump(build.asdict()))

    info = lookup_image(build)
    link = parse_self_link(info["selfLink"])
    project = link["project"]
    print(
        "Source image",
        f"{'project:':10}{project}",
        f"{'name:':10}{info['name']}",
        f"{'family:':10}{info['family']}",
        f"{'creation:':10}{info['creationTimestamp']}",
        "",
        sep="\n",
    )
    tag = re.match(r".*-(?P<tag>\S+)", info["name"])
    if tag is None:
        raise Exception(f"image name {info['name']} does not match regex")
    if build.copy:
        source = build.asdict()
        tag = tag["tag"]
        image_family = "{prefix}-{slurm_gcp_version}-{os_tag}".format(**source)
        image_name = f"{image_family}-{tag}"

        print(
            "Publish image",
            f"{'project:':10}{build.publish_project}",
            f"{'family:':10}{image_family}",
            f"{'name:':10}{image_name}",
            "",
            sep="\n",
        )

        create_cmd = [
            "gcloud",
            "compute",
            "images",
            "create",
            image_name,
            "--project",
            build.publish_project,
            "--family",
            image_family,
            "--source-image-project",
            project,
            "--source-image",
            info["name"],
            "--licenses",
            build.license,
            "--description",
            f"Public Slurm image based on the {build.os_tag} image" "--force",
        ]
        print(" ".join(create_cmd))
        if not confirm():
            exit(1)

        sp.run(create_cmd, check=True)
    else:
        build.publish_project = build.source_project
        image_name = info["name"]
        print("Add permissions to source image?")
        if not confirm():
            exit(1)

    user_cmd = (
        f"gcloud compute images add-iam-policy-binding {image_name} "
        "--member='allAuthenticatedUsers' "
        "--role='roles/compute.imageUser' "
        f"--project={build.publish_project}"
    )
    print(user_cmd)
    sp.run(shlex.split(user_cmd), check=True)

    viewer_cmd = (
        f"gcloud compute images add-iam-policy-binding {image_name} "
        "--member='allAuthenticatedUsers' "
        "--role='roles/compute.viewer' "
        f"--project={build.publish_project}"
    )
    print(viewer_cmd)
    sp.run(shlex.split(viewer_cmd), check=True)


parser = argparse.ArgumentParser(__doc__)

parser.add_argument(
    "--config",
    "-c",
    default="publish_builds.yaml",
    type=Path,
    help="Path location of config yaml file",
)
parser.add_argument(
    "--select",
    "-s",
    action="store",
    type=int,
    help="Index of build in config to publish",
)
parser.add_argument("--source-project", help="project of source image")
parser.add_argument(
    "--publish-project",
    "-p",
    default="schedmd-slurm-public",
    help="project to publish image to",
)
parser.add_argument("--source-image-family", help="name of source image family")
parser.add_argument("--source-image", help="name of source image")
parser.add_argument(
    "--version", dest="slurm_gcp_version", help="slurm-gcp version to publish to"
)
parser.add_argument("--os", dest="os_tag", help="os family of image")
parser.add_argument("--prefix", default="slurm-gcp", help="published image prefix")
parser.add_argument(
    "--license", default=default_license, help="license to add to published image"
)
parser.add_argument(
    "--no-copy",
    dest="copy",
    action="store_false",
    help="Do not copy, just add public permissions",
)


if __name__ == "__main__":
    args = parser.parse_args(namespace=ArgsNamespace())
    # print(args)
    sys.exit(main(args))
