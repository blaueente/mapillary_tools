import queue
from typing import List, Optional, Iterable
import copy
import json
import os
import socket
import sys

from queue import Queue
import threading
import time
import zipfile

import requests

from . import processing
from . import api_v3
from .exif_read import ExifRead
from . import upload_api_v3
from . import upload_api_v4
from . import ipc
from .login import authenticate_user
from .utils import force_decode
from . import MAPILLARY_API_VERSION

NUMBER_THREADS = int(os.getenv("NUMBER_THREADS", "5"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "50"))
DRY_RUN = bool(os.getenv("DRY_RUN", False))


class UploadThread(threading.Thread):
    q: Queue
    total_task: int

    def __init__(self, queue):
        super().__init__()
        self.q = queue
        self.total_task = self.q.qsize()

    def run(self):
        while not self.q.empty():
            # fetch file from the queue and upload
            try:
                filepath, max_attempts, session = self.q.get(timeout=5)
            except:
                # If it can't get a task after 5 seconds, continue and check if
                # task list is empty
                continue
            progress(
                self.total_task - self.q.qsize(),
                self.total_task,
                f"... {self.q.qsize()} images left.",
            )
            upload_file(filepath, max_attempts, session)
            self.q.task_done()


def flag_finalization(finalize_file_list):
    for file in finalize_file_list:
        finalize_flag = os.path.join(log_rootpath(file), "upload_finalized")
        open(finalize_flag, "a").close()


def get_upload_file_list(import_path: str, skip_subfolders: bool = False) -> List[str]:
    upload_file_list: List[str] = []
    if skip_subfolders:
        upload_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
            and preform_upload(import_path, file)
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            upload_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
                and preform_upload(root, file)
            )
    return sorted(upload_file_list)


# get a list of video files in a video_file
# TODO: Create list of supported files instead of adding only these 3
def get_video_file_list(video_file, skip_subfolders=False):
    video_file_list = []
    supported_files = ("mp4", "avi", "tavi", "mov", "mkv")
    if skip_subfolders:
        video_file_list.extend(
            os.path.join(os.path.abspath(video_file), file)
            for file in os.listdir(video_file)
            if (file.lower().endswith(supported_files))
        )
    else:
        for root, _, files in os.walk(video_file):
            video_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if (file.lower().endswith(supported_files))
            )
    return sorted(video_file_list)


def get_total_file_list(import_path: str, skip_subfolders: bool = False) -> List[str]:
    total_file_list: List[str] = []
    if skip_subfolders:
        total_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            total_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
            )
    return sorted(total_file_list)


def get_failed_upload_file_list(
    import_path: str, skip_subfolders: bool = False
) -> List[str]:
    failed_upload_file_list: List[str] = []
    if skip_subfolders:
        failed_upload_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
            and failed_upload(import_path, file)
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            failed_upload_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
                and failed_upload(root, file)
            )

    return sorted(failed_upload_file_list)


def get_success_upload_file_list(
    import_path: str, skip_subfolders: bool = False
) -> List[str]:
    success_upload_file_list: List[str] = []
    if skip_subfolders:
        success_upload_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
            and success_upload(import_path, file)
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            success_upload_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
                and success_upload(root, file)
            )

    return sorted(success_upload_file_list)


def success_upload(root: str, file: str) -> bool:
    file_path = os.path.join(root, file)
    log_root = log_rootpath(file_path)
    upload_success = os.path.join(log_root, "upload_success")
    upload_finalization = os.path.join(log_root, "upload_finalized")
    manual_upload = os.path.join(log_root, "manual_upload")
    success = (
        os.path.isfile(upload_success) and not os.path.isfile(manual_upload)
    ) or (
        os.path.isfile(upload_success)
        and os.path.isfile(manual_upload)
        and os.path.isfile(upload_finalization)
    )
    return success


