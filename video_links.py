import re
from urllib.parse import urlsplit


SUPPORTED_SITES = {
    "YouTube": ("youtube.com", "youtu.be"),
    "TikTok": ("tiktok.com",),
    "Instagram": ("instagram.com",),
    "Twitter/X": ("twitter.com", "x.com"),
    "VK": ("vk.com",),
    "Facebook": ("facebook.com", "fb.watch"),
    "Reddit": ("reddit.com",),
    "Vimeo": ("vimeo.com",),
}
SITE_NAMES = ", ".join(SUPPORTED_SITES)
URL_RE = re.compile(r"""https?://[^\s<>"«»]+""", re.IGNORECASE)


def is_supported(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or parts.username is not None:
            return False
        if not host or parts.port not in (None, 80, 443):
            return False
    except ValueError:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domains in SUPPORTED_SITES.values()
        for domain in domains
    )


def first_supported_link(text: str):
    for match in URL_RE.finditer(text):
        url = match.group().rstrip(".,!?'")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if is_supported(url):
            return url
    return None
