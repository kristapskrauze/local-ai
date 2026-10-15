"""Addresses of the other machines on the network, from the environment.

The media box (Jellyfin, downloads, the published game pages) is a separate
machine. Over Tailscale each tailnet sees it under its own IP, so the owner
and the partner get different links to the same box.

    MEDIA_BOX_IP          media box as the owner's tailnet sees it
    MEDIA_BOX_IP_PARTNER  the same box as the partner's tailnet sees it
    MEDIA_BOX_HOST        its MagicDNS name (works on either tailnet)
    MEDIA_BOX_LAN_IP      its LAN address, for SSH and status calls
    RIG_HOST              this machine's MagicDNS name, for links into the dashboard
    MEDIA_BOX_HOME        home folder of the SSH account on the media box
"""
import os

MEDIA_BOX_IP = os.environ.get("MEDIA_BOX_IP", "127.0.0.1")
MEDIA_BOX_IP_PARTNER = os.environ.get("MEDIA_BOX_IP_PARTNER", MEDIA_BOX_IP)
MEDIA_BOX_HOST = os.environ.get("MEDIA_BOX_HOST", "localhost")
MEDIA_BOX_LAN_IP = os.environ.get("MEDIA_BOX_LAN_IP", MEDIA_BOX_IP)
RIG_HOST = os.environ.get("RIG_HOST", "localhost")
MEDIA_BOX_HOME = os.environ.get("MEDIA_BOX_HOME", "/home/media").rstrip("/")

MEDIA_CONSOLE_URL = "http://%s:8183" % MEDIA_BOX_IP
JELLYFIN_URL = "http://%s:8096" % MEDIA_BOX_IP