def get_success_only_manual_upload_file_list(import_path, skip_subfolders=False):
    success_only_manual_upload_file_list = []
    if skip_subfolders:
        success_only_manual_upload_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
            and success_only_manual_upload(import_path, file)
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            success_only_manual_upload_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
                and success_only_manual_upload(root, file)
            )

    return sorted(success_only_manual_upload_file_list)


def success_only_manual_upload(root, file):
    file_path = os.path.join(root, file)
    log_root = log_rootpath(file_path)
    upload_success = os.path.join(log_root, "upload_success")
    manual_upload = os.path.join(log_root, "manual_upload")
    success = os.path.isfile(upload_success) and os.path.isfile(manual_upload)
    return success


def preform_upload(root: str, file: str) -> bool:
    file_path = os.path.join(root, file)
    log_root = log_rootpath(file_path)
    process_success = os.path.join(log_root, "mapillary_image_description_success")
    duplicate = os.path.join(log_root, "duplicate")
    upload_succes = os.path.join(log_root, "upload_success")
    upload = (
        not os.path.isfile(upload_succes)
        and os.path.isfile(process_success)
        and not os.path.isfile(duplicate)
    )
    return upload


def failed_upload(root: str, file: str) -> bool:
    file_path = os.path.join(root, file)
    log_root = log_rootpath(file_path)
    process_failed = os.path.join(log_root, "mapillary_image_description_failed")
    duplicate = os.path.join(log_root, "duplicate")
    upload_failed = os.path.join(log_root, "upload_failed")
    failed = (
        os.path.isfile(upload_failed)
        and not os.path.isfile(process_failed)
        and not os.path.isfile(duplicate)
    )
    return failed


def get_finalize_file_list(
    import_path: str, skip_subfolders: bool = False
) -> List[str]:
    finalize_file_list: List[str] = []
    if skip_subfolders:
        finalize_file_list.extend(
            os.path.join(os.path.abspath(import_path), file)
            for file in os.listdir(import_path)
            if file.lower().endswith(
                ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
            )
            and preform_finalize(import_path, file)
        )
    else:
        for root, _, files in os.walk(import_path):
            if os.path.join(".mapillary", "logs") in root:
                continue
            finalize_file_list.extend(
                os.path.join(os.path.abspath(root), file)
                for file in files
                if file.lower().endswith(
                    ("jpg", "jpeg", "tif", "tiff", "pgm", "pnm", "gif")
                )
                and preform_finalize(root, file)
            )

    return sorted(finalize_file_list)


def preform_finalize(root: str, file: str) -> bool:
    file_path = os.path.join(root, file)
    log_root = log_rootpath(file_path)
    upload_succes = os.path.join(log_root, "upload_success")
    upload_finalized = os.path.join(log_root, "upload_finalized")
    manual_upload = os.path.join(log_root, "manual_upload")
    finalize = (
        os.path.isfile(upload_succes)
        and not os.path.isfile(upload_finalized)
        and os.path.isfile(manual_upload)
    )
    return finalize


def print_summary(file_list):
    # inform upload has finished and print out the summary
    print(f"Done uploading {len(file_list)} images.")  # improve upload summary


def get_organization_key(user_key, organization_username, upload_token):
    organization_key = None

    organization_usernames = []
    orgs = (
        api_v3.fetch_user_organizations(user_key, upload_token)
        if MAPILLARY_API_VERSION == "v3"
        else []
    )
    for org in orgs:
        organization_usernames.append(org["name"])
        if org["name"] == organization_username:
            organization_key = org["key"]

    if not organization_key:
        print(
            f"No valid organization key found for organization user name {organization_username}"
        )
        print("Available organization user names for current user are : ")
        print(organization_usernames)
        sys.exit(1)
    return organization_key


def validate_organization_key(user_key, organization_key, upload_token):
    orgs = (
        api_v3.fetch_user_organizations(user_key, upload_token)
        if MAPILLARY_API_VERSION == "v3"
        else []
    )
    for org in orgs:
        if org["key"] == organization_key:
            return
    raise Exception("Organization key does not exist.")


