"""URL normalization and tracking parameter stripping utilities."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Common tracking parameters present in Substack links
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "r",
    "s",
    "ref",
    "post_id",
    "isFreemail",
    "triedSigningIn",
    "action",
    "showWelcome",
}


def canonicalize_url(url: str) -> str:
    """Normalize post URL by stripping tracking query params and trailing slash.

    Example:
      Input:  https://example.substack.com/p/my-post-slug?utm_source=substack&r=1a2b3#comments
      Output: https://example.substack.com/p/my-post-slug
    """
    if not url:
        return ""

    parsed = urlparse(url.strip())
    # Keep query parameters that are NOT in TRACKING_PARAMS
    query_params = parse_qsl(parsed.query, keep_blank_values=False)
    filtered_params = [(k, v) for k, v in query_params if k not in TRACKING_PARAMS]
    clean_query = urlencode(filtered_params)

    # Normalize path (remove trailing slash except for root path)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    # Reconstruct clean canonical URL (strip fragment/anchor as well)
    clean_url = urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            clean_query,
            "",  # fragment stripped
        )
    )
    return clean_url
