"""Select an observed full-text file belonging to the fetched publication."""
import re
from urllib.parse import urlsplit

POLICY = 'observed-publication-file-v1'


def publication_file(fetched, seen):
    """Return one same-origin, queryless link with the same publication ID.

    OJS article/file paths and PDF catalogue information pages are supported.
    Every returned URL must be present in the fetched page's actual links.
    No URL, host, filename, or document identifier is inferred or constructed.
    """
    try:
        parent = urlsplit(fetched['url'])
    except (KeyError, TypeError, ValueError):
        return None
    article = re.fullmatch(r'(.*?/article/)(?:view|viewFile)/(\d+)(?:/\d+)?/?', parent.path)
    catalogue = re.fullmatch(r'(.+\.pdf)/(?:[a-z]{2}/)?(?:info|details)/?', parent.path, re.I)
    if not article and not catalogue:
        return None
    candidates = []
    for link in fetched.get('links', []):
        if not isinstance(link, dict) or not isinstance(link.get('url'), str):
            continue
        url = link['url']
        try:
            target = urlsplit(url)
        except ValueError:
            continue
        if (url in seen or url == fetched['url'] or target.scheme not in ('https','http')
                or target.scheme != parent.scheme or target.netloc != parent.netloc
                or target.query or target.fragment or target.username or target.password):
            continue
        label = link.get('text', '')
        if not isinstance(label,str) or not re.search(r'\bpdf\b',label,re.I):
            continue
        if article:
            prefix, identifier = article.groups()
            if not re.fullmatch(re.escape(prefix) + r'(?:view|viewFile|download)/'
                                + re.escape(identifier) + r'/\d+(?:\.pdf)?/?', target.path):
                continue
        elif target.path != catalogue.group(1):
            continue
        candidates.append(url)
    return min(candidates) if candidates else None