def validate_organization_privacy(user_key, organization_key, private, upload_token):
    orgs = (
        api_v3.fetch_user_organizations(user_key, upload_token)
        if MAPILLARY_API_VERSION == "v3"
        else []
    )
    for org in orgs:
        if org["key"] == organization_key:
            if (
                private
                and (("private_repository" not in org) or not org["private_repository"])
            ) or (
                not private
                and (("public_repository" not in org) or not org["public_repository"])
            ):
                print("Organization privacy does not match provided privacy settings.")
                privacy = (
                    "private"
                    if "private_repository" in org and org["private_repository"]
                    else "public"
                )
                privacy_provided = "private" if private else "public"
                print(
                    f"Organization {org['name']} with key {org['key']} is {privacy} while your import privacy settings state {privacy_provided}"
                )
                sys.exit(1)


def progress(count, total, suffix=""):
    """
    Display progress bar
    sources: https://gist.github.com/vladignatyev/06860ec2040cb497f0f3
    """
    bar_len = 60
    filled_len = int(round(bar_len * count / float(total)))
    percents = round(100.0 * count / float(total), 1)
    bar = "=" * filled_len + "-" * (bar_len - filled_len)
    sys.stdout.write(f"[{bar}] {percents}% {suffix}\r")
    sys.stdout.flush()


def upload_file(filepath, max_attempts, session):
    """
    Upload file at filepath.
    """
    if max_attempts is None:
        max_attempts = MAX_ATTEMPTS

    exif_read = ExifRead(filepath)

    filename = os.path.basename(filepath)

    try:
        exif_name = exif_read.exif_name()
        _, file_extension = os.path.splitext(filename)
        s3_filename = exif_name + file_extension
    except:
        s3_filename = filename

    try:
        lat, lon, ca, captured_at = exif_read.exif_properties()
        new_session = copy.deepcopy(session)
        session_fields = new_session["fields"]
        session_fields["X-Amz-Meta-Latitude"] = lat
        session_fields["X-Amz-Meta-Longitude"] = lon
        session_fields["X-Amz-Meta-Compass-Angle"] = ca
        session_fields["X-Amz-Meta-Captured-At"] = captured_at
        session = new_session
    except:
        pass

    filepath_keep_original = processing.processed_images_rootpath(filepath)
    filepath_in = filepath
    if os.path.isfile(filepath_keep_original):
        filepath = filepath_keep_original

    if DRY_RUN:
        print("DRY_RUN, Skipping actual image upload. Use this for debug only.")
        return

    displayed_upload_error = False
    for attempt in range(max_attempts):
        try:
            response = upload_api_v3.upload_file(session, filepath, s3_filename)
            if 200 <= response.status_code < 300:
                create_upload_log(filepath_in, "upload_success")
                if displayed_upload_error:
                    print(f"Successful upload of {filename} on attempt {attempt + 1}")
            else:
                create_upload_log(filepath_in, "upload_failed")
                print(response.text)
            break  # attempts
        except requests.RequestException as e:
            print(
                f"HTTP error: {e} on {filename}, will attempt upload again for {max_attempts - attempt - 1} more times"
            )
            displayed_upload_error = True
            time.sleep(5)
        except socket.timeout:
            # Specific timeout handling for Python 2.7
            print(
                f"Timeout error: {filename} (retrying), will attempt upload again for {max_attempts - attempt - 1} more times"
            )
        except OSError as e:
            print(
                f"OS error: {e} on {filename}, will attempt upload again for {max_attempts - attempt - 1} more times"
            )
            time.sleep(5)


def upload_file_list_direct(file_list, number_threads=None, max_attempts=None):
    # set some uploader params first
    if number_threads is None:
        number_threads = NUMBER_THREADS

    if max_attempts is None:
        max_attempts = MAX_ATTEMPTS

    # create upload queue with all files per sequence
    q = Queue()
    for filepath in file_list:
        # FIXME: the third param should be session
        q.put((filepath, max_attempts, None))
    # create uploader threads
    uploaders = [UploadThread(q) for _ in range(number_threads)]

    # start uploaders as daemon threads that can be stopped (ctrl-c)
    try:
        print(f"Uploading with {number_threads} threads")
        for uploader in uploaders:
            uploader.daemon = True
            uploader.start()

        while q.unfinished_tasks:
            time.sleep(1)
        q.join()
    except (KeyboardInterrupt, SystemExit):
        print("\nBREAK: Stopping upload.")
        sys.exit(1)


