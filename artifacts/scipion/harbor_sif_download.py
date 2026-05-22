#!/usr/bin/env python3
"""
harbor_sif_download.py — Download SIF images from a Harbor registry with automatic resume support.
 
Usage:
    python3 harbor_sif_download.py rinchen.cnb.csic.es/scipion/apptainer-spa:latest
    python3 harbor_sif_download.py rinchen.cnb.csic.es/scipion/apptainer-spa:latest -o my_image.sif
    python3 harbor_sif_download.py rinchen.cnb.csic.es/scipion/apptainer-spa:latest --user myuser --password mypass
"""
 
import sys
import os
import json
import argparse
import time
import hashlib
import urllib.request
import urllib.error
 
 
# ── Terminal colors ───────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
GREY   = "\033[90m"
 
 
def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}", flush=True)
 
 
def parse_image_ref(ref):
    """
    Parses an image reference of the form:
      host/project/image:tag
      host:port/project/image:tag
    Returns (host, repo, tag).
    """
    # Split off tag
    if ":" in ref.split("/")[-1]:
        ref_no_tag, tag = ref.rsplit(":", 1)
    else:
        ref_no_tag, tag = ref, "latest"
 
    # Split host from the rest
    parts = ref_no_tag.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid reference: '{ref}'. Expected format: host/project/image:tag")
 
    host, repo = parts
    return host, repo, tag
 
 
def get_token(host, repo, user=None, password=None):
    """Obtains an anonymous or authenticated Bearer token from Harbor."""
    token_url = (
        f"https://{host}/service/token"
        f"?service=harbor-registry"
        f"&scope=repository:{repo}:pull"
    )
    req = urllib.request.Request(token_url)
 
    if user and password:
        import base64
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
 
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            token = data.get("token") or data.get("access_token")
            if not token:
                raise RuntimeError("Server response does not contain a token.")
            return token
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Error fetching token ({e.code}): {e.reason}")
 
 
def get_manifest(host, repo, tag, token):
    """Fetches the OCI manifest for the image."""
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.oci.image.manifest.v1+json")
 
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Error fetching manifest ({e.code}): {body}")
 
 
def find_sif_layer(manifest):
    """Extracts the SIF layer from the manifest. Raises an error if the image is not a SIF."""
    layers = manifest.get("layers", [])
    for layer in layers:
        if "sif" in layer.get("mediaType", "").lower():
            return layer
    raise RuntimeError(
        "The image contains no SIF layers. "
        "It may be a standard OCI/Docker image; use 'apptainer pull' instead."
    )
 
 
