"""
Bhavcopy file status codes used in bhavcopy_files.status (INTEGER column).
"""
from enum import IntEnum


class FileStatus(IntEnum):
    DOWNLOADED      = 1
    DOWNLOAD_FAILED = 2
    SYNCED          = 3
    SYNC_FAILED     = 4
