#!/usr/bin/env python3
"""
This script scans an S3 bucket, files, or directories for leaked strings in various file types, including tar.gz, tgz, gz, zip, deb, rpm, and tar files.

Python dependencies:
- boto3
- zstandard (optional, for .zst files)

External dependencies:
- dpkg-deb (for .deb files)
- rpm2cpio and cpio (for .rpm files)
"""

import tarfile
import zipfile
import gzip
import re
import io
import argparse
import subprocess
import os
from tempfile import NamedTemporaryFile

import boto3

try:
    import zstandard as zstd
except ImportError:
    print("WARNING: zstandard package not found. Install with `pip install zstandard`")

# Initialize S3 client
s3 = boto3.client("s3")

# Define the pattern to search for
leaked_string_pattern = re.compile(r"[A-Z_]*(SECRET|PASSWORD|ACCESS_KEY|TOKEN)[A-Z_]*")

# Additional strings to check for
sensitive_strings = []


class LeakScanner:
    def __init__(self, bucket_name=None, prefix=None, env_secrets_only=False):
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.matches = []
        self.continuation_token = None
        self.env_secrets_only = env_secrets_only

        # Mapping of file extensions to their respective scanning functions
        self.extension_to_scan_function = {
            ".tar": self.scan_tar,
            ".tar.gz": self.scan_tar_gz,
            ".tgz": self.scan_tar_gz,
            ".gz": self.scan_gz,
            ".tar.zst": self.scan_tar_zst,
            ".zst": self.scan_zst,
            ".zip": self.scan_zip,
            ".deb": self.scan_deb,
            ".rpm": self.scan_rpm,
        }

    def scan_env_vars(self):
        """Scan environment variables for sensitive strings."""
        for var_name, var_value in os.environ.items():
            if leaked_string_pattern.match(var_name):
                sensitive_strings.append(var_value)

    def scan_file(self, file_content, file_name):
        """Scan the content of a file for leaked strings."""
        matches = []
        for line_number, line in enumerate(file_content.splitlines(), start=1):
            if not self.env_secrets_only:
                for match in leaked_string_pattern.finditer(line):
                    matches.append((file_name, line_number, match.group(0)))
            for secret_string in sensitive_strings:
                if secret_string in line:
                    matches.append((file_name, line_number, f"{secret_string[:4]}..."))
        return matches

    def scan_local_file(self, file_path):
        """Scan a single local file for leaked strings."""
        matches = []
        try:
            # Check the file extension and use the appropriate scan function
            for extension, scan_function in self.extension_to_scan_function.items():
                if file_path.endswith(extension):
                    with open(file_path, "rb") as f:
                        file_content = f.read()
                    matches.extend(scan_function(file_content, file_path))
                    return matches

            # If no special handling is required, scan as a plain text file
            with open(file_path, "rb") as f:
                file_content = f.read()
            matches.extend(
                self.scan_file(file_content.decode("utf-8", errors="ignore"), file_path)
            )
        except Exception as e:
            print(f"Error reading file {file_path}: {e}")
        return matches

    def scan_local_directory(self, directory_path):
        """Recursively scan a directory for leaked strings."""
        matches = []
        for root, _, files in os.walk(directory_path):
            for file in files:
                file_path = os.path.join(root, file)
                matches.extend(self.scan_local_file(file_path))
        return matches

    def scan_paths(self, paths):
        """Scan a list of files and directories for leaked strings."""
        matches = []
        for path in paths:
            if os.path.isfile(path):
                matches.extend(self.scan_local_file(path))
            elif os.path.isdir(path):
                matches.extend(self.scan_local_directory(path))
            else:
                print(f"Invalid path: {path}")
        return matches

    def scan_tar(self, file_content, package_name):
        """Scan the contents of a tar archive for leaked strings."""
        matches = []
        with tarfile.open(fileobj=io.BytesIO(file_content), mode="r:") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        file_content = f.read().decode("utf-8", errors="ignore")
                        matches.extend(
                            self.scan_file(
                                file_content, f"{package_name}/{member.name}"
                            )
                        )
        return matches

    def scan_tar_gz(self, file_content, package_name):
        """Scan the contents of a tar.gz or tgz archive for leaked strings."""
        matches = []
        with tarfile.open(fileobj=io.BytesIO(file_content), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        file_content = f.read().decode("utf-8", errors="ignore")
                        matches.extend(
                            self.scan_file(
                                file_content, f"{package_name}/{member.name}"
                            )
                        )
        return matches

    def scan_gz(self, file_content, file_name):
        """Scan the contents of a gzipped file for leaked strings."""
        matches = []
        with gzip.GzipFile(fileobj=io.BytesIO(file_content)) as gz:
            file_content = gz.read().decode("utf-8", errors="ignore")
            matches.extend(self.scan_file(file_content, file_name))
        return matches

    def scan_tar_zst(self, file_content, package_name):
        """Scan the contents of a tar.zst archive for leaked strings."""
        matches = []
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(file_content)) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        f = tar.extractfile(member)
                        if f:
                            file_content = f.read().decode("utf-8", errors="ignore")
                            matches.extend(
                                self.scan_file(
                                    file_content, f"{package_name}/{member.name}"
                                )
                            )
        return matches

    def scan_zst(self, file_content, file_name):
        """Scan the contents of a zst file for leaked strings."""
        matches = []
        dctx = zstd.ZstdDecompressor()
        file_content = dctx.decompress(file_content).decode("utf-8", errors="ignore")
        matches.extend(self.scan_file(file_content, file_name))
        return matches

    def scan_zip(self, file_content, package_name):
        """Scan the contents of a zip archive for leaked strings."""
        matches = []
        with zipfile.ZipFile(io.BytesIO(file_content)) as zip:
            for member in zip.infolist():
                with zip.open(member) as f:
                    file_content = f.read().decode("utf-8", errors="ignore")
                    matches.extend(
                        self.scan_file(
                            file_content, f"{package_name}/{member.filename}"
                        )
                    )
        return matches

    def scan_deb(self, file_content, package_name):
        """Scan the contents of a .deb package for leaked strings."""
        matches = []
        with NamedTemporaryFile(delete=False, suffix=".deb") as tmp_file:
            tmp_file.write(file_content)
            tmp_file_path = tmp_file.name
        subprocess.run(["dpkg-deb", "-x", tmp_file_path, "/tmp/package"])
        for root, _, files in os.walk("/tmp/package"):
            for file in files:
                file_path = os.path.join(root, file)
                with open(file_path, "r", errors="ignore") as f:
                    file_content = f.read()
                    matches.extend(
                        self.scan_file(file_content, f"{package_name}/{file_path}")
                    )
        os.remove(tmp_file_path)
        return matches

    def scan_rpm(self, file_content, package_name):
        """Scan the contents of an .rpm package for leaked strings."""
        matches = []
        with NamedTemporaryFile(delete=False, suffix=".rpm") as tmp_file:
            tmp_file.write(file_content)
            tmp_file_path = tmp_file.name
        subprocess.run(["rpm2cpio", tmp_file_path], stdout=subprocess.PIPE)
        subprocess.run(["cpio", "-idmv"], stdin=subprocess.PIPE, cwd="/tmp/package")
        for root, _, files in os.walk("/tmp/package"):
            for file in files:
                file_path = os.path.join(root, file)
                with open(file_path, "r", errors="ignore") as f:
                    file_content = f.read()
                    matches.extend(
                        self.scan_file(file_content, f"{package_name}/{file_path}")
                    )
        os.remove(tmp_file_path)
        return matches

    def scan_s3_bucket(self):
        """Scan all files in an S3 bucket with the specified prefix for leaked strings."""
        while True:
            if self.continuation_token:
                response = s3.list_objects_v2(
                    Bucket=self.bucket_name,
                    Prefix=self.prefix,
                    ContinuationToken=self.continuation_token,
                )
            else:
                response = s3.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=self.prefix
                )

            for obj in response.get("Contents", []):
                key = obj["Key"]
                print(f"Scanning {key}...")
                file_obj = s3.get_object(Bucket=self.bucket_name, Key=key)
                file_content = file_obj["Body"].read()

                for extension, scan_function in self.extension_to_scan_function.items():
                    if key.endswith(extension):
                        self.matches.extend(scan_function(file_content, key))
                        break
                else:
                    file_content = file_content.decode("utf-8", errors="ignore")
                    self.matches.extend(self.scan_file(file_content, key))

            if response.get("IsTruncated"):
                self.continuation_token = response.get("NextContinuationToken")
            else:
                break

        return self.matches


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan for leaked strings in S3 buckets or local files and directories."
    )
    subparsers = parser.add_subparsers(
        dest="mode", required=True, help="Mode of operation"
    )

    # Subcommand for S3 scanning
    s3_parser = subparsers.add_parser(
        "s3", help="Scan an S3 bucket for leaked strings."
    )
    s3_parser.add_argument("bucket_name", help="The name of the S3 bucket to scan")
    s3_parser.add_argument("prefix", help="The prefix to restrict the scan to")
    s3_parser.add_argument(
        "--env-secrets-only",
        action="store_true",
        help="Only scan for leaked environment secrets",
    )

    # Subcommand for file and directory scanning
    files_parser = subparsers.add_parser(
        "files", help="Scan local files and directories for leaked strings."
    )
    files_parser.add_argument(
        "paths",
        nargs="+",
        help="List of files and directories to scan for leaked strings",
    )
    files_parser.add_argument(
        "--env-secrets-only",
        action="store_true",
        help="Only scan for leaked environment secrets",
    )

    args = parser.parse_args()

    if args.mode == "s3":
        # S3 scanning mode
        scanner = LeakScanner(
            bucket_name=args.bucket_name,
            prefix=args.prefix,
            env_secrets_only=args.env_secrets_only,
        )
        scanner.scan_env_vars()
        matches = scanner.scan_s3_bucket()

    elif args.mode == "files":
        # File and directory scanning mode
        scanner = LeakScanner(env_secrets_only=args.env_secrets_only)
        scanner.scan_env_vars()
        matches = scanner.scan_paths(args.paths)

    # Output results
    if matches:
        print("Leaks found:")
        for file_name, line_number, match in matches:
            print(f"{file_name}:{line_number}: {match}")
        exit(1)
    else:
        print("No leaked strings found.")
        exit(0)