def format_size(n_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"
 
 
def format_speed(bps):
    return format_size(bps) + "/s"
 
 
def format_eta(seconds):
    if seconds < 0 or seconds > 86400 * 7:
        return "--:--:--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
 
 
def verify_sha256(filepath, expected_digest):
    """Verifies the SHA-256 checksum of the downloaded file."""
    expected = expected_digest.replace("sha256:", "")
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest() == expected
 
 
def download_blob(host, repo, digest, output_path, token, total_size):
    """
    Downloads a blob with resume support (Range requests).
    Returns True if completed, False if the download should be retried
    (e.g. expired token or network error).
    """
    url = f"https://{host}/v2/{repo}/blobs/{digest}"
    existing = os.path.getsize(output_path) if os.path.exists(output_path) else 0
 
    if existing >= total_size:
        log("  File already complete.", GREEN)
        return True
 
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    if existing > 0:
        req.add_header("Range", f"bytes={existing}-")
        log(f"  Resuming from {format_size(existing)} / {format_size(total_size)}", CYAN)
    else:
        log(f"  Total size: {format_size(total_size)}", CYAN)
 
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            # 206 = Partial Content (resume), 200 = full download from start
            status = resp.status
            if status == 200 and existing > 0:
                # Server does not support Range requests — restart from scratch
                log("  Server does not support resume; restarting download...", YELLOW)
                existing = 0
 
            mode = "ab" if (status == 206) else "wb"
            downloaded = existing
            start_time = time.time()
            last_print = start_time
 
            with open(output_path, mode) as f:
                chunk_size = 1024 * 1024  # 1 MB
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
 
                    now = time.time()
                    elapsed = now - start_time
                    if now - last_print >= 1.0:
                        speed = (downloaded - existing) / elapsed if elapsed > 0 else 0
                        pct = downloaded / total_size * 100 if total_size else 0
                        remaining = (total_size - downloaded) / speed if speed > 0 else -1
                        bar_len = 30
                        filled = int(bar_len * pct / 100)
                        bar = "█" * filled + "░" * (bar_len - filled)
                        print(
                            f"\r  [{bar}] {pct:5.1f}%  "
                            f"{format_size(downloaded)}/{format_size(total_size)}  "
                            f"{format_speed(speed)}  ETA {format_eta(remaining)}   ",
                            end="", flush=True
                        )
                        last_print = now
 
            print()  # newline after progress bar
            return True
 
    except urllib.error.HTTPError as e:
        print()
        if e.code == 401:
            log("  Token expired, refreshing...", YELLOW)
            return False  # signal to refresh token and retry
        raise RuntimeError(f"HTTP error {e.code}: {e.reason}")
 
    except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
        print()
        log(f"  Network error: {e}", YELLOW)
        return False  # retryable
 
 
def main():
    parser = argparse.ArgumentParser(
        description="Download SIF images from a Harbor registry with automatic resume support."
    )
    parser.add_argument("image", help="Image reference: host/project/image:tag")
    parser.add_argument("-o", "--output", default=None, help="Output filename (.sif)")
    parser.add_argument("-u", "--user", default=None, help="Username (optional, for private registries)")
    parser.add_argument("-p", "--password", default=None, help="Password (optional)")
    parser.add_argument("--no-verify", action="store_true", help="Skip SHA-256 verification after download")
    parser.add_argument("--retries", type=int, default=20, help="Maximum number of retries (default: 20)")
    parser.add_argument("--wait", type=int, default=10, help="Seconds between retries (default: 10)")
    args = parser.parse_args()
 
    # ── Parse image reference ─────────────────────────────────────────────────
    try:
        host, repo, tag = parse_image_ref(args.image)
    except ValueError as e:
        log(f"ERROR: {e}", RED)
        sys.exit(1)
 
    # ── Output filename ───────────────────────────────────────────────────────
    image_name = repo.split("/")[-1]
    output_path = args.output or f"{image_name}-{tag}.sif"
 
    log(f"\n{BOLD}Harbor SIF Downloader{RESET}")
    log(f"  Registry : {host}", GREY)
    log(f"  Image    : {repo}:{tag}", GREY)
    log(f"  Output   : {output_path}", GREY)
    log("")
 
    # ── Fetch manifest ────────────────────────────────────────────────────────
    log("Fetching manifest...", CYAN)
    try:
        token = get_token(host, repo, args.user, args.password)
        manifest = get_manifest(host, repo, tag, token)
        layer = find_sif_layer(manifest)
    except RuntimeError as e:
        log(f"ERROR: {e}", RED)
        sys.exit(1)
 
    digest = layer["digest"]
    total_size = layer["size"]
    log(f"  Digest   : {digest[:19]}...{digest[-8:]}", GREY)
    log(f"  Size     : {format_size(total_size)}", GREY)
    log("")
 
    # ── Download loop with retries ────────────────────────────────────────────
    log("Starting download...", CYAN)
    for attempt in range(1, args.retries + 1):
        try:
            token = get_token(host, repo, args.user, args.password)
            success = download_blob(host, repo, digest, output_path, token, total_size)
        except RuntimeError as e:
            log(f"  ERROR: {e}", RED)
            success = False
 
        if success:
            break
 
        if attempt < args.retries:
            log(f"  Retry {attempt}/{args.retries} in {args.wait}s...", YELLOW)
            time.sleep(args.wait)
        else:
            log(f"\nERROR: Maximum retries ({args.retries}) reached.", RED)
            sys.exit(1)
 
    # ── SHA-256 verification ──────────────────────────────────────────────────
    if not args.no_verify:
        log("\nVerifying integrity (SHA-256)...", CYAN)
        if verify_sha256(output_path, digest):
            log("  ✓ Checksum verified successfully.", GREEN)
        else:
            log("  ✗ SHA-256 mismatch. The file may be corrupted.", RED)
            sys.exit(1)
 
    log(f"\n{BOLD}✓ Download complete: {output_path}{RESET}", GREEN)
 
 
if __name__ == "__main__":
    main()
 