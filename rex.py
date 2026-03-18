#!/usr/bin/env python3

import base64
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from seleniumbase import SB

# ─────────────────────────── Configuration ───────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("stream_viewer")

GEO_API_URL = "http://ip-api.com/json/"
GEO_API_TIMEOUT = 10  # seconds

# Viewing duration range (seconds) per session
MIN_VIEW_DURATION = 450
MAX_VIEW_DURATION = 800

# Element interaction delays (seconds)
SHORT_DELAY = 2
MEDIUM_DELAY = 10
LONG_DELAY = 12

# Maximum consecutive failures before exiting
MAX_CONSECUTIVE_FAILURES = 0

# Retry settings for geo-location fetch
GEO_MAX_RETRIES = 3
GEO_RETRY_DELAY = 5  # seconds


# ─────────────────────────── Data Models ─────────────────────────────

@dataclass
class GeoData:
    """Holds geolocation information for browser spoofing."""
    latitude: float
    longitude: float
    timezone_id: str
    country_code: str

    def __str__(self) -> str:
        return (
            f"GeoData(lat={self.latitude}, lon={self.longitude}, "
            f"tz={self.timezone_id}, cc={self.country_code})"
        )


@dataclass
class ViewerConfig:
    """Central configuration for the stream viewer."""
    target_url: str
    geo: GeoData
    proxy: Optional[str] = None
    min_view_duration: int = MIN_VIEW_DURATION
    max_view_duration: int = MAX_VIEW_DURATION
    use_second_driver: bool = True
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES


# ─────────────────────────── Geo Lookup ──────────────────────────────

