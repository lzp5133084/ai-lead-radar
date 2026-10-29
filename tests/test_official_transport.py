"""Fixed-host TLS transport regressions. No real credential or network call."""
import io
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch
from radar.collectors import (CollectorError, MAX_BYTES, _RejectRedirect,
                               official_tavily_fetch, validate_public_url)

ENDPOINT = 'https://api.tavily.com/search'
FAKE_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.148', 443))]


class Reply:
    status = 200
    headers = {'Content-Type': 'application/json'}
    def __init__(self, content=b'{"results":[]}'):
        self.stream = io.BytesIO(content)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.stream.close()
    def read1(self, size):
        return self.stream.read1(size)


class OfficialTransportTests(unittest.TestCase):
    def test_tls_verified_fixed_endpoint_works_without_resolving_synthetic_dns(self):
        with patch('radar.collectors._resolve_addresses', return_value=FAKE_DNS) as dns, \
             patch('radar.collectors.urllib.request.build_opener') as build:
            build.return_value.open.return_value = Reply()
            result = official_tavily_fetch(ENDPOINT, method='POST', body=b'{"query":"ordinary demand"}',
                           headers={'Authorization': 'Bearer TEST_ONLY_KEY'})
            dns.assert_not_called()
            handler = next(h for h in build.call_args.args if isinstance(h, __import__('urllib.request', fromlist=['HTTPSHandler']).HTTPSHandler))
            self.assertTrue(handler._context.check_hostname)
            self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(any(isinstance(h, _RejectRedirect) for h in build.call_args.args))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.get_header('Authorization'), 'Bearer TEST_ONLY_KEY')
            self.assertEqual(result.body, b'{"results":[]}')
        # Generic user URLs retain the strict public-address check.
        with patch('radar.collectors._resolve_addresses', return_value=FAKE_DNS):
            with self.assertRaises(CollectorError):
                validate_public_url('https://arbitrary.example/feed')
            self.assertEqual(validate_public_url('https://arbitrary.example/post', reference_only=True)[3], [])

    def test_no_other_domain_port_path_credentials_or_protocol_can_use_exception(self):
        urls = ['http://api.tavily.com/search', 'https://api.tavily.com/other',
                'https://api.tavily.com/search?token=private', 'https://api.tavily.com:8443/search',
                'https://api.tavily.com.other.example/search', 'http://localhost/search',
                'https://user:private@api.tavily.com/search']
        with patch('radar.collectors.urllib.request.build_opener') as build:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(CollectorError):
                    official_tavily_fetch(url, method='POST', headers={}, body=b'{}')
            with self.assertRaises(CollectorError):
                official_tavily_fetch(ENDPOINT, method='GET', headers={}, body=b'{}')
            build.assert_not_called()

    def test_reference_links_reject_private_literals_and_secret_queries(self):
        for url in ('http://127.0.0.1/post', 'http://192.168.1.10/post', 'http://[::1]/post',
                    'http://localhost/post', 'http://router.local/post', 'https://public.example/?api_key=secret'):
            with self.subTest(url=url), self.assertRaises(CollectorError):
                validate_public_url(url, reference_only=True)

    def test_redirects_never_forward_credentials_or_follow_result_pages(self):
        handler = _RejectRedirect()
        for target in ('http://localhost/private', 'https://other.example/steal', ENDPOINT):
            with self.subTest(target=target), self.assertRaises(CollectorError):
                handler.redirect_request(None, None, 302, 'redirect', {}, target)

    def test_remote_errors_redact_body_and_certificate_failure_does_not_retry(self):
        for failure in (urllib.error.HTTPError(ENDPOINT, 401, 'secret echo', {}, io.BytesIO(b'TEST_ONLY_KEY')),
                        urllib.error.URLError(ssl.SSLCertVerificationError('certificate rejected TEST_ONLY_KEY'))):
            with self.subTest(failure=type(failure).__name__), patch('radar.collectors.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(CollectorError) as exc:
                    official_tavily_fetch(ENDPOINT, method='POST', headers={'Authorization': 'Bearer TEST_ONLY_KEY'}, body=b'{}')
                self.assertNotIn('TEST_ONLY_KEY', str(exc.exception))
                build.return_value.open.assert_called_once()

    def test_response_size_bound(self):
        with patch('radar.collectors.urllib.request.build_opener') as build:
            build.return_value.open.return_value = Reply(b'x' * (MAX_BYTES + 1))
            with self.assertRaises(CollectorError):
                official_tavily_fetch(ENDPOINT, method='POST', headers={}, body=b'{}')


if __name__ == '__main__':
    unittest.main()
