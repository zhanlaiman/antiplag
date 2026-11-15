"""Same-host browser icons must survive the real static server and upload manifest."""
from hashlib import sha256
from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import struct
from threading import Thread
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from cloud.modal_web import source_manifest
from server import Handler, ROOT

ICONS = {
    'favicon.ico': 'image/vnd.microsoft.icon',
    'favicon.png': 'image/png',
    'favicon.svg': 'image/svg+xml',
    'apple-touch-icon.png': 'image/png',
    'safari-pinned-tab.svg': 'image/svg+xml',
}


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.links = []; self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'link': self.links.append(dict(attrs))


class FaviconTests(unittest.TestCase):
    def test_browser_icons_are_real_static_responses_without_data_runtime(self):
        # Handler.static uses no model, search provider, tenant DB or credentials.
        service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=service.serve_forever, daemon=True); thread.start()
        try:
            for name, mime in ICONS.items():
                with self.subTest(name=name):
                    connection = HTTPConnection('127.0.0.1', service.server_port, timeout=5)
                    try:
                        connection.request('GET', '/' + name)
                        response = connection.getresponse(); body = response.read()
                        self.assertEqual(response.status, 200)
                        # Some platforms register the equivalent legacy ICO MIME.
                        types = {'image/vnd.microsoft.icon', 'image/x-icon'} if name.endswith('.ico') else {mime}
                        self.assertIn(response.getheader('Content-Type'), types)
                        self.assertEqual(body, (ROOT / 'web' / name).read_bytes())
                        self.assertEqual(int(response.getheader('Content-Length')), len(body))
                        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
                        self.assertEqual(response.getheader('Cache-Control'), 'no-cache')
                    finally: connection.close()
            connection = HTTPConnection('127.0.0.1', service.server_port, timeout=5)
            try:
                connection.request('GET', '/not-an-allowed-icon.svg')
                response = connection.getresponse(); response.read()
                self.assertEqual(response.status, 404)
            finally: connection.close()
        finally:
            service.shutdown(); service.server_close(); thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_every_html_links_same_host_icons_in_upload_manifest(self):
        manifest = {item['path']: item for item in source_manifest()}
        for name in ICONS:
            item = manifest['web/' + name]
            self.assertEqual(item['sha256'], sha256((ROOT / 'web' / name).read_bytes()).hexdigest())
        for path in sorted((ROOT / 'web').glob('*.html')):
            with self.subTest(page=path.name):
                links = Links(path.read_text()).links
                icons = [link for link in links if link.get('rel') in {'icon', 'apple-touch-icon', 'mask-icon'}]
                self.assertTrue(all(link['href'].startswith('/') and not link['href'].startswith('//') for link in icons))
                self.assertEqual({urlsplit(link['href']).path for link in icons}, {'/' + name for name in ICONS})
                ico = next(link for link in icons if urlsplit(link['href']).path == '/favicon.ico')
                self.assertEqual(ico['href'], '/favicon.ico?v=20261115')
                touch = next(link for link in icons if link['rel'] == 'apple-touch-icon')
                self.assertEqual(touch['sizes'], '180x180')
                mask = next(link for link in icons if link['rel'] == 'mask-icon')
                self.assertEqual(mask['color'], '#285b43')
                svg = next(link for link in icons if link['href'] == '/favicon.svg')
                self.assertEqual((svg['type'], svg['sizes']), ('image/svg+xml', 'any'))

    def test_raster_sizes_and_self_contained_vector(self):
        for name, size in [('favicon.png', 48), ('apple-touch-icon.png', 180)]:
            body = (ROOT / 'web' / name).read_bytes()
            self.assertEqual(body[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', body[16:24]), (size, size))
        ico = (ROOT / 'web/favicon.ico').read_bytes()
        self.assertEqual(struct.unpack_from('<HHH', ico), (0, 1, 3))
        end = 6 + 3 * 16
        for index, size in enumerate([16, 32, 48]):
            width, height, colors, reserved, planes, bits, length, offset = struct.unpack_from('<BBBBHHII', ico, 6 + index * 16)
            self.assertEqual((width, height, planes, bits), (size, size, 1, 32))
            self.assertEqual(offset, end)
            self.assertEqual(struct.unpack_from('<IiiHH', ico, offset), (40, size, size * 2, 1, 32))
            end = offset + length
        self.assertEqual(end, len(ico))
        paths = []
        for name in ['favicon.svg', 'safari-pinned-tab.svg']:
            svg = ET.parse(ROOT / 'web' / name).getroot()
            self.assertEqual(svg.attrib['viewBox'], '0 0 64 64')
            self.assertTrue(all(node.tag.rsplit('}', 1)[-1] in {'svg', 'rect', 'path'} for node in svg.iter()))
            self.assertTrue(all(not any(key.endswith('href') for key in node.attrib) for node in svg.iter()))
            paths.append(svg.find('{http://www.w3.org/2000/svg}path').attrib['d'])
        self.assertEqual(*paths)
