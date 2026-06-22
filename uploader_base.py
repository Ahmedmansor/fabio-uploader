"""
uploader_base.py — Abstract base class for all uploaders.

Defines the interface that every platform uploader (YouTube, Instagram,
Facebook, TikTok, etc.) must implement. This enforces a consistent contract
so that main.py can call any uploader without knowing its internals.

SCALABILITY:
  To add Meta Business Suite (Instagram Reels) in Phase 2:
    1. Create instagram_uploader.py
    2. Define class InstagramUploader(BaseUploader)
    3. Implement upload() and verify_channel()
    4. Plug it into main.py with zero changes to this file or youtube_uploader.py
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class BaseUploader(ABC):
    """
    Abstract interface for a platform-specific video uploader.

    Each subclass is responsible for:
      - Connecting to the correct authenticated browser session (via AdsPower).
      - Verifying it is on the correct channel/account.
      - Uploading the video with the given metadata.
      - Scheduling the upload for the provided datetime.
      - Cleaning up the browser session after the upload.
    """

    # Human-readable name of the platform, e.g. "YouTube", "Instagram"
    PLATFORM_NAME: str = "Base"

    def __init__(self, lang: str):
        """
        Args:
            lang: Language code for this upload session (e.g. "IT", "EN").
                  Determines which AdsPower profile is used.
        """
        self.lang = lang
        self.logger = logging.getLogger(
            f"{__name__}.{self.__class__.__name__}[{lang}]"
        )

    @abstractmethod
    def upload(
        self,
        video_path: Path,
        metadata: dict,
        scheduled_time: datetime,
        dry_run: bool = False,
    ) -> bool:
        """
        Execute the full upload pipeline for one video.

        Args:
            video_path:     Absolute Path to the .mp4 file to upload.
            metadata:       Dict with keys "title", "description", "tags".
            scheduled_time: Timezone-aware datetime for scheduling the publish.
            dry_run:        If True, skip all browser actions and return True.

        Returns:
            True  — upload was successful and confirmed.
            False — upload failed (caller handles retry/error marking).
        """
        ...

    @abstractmethod
    def verify_channel(self, page) -> bool:
        """
        Verify that the currently active channel/account is the correct one.
        If on the wrong channel, switch to the correct one.

        Args:
            page: The Playwright Page object of the active browser session.

        Returns:
            True  — correct channel confirmed (or switched successfully).
            False — could not verify or switch.
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(platform={self.PLATFORM_NAME}, lang={self.lang})"