def find_root_dir(file_list: Iterable[str]) -> Optional[str]:
    """
    find the common root path
    """
    dirs = set()
    for path in file_list:
        dirs.add(os.path.dirname(path))
    if len(dirs) == 0:
        return None
    elif len(dirs) == 1:
        return list(dirs)[0]
    else:
        return find_root_dir(dirs)


def upload_sequence_v4(file_list: list, sequence_uuid: str, file_params: dict):
    first_image = list(file_params.values())[0]
    user_name = first_image["user_name"]

    def _read_captured_at(path):
        return file_params.get(path, {}).get("MAPCaptureTime", "")

    # Sorting images by captured_at
    file_list.sort(key=_read_captured_at)

    root_dir = find_root_dir(file_list)
    if root_dir is None:
        raise RuntimeError(f"Unable to find the root dir of sequence {sequence_uuid}")
    sequence_zip_path = os.path.join(root_dir, f"sequence_{sequence_uuid}.zip")

    if not os.path.isfile(sequence_zip_path):
        print(f"Compressing {len(file_list)} image files to {sequence_zip_path}")
        # compress files
        with zipfile.ZipFile(sequence_zip_path, "w", zipfile.ZIP_DEFLATED) as ziph:
            for fullpath in file_list:
                relpath = os.path.relpath(fullpath, root_dir)
                print(
                    f"Writing {fullpath} to {sequence_zip_path}/{relpath} captured at {_read_captured_at(fullpath)}"
                )
                ziph.write(fullpath, relpath)
    else:
        print(f"Found the compressed sequence file {sequence_zip_path}")

    credentials = authenticate_user(user_name)
    if credentials is None:
        raise RuntimeError(f"Unable to find credentials for user {user_name}")

    service = upload_api_v4.UploadService(credentials["user_upload_token"])

    data_size = os.path.getsize(sequence_zip_path)
    print(f"Uploading {sequence_zip_path} ({data_size} bytes) ...")
    with open(sequence_zip_path, "rb") as fp:
        upload_resp = service.upload(sequence_uuid, data_size, fp)
    try:
        upload_resp.raise_for_status()
    except requests.RequestException as ex:
        for path in file_list:
            create_upload_log(path, "upload_failed")
        raise RuntimeError(f"Upload server error: {ex.response.text}")

    upload_resp_json = upload_resp.json()
    try:
        file_handle = upload_resp_json["h"]
    except KeyError:
        raise RuntimeError(
            f"File handle not found in the upload response {upload_resp.text}"
        )

    print(f"Finishing uploading {sequence_zip_path} with file handle {file_handle}")
    finish_resp = service.finish(file_handle)
    try:
        finish_resp.raise_for_status()
    except requests.RequestException as ex:
        for path in file_list:
            create_upload_log(path, "upload_failed")
        raise RuntimeError(f"Upload server error: {ex.response.text}")

    finish_data = finish_resp.json()
    cluster_id = finish_data.get("cluster_id")
    if cluster_id is None:
        for path in file_list:
            create_upload_log(path, "upload_failed")
        raise RuntimeError(
            f"Upload server error: failed to create the cluster {finish_resp.text}"
        )
    else:
        print(f"Cluster {cluster_id} created")

    for path in file_list:
        create_upload_log(path, "upload_success")

    flag_finalization(file_list)

    print(f"Deleting {sequence_zip_path}...")
    os.remove(sequence_zip_path)


