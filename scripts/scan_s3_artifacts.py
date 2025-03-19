#!/usr/bin/env python3
"""
This script scans an S3 bucket for leaked strings in various file types, including tar.gz, tgz, zip, deb, and rpm files.

External dependencies:
- dpkg-deb (for .deb files)
- rpm2cpio and cpio (for .rpm files)
"""

import boto3
import tarfile
import zipfile
import re
import io
import argparse
import subprocess
import os

# Initialize S3 client
s3 = boto3.client('s3')

# Define the pattern to search for
leaked_string_pattern = re.compile(r'[A-Z_]*(SECRET|PASSWORD)[A-Z_]*')

def scan_file(file_content, file_name):
    """Scan the content of a file for leaked strings."""
    matches = []
    for line_number, line in enumerate(file_content.splitlines(), start=1):
        for match in leaked_string_pattern.finditer(line):
            matches.append((file_name, line_number, match.group(0)))
    return matches

def scan_tar_gz(file_content, package_name):
    """Scan the contents of a tar.gz or tgz archive for leaked strings."""
    matches = []
    with tarfile.open(fileobj=io.BytesIO(file_content), mode='r:gz') as tar:
        for member in tar.getmembers():
            if member.isfile():
                f = tar.extractfile(member)
                if f:
                    file_content = f.read().decode('utf-8', errors='ignore')
                    matches.extend(scan_file(file_content, f"{package_name}/{member.name}"))
    return matches

def scan_zip(file_content, package_name):
    """Scan the contents of a zip archive for leaked strings."""
    matches = []
    with zipfile.ZipFile(io.BytesIO(file_content)) as zip:
        for member in zip.infolist():
            with zip.open(member) as f:
                file_content = f.read().decode('utf-8', errors='ignore')
                matches.extend(scan_file(file_content, f"{package_name}/{member.filename}"))
    return matches

def scan_deb(file_content, package_name):
    """Scan the contents of a .deb package for leaked strings."""
    matches = []
    with open('/tmp/package.deb', 'wb') as f:
        f.write(file_content)
    subprocess.run(['dpkg-deb', '-x', '/tmp/package.deb', '/tmp/package'])
    for root, _, files in os.walk('/tmp/package'):
        for file in files:
            file_path = os.path.join(root, file)
            with open(file_path, 'r', errors='ignore') as f:
                file_content = f.read()
                matches.extend(scan_file(file_content, f"{package_name}/{file_path}"))
    return matches

def scan_rpm(file_content, package_name):
    """Scan the contents of an .rpm package for leaked strings."""
    matches = []
    with open('/tmp/package.rpm', 'wb') as f:
        f.write(file_content)
    subprocess.run(['rpm2cpio', '/tmp/package.rpm'], stdout=subprocess.PIPE)
    subprocess.run(['cpio', '-idmv'], stdin=subprocess.PIPE, cwd='/tmp/package')
    for root, _, files in os.walk('/tmp/package'):
        for file in files:
            file_path = os.path.join(root, file)
            with open(file_path, 'r', errors='ignore') as f:
                file_content = f.read()
                matches.extend(scan_file(file_content, f"{package_name}/{file_path}"))
    return matches

def scan_s3_bucket(bucket_name, prefix):
    """Scan all files in an S3 bucket with the specified prefix for leaked strings."""
    matches = []
    continuation_token = None

    while True:
        if continuation_token:
            response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix, ContinuationToken=continuation_token)
        else:
            response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)

        for obj in response.get('Contents', []):
            key = obj['Key']
            print(f'Scanning {key}...')
            file_obj = s3.get_object(Bucket=bucket_name, Key=key)
            file_content = file_obj['Body'].read()

            if key.endswith(('.tar.gz', '.tgz')):
                matches.extend(scan_tar_gz(file_content, key))
            elif key.endswith('.zip'):
                matches.extend(scan_zip(file_content, key))
            elif key.endswith('.deb'):
                matches.extend(scan_deb(file_content, key))
            elif key.endswith('.rpm'):
                matches.extend(scan_rpm(file_content, key))
            else:
                file_content = file_content.decode('utf-8', errors='ignore')
                matches.extend(scan_file(file_content, key))

        if response.get('IsTruncated'):
            continuation_token = response.get('NextContinuationToken')
        else:
            break

    return matches

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Scan an S3 bucket for leaked strings.')
    parser.add_argument('bucket_name', help='The name of the S3 bucket to scan')
    parser.add_argument('prefix', help='The prefix to restrict the scan to')
    
    args = parser.parse_args()

    matches = scan_s3_bucket(args.bucket_name, args.prefix)
    if matches:
        print('Leaks found:')
        for file_name, line_number, match in matches:
            print(f"{file_name}:{line_number}: {match}")
        exit(1)
    else:
        print('No leaked strings found.')
        exit(0)