def fetch_geo_data(retries: int = GEO_MAX_RETRIES) -> GeoData:
    """
    Fetch geolocation data from ip-api.com with retry logic.

    Returns:
        GeoData instance populated from the API response.

    Raises:
        RuntimeError: If all retry attempts fail.
    """
    for attempt in range(1, retries + 1):
        try:
            logger.info("Fetching geolocation data (attempt %d/%d)...", attempt, retries)
            response = requests.get(GEO_API_URL, timeout=GEO_API_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise ValueError(f"API returned non-success status: {data.get('message', 'unknown')}")

            geo = GeoData(
                latitude=data["lat"],
                longitude=data["lon"],
                timezone_id=data["timezone"],
                country_code=data["countryCode"].lower(),
            )
            logger.info("Geolocation resolved: %s", geo)
            return geo

        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("Geo fetch attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                time.sleep(GEO_RETRY_DELAY)

    raise RuntimeError(f"Failed to fetch geolocation data after {retries} attempts")


# ─────────────────────────── URL Builder ─────────────────────────────

def decode_target_name(encoded: str) -> str:
    """Decode a base64-encoded channel/username."""
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        logger.debug("Decoded target name: %s", decoded)
        return decoded
    except Exception as exc:
        raise ValueError(f"Failed to decode target name: {exc}") from exc


def build_twitch_url(channel_name: str) -> str:
    """Build a Twitch channel URL."""
    return f"https://www.twitch.tv/{channel_name}"


def build_youtube_url(channel_name: str) -> str:
    """Build a YouTube live URL."""
    return f"https://www.youtube.com/@{channel_name}/live"


# ─────────────────────────── Browser Helpers ─────────────────────────

def dismiss_consent_dialogs(driver, label: str = "primary") -> None:
    """Click any 'Accept' consent buttons if present."""
    try:
        if driver.is_element_present('button:contains("Accept")'):
            driver.cdp.click('button:contains("Accept")', timeout=4)
            logger.info("[%s] Dismissed consent dialog.", label)
    except Exception as exc:
        logger.debug("[%s] Consent dialog dismiss failed (non-critical): %s", label, exc)


def click_start_watching(driver, label: str = "primary") -> None:
    """Click the 'Start Watching' button if it appears (Twitch mature content gate)."""
    try:
        if driver.is_element_present('button:contains("Start Watching")'):
            driver.cdp.click('button:contains("Start Watching")', timeout=4)
            logger.info("[%s] Clicked 'Start Watching'.", label)
            driver.sleep(MEDIUM_DELAY)
    except Exception as exc:
        logger.debug("[%s] 'Start Watching' click failed (non-critical): %s", label, exc)


def is_stream_live(driver) -> bool:
    """Check if the Twitch live-channel stream information element is present."""
    return driver.is_element_present("#live-channel-stream-information")


def activate_and_prepare(driver, url: str, geo: GeoData, label: str = "primary") -> None:
    """Activate CDP mode with geo/timezone spoofing and handle initial dialogs."""
    logger.info("[%s] Navigating to %s", label, url)
    driver.activate_cdp_mode(
        url,
        tzone=geo.timezone_id,
        geoloc=(geo.latitude, geo.longitude),
    )
    driver.sleep(SHORT_DELAY)
    dismiss_consent_dialogs(driver, label)
    driver.sleep(SHORT_DELAY)


# ─────────────────────────── Session Logic ───────────────────────────

def run_secondary_driver(primary_driver, config: ViewerConfig) -> None:
    """
    Spawn a second browser window for an additional view.
    Managed as a sub-driver of the primary SeleniumBase instance.
    """
    logger.info("Launching secondary driver...")
    try:
        secondary = primary_driver.get_new_driver(undetectable=True)
        activate_and_prepare(secondary, config.target_url, config.geo, label="secondary")

        secondary.sleep(MEDIUM_DELAY)
        click_start_watching(secondary, label="secondary")
        dismiss_consent_dialogs(secondary, label="secondary")

        # Let the secondary driver settle
        primary_driver.sleep(MEDIUM_DELAY)
        logger.info("Secondary driver is active and viewing.")

    except Exception as exc:
        logger.error("Secondary driver failed: %s", exc)


def run_single_session(config: ViewerConfig) -> bool:
    """
    Execute one complete viewing session.

    Returns:
        True  – session completed normally (stream was live).
        False – stream appears offline; caller should stop retrying.
    """
    view_duration = random.randint(config.min_view_duration, config.max_view_duration)
    logger.info(
        "Starting session (planned duration: %ds, proxy: %s)",
        view_duration,
        config.proxy or "none",
    )

    proxy_arg = config.proxy if config.proxy else False

    with SB(
        uc=True,
        locale="en",
        ad_block=True,
        chromium_arg="--disable-webgl",
        proxy=proxy_arg,
    ) as driver:
        # ── Navigate & handle consent / mature gate ──
        activate_and_prepare(driver, config.target_url, config.geo, label="primary")

        driver.sleep(LONG_DELAY)
        click_start_watching(driver, label="primary")
        dismiss_consent_dialogs(driver, label="primary")

        # ── Verify stream is live ──
        if not is_stream_live(driver):
            logger.warning("Stream does not appear to be live. Ending loop.")
            return False

        logger.info("Stream is live — viewing for %d seconds.", view_duration)
        dismiss_consent_dialogs(driver, label="primary")

        # ── Optional second driver ──
        if config.use_second_driver:
            run_secondary_driver(driver, config)

        # ── Watch for the configured duration ──
        driver.sleep(view_duration)
        logger.info("Session complete.")

    return True


# ─────────────────────────── Main Loop ───────────────────────────────

def main() -> None:
    """Entry point: resolve geo, build config, and loop sessions."""

    # Resolve geolocation
    try:
        geo = fetch_geo_data()
    except RuntimeError as exc:
        logger.critical("Cannot proceed without geolocation: %s", exc)
        sys.exit(1)

    # Decode target channel
    encoded_name = "YnJ1dGFsbGVz"
    try:
        channel_name = decode_target_name(encoded_name)
    except ValueError as exc:
        logger.critical("Target name decode failed: %s", exc)
        sys.exit(1)

    target_url = build_twitch_url(channel_name)
    logger.info("Target URL: %s", target_url)

    config = ViewerConfig(
        target_url=target_url,
        geo=geo,
        proxy=None,  # Set to "host:port" or "user:pass@host:port" if needed
        use_second_driver=True,
    )

    consecutive_failures = 0

    while True:
        try:
            stream_is_live = run_single_session(config)

            if not stream_is_live:
                logger.info("Stream offline — exiting.")
                break

            # Reset failure counter on success
            consecutive_failures = 0

        except KeyboardInterrupt:
            logger.info("Interrupted by user — shutting down.")
            break

        except Exception as exc:
            consecutive_failures += 1
            logger.error(
                "Session crashed (%d/%d consecutive failures): %s",
                consecutive_failures,
                config.max_consecutive_failures,
                exc,
                exc_info=True,
            )

            if consecutive_failures >= config.max_consecutive_failures:
                logger.critical(
                    "Reached %d consecutive failures — aborting.",
                    config.max_consecutive_failures,
                )
                sys.exit(0)

            # Back off before retrying
            backoff = min(30, 5 * consecutive_failures)
            logger.info("Retrying in %d seconds...", backoff)
            time.sleep(backoff)


if __name__ == "__main__":
    main()