def upload_file_list_manual(
    file_list: list,
    sequence_uuid: str,
    file_params,
    sequence_idx: int,
    number_threads: int = None,
    max_attempts: int = None,
):
    # set some uploader params first
    if number_threads is None:
        number_threads = NUMBER_THREADS
    if max_attempts is None:
        max_attempts = MAX_ATTEMPTS

    first_image = list(file_params.values())[0]

    user_name = first_image["user_name"]
    organization_key = first_image.get("organization_key")
    private = first_image.get("private", False)

    credentials = authenticate_user(user_name)
    if credentials is None:
        raise RuntimeError(f"Unable to find credentials for user {user_name}")

    upload_options = {
        "token": credentials["user_upload_token"],
    }

    session_path = os.path.join(
        log_folder(file_list[0]), f"session_{sequence_uuid}.json"
    )

    # read session from file
    if os.path.isfile(session_path):
        print(f"Read session from {session_path}")
        with open(session_path, "r") as fp:
            session = json.load(fp)
        # if session not found, delete the file
        resp = upload_api_v3.get_upload_session(session, upload_options)
        if resp.status_code == 404:
            print(f"Invalid session so deleting {session_path}")
            os.remove(session_path)
            session = None
    else:
        session = None

    if not session:
        upload_metadata = {}
        if organization_key:
            upload_metadata["organization_key"] = organization_key
            upload_metadata["private"] = private
        resp = upload_api_v3.create_upload_session(
            "images/sequence", upload_metadata, upload_options
        )
        resp.raise_for_status()
        session = resp.json()
        with open(session_path, "w") as f:
            json.dump(session, f)

    print(f"\nUsing upload session {session['key']}")

    # create upload queue with all files per sequence
    q: queue.Queue = Queue()
    for filepath in file_list:
        q.put((filepath, max_attempts, session))

    # create uploader threads
    uploaders = [UploadThread(q) for _ in range(number_threads)]

    # start uploaders as daemon threads that can be stopped (ctrl-c)
    try:
        print(f"Uploading {sequence_idx + 1}. sequence with {number_threads} threads")
        for uploader in uploaders:
            uploader.daemon = True
            uploader.start()

        while q.unfinished_tasks:
            time.sleep(1)
        q.join()
    except (KeyboardInterrupt, SystemExit):
        print("\nBREAK: Stopping upload.")
        sys.exit(1)

    resp = upload_api_v3.close_upload_session(session, None, upload_options)
    resp.raise_for_status()

    print(f"\nClosed upload session {session['key']} so deleting {session_path}")
    os.remove(session_path)

    flag_finalization(file_list)


def log_rootpath(filepath: str) -> str:
    return os.path.join(
        os.path.dirname(filepath),
        ".mapillary",
        "logs",
        os.path.splitext(os.path.basename(filepath))[0],
    )


def log_folder(filepath: str) -> str:
    return os.path.join(os.path.dirname(filepath), ".mapillary", "logs")


def create_upload_log(filepath: str, status: str) -> None:
    assert status in ["upload_success", "upload_failed"], f"invalid status {status}"
    upload_log_root = log_rootpath(filepath)
    upload_log_filepath = os.path.join(upload_log_root, status)
    opposite_status = {
        "upload_success": "upload_failed",
        "upload_failed": "upload_success",
    }
    upload_opposite_log_filepath = os.path.join(
        upload_log_root, opposite_status[status]
    )
    suffix = str(time.strftime("%Y_%m_%d_%H_%M_%S", time.gmtime()))
    if not os.path.isdir(upload_log_root):
        os.makedirs(upload_log_root)
        open(upload_log_filepath, "w").close()
        open(f"{upload_log_filepath}_{suffix}", "w").close()
    else:
        if not os.path.isfile(upload_log_filepath):
            open(upload_log_filepath, "w").close()
            open(f"{upload_log_filepath}_{suffix}", "w").close()
        if os.path.isfile(upload_opposite_log_filepath):
            os.remove(upload_opposite_log_filepath)

    decoded_filepath = force_decode(filepath)

    ipc.send(
        "upload",
        {
            "image": decoded_filepath,
            "status": "success" if status == "upload_success" else "failed",
        },
    )
