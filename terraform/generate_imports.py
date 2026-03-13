#!/usr/bin/env python3
"""
Parses a Terraform .tf file and generates terraform import commands
for all resource and module blocks found.

Usage:
    python generate_imports.py main.tf > import_commands.sh
    python generate_imports.py main.tf --blocks > imports.tf
"""

import re
import argparse


def parse_tf_file(filepath):
    with open(filepath, "r") as f:
        content = f.read()

    blocks = []
    pattern = re.compile(
        r'(resource|module)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{'
    )

    for match in pattern.finditer(content):
        block_type = match.group(1)
        type_or_name = match.group(2)
        resource_name = match.group(3)

        start = match.end()
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            if content[pos] == "{": depth += 1
            elif content[pos] == "}": depth -= 1
            pos += 1
        body = content[start:pos - 1]
        attrs = parse_attributes(body)

        blocks.append({
            "block_type": block_type,
            "resource_type": type_or_name if block_type == "resource" else None,
            "name": resource_name if block_type == "resource" else type_or_name,
            "attrs": attrs,
        })
    return blocks


def parse_attributes(body):
    attrs = {}
    for m in re.finditer(r'^\s*([\w-]+)\s*=\s*"([^"]*)"\s*$', body, re.MULTILINE):
        attrs[m.group(1)] = m.group(2)
    for m in re.finditer(r'^\s*([\w-]+)\s*=\s*([\w.]+)\s*$', body, re.MULTILINE):
        if m.group(1) not in attrs:
            attrs[m.group(1)] = m.group(2)
    for m in re.finditer(r'^\s*([\w-]+)\s*=\s*"([^"]*$\{[^}]+\}[^"]*)"\s*$', body, re.MULTILINE):
        attrs[m.group(1)] = m.group(2)
    return attrs


def resolve_value(val, project_id):
    if val in ("local.project", "data.google_project.project.project_id"):
        return project_id
    return val


def resolve_sa_email(member, project_id, sa_accounts):
    sa_ref = re.search(r"$\{google_service_account\.([-\w]+)\.email\}", member)
    if sa_ref:
        sa_name = sa_ref.group(1)
        account_id = sa_accounts.get(sa_name, sa_name)
        email = f"{account_id}@{project_id}.iam.gserviceaccount.com"
        return member.replace(sa_ref.group(0), email)
    return member


def generate_imports(blocks, project_id):
    sa_accounts = {}
    for b in blocks:
        if b["block_type"] == "resource" and b["resource_type"] == "google_service_account":
            sa_accounts[b["name"]] = b["attrs"].get("account_id", b["name"])

    results = []
    for block in blocks:
        attrs = block["attrs"]
        name = block["name"]

        if block["block_type"] == "module":
            source = attrs.get("source", "")
            bucket_name = attrs.get("name", "")
            if "gcs_storage_bucket" in source:
                project = resolve_value(attrs.get("project", project_id), project_id)
                results.append((f"module.{name}.google_storage_bucket.bucket", f"{project}/{bucket_name}"))
            continue

        rtype = block["resource_type"]
        tf_addr = f"{rtype}.{name}"

        if rtype == "google_storage_bucket":
            project = resolve_value(attrs.get("project", project_id), project_id)
            results.append((tf_addr, f"{project}/{attrs.get('name', '')}"))

        elif rtype == "google_storage_bucket_object":
            results.append((tf_addr, f"{attrs.get('bucket', '')}/{attrs.get('name', '')}"))

        elif rtype == "google_service_account":
            project = resolve_value(attrs.get("project", project_id), project_id)
            acct = attrs.get("account_id", "")
            email = f"{acct}@{project}.iam.gserviceaccount.com"
            results.append((tf_addr, f"projects/{project}/serviceAccounts/{email}"))

        elif rtype == "google_project_iam_member":
            project = resolve_value(attrs.get("project", project_id), project_id)
            role = attrs.get("role", "")
            member = resolve_sa_email(attrs.get("member", ""), project_id, sa_accounts)
            results.append((tf_addr, f"{project} {role} {member}"))

        elif rtype == "google_storage_bucket_iam_member":
            bucket = attrs.get("bucket", "")
            role = attrs.get("role", "")
            member = resolve_sa_email(attrs.get("member", ""), project_id, sa_accounts)
            results.append((tf_addr, f"{bucket} {role} {member}"))

        elif rtype == "google_service_account_iam_member":
            sa_id = attrs.get("service_account_id", "")
            sa_ref = re.search(r"google_service_account\.([-\w]+)\.name", sa_id)
            if sa_ref:
                sa_key = sa_ref.group(1)
                acct = sa_accounts.get(sa_key, sa_key)
                sa_id = f"projects/{project_id}/serviceAccounts/{acct}@{project_id}.iam.gserviceaccount.com"
            role = attrs.get("role", "")
            member = attrs.get("member", "")
            results.append((tf_addr, f"{sa_id} {role} {member}"))

        elif rtype == "google_redis_instance":
            inst = attrs.get("name", "")
            region = attrs.get("region", "")
            project = resolve_value(attrs.get("project", project_id), project_id)
            results.append((tf_addr, f"projects/{project}/locations/{region}/instances/{inst}"))

        else:
            results.append((tf_addr, f"<MANUAL: look up import ID for {rtype}>"))

    return results


def main():
    parser = argparse.ArgumentParser(description="Generate terraform import commands from a .tf file")
    parser.add_argument("tf_file", help="Path to the .tf file")
    parser.add_argument("--project", default="cig-acctgateway-dev-1", help="GCP project ID")
    parser.add_argument("--blocks", action="store_true", help="Output TF 1.5+ import blocks")
    args = parser.parse_args()

    blocks = parse_tf_file(args.tf_file)
    results = generate_imports(blocks, args.project)

    print(f"# Generated from: {args.tf_file}")
    print(f"# Project: {args.project}")
    print(f"# Total import commands: {len(results)}")
    print()

    for tf_addr, import_id in results:
        if args.blocks:
            print("import {")
            print(f"  to = {tf_addr}")
            print(f'  id = "{import_id}"')
            print("}")
            print()
        else:
            print(f"terraform import '{tf_addr}' '{import_id}'")


if __name__ == "__main__":
    main()